"""Contract, dependence, provenance and stream tests using only small fixtures."""

from collections import Counter
from copy import deepcopy
import csv
from dataclasses import FrozenInstanceError
import hashlib
import io
import json
from pathlib import Path

import numpy as np
import pytest

from jamboree_sdv.conditional_joint import JointDistribution, JointSampler, JointTable


def resolve(pin):
    # Deliberately does not verify: the engine must independently enforce pins.
    return Path(pin["path"])


def write_table(tmp_path, *, rule_id="paired", given=("segment",),
                outputs=("left", "right"), rows=None, backoff=(), count_column="count"):
    if rows is None:
        rows = [("known", "001", "001", 1), ("known", "002", "002", 3)]
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow([*given, *outputs, count_column])
    writer.writerows(rows)
    path = tmp_path / (rule_id + ".csv")
    content = stream.getvalue().encode("utf-8")
    path.write_bytes(content)
    return {
        "id": rule_id, "type": "conditional_joint_table", "type_version": 1,
        "given": list(given), "outputs": list(outputs), "count_column": count_column,
        "table": {"path": str(path), "sha256": hashlib.sha256(content).hexdigest()},
        "backoff": [list(fields) for fields in backoff],
    }


def replace_csv(spec, content):
    if isinstance(content, str):
        content = content.encode("utf-8")
    Path(spec["table"]["path"]).write_bytes(content)
    spec["table"]["sha256"] = hashlib.sha256(content).hexdigest()


def backoff_spec(tmp_path):
    return write_table(
        tmp_path, given=("region", "band"), backoff=(("region",), ("band",), ()),
        rows=[("r1", "b1", "x", "x", 3), ("r1", "b1", "y", "y", 1),
              ("r1", "b2", "x", "x", 2), ("r1", "b2", "z", "z", 4),
              ("r2", "b1", "y", "y", 6)],
    )


def chained_specs(tmp_path):
    weather = write_table(
        tmp_path, rule_id="z_weather", given=(), outputs=("sky", "temperature"),
        rows=[("sun", "warm", 2), ("rain", "cool", 1)],
    )
    outing = write_table(
        tmp_path, rule_id="a_outing", given=("sky",), outputs=("activity", "beverage"),
        rows=[("sun", "walk", "water", 3), ("sun", "swim", "juice", 1),
              ("rain", "read", "tea", 1), ("rain", "cook", "coffee", 2)],
    )
    return [outing, weather]  # Deliberately reverse dependency/lexical order.


def test_perfect_coupling_and_known_weights(tmp_path):
    spec = write_table(tmp_path)
    rows = [{"segment": "known", "record_key": str(i), "left": "old"} for i in range(8000)]
    before = deepcopy(rows)
    sampler = JointSampler([spec], resolve, 73, ["segment", "record_key"], ["record_key"])
    output = sampler.sample_rows(rows)
    assert rows == before
    assert all(original is not new for original, new in zip(rows, output))
    assert [row["record_key"] for row in output] == [row["record_key"] for row in rows]
    observed = Counter((row["left"], row["right"]) for row in output)
    assert set(observed) == {("001", "001"), ("002", "002")}
    assert observed[("001", "001")] / len(rows) == pytest.approx(.25, abs=.02)
    # Independent draws would put substantial mass outside the diagonal support.
    assert observed[("001", "002")] == observed[("002", "001")] == 0


def test_second_configuration_chains_without_engine_changes(tmp_path):
    specs = chained_specs(tmp_path)
    sampler = JointSampler(specs, resolve, 7, [])
    assert [table.id for table in sampler.tables] == ["z_weather", "a_outing"]
    rows = sampler.sample_rows([{} for _ in range(3000)])
    support = {
        ("sun", "warm", "walk", "water"), ("sun", "warm", "swim", "juice"),
        ("rain", "cool", "read", "tea"), ("rain", "cool", "cook", "coffee"),
    }
    observed = {(r["sky"], r["temperature"], r["activity"], r["beverage"]) for r in rows}
    assert observed == support
    assert sum(r["sky"] == "sun" for r in rows) / len(rows) == pytest.approx(2 / 3, abs=.03)
    sun = [r for r in rows if r["sky"] == "sun"]
    assert sum(r["activity"] == "walk" for r in sun) / len(sun) == pytest.approx(.75, abs=.03)


