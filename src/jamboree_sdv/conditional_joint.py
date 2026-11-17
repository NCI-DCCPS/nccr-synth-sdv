"""Pinned categorical joint tables and deterministic dependency-ordered sampling.

``JointTable(spec, resolver).distribution(row)`` returns a ``JointDistribution``
named tuple: (conditioning_fields, condition_values, output_tuples, probabilities).
The last member is an immutable float64 NumPy array. Tables, including all backoff
distributions, are immutable snapshots of the bytes verified at construction.

``JointSampler(specs, resolver, seed, initial_fields, protected_fields=())``
compiles on construction; no separate compile call is needed. ``sample_rows``
returns shallow dictionary copies, replacing declared outputs, and ``report``
returns a detached JSON-compatible aggregate. Successful calls continue each
rule's stream; failed calls leave streams and counters unchanged. Instances are
stateful and are intended for sequential use, not concurrent sampling.

Specs have exactly the keys shown in ``_SPEC_KEYS``; even an empty ``backoff``
must be explicit. Names use ASCII letters/underscore initially, then letters,
digits, underscore, dot or hyphen. The count column is configurable and distinct
from categorical fields. CSV headers may be reordered but must have exactly the
declared membership, without duplicates. UTF-8 categories are never stripped or
coerced; an empty string is a category, whereas a missing row key is an error.
Counts are positive decimal integers. Backoff contains ordered, distinct proper
subsets of ``given`` (including [] for unconditional fallback). It applies only
to unseen *complete* conditions, never to missing or non-string context.

``resolver`` receives a fresh {path, sha256} dictionary and returns a Path. It
owns path resolution/access policy. This module independently hashes and parses
the same bytes, even if the resolver has already verified them. Float64 weights
that underflow or collapse cumulative intervals are rejected rather than
silently losing table support.
Reports retain only configured categorical strata, never unrelated row fields;
callers must not configure patient identifiers as categorical conditioning fields.
"""

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import deepcopy
import csv
from dataclasses import dataclass
import hashlib
import io
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, NamedTuple

import numpy as np

from .random_streams import rng_for


_SPEC_KEYS = frozenset({
    "id", "type", "type_version", "given", "outputs", "table", "count_column", "backoff",
})
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*", re.ASCII)
_SHA256 = re.compile(r"[0-9a-fA-F]{64}", re.ASCII)
_COUNT = re.compile(r"[0-9]+", re.ASCII)

Fields = tuple[str, ...]
Values = tuple[str, ...]
Condition = tuple[Fields, Values]
Resolver = Callable[[dict[str, str]], Path]


class JointDistribution(NamedTuple):
    """One effective conditioning stratum and its whole-tuple probabilities."""

    conditioning_fields: Fields
    condition_values: Values
    output_tuples: tuple[Values, ...]
    probabilities: np.ndarray


def _name(value: Any) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise ValueError("Invalid name: use an ASCII identifier with optional dots/hyphens")
    return value


def _fields(value: Any, *, list_only: bool = True) -> Fields:
    if (list_only and not isinstance(value, list)) or isinstance(value, (str, bytes, Mapping)):
        raise ValueError("Fields must be a list of distinct names")
    try:
        fields = tuple(_name(field) for field in value)
    except TypeError as exc:
        raise ValueError("Fields must be an iterable of distinct names") from exc
    if len(set(fields)) != len(fields):
        raise ValueError("Duplicate fields")
    return fields


def _immutable_array(values: Iterable[float]) -> np.ndarray:
    # An immutable bytes base prevents callers re-enabling WRITEABLE via setflags.
    return np.frombuffer(np.asarray(tuple(values), dtype=np.float64).tobytes(), dtype=np.float64)


