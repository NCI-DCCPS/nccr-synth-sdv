"""Behavioral checks for generic partition, anchor and hierarchy composition."""

from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path

import pytest

from jamboree_sdv import joint_blocks
from jamboree_sdv.joint_blocks import apply_blocks
from jamboree_sdv.random_streams import seed_for


@pytest.fixture
def rule(tmp_path):
    def make(name, given, outputs, cells):
        path = tmp_path / (name + ".csv")
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow([*given, *outputs, "weight"])
            writer.writerows(cells)
        return {
            "id": name, "type": "conditional_joint_table", "type_version": 1,
            "given": given, "outputs": outputs,
            "table": {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
            "count_column": "weight", "backoff": [],
        }
    return make


def resolve(pin):
    return Path(pin["path"])


def block(name, rules, select=None, broadcast=None):
    return {"id": name, "select": select, "broadcast": broadcast, "rules": rules}


def select(*values):
    return {"column": "kind", "values": list(values)}


@pytest.fixture
def rows():
    # Anchors are interleaved with siblings; the first row is not an anchor.
    layout = [("P02", "other", "020"), ("P01", "index", "010"),
              ("P02", "index", "020"), ("P01", "other", "010"),
              ("P01", "other", "010")]
    return [{"person_id": person, "row_id": f"00{i}", "kind": kind, "age": age,
             "sex": "old", "leaf": "unmapped-original", "family": "original-family",
             "domain": "original-domain", "score": "old", "free context": f"\t{i}\n café  "}
            for i, (person, kind, age) in enumerate(layout)]


@pytest.fixture
def hierarchy():
    return {"leaf_column": "leaf", "parent_columns": ["family", "domain"],
            "mapping": {leaf: {"family": leaf[0], "domain": "D" + leaf[0]}
                        for leaf in ("A1", "A2", "B1", "B2")}}


@pytest.fixture
def patient_plan(rule):
    return [
        block("anchor", [rule("anchor_joint", ["age"], ["sex", "leaf"],
                              [["010", "F", "A1", 7], ["020", "M", "B1", 4]])],
              select("index"), {"key": "person_id", "fields": ["sex"]}),
        block("siblings", [rule("sibling_joint", ["age", "sex"], ["leaf"],
                                [["010", "F", "A2", 3], ["020", "M", "B2", 5]])],
              select("other")),
        block("downstream", [rule("by_family", ["family"], ["score"],
                                  [["A", "alpha", 1], ["B", "beta", 1]])]),
    ]


def test_patient_coherence_order_context_hierarchy_and_diagnostics(rows, patient_plan, hierarchy):
    before = deepcopy((rows, patient_plan, hierarchy))
    protected = ("person_id", "row_id", "kind", "age", "free context")
    actual, report = apply_blocks(rows, patient_plan, resolve, 123, protected, hierarchy)
    assert (rows, patient_plan, hierarchy) == before
    assert actual is not rows and all(new is not old for new, old in zip(actual, rows))
    assert [list(row) for row in actual] == [list(row) for row in rows]
    for new, old in zip(actual, rows):
        assert {field: new[field] for field in protected} == {field: old[field] for field in protected}
        is_a = new["person_id"] == "P01"
        assert new["sex"] == ("F" if is_a else "M")
        assert new["leaf"] == ("A" if is_a else "B") + ("1" if new["kind"] == "index" else "2")
        assert new["family"] == ("A" if is_a else "B")
        assert new["domain"] == ("DA" if is_a else "DB")
        assert new["score"] == ("alpha" if is_a else "beta")
    assert [item["selected_row_count"] for item in report["blocks"]] == [2, 3, 5]
    assert [item["hierarchy_count"] for item in report["blocks"]] == [2, 3, 0]
    assert [item["sampler_report"]["rows_sampled"] for item in report["blocks"]] == [2, 3, 5]
    first = report["blocks"][0]
    assert first["broadcast_groups"] == 2 and first["broadcast_row_count"] == 3
    assert first["changed_field_inventory"] == {"sex": 5, "leaf": 2, "family": 2, "domain": 2}
    assert first["written_fields"] == ["domain", "family", "leaf", "sex"]
    assert first["seed"] == first["sampler_report"]["seed"] == seed_for(123, "joint-block:anchor")
    assert report["changed_field_inventory"] == {"sex": 5, "leaf": 5, "family": 5, "domain": 5, "score": 5}
    assert report["full_joint_target_claimed"] is False
    assert report["partition_basis"] == "input_rows"
    assert "P01" not in json.dumps(report) and "P02" not in json.dumps(report)
    assert (actual, report) == apply_blocks(rows, patient_plan, resolve, 123, protected, hierarchy)


def test_hierarchy_changes_only_leaf_recipients(rows, patient_plan, hierarchy):
    actual, report = apply_blocks(rows, patient_plan[:1], resolve, 8, hierarchy=hierarchy)
    for old, new in zip(rows, actual):
        if old["kind"] == "other":
            assert all(new[field] == old[field] for field in ("leaf", "family", "domain", "score"))
    assert report["blocks"][0]["hierarchy_count"] == 2


def test_broadcast_leaf_derives_parents_for_all_recipients(rows, patient_plan, hierarchy):
    plan = deepcopy(patient_plan[:1])
    plan[0]["broadcast"]["fields"].append("leaf")
    actual, report = apply_blocks(rows, plan, resolve, 8, hierarchy=hierarchy)
    assert report["blocks"][0]["hierarchy_count"] == len(rows)
    for row in actual:
        assert row["leaf"] == ("A1" if row["person_id"] == "P01" else "B1")
        assert {field: row[field] for field in hierarchy["parent_columns"]} == hierarchy["mapping"][row["leaf"]]


def test_disjoint_physical_row_writers_are_accepted(rows, rule):
    plan = [block("first", [rule("first_rule", [], ["score"], [["X", 1]])], select("index")),
            block("second", [rule("second_rule", [], ["score"], [["Y", 1]])], select("other"))]
    actual, report = apply_blocks(rows, plan, resolve, 0)
    assert [row["score"] for row in actual] == ["Y", "X", "X", "Y", "Y"]
    assert report["changed_field_inventory"] == {"score": 5}


def test_overlapping_writers_fail_before_resolving_tables(rows, rule):
    plan = [block("first", [rule("first_rule", [], ["score"], [["X", 1]])]),
            block("second", [rule("second_rule", [], ["score"], [["Y", 1]])], select("other"))]
    before = deepcopy(rows)
    with pytest.raises(ValueError, match="Overlapping writers"):
        apply_blocks(rows, plan, lambda pin: pytest.fail("Resolved before ownership validation"), 0)
    assert rows == before


def test_broadcast_ownership_includes_nonanchors(rows, rule):
    plan = [block("first", [rule("first_rule", [], ["sex"], [["X", 1]])], select("index"),
                  {"key": "person_id", "fields": ["sex"]}),
            block("second", [rule("second_rule", [], ["sex"], [["Y", 1]])], select("other"))]
    with pytest.raises(ValueError, match="Overlapping writers"):
        apply_blocks(rows, plan, resolve, 0)


@pytest.mark.parametrize("kind", ["zero", "multiple"])
def test_every_group_needs_exactly_one_anchor(rows, patient_plan, kind):
    rows[2 if kind == "zero" else 0]["kind"] = "other" if kind == "zero" else "index"
    before = deepcopy(rows)
    with pytest.raises(ValueError, match="exactly one selected anchor"):
        apply_blocks(rows, patient_plan[:1], lambda pin: pytest.fail("Resolved before anchors validated"), 0)
    assert rows == before


@pytest.mark.parametrize("protected", [("family",), ("domain",), ("sex",), ("leaf",)])
def test_protected_sample_broadcast_and_hierarchy_fields_fail(rows, patient_plan, hierarchy, protected):
    before = deepcopy(rows)
    with pytest.raises(ValueError, match="protected fields"):
        apply_blocks(rows, patient_plan, resolve, 0, protected, hierarchy)
    assert rows == before


def test_future_given_writer_fails_including_broadcast_to_other_partition(rows, rule):
    plan = [block("read_first", [rule("read_first_rule", ["sex"], ["score"], [["old", "X", 1]])],
                  select("other")),
            block("write_later", [rule("write_later_rule", [], ["sex"], [["F", 1]])], select("index"),
                  {"key": "person_id", "fields": ["sex"]})]
    before = deepcopy(rows)
    with pytest.raises(ValueError, match="given field sex has a future writer"):
        apply_blocks(rows, plan, lambda pin: pytest.fail("Resolved before future reads validated"), 0)
    assert rows == before


def test_future_given_writer_on_disjoint_rows_is_allowed(rows, rule):
    plan = [block("read_first", [rule("read_first_rule", ["sex"], ["score"], [["old", "X", 1]])],
                  select("index")),
            block("write_later", [rule("write_later_rule", [], ["sex"], [["F", 1]])], select("other"))]
    actual, _ = apply_blocks(rows, plan, resolve, 0)
    assert [(r["score"], r["sex"]) for r in actual] == [("old", "F"), ("X", "old"),
                                                           ("X", "old"), ("old", "F"), ("old", "F")]


def test_future_hierarchy_writer_cannot_invalidate_earlier_given(rows, rule, patient_plan, hierarchy):
    earlier = block("earlier", [rule("earlier_rule", ["family"], ["score"], [["original-family", "X", 1]])])
    with pytest.raises(ValueError, match="given field family has a future writer"):
        apply_blocks(rows, [earlier, patient_plan[0]], resolve, 0, hierarchy=hierarchy)


def test_hierarchy_parents_cannot_be_sampled(rows, rule, hierarchy):
    plan = [block("bad", [rule("bad_rule", [], ["family"], [["A", 1]])])]
    with pytest.raises(ValueError, match="Hierarchy parents cannot be sampled"):
        apply_blocks(rows, plan, resolve, 0, hierarchy=hierarchy)


def test_same_block_cannot_read_parents_that_are_derived_after_it(rows, rule, patient_plan, hierarchy):
    plan = deepcopy(patient_plan[:1])
    plan[0]["rules"].append(rule("premature", ["family"], ["score"], [["original-family", "X", 1]]))
    with pytest.raises(ValueError, match="downstream block"):
        apply_blocks(rows, plan, resolve, 0, hierarchy=hierarchy)


@pytest.mark.parametrize("fields", [["score"], ["person_id"], ["absent"]])
def test_broadcast_requires_own_sampled_output_columns(rows, patient_plan, fields):
    plan = deepcopy(patient_plan[:1])
    plan[0]["broadcast"]["fields"] = fields
    with pytest.raises(ValueError, match="sampled outputs|unknown columns"):
        apply_blocks(rows, plan, resolve, 0)


def test_broadcast_group_key_cannot_be_written_by_any_block(rows, patient_plan, rule):
    plan = deepcopy(patient_plan[:1])
    plan.append(block("key_writer", [rule("key_rule", [], ["person_id"], [["replaced", 1]])]))
    with pytest.raises(ValueError, match="Broadcast keys cannot be written"):
        apply_blocks(rows, plan, resolve, 0)


@pytest.mark.parametrize("case", ["block_extra", "block_missing", "block_id", "duplicate_block", "duplicate_rule",
                                  "selector_extra", "selector_column", "selector_values", "selector_duplicate",
                                  "broadcast_extra", "broadcast_empty", "rules_empty", "output_missing"])
def test_strict_plan_validation(rows, patient_plan, case):
    plan = deepcopy(patient_plan)
    if case == "block_extra": plan[0]["unexpected"] = True
    if case == "block_missing": del plan[0]["broadcast"]
    if case == "block_id": plan[0]["id"] = []
    if case == "duplicate_block": plan[1]["id"] = plan[0]["id"]
    if case == "duplicate_rule": plan[1]["rules"][0]["id"] = plan[0]["rules"][0]["id"]
    if case == "selector_extra": plan[0]["select"]["typo"] = "index"
    if case == "selector_column": plan[0]["select"]["column"] = "absent"
    if case == "selector_values": plan[0]["select"]["values"] = "index"
    if case == "selector_duplicate": plan[0]["select"]["values"] = ["index", "index"]
    if case == "broadcast_extra": plan[0]["broadcast"]["typo"] = True
    if case == "broadcast_empty": plan[0]["broadcast"]["fields"] = []
    if case == "rules_empty": plan[0]["rules"] = []
    if case == "output_missing": plan[1]["rules"][0]["outputs"] = ["absent"]
    before = deepcopy((rows, plan))
    with pytest.raises(ValueError):
        apply_blocks(rows, plan, lambda pin: pytest.fail("Resolved before shape validation"), 0)
    assert (rows, plan) == before


@pytest.mark.parametrize("bad", [None, (), [None], [{1: "x"}], [{"field": 1}],
                               [{"field": "x"}, {"other": "x"}], [{"": "x"}]])
def test_rows_require_uniform_dicts_of_strings(bad):
    before = deepcopy(bad)
    with pytest.raises(ValueError):
        apply_blocks(bad, [], resolve, 0)
    assert bad == before


@pytest.mark.parametrize("case", ["extra", "leaf", "parents", "parent_missing", "parent_sampled_leaf",
                                  "mapping_empty", "mapping_missing", "mapping_extra", "mapping_nonstr"])
def test_strict_hierarchy_shape(rows, patient_plan, hierarchy, case):
    if case == "extra": hierarchy["typo"] = True
    if case == "leaf": hierarchy["leaf_column"] = "absent"
    if case == "parents": hierarchy["parent_columns"] = ["family", "family"]
    if case == "parent_missing": hierarchy["parent_columns"] = ["absent"]
    if case == "parent_sampled_leaf": hierarchy["parent_columns"] = ["leaf"]
    if case == "mapping_empty": hierarchy["mapping"] = {}
    if case == "mapping_missing": del hierarchy["mapping"]["A1"]["domain"]
    if case == "mapping_extra": hierarchy["mapping"]["A1"]["unrelated"] = "x"
    if case == "mapping_nonstr": hierarchy["mapping"]["A1"]["family"] = 1
    before = deepcopy((rows, hierarchy))
    with pytest.raises(ValueError):
        apply_blocks(rows, patient_plan, resolve, 0, hierarchy=hierarchy)
    assert (rows, hierarchy) == before


def test_unmapped_sampled_leaf_failure_is_atomic(rows, patient_plan, hierarchy):
    del hierarchy["mapping"]["B1"]
    before = deepcopy((rows, patient_plan, hierarchy))
    with pytest.raises(ValueError, match="absent from mapping"):
        apply_blocks(rows, patient_plan, resolve, 0, hierarchy=hierarchy)
    assert (rows, patient_plan, hierarchy) == before


def test_later_uncovered_condition_failure_is_atomic(rows, patient_plan, hierarchy):
    # First block succeeds internally, but a later draw has no declared support.
    rows[0]["age"] = "999"
    before = deepcopy((rows, patient_plan, hierarchy))
    with pytest.raises(ValueError, match="uncovered condition"):
        apply_blocks(rows, patient_plan, resolve, 0, hierarchy=hierarchy)
    assert (rows, patient_plan, hierarchy) == before


def test_all_samplers_compile_before_any_draw(rows, patient_plan, hierarchy, monkeypatch):
    plan = deepcopy(patient_plan)
    plan[-1]["rules"][0]["type_version"] = 99
    monkeypatch.setattr(joint_blocks.JointSampler, "sample_rows", lambda *args: pytest.fail("Drew before compiling all blocks"))
    with pytest.raises(ValueError, match="type_version"):
        apply_blocks(rows, plan, resolve, 0, hierarchy=hierarchy)


def test_engine_handles_within_block_dependency_order(rows, rule):
    plan = [block("dag", [rule("downstream_first", ["sex"], ["score"], [["F", "X", 1]]),
                          rule("upstream_last", [], ["sex"], [["F", 1]])])]
    actual, report = apply_blocks(rows, plan, resolve, 4)
    assert all(row["sex"] == "F" and row["score"] == "X" for row in actual)
    assert report["blocks"][0]["sampler_report"]["rule_order"] == ["upstream_last", "downstream_first"]


def test_empty_selection_still_validates_table_and_reports_zero_draws(rows, rule):
    plan = [block("empty", [rule("unused", [], ["score"], [["X", 1]])], select())]
    actual, report = apply_blocks(rows, plan, resolve, 0)
    assert actual == rows and actual is not rows
    assert report["changed_field_inventory"] == {}
    item = report["blocks"][0]
    assert item["selected_row_count"] == item["hierarchy_count"] == item["broadcast_groups"] == 0
    assert item["sampler_report"]["rules"][0]["draw_count"] == 0
    plan[0]["rules"][0]["table"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        apply_blocks(rows, plan, resolve, 0)


def test_no_blocks_returns_detached_rows_and_empty_report(rows):
    actual, report = apply_blocks(rows, [], resolve, 0)
    assert actual == rows and all(a is not b for a, b in zip(actual, rows))
    assert report["blocks"] == [] and report["changed_field_inventory"] == {}
    assert apply_blocks([], [], resolve, 0)[0] == []


def test_unchanged_assignments_are_not_reported_as_changed(rows, rule):
    plan = [block("same", [rule("same_rule", [], ["sex"], [["old", 1]])], select("index"),
                  {"key": "person_id", "fields": ["sex"]})]
    actual, report = apply_blocks(rows, plan, resolve, 0)
    assert actual == rows
    assert report["blocks"][0]["written_fields"] == ["sex"]
    assert report["changed_field_inventory"] == {}


def test_second_domain_uses_same_adapter_without_code_changes(rule):
    rows = [{"batch": "01", "anchor": "yes", "region": "north", "device": "?", "flag": "?", "memo": ""},
            {"batch": "01", "anchor": "no", "region": "north", "device": "?", "flag": "?", "memo": " 00 "},
            {"batch": "02", "anchor": "yes", "region": "south", "device": "?", "flag": "?", "memo": "é"}]
    plan = [block("device_assignment", [rule("device_rule", ["region"], ["device"],
                                           [["north", "scanner", 10], ["south", "sensor", 10]])],
                  {"column": "anchor", "values": ["yes"]}, {"key": "batch", "fields": ["device"]}),
            block("device_flag", [rule("flag_rule", ["device"], ["flag"],
                                     [["scanner", "S", 5], ["sensor", "T", 5]])])]
    before = deepcopy(rows)
    actual, report = apply_blocks(rows, plan, resolve, 38, ("batch", "anchor", "region", "memo"))
    assert rows == before
    assert [(r["device"], r["flag"]) for r in actual] == [("scanner", "S"), ("scanner", "S"), ("sensor", "T")]
    assert [r["memo"] for r in actual] == [r["memo"] for r in rows]
    assert report["blocks"][0]["broadcast_groups"] == 2