def test_distribution_named_tuple_reordered_headers_and_string_categories(tmp_path):
    spec = write_table(tmp_path, count_column="frequency")
    replace_csv(spec, 'right,frequency,segment,left\n"",1,"",001\n" β ",3,"",002\n')
    table = JointTable(spec, resolve)
    result = table.distribution({"segment": "", "extra": "ignored"})
    assert isinstance(result, JointDistribution)
    fields, values, outcomes, probabilities = result
    assert fields == ("segment",) and values == ("",)
    assert outcomes == (("001", ""), ("002", " β "))
    np.testing.assert_array_equal(probabilities, [.25, .75])


def test_empty_string_condition_is_distinct_from_missing_or_unknown(tmp_path):
    spec = write_table(tmp_path, rows=[("", "", "001", 1)])
    table = JointTable(spec, resolve)
    assert table.distribution({"segment": ""}).output_tuples == (("", "001"),)
    with pytest.raises(ValueError, match="missing conditioning"):
        table.distribution({})
    with pytest.raises(ValueError, match="uncovered condition"):
        table.distribution({"segment": "unknown"})


def test_ordered_backoff_marginalizes_whole_output_tuples(tmp_path):
    table = JointTable(backoff_spec(tmp_path), resolve)
    exact = table.distribution({"region": "r1", "band": "b1"})
    assert exact.conditioning_fields == ("region", "band")
    assert exact.output_tuples == (("x", "x"), ("y", "y"))
    np.testing.assert_allclose(exact.probabilities, [.75, .25])
    region = table.distribution({"region": "r1", "band": "unseen"})
    assert region.conditioning_fields == ("region",) and region.condition_values == ("r1",)
    assert region.output_tuples == (("x", "x"), ("y", "y"), ("z", "z"))
    np.testing.assert_allclose(region.probabilities, [.5, .1, .4])
    band = table.distribution({"region": "unseen", "band": "b1"})
    assert band.conditioning_fields == ("band",)
    np.testing.assert_allclose(band.probabilities, [.3, .7])
    unconditional = table.distribution({"region": "unseen", "band": "unseen"})
    assert unconditional.conditioning_fields == unconditional.condition_values == ()
    np.testing.assert_allclose(unconditional.probabilities, [5 / 16, 7 / 16, 4 / 16])
    # Both first and second backoff cover this absent primary cell; first wins.
    first = table.distribution({"region": "r2", "band": "b2"})
    assert first.conditioning_fields == ("region",)
    assert first.output_tuples == (("y", "y"),)


def test_backoff_subset_field_order_is_explicit(tmp_path):
    spec = write_table(tmp_path, given=("one", "two", "three"),
                       rows=[("A", "B", "C", "x", "x", 1)], backoff=(("three", "one"),))
    distribution = JointTable(spec, resolve).distribution({"one": "A", "two": "?", "three": "C"})
    assert distribution.conditioning_fields == ("three", "one")
    assert distribution.condition_values == ("C", "A")


