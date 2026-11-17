"""Partition conditional draws, broadcast anchor outputs, then derive parents.

``apply_blocks`` returns detached rows and aggregate diagnostics. Selectors use
exact string equality against the *input* rows; row positions and dictionary
key order are preserved. Broadcast keys are immutable throughout the plan.
Each broadcast requires exactly one selected anchor in every input key group,
and may copy only that block's sampled outputs. Hierarchy mappings are supplied
by the caller after verification: ``leaf -> {parent_column: value}``.

All shapes, column references, anchors, write ownership and cross-block reads
are checked, and all samplers compiled, before sampling begins. Data-dependent
failures (uncovered conditions or unmapped sampled leaves) also leave caller
inputs unchanged. Rule IDs are unique across the whole plan. Block IDs select
stable named seeds via ``seed_for(seed, 'joint-block:' + block_id)``.

Hierarchy parents are derived after a block that samples its leaf, on precisely
the rows receiving that leaf (including broadcast recipients). Rules needing
those new parents belong in a subsequent block. Within-block rule dependencies
are compiled by JointSampler. Partition draws and anchor broadcasts do not
establish a full joint target across all rows or partitions.
"""

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass

from .conditional_joint import JointSampler
from .random_streams import seed_for


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _shape(value, keys, label):
    _require(isinstance(value, dict) and set(value) == set(keys),
             f"{label}: missing or unknown keys")


def _fields(value, label, *, nonempty=False):
    _require(isinstance(value, list), f"{label}: expected a list of distinct field names")
    _require(all(isinstance(field, str) and field for field in value),
             f"{label}: field names must be nonempty strings")
    _require(len(set(value)) == len(value), f"{label}: duplicate fields")
    _require(not nonempty or value, f"{label}: fields cannot be empty")
    return tuple(value)


def _columns(fields, header, label):
    missing = set(fields) - header
    _require(not missing, f"{label}: unknown columns: {', '.join(sorted(missing))}")


def _hierarchy(spec, header):
    if spec is None:
        return None, (), {}
    _shape(spec, {"leaf_column", "parent_columns", "mapping"}, "Hierarchy")
    leaf = spec["leaf_column"]
    _require(isinstance(leaf, str) and leaf, "Hierarchy leaf must be a nonempty string")
    parents = _fields(spec["parent_columns"], "Hierarchy parents", nonempty=True)
    _require(leaf not in parents, "Hierarchy leaf cannot also be a parent")
    _columns((leaf, *parents), header, "Hierarchy")
    mapping = spec["mapping"]
    _require(isinstance(mapping, dict) and mapping, "Hierarchy mapping must be a nonempty dict")
    for value, derived in mapping.items():
        _require(isinstance(value, str), "Hierarchy mapping leaves must be strings")
        _shape(derived, parents, "Hierarchy mapping parents")
        _require(all(isinstance(item, str) for item in derived.values()),
                 "Hierarchy mapping parent values must be strings")
    return leaf, parents, deepcopy(mapping)


@dataclass
class _Block:
    id: str
    seed: int
    rules: list
    selected: tuple
    outputs: frozenset
    given: frozenset
    broadcast_fields: tuple
    anchors: tuple
    hierarchy_rows: tuple
    writes: dict