@dataclass(frozen=True, init=False, slots=True, eq=False)
class JointTable:
    """Strictly validated count table with precomputed joint marginalizations."""

    id: str
    given: Fields
    outputs: Fields
    table: Mapping[str, str]
    count_column: str
    backoff: tuple[Fields, ...]
    joint_cell_count: int
    total_count: int
    _distributions: Mapping[Condition, JointDistribution]
    _cdfs: Mapping[Condition, np.ndarray]

    def __init__(self, spec: Mapping[str, Any], resolver: Resolver):
        if not isinstance(spec, Mapping) or set(spec) != _SPEC_KEYS:
            raise ValueError("Joint table spec has missing or unknown keys")
        if (spec["type"] != "conditional_joint_table"
                or type(spec["type_version"]) is not int or spec["type_version"] != 1):
            raise ValueError("Unsupported joint table type or type_version")
        rule_id = _name(spec["id"])
        given, outputs = _fields(spec["given"]), _fields(spec["outputs"])
        count_column = _name(spec["count_column"])
        if not outputs or set(given) & set(outputs):
            raise ValueError("Outputs must be nonempty and disjoint from given")
        if count_column in given + outputs:
            raise ValueError("Count column overlaps categorical fields")
        if not isinstance(spec["backoff"], list):
            raise ValueError("Backoff must be a list of proper subsets of given")
        backoff = tuple(_fields(fields) for fields in spec["backoff"])
        seen = set()
        for fields in backoff:
            subset = frozenset(fields)
            if not subset < set(given) or subset in seen:
                raise ValueError("Backoff must contain distinct proper subsets of given")
            seen.add(subset)
        pin = spec["table"]
        if not isinstance(pin, Mapping) or set(pin) != {"path", "sha256"}:
            raise ValueError("Table pin must have exactly path and sha256")
        if (not isinstance(pin["path"], str) or not pin["path"].strip() or "\x00" in pin["path"]
                or not isinstance(pin["sha256"], str) or _SHA256.fullmatch(pin["sha256"]) is None):
            raise ValueError("Invalid table path or SHA-256 pin")
        pin = {"path": pin["path"], "sha256": pin["sha256"].lower()}
        if not callable(resolver):
            raise ValueError("Table resolver must be callable")
        path = Path(resolver(dict(pin)))
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != pin["sha256"]:
            raise ValueError(f"Rule {rule_id}: table SHA-256 mismatch")

        columns = given + outputs + (count_column,)
        cells: dict[tuple[Values, Values], int] = {}
        try:
            reader = csv.reader(io.StringIO(content.decode("utf-8"), newline=""), strict=True)
            header = next(reader, None)
            if (header is None or len(header) != len(columns)
                    or len(set(header)) != len(header) or set(header) != set(columns)):
                raise ValueError(f"Rule {rule_id}: CSV header must match the declared columns exactly")
            indices = tuple(header.index(column) for column in columns)
            for line in reader:
                if len(line) != len(columns):
                    raise ValueError(f"Rule {rule_id}: malformed CSV width at line {reader.line_num}")
                ordered = tuple(line[index] for index in indices)
                count_text = ordered[-1]
                if _COUNT.fullmatch(count_text) is None or int(count_text) <= 0:
                    raise ValueError(f"Rule {rule_id}: count must be a positive integer at line {reader.line_num}")
                key = (ordered[:len(given)], ordered[len(given):-1])
                if key in cells:
                    raise ValueError(f"Rule {rule_id}: duplicate joint cell at line {reader.line_num}")
                cells[key] = int(count_text)
        except (csv.Error, UnicodeError) as exc:
            raise ValueError(f"Rule {rule_id}: invalid UTF-8 CSV") from exc
        if not cells:
            raise ValueError(f"Rule {rule_id}: empty table")

        distributions = {}
        cdfs = {}
        for fields in (given,) + backoff:
            positions = tuple(given.index(field) for field in fields)
            grouped: dict[Values, Counter] = defaultdict(Counter)
            for (condition, output), count in cells.items():
                grouped[tuple(condition[index] for index in positions)][output] += count
            for condition, counts in grouped.items():
                outcomes = tuple(sorted(counts))
                total = sum(counts.values())
                probabilities = np.asarray([counts[outcome] / total for outcome in outcomes])
                if not np.isfinite(probabilities).all() or np.any(probabilities <= 0):
                    raise ValueError(f"Rule {rule_id}: counts cannot be represented as positive float64 probabilities")
                probabilities /= probabilities.sum()
                cumulative = probabilities.cumsum()
                cumulative[-1] = 1.0
                if np.any(np.diff(np.concatenate(([0.0], cumulative))) <= 0):
                    raise ValueError(f"Rule {rule_id}: counts cannot retain positive float64 sampling intervals")
                key = (fields, condition)
                distributions[key] = JointDistribution(
                    fields, condition, outcomes, _immutable_array(probabilities),
                )
                cdfs[key] = _immutable_array(cumulative)

        for name, value in {
            "id": rule_id, "given": given, "outputs": outputs,
            "table": MappingProxyType(pin), "count_column": count_column, "backoff": backoff,
            "joint_cell_count": len(cells), "total_count": sum(cells.values()),
            "_distributions": MappingProxyType(distributions), "_cdfs": MappingProxyType(cdfs),
        }.items():
            object.__setattr__(self, name, value)

    def distribution(self, row: Mapping[str, Any]) -> JointDistribution:
        """Resolve exact conditioning, then the first covered declared backoff.

        Every given field must be present and a string, even if fallback would
        discard that field. Extra fields are ignored. Errors do not echo values.
        """
        if not isinstance(row, Mapping):
            raise ValueError(f"Rule {self.id}: row must be a mapping")
        for field in self.given:
            if field not in row:
                raise ValueError(f"Rule {self.id}: missing conditioning field {field}")
            if not isinstance(row[field], str):
                raise ValueError(f"Rule {self.id}: conditioning field {field} must be a string")
        for fields in (self.given,) + self.backoff:
            key = (fields, tuple(row[field] for field in fields))
            if key in self._distributions:
                distribution = self._distributions[key]
                # Share immutable bytes, not ndarray metadata: shape/dtype changes
                # to a returned array must not alter future table lookups.
                return distribution._replace(probabilities=np.frombuffer(
                    distribution.probabilities.base, dtype=np.float64,
                ))
        raise ValueError(f"Rule {self.id}: uncovered condition; no declared backoff covers it")