def test_report_aggregates_effective_strata_without_unrelated_row_data(tmp_path):
    spec = backoff_spec(tmp_path)
    sampler = JointSampler([spec], resolve, 19, ["region", "band"])
    rows = [{"region": "r1", "band": "b1"}, {"region": "r1", "band": "new-band"},
            {"region": "new-region", "band": "b1"}, {"region": "new-region", "band": "new-band"}]
    rows[0]["record_key"] = "SECRET-IDENTIFIER-999"
    rows[1]["free_text"] = "PRIVATE free text"
    sampler.sample_rows(rows)
    sampler.sample_rows(rows[:2])
    report = sampler.report()
    rule = report["rules"][0]
    assert report["rows_sampled"] == 6
    assert report["seed"] == 19 and report["stream_scheme"] == "jamboree-sdv-v1"
    assert rule["table"] == spec["table"] and rule["stream"] == "joint:paired"
    assert rule["joint_cell_count"] == 5 and rule["total_count"] == 16
    assert (rule["draw_count"], rule["exact_draw_count"], rule["backoff_draw_count"]) == (6, 2, 4)
    assert rule["conditioning_usage"] == [
        {"conditioning_fields": ["region", "band"], "backoff_index": None, "draw_count": 2},
        {"conditioning_fields": ["region"], "backoff_index": 0, "draw_count": 2},
        {"conditioning_fields": ["band"], "backoff_index": 1, "draw_count": 1},
        {"conditioning_fields": [], "backoff_index": 2, "draw_count": 1},
    ]
    assert rule["effective_conditions"] == [
        {"conditioning_fields": ["region", "band"], "condition_values": ["r1", "b1"], "draw_count": 2},
        {"conditioning_fields": ["region"], "condition_values": ["r1"], "draw_count": 2},
        {"conditioning_fields": ["band"], "condition_values": ["b1"], "draw_count": 1},
        {"conditioning_fields": [], "condition_values": [], "draw_count": 1},
    ]
    encoded = json.dumps(report)
    for forbidden in ("record_key", "SECRET-IDENTIFIER-999", "free_text", "PRIVATE", "new-region", "new-band"):
        assert forbidden not in encoded
    saved = deepcopy(report)
    report["rules"][0]["table"]["path"] = "changed"
    report["rules"][0]["conditioning_usage"][0]["draw_count"] = -1
    assert sampler.report() == saved


def test_unknown_context_fails_without_hidden_fallback(tmp_path):
    spec = backoff_spec(tmp_path)
    spec["backoff"] = [["region"]]
    table = JointTable(spec, resolve)
    with pytest.raises(ValueError, match="uncovered condition"):
        table.distribution({"region": "unknown", "band": "b1"})


@pytest.mark.parametrize("row", [{"region": "r1"}, {"region": None, "band": "b1"},
                                 {"region": 1, "band": "b1"}, {"region": "r1", "band": False}, []])
def test_backoff_does_not_hide_missing_or_non_string_context(tmp_path, row):
    with pytest.raises(ValueError):
        JointTable(backoff_spec(tmp_path), resolve).distribution(row)


def test_seed_order_unrelated_rules_and_chunk_invariance(tmp_path):
    specs = chained_specs(tmp_path)
    unrelated = write_table(tmp_path, rule_id="middle", given=(), outputs=("coin",),
                            rows=[("heads", 1), ("tails", 1)])
    rows = [{"record_key": str(i)} for i in range(1003)]
    full = JointSampler(specs, resolve, 1847, ["record_key"])
    expected = full.sample_rows(rows)
    chunked = JointSampler(list(reversed(specs)), resolve, 1847, ["record_key"])
    actual = chunked.sample_rows([])
    for index in range(0, len(rows), 37):
        actual.extend(chunked.sample_rows(rows[index:index + 37]))
    assert actual == expected
    assert chunked.report() == full.report()
    extra = JointSampler([unrelated, *specs], resolve, 1847, ["record_key"])
    with_extra = extra.sample_rows(rows)
    assert [{k: v for k, v in row.items() if k != "coin"} for row in with_extra] == expected
    assert [r for r in extra.report()["rules"] if r["id"] != "middle"] == full.report()["rules"]
    assert JointSampler(specs, resolve, 1848, ["record_key"]).sample_rows(rows) != expected


def test_chunk_invariance_with_mixed_backoff_levels(tmp_path):
    spec = backoff_spec(tmp_path)
    rows = ([{"region": "r1", "band": "b1"}, {"region": "r1", "band": "?"},
             {"region": "?", "band": "b1"}, {"region": "?", "band": "?"}] * 73)
    full = JointSampler([spec], resolve, 2, ["region", "band"])
    chunked = JointSampler([spec], resolve, 2, ["region", "band"])
    expected = full.sample_rows(rows)
    actual = []
    for index in range(0, len(rows), 17):
        actual.extend(chunked.sample_rows(rows[index:index + 17]))
    assert actual == expected and chunked.report() == full.report()
    assert all(row["left"] == row["right"] for row in actual)