def _plan(rows, blocks, header, seed, protected, leaf, parents):
    _require(isinstance(blocks, list), "Blocks must be a list")
    block_ids, rule_ids, plans, broadcast_keys = set(), set(), [], set()
    all_rows = frozenset(range(len(rows)))
    for block in blocks:
        _shape(block, {"id", "select", "broadcast", "rules"}, "Block")
        name = block["id"]
        _require(isinstance(name, str) and name.strip(), "Block id must be a nonempty string")
        _require(name not in block_ids, f"Duplicate block id: {name}")
        block_ids.add(name)
        specs = block["rules"]
        _require(isinstance(specs, list) and specs, f"Block {name}: rules must be a nonempty list")
        outputs, given = set(), set()
        for rule in specs:
            _require(isinstance(rule, dict) and {"id", "given", "outputs"} <= rule.keys(),
                     f"Block {name}: invalid rule shape")
            rule_id = rule["id"]
            _require(isinstance(rule_id, str) and rule_id, "Rule id must be a nonempty string")
            _require(rule_id not in rule_ids, f"Duplicate rule id across blocks: {rule_id}")
            rule_ids.add(rule_id)
            outputs.update(_fields(rule["outputs"], "Rule outputs", nonempty=True))
            given.update(_fields(rule["given"], "Rule given"))
        _columns(outputs | given, header, f"Block {name}")
        _require(not outputs.intersection(parents), "Hierarchy parents cannot be sampled")

        selector = block["select"]
        if selector is None:
            selected = tuple(range(len(rows)))
        else:
            _shape(selector, {"column", "values"}, "Selector")
            column = selector["column"]
            _require(isinstance(column, str) and column, "Selector column must be a nonempty string")
            _columns((column,), header, "Selector")
            values = selector["values"]
            _require(isinstance(values, list) and all(isinstance(value, str) for value in values),
                     "Selector values must be a list of strings")
            _require(len(values) == len(set(values)), "Duplicate selector values")
            selected_values = set(values)
            selected = tuple(i for i, row in enumerate(rows) if row[column] in selected_values)
        selected_set = frozenset(selected)
        writes = {field: selected_set for field in outputs}

        broadcast_fields, anchors = (), ()
        broadcast = block["broadcast"]
        if broadcast is not None:
            _shape(broadcast, {"key", "fields"}, "Broadcast")
            key = broadcast["key"]
            _require(isinstance(key, str) and key, "Broadcast key must be a nonempty string")
            _columns((key,), header, "Broadcast")
            broadcast_keys.add(key)
            broadcast_fields = _fields(broadcast["fields"], "Broadcast fields", nonempty=True)
            _columns(broadcast_fields, header, "Broadcast")
            _require(set(broadcast_fields) <= outputs,
                     "Broadcast fields must be sampled outputs of the same block")
            groups = defaultdict(list)
            for i, row in enumerate(rows):
                groups[row[key]].append(i)
            anchor_groups = []
            for indices in groups.values():
                candidates = [i for i in indices if i in selected_set]
                _require(len(candidates) == 1,
                         f"Block {name}: broadcast requires exactly one selected anchor per group")
                anchor_groups.append((candidates[0], tuple(i for i in indices if i != candidates[0])))
            anchors = tuple(anchor_groups)
            writes.update({field: all_rows for field in broadcast_fields})

        hierarchy_rows = ()
        if leaf in outputs:
            _require(not given.intersection(parents),
                     "Hierarchy parents are derived after the block; use a downstream block for given parents")
            hierarchy_rows = tuple(sorted(writes[leaf]))
            writes.update({field: writes[leaf] for field in parents})
        _require(not protected.intersection(writes),
                 f"Block {name}: protected fields cannot be written")
        plans.append(_Block(name, seed_for(seed, "joint-block:" + name), deepcopy(specs),
                            selected, frozenset(outputs), frozenset(given), broadcast_fields,
                            anchors, hierarchy_rows, writes))

    owners = defaultdict(set)
    for plan in plans:
        _require(not broadcast_keys.intersection(plan.writes),
                 "Broadcast keys cannot be written by sampling, broadcast or hierarchy")
        for field, indices in plan.writes.items():
            _require(not owners[field].intersection(indices),
                     f"Overlapping writers for field {field} on the same physical row")
            owners[field].update(indices)

    future_writes = defaultdict(set)
    for plan in reversed(plans):
        for field in plan.given:
            _require(not future_writes[field].intersection(plan.selected),
                     f"Block {plan.id}: given field {field} has a future writer on a selected row")
        for field, indices in plan.writes.items():
            future_writes[field].update(indices)
    return plans