class JointSampler:
    """Compile a categorical DAG and maintain one named RNG per rule.

    A writer of a given field always supplies that field, even if it is also
    declared initial. Otherwise it must be in initial_fields. Existing output
    values may be replaced unless protected. Rule IDs are unique; ready nodes
    are compiled in lexical ID order, independent of spec order. Row order is
    significant. Each rule consumes one uniform draw per successful row.
    """

    def __init__(self, specs: Sequence[Mapping[str, Any]], resolver: Resolver, seed: int,
                 initial_fields: Iterable[str], protected_fields: Iterable[str] = ()):
        if isinstance(specs, (str, bytes)) or not isinstance(specs, Sequence):
            raise ValueError("Specs must be a sequence of joint table specifications")
        if type(seed) is not int or seed < 0:
            raise ValueError("Seed must be a nonnegative integer")
        initial = frozenset(_fields(initial_fields, list_only=False))
        protected = frozenset(_fields(protected_fields, list_only=False))
        tables = {}
        writers = {}
        for spec in specs:
            table = JointTable(spec, resolver)
            if table.id in tables:
                raise ValueError(f"Duplicate rule id: {table.id}")
            tables[table.id] = table
            for field in table.outputs:
                if field in protected:
                    raise ValueError(f"Protected output field: {field}")
                if field in writers:
                    raise ValueError(f"Duplicate writers for field: {field}")
                writers[field] = table.id
        dependencies = {}
        for table in tables.values():
            missing = set(table.given) - writers.keys() - initial
            if missing:
                raise ValueError(f"Rule {table.id}: missing fields: {', '.join(sorted(missing))}")
            dependencies[table.id] = {writers[field] for field in table.given if field in writers}
        ordered = []
        while dependencies:
            ready = sorted(rule_id for rule_id, parents in dependencies.items() if not parents)
            if not ready:
                raise ValueError("Cycle in conditional joint rule dependencies")
            for rule_id in ready:
                ordered.append(tables[rule_id])
                del dependencies[rule_id]
            for parents in dependencies.values():
                parents.difference_update(ready)
        self._tables = tuple(ordered)
        self._seed = seed
        self._initial_fields = initial
        self._rngs = MappingProxyType({table.id: rng_for(seed, "joint:" + table.id) for table in ordered})
        self._counts: dict[str, Counter] = {table.id: Counter() for table in ordered}
        self._rows_sampled = 0

    @property
    def tables(self) -> tuple[JointTable, ...]:
        """Immutable tables in compiled execution order."""
        return self._tables

    def sample_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Sample a batch without mutating inputs; failures do not advance state."""
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Rows must be a list of dictionaries")
        for row in rows:
            if not self._initial_fields <= row.keys():
                raise ValueError("Row is missing declared initial fields")
        result = [dict(row) for row in rows]
        states = {rule_id: deepcopy(rng.bit_generator.state) for rule_id, rng in self._rngs.items()}
        counts = {table.id: Counter() for table in self._tables}
        try:
            for row in result:
                for table in self._tables:
                    distribution = table.distribution(row)
                    key = (distribution.conditioning_fields, distribution.condition_values)
                    index = int(np.searchsorted(table._cdfs[key], self._rngs[table.id].random(), side="right"))
                    row.update(zip(table.outputs, distribution.output_tuples[index]))
                    counts[table.id][key] += 1
        except Exception:
            for rule_id, state in states.items():
                self._rngs[rule_id].bit_generator.state = state
            raise
        for rule_id, draws in counts.items():
            self._counts[rule_id].update(draws)
        self._rows_sampled += len(result)
        return result

    def report(self) -> dict[str, Any]:
        """Detached aggregate provenance, backoff usage and effective draw counts.

        conditioning_usage includes zero-use levels; backoff_index is null for
        exact conditioning and zero-based for declared fallbacks. Effective
        conditions contain only used strata, including [] for unconditional.
        """
        rules = []
        for table in self._tables:
            counts = self._counts[table.id]
            levels = (table.given,) + table.backoff
            usage = Counter()
            for (fields, _), count in counts.items():
                usage[fields] += count
            rules.append({
                "id": table.id, "type": "conditional_joint_table", "type_version": 1,
                "given": list(table.given), "outputs": list(table.outputs),
                "count_column": table.count_column, "table": dict(table.table),
                "backoff": [list(fields) for fields in table.backoff],
                "joint_cell_count": table.joint_cell_count, "total_count": table.total_count,
                "stream": "joint:" + table.id, "draw_count": sum(counts.values()),
                "exact_draw_count": usage[table.given],
                "backoff_draw_count": sum(usage[fields] for fields in table.backoff),
                "conditioning_usage": [
                    {"conditioning_fields": list(fields), "backoff_index": index - 1 if index else None,
                     "draw_count": usage[fields]}
                    for index, fields in enumerate(levels)
                ],
                "effective_conditions": [
                    {"conditioning_fields": list(fields), "condition_values": list(values), "draw_count": count}
                    for (fields, values), count in sorted(
                        counts.items(), key=lambda item: (levels.index(item[0][0]), item[0][1]),
                    )
                ],
            })
        return {
            "type": "conditional_joint_sampler", "type_version": 1,
            "seed": self._seed, "stream_scheme": "jamboree-sdv-v1",
            "rows_sampled": self._rows_sampled, "rule_order": [table.id for table in self._tables],
            "rules": rules,
        }