def test_failed_batch_rolls_back_all_streams_and_report(tmp_path):
    specs = chained_specs(tmp_path)
    last = write_table(tmp_path, rule_id="last", given=("external", "activity"), outputs=("decision",),
                       rows=[("yes", activity, "ok", 1) for activity in ("walk", "swim", "read", "cook")])
    specs.append(last)
    sampler = JointSampler(specs, resolve, 41, ["external"])
    reference = JointSampler(specs, resolve, 41, ["external"])
    first = [{"external": "yes"}]
    assert sampler.sample_rows(first) == reference.sample_rows(first)
    before = sampler.report()
    invalid = [{"external": "yes"}, {"external": "unknown"}]
    with pytest.raises(ValueError, match="uncovered condition"):
        sampler.sample_rows(invalid)
    assert sampler.report() == before and invalid == [{"external": "yes"}, {"external": "unknown"}]
    subsequent = [{"external": "yes"} for _ in range(101)]
    assert sampler.sample_rows(subsequent) == reference.sample_rows(subsequent)
    assert sampler.report() == reference.report()


def test_engine_checks_hash_even_after_resolver_claims_verification(tmp_path):
    spec = write_table(tmp_path)

    def tampering_resolver(pin):
        path = resolve(pin)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == pin["sha256"]
        path.write_bytes(path.read_bytes().replace(b"001", b"999"))
        # Even mutating the resolver's pin cannot change the engine's expectation.
        pin["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return path

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        JointTable(spec, tampering_resolver)


def test_wrong_pin_is_rejected(tmp_path):
    spec = write_table(tmp_path)
    spec["table"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        JointTable(spec, resolve)


@pytest.mark.filterwarnings("ignore:Setting the (shape|dtype) on a NumPy array has been deprecated:DeprecationWarning")
def test_loaded_resources_are_immutable_and_detached(tmp_path):
    spec = write_table(tmp_path)
    table = JointTable(spec, resolve)
    pin = dict(table.table)
    distribution = table.distribution({"segment": "known"})
    spec["given"].append("new")
    spec["outputs"][0] = "new"
    spec["table"]["sha256"] = "0" * 64
    Path(pin["path"]).write_bytes(b"invalid replacement file")
    assert table.given == ("segment",) and table.outputs == ("left", "right")
    assert dict(table.table) == pin
    assert table.distribution({"segment": "known"}).output_tuples == distribution.output_tuples
    np.testing.assert_array_equal(distribution.probabilities, [.25, .75])
    with pytest.raises(FrozenInstanceError):
        table.outputs = ("changed",)
    with pytest.raises(TypeError):
        table.table["sha256"] = "0" * 64
    with pytest.raises(TypeError):
        table._distributions[(("segment",), ("known",))] = distribution
    with pytest.raises(ValueError):
        distribution.probabilities[0] = 0
    with pytest.raises(ValueError):
        distribution.probabilities.setflags(write=True)
    with pytest.raises(AttributeError):
        distribution.output_tuples = ()
    # Deliberate public-array metadata tampering; NumPy 2.5 deprecates these setters.
    distribution.probabilities.shape = (2, 1)
    distribution.probabilities.dtype = np.uint64
    fresh = table.distribution({"segment": "known"}).probabilities
    assert fresh.shape == (2,) and fresh.dtype == np.float64
    np.testing.assert_array_equal(fresh, [.25, .75])


def test_csv_order_does_not_change_draws(tmp_path):
    first = write_table(tmp_path)
    sampler = JointSampler([first], resolve, 12, ["segment"])
    replace_csv(first, "count,right,left,segment\n3,002,002,known\n1,001,001,known\n")
    reordered = JointSampler([first], resolve, 12, ["segment"])
    rows = [{"segment": "known"} for _ in range(100)]
    assert sampler.sample_rows(rows) == reordered.sample_rows(rows)


@pytest.mark.parametrize("updates", [
    {"extra": "unknown"}, {"type": "joint_table"}, {"type_version": 2},
    {"type_version": True}, {"type_version": 1.0}, {"id": ""}, {"id": " bad"},
    {"id": "line\nbreak"}, {"id": "1bad"}, {"given": "segment"}, {"given": ["segment", "segment"]},
    {"given": [None]}, {"outputs": []}, {"outputs": ["segment"]},
    {"outputs": ["left", "left"]}, {"outputs": ["has space"]}, {"outputs": ["café"]},
    {"count_column": "left"}, {"count_column": "segment"}, {"count_column": ""},
    {"backoff": None}, {"backoff": ""}, {"backoff": ["segment"]},
    {"backoff": [["segment"]]}, {"backoff": [["absent"]]}, {"backoff": [[], []]},
    {"backoff": [["segment", "segment"]]},
    {"table": {}}, {"table": {"path": "x", "sha256": "z" * 64}},
    {"table": {"path": "", "sha256": "0" * 64}},
    {"table": {"path": "\x00", "sha256": "0" * 64}},
    {"table": {"path": "x", "sha256": "0" * 63}},
    {"table": {"path": "x", "sha256": "0" * 64, "extra": True}},
])
def test_invalid_spec_fails_before_resolution(tmp_path, updates):
    spec = write_table(tmp_path)
    spec.update(updates)

    def forbidden(pin):
        pytest.fail("Invalid specs must fail before resolving any table")

    with pytest.raises(ValueError):
        JointTable(spec, forbidden)


@pytest.mark.parametrize("missing", ["id", "type", "type_version", "given", "outputs", "table", "count_column", "backoff"])
def test_every_spec_key_is_required(tmp_path, missing):
    spec = write_table(tmp_path)
    del spec[missing]
    with pytest.raises(ValueError, match="missing or unknown keys"):
        JointTable(spec, resolve)


def test_backoff_rejects_duplicate_subsets_regardless_of_order(tmp_path):
    spec = write_table(tmp_path, given=("one", "two", "three"),
                       rows=[("a", "b", "c", "x", "x", 1)], backoff=(("one", "two"), ("two", "one")))
    with pytest.raises(ValueError, match="distinct proper subsets"):
        JointTable(spec, resolve)


def test_unconditional_table_cannot_declare_redundant_backoff(tmp_path):
    spec = write_table(tmp_path, given=(), rows=[("x", "x", 1)], backoff=((),))
    with pytest.raises(ValueError, match="proper subsets"):
        JointTable(spec, resolve)


@pytest.mark.parametrize("content", [
    "", "segment,left,right,count\n", "segment,left,right,count\n\n",
    "segment,left,right,count\nknown,001,001\n",
    "segment,left,right,count\nknown,001,001,1,extra\n",
    "segment,left,right,count,extra\nknown,001,001,1,x\n",
    "segment,left,count\nknown,001,1\n",
    "segment,left,left,count\nknown,001,001,1\n",
    "segment,left,,count\nknown,001,001,1\n",
    "segment,left,right, count\nknown,001,001,1\n",
    'segment,left,right,count\nknown,"unterminated,001,1\n',
    "segment,left,right,count\nknown,001,001,1\nknown,001,001,2\n",
    b"segment,left,right,count\nknown,\xff,001,1\n",
])
def test_malformed_schema_empty_tables_and_duplicate_cells(tmp_path, content):
    spec = write_table(tmp_path)
    replace_csv(spec, content)
    with pytest.raises(ValueError):
        JointTable(spec, resolve)


@pytest.mark.parametrize("count", ["0", "-1", "+1", "1.0", "1e3", "nan", "NaN", "inf", "Infinity", "", " 1", "1 ", "١"])
def test_counts_are_positive_decimal_integers(tmp_path, count):
    spec = write_table(tmp_path, rows=[("known", "001", "001", count)])
    with pytest.raises(ValueError, match="positive integer"):
        JointTable(spec, resolve)


def test_large_integer_counts_do_not_overflow(tmp_path):
    count = 10 ** 400
    spec = write_table(tmp_path, rows=[("known", "001", "001", count), ("known", "002", "002", count)])
    table = JointTable(spec, resolve)
    assert table.total_count == 2 * count
    np.testing.assert_array_equal(table.distribution({"segment": "known"}).probabilities, [.5, .5])


def test_probability_underflow_is_not_silently_dropped(tmp_path):
    spec = write_table(tmp_path, rows=[("known", "001", "001", 1), ("known", "002", "002", 10 ** 400)])
    with pytest.raises(ValueError, match="positive float64 probabilities"):
        JointTable(spec, resolve)


def test_cumulative_rounding_cannot_silently_remove_a_joint_cell(tmp_path):
    spec = write_table(tmp_path, rows=[("known", "001", "001", 10 ** 30), ("known", "002", "002", 1)])
    with pytest.raises(ValueError, match="positive float64 sampling intervals"):
        JointTable(spec, resolve)


def test_missing_fields_cycles_and_initial_values_do_not_bypass_dependencies(tmp_path):
    first = write_table(tmp_path, rule_id="first", given=("y",), outputs=("x",), rows=[("a", "a", 1)])
    second = write_table(tmp_path, rule_id="second", given=("x",), outputs=("y",), rows=[("a", "a", 1)])
    with pytest.raises(ValueError, match="missing fields: y"):
        JointSampler([first], resolve, 1, [])
    with pytest.raises(ValueError, match="Cycle"):
        JointSampler([first, second], resolve, 1, [])
    with pytest.raises(ValueError, match="Cycle"):
        JointSampler([first, second], resolve, 1, ["x", "y"])


def test_duplicate_writers_ids_and_protected_outputs(tmp_path):
    first = write_table(tmp_path)
    second = write_table(tmp_path, rule_id="other")
    with pytest.raises(ValueError, match="Duplicate writers"):
        JointSampler([first, second], resolve, 1, ["segment"])
    with pytest.raises(ValueError, match="Duplicate rule id"):
        JointSampler([first, first], resolve, 1, ["segment"])
    with pytest.raises(ValueError, match="Protected output"):
        JointSampler([first], resolve, 1, ["segment"], ["right"])


def test_writer_takes_precedence_over_existing_initial_value(tmp_path):
    specs = chained_specs(tmp_path)
    sampler = JointSampler(specs, resolve, 4, ["sky"])
    rows = sampler.sample_rows([{"sky": "old"} for _ in range(20)])
    assert {row["sky"] for row in rows} <= {"sun", "rain"}


@pytest.mark.parametrize("seed", [-1, True, 1.5, "1", None])
def test_invalid_seed(tmp_path, seed):
    with pytest.raises(ValueError, match="Seed"):
        JointSampler([write_table(tmp_path)], resolve, seed, ["segment"])


@pytest.mark.parametrize("fields", [None, "segment", ["segment", "segment"], ["bad name"], {"segment": 1}])
def test_invalid_initial_or_protected_fields(tmp_path, fields):
    spec = write_table(tmp_path)
    with pytest.raises(ValueError):
        JointSampler([spec], resolve, 1, fields)
    with pytest.raises(ValueError):
        JointSampler([spec], resolve, 1, ["segment"], fields)


@pytest.mark.parametrize("rows", [None, {}, ({"segment": "known"},), [None], [{}]])
def test_invalid_batch_does_not_change_report(tmp_path, rows):
    sampler = JointSampler([write_table(tmp_path)], resolve, 1, ["segment"])
    before = sampler.report()
    with pytest.raises(ValueError):
        sampler.sample_rows(rows)
    assert sampler.report() == before


def test_empty_rule_set_is_identity_with_new_dictionaries():
    sampler = JointSampler([], resolve, 0, {"field"}, {"field"})
    rows = [{"field": "value"}]
    result = sampler.sample_rows(rows)
    assert result == rows and result[0] is not rows[0]
    assert sampler.tables == () and sampler.report()["rules"] == []
    assert sampler.report()["rows_sampled"] == 1