def apply_blocks(rows, blocks, resolver, seed, protected_fields=(), hierarchy=None):
    """Apply a validated ordered block plan without mutating any caller inputs.

    Rows must be a list of dicts with the same nonempty string keys and string
    values; categorical blanks and leading zeroes are preserved. Empty rows are
    supported with an empty plan; a nonempty plan needs a header to validate.
    ``protected_fields`` may be any iterable of distinct field names.

    Report counts distinguish selected draws, broadcast groups/recipients
    (recipients exclude anchors), hierarchy rows, and actual changed cells per
    field. ``written_fields`` also includes declared writes that kept a value.
    Counts and inventories contain no row identifiers. Sampler reports may
    contain configured conditioning categories; identifiers must not be used as
    conditional-table categorical fields.
    """
    _require(isinstance(rows, list), "Rows must be a list of dictionaries")
    header = set(rows[0]) if rows and isinstance(rows[0], dict) else set()
    for row in rows:
        _require(isinstance(row, dict), "Rows must be dictionaries")
        _require(all(isinstance(key, str) and key for key in row), "Row header must contain nonempty strings")
        _require(set(row) == header, "Rows must have a uniform header")
        _require(all(isinstance(value, str) for value in row.values()), "Row values must be strings")
    _require(type(seed) is int and seed >= 0, "Seed must be a nonnegative integer")
    _require(callable(resolver), "Resolver must be callable")
    _require(not isinstance(protected_fields, (str, bytes, dict)), "Protected fields must be an iterable of names")
    try:
        protected = frozenset(_fields(list(protected_fields), "Protected fields"))
    except TypeError as exc:
        raise ValueError("Protected fields must be an iterable of names") from exc
    leaf, parents, mapping = _hierarchy(hierarchy, header)
    plans = _plan(rows, blocks, header, seed, protected, leaf, parents)
    # Compile every table/DAG before any draw, including blocks selecting no rows.
    # Only referenced context is declared initial; unrelated header strings need
    # not obey the engine's categorical identifier syntax.
    samplers = [JointSampler(plan.rules, resolver, plan.seed,
                             initial_fields=sorted(plan.given - plan.outputs),
                             protected_fields=sorted(protected.intersection(plan.given | plan.outputs)))
                for plan in plans]
    result = [dict(row) for row in rows]
    reports = []
    total_changes = Counter()
    for plan, sampler in zip(plans, samplers):
        changes = Counter()

        def assign(index, field, value):
            _require(isinstance(value, str), "Generated values must be strings")
            if result[index][field] != value:
                changes[field] += 1
            result[index][field] = value

        # Copy only fields consumed by this block, keeping the sampler detached
        # from all unrelated delivery/context fields in the full-width frame.
        needed = sorted(plan.given | plan.outputs)
        sampled = sampler.sample_rows([{field: result[i][field] for field in needed}
                                       for i in plan.selected])
        _require(isinstance(sampled, list) and len(sampled) == len(plan.selected),
                 "Sampler must preserve selected row count")
        for i, row in zip(plan.selected, sampled):
            for field in sorted(plan.outputs):
                assign(i, field, row[field])
        for anchor, recipients in plan.anchors:
            for i in recipients:
                for field in plan.broadcast_fields:
                    assign(i, field, result[anchor][field])
        for i in plan.hierarchy_rows:
            _require(result[i][leaf] in mapping, "Sampled hierarchy leaf is absent from mapping")
            for field in parents:
                assign(i, field, mapping[result[i][leaf]][field])
        total_changes.update(changes)
        reports.append({
            "id": plan.id, "seed": plan.seed,
            "selected_row_count": len(plan.selected),
            "broadcast_groups": len(plan.anchors),
            "broadcast_row_count": sum(len(indices) for _, indices in plan.anchors),
            "hierarchy_count": len(plan.hierarchy_rows),
            "written_fields": sorted(plan.writes),
            "changed_field_inventory": dict(sorted(changes.items())),
            "sampler_report": sampler.report(),
        })
        del sampled
    return result, {
        "type": "conditional_joint_blocks", "type_version": 1,
        "seed": seed, "row_count": len(rows), "partition_basis": "input_rows",
        "full_joint_target_claimed": False, "blocks": reports,
        "changed_field_inventory": dict(sorted(total_changes.items())),
    }
