#!/usr/bin/env python3
"""Build an identifier-free, file-derived G1 candidate; standard library only.

No generator profile changes, clinical exclusions, repairs, or automatic promotion.
Replay: python provenance/build_g1_snapshot.py --replay-bundle BUNDLE
  --replay-manifest-sha256 PIN --source-csv RAW --out NEW_EMPTY_DIRECTORY
"""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import platform
import shutil
import sys
import time

VERSION = "1.0.1"
SOURCE_SHA = "23d71d7177b11de873b1ca3786a4b49b89a0bda4722f7973c13d9fcf950029a1"
REFERENCE_SHA = "775ca904314fa8ff37c3563a3d944c6e78fb30d53eb03b1b404e2471bc78619a"
HIERARCHY_SHA = "1ef2a47229785b209d5e1514d548739816879233a57728811e81211e2e64c3ed"
METADATA_SHA = "5b3fbc18614971481f0c31975dab71678f081b2d2800354607743ece0b6d4b70"
P16_RULES_SHA = "0ab9a6a00706c407fb8528393974b724d27ffa38d2ef83dfdfe491ba2fa198f8"
P16_CHECKER_SHA = "ea68d321e3cb0ac13eba157f54d5725938ce85fdb83b99e6cf640bdd028f79e2"
AGE, SEX, RACE, LEAF = "ageRecodeSingle90p", "sex", "raceAndOriginRecode", "iccc2017ExtendedRecode"
GROUP, YEAR, INDEX = "ageRecode20Groups", "yearOfDiagnosis", "indexCancer"
SCHEMA, P16 = "schemaId", "p16Recode"
PID, TUMOR = "Data Request Patient ID", "tumorRecordNumber"
PARENTS = ["iccc2017Recode", "icccMajor"]
AXES = [AGE, SEX, RACE, LEAF]
SCOPES = ("all", "index", "nonindex", "through2022_all", "through2022_index", "through2022_nonindex")
REQUIRED = [PID, TUMOR, *AXES, GROUP, YEAR, INDEX, *PARENTS, SCHEMA, P16]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def now():
    return datetime.now().astimezone().isoformat()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def pin(path):
    return {"sha256": digest(path), "size_bytes": Path(path).stat().st_size}


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load(path):
    return json.loads(Path(path).read_bytes())


def checked(path, expected):
    require(digest(path) == expected, "Input SHA-256 mismatch: " + Path(path).name)
    return Path(path)


def safe_member(root, relative):
    rel = Path(relative)
    require(not rel.is_absolute() and ".." not in rel.parts, "Unsafe archive path")
    path = root / rel
    require(path.resolve().is_relative_to(root.resolve()), "Archive member escapes root")
    return path


def verify_inventory(root, expected_manifest):
    manifest = load(checked(root / "manifest.json", expected_manifest))
    names = set()
    for entry in manifest["files"]:
        require(entry["path"] not in names, "Duplicate archive inventory path")
        names.add(entry["path"])
        path = safe_member(root, entry["path"])
        require(pin(path) == {k: entry[k] for k in ("sha256", "size_bytes")},
                "Archive inventory mismatch: " + entry["path"])
    return manifest


def copy_exact(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    require(not target.exists(), "Refusing archive overwrite")
    original = pin(source)
    shutil.copyfile(source, target)
    require(pin(target) == original == pin(source), "Input changed while archiving")


def grouped_age(age):
    # Fixed, two-character source encoding; no integer coercion of malformed codes.
    require(type(age) is str and age in {f"{x:02d}" for x in range(91)} | {"99"},
            "Invalid single-year age encoding")
    if age == "99":
        return "99"
    value = int(age)
    return f"{0 if value == 0 else value // 5 + 1:02d}"


def through2022(year):
    return (type(year) is str and len(year) == 4 and
            all(c in "0123456789" for c in year) and "1995" <= year <= "2022")


def scopes_for(index, year):
    require(index in ("0", "1", "2"), "Invalid indexCancer code")
    selected = ["all", "nonindex" if index == "0" else "index"]
    return selected + ["through2022_" + s for s in selected] if through2022(year) else selected


def metadata_contract(metadata):
    fields = metadata["fields"]
    domains = {}
    for name in [AGE, GROUP, SEX, RACE, INDEX, LEAF, *PARENTS]:
        require(name in fields, "Missing metadata field: " + name)
        values = fields[name]["permissible_values"]
        require(all(type(v["value"]) is str for v in values if v.get("kind") == "literal"),
                "Metadata codes must be exact strings")
        domains[name] = {v["value"] for v in values if v.get("kind") == "literal"}
        require(domains[name], "Empty metadata domain: " + name)
    require(domains[AGE] == {f"{n:02d}" for n in range(91)} | {"99"}, "Unexpected age metadata")
    require(domains[GROUP] == {f"{n:02d}" for n in range(20)} | {"99"}, "Unexpected grouped-age metadata")
    require(domains[INDEX] == {"0", "1", "2"}, "Unexpected index metadata")
    return domains


def load_contracts(reference, rules_path, checker_path):
    metadata = load(checked(reference / "inputs/ctc-normalized.json", METADATA_SHA))
    hierarchy_bundle = load(checked(reference / "inputs/rules-v2.3.0.json", HIERARCHY_SHA))
    require(hierarchy_bundle["version"] == "2.3.0", "Wrong hierarchy version")
    hierarchy = next(r for r in hierarchy_bundle["rules"] if r["id"] == "ctc_iccc_2017_hierarchy_is_consistent")
    require(hierarchy["enabled"] is True and hierarchy["review_status"] == "confirmed", "Hierarchy not confirmed")
    bundle = load(checked(rules_path, P16_RULES_SHA))
    require(bundle["version"] == "3.1.0", "Wrong p16 rule version")
    rule = next(r for r in bundle["rules"] if r["id"] == "ctc_p16_applicability")
    checked(checker_path, P16_CHECKER_SHA)
    namespace = {"__name__": "archived_p16_rules", "__file__": str(checker_path)}
    # Execute the pinned standalone checker without importing any experiment package.
    exec(compile(checker_path.read_bytes(), str(checker_path), "exec"), namespace)
    return metadata_contract(metadata), hierarchy["mapping"], rule, namespace["P16Check"]


def rollup(counts, positions, transform=None):
    result = Counter()
    for key, value in counts.items():
        projected = tuple(key[i] for i in positions)
        result[transform(projected) if transform else projected] += value
    return result


def read_counts(path, columns, exact_header=False):
    result = Counter()
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream, strict=True)
        require(reader.fieldnames and len(set(reader.fieldnames)) == len(reader.fieldnames), "Invalid count header")
        require(all(k in reader.fieldnames for k in columns + ["count"]), "Missing count column")
        if exact_header:
            require(reader.fieldnames == columns + ["count"], "Unexpected count header")
        previous = None
        for row in reader:
            require(None not in row and all(v is not None for v in row.values()), "Malformed count row")
            key, text = tuple(row[k] for k in columns), row["count"]
            require(text.isascii() and text.isdigit() and int(text) > 0 and str(int(text)) == text,
                    "Counts must be canonical positive integers")
            require(key not in result, "Duplicate count key")
            if exact_header:
                require(previous is None or previous < key, "Count tuples not sorted")
            previous = key
            result[key] = int(text)
    return result


def write_counts(path, columns, counts):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(columns + ["count"])
        for key, value in sorted(counts.items()):
            require(type(value) is int and value > 0, "Nonpositive/noninteger count")
            require(len(key) == len(columns) and all(type(k) is str for k in key), "Non-string count code")
            writer.writerow([*key, value])
    require(read_counts(path, columns, True) == counts, "Count CSV readback mismatch")
    return {"columns": columns + ["count"], "cells": len(counts), "count_total": sum(counts.values()), **pin(path)}


def aggregate(source, domains, hierarchy, p16_rule, checker_class, expected_rows, expected_patients):
    counts = {s: Counter() for s in SCOPES}
    grouped = {s: Counter() for s in SCOPES}
    marginals = {k: Counter() for k in REQUIRED if k not in (PID, TUMOR)}
    patients, keys = {}, set()
    diagnostics = Counter()
    check = checker_class(p16_rule)
    reasons = {"all": Counter(), "through2022": Counter()}
    coverage, p16_counts = defaultdict(Counter), Counter()
    total = 0
    age_map = {a: grouped_age(a) for a in domains[AGE]}
    with source.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.reader(stream, strict=True)
        header = next(reader, [])
        require(len(header) == len(set(header)), "Duplicate source header")
        require(set(REQUIRED) <= set(header), "Missing required source column")
        positions = {k: header.index(k) for k in REQUIRED}
        for raw in reader:
            require(len(raw) == len(header), "Malformed source row width")
            row = {k: raw[i] for k, i in positions.items()}
            total += 1
            require(row[PID] != "" and row[TUMOR] != "", "Missing patient/tumor key")
            key = (row[PID], row[TUMOR])
            require(key not in keys, "Duplicate patient/tumor key")
            keys.add(key)
            require(row[AGE] in age_map, "Invalid single-year age encoding")
            require(row[GROUP] == age_map[row[AGE]], "Grouped-age encoding mismatch")
            require(row[LEAF] in hierarchy, "Unmapped ICCC leaf")
            require(all(row[k] == hierarchy[row[LEAF]][k] for k in PARENTS), "ICCC hierarchy mismatch")
            selected = scopes_for(row[INDEX], row[YEAR])
            is_index = row[INDEX] in ("1", "2")
            if row[PID] not in patients:
                patients[row[PID]] = [row[SEX], row[RACE], 0]
            state = patients[row[PID]]
            require(state[:2] == [row[SEX], row[RACE]], "Patient sex/race inconsistency")
            state[2] += int(is_index)
            require(state[2] <= 1, "Multiple index tumors for patient")
            for k in marginals:
                marginals[k][row[k]] += 1
            joint = tuple(row[k] for k in AXES)
            group_joint = (row[GROUP], *joint[1:])
            for scope in selected:
                counts[scope][joint] += 1
                grouped[scope][group_joint] += 1
            status, reason = check.add(row)
            reasons["all"][(status, reason)] += 1
            coverage[(row[YEAR], row[SCHEMA])][status] += 1
            if through2022(row[YEAR]):
                reasons["through2022"][(status, reason)] += 1
                if status == "passed":
                    p16_counts[(row[YEAR], row[SCHEMA], row[P16])] += 1
            if total % 400000 == 0:
                print(json.dumps({"phase": "aggregate", "rows": total}), flush=True)
    require(total == expected_rows, "Source row total mismatch")
    require(len(patients) == expected_patients, "Patient total mismatch")
    require(all(s[2] == 1 for s in patients.values()), "Patient without exactly one index tumor")
    for scope in SCOPES:
        reduced = rollup(counts[scope], (0, 1, 2, 3), lambda k: (age_map[k[0]], *k[1:]))
        require(reduced == grouped[scope], "Single/grouped-age rollup mismatch")
    for prefix in ("", "through2022_"):
        require(counts[prefix + "all"] == counts[prefix + "index"] + counts[prefix + "nonindex"],
                "Index partition mismatch")
    diagnostics.update({"rows": total, "patients": len(patients), "unique_patient_tumor_keys": len(keys),
                        "patients_with_exactly_one_index": len(patients)})
    # No identifiers, identifier hashes, or source rows leave this function.
    validation = {"status": "passed", **dict(diagnostics), "duplicate_keys": 0,
                  "sex_race_conflicts": 0, "hierarchy_mismatches": 0, "grouped_age_mismatches": 0,
                  "source_repairs": 0, "g1_excluded_rows": 0,
                  "metadata_unlisted_codes": {k: {v: n for v, n in sorted(marginals[k].items()) if v not in domain}
                                              for k, domain in domains.items()},
                  "metadata_policy": "Metadata is a bounded vocabulary. Unlisted sex/race strings are retained and reported; no imputation. ICCC hierarchy and age/index encodings are enforced."}
    return {"counts": counts, "grouped": grouped, "marginals": marginals, "validation": validation,
            "p16_check": check.finish(), "p16_reasons": reasons, "p16_coverage": coverage, "p16_counts": p16_counts}


def compare_reference(result, reference, hierarchy):
    comparisons = {}

    def compare(name, actual, expected):
        require(actual == expected, "Frozen reference rollup mismatch: " + name)
        comparisons[name] = {"status": "passed", "cells": len(actual), "count_total": sum(actual.values())}

    analysis = reference / "analysis"
    simple = read_counts(analysis / "iccc_extended_age_race_counts.csv", ["age_code", "race_code", "iccc_code"])
    compare("all_sum_over_sex", rollup(result["counts"]["all"], (0, 2, 3)), simple)
    for name, scope in [("iccc_extended_age_group_race_counts.csv", "all"),
                        ("index_iccc_extended_age_group_race_counts.csv", "index"),
                        ("through_2022_iccc_extended_age_group_race_counts.csv", "through2022_all")]:
        expected = read_counts(analysis / name, ["age_code", "race_code", "iccc_code"])
        compare(name, rollup(result["grouped"][scope], (0, 2, 3)), expected)
    detailed = read_counts(analysis / "iccc_extended_age_group_race_year_index_counts.csv", [GROUP, RACE, LEAF, YEAR, INDEX])
    expected = {s: Counter() for s in SCOPES}
    for (age, race, leaf, year, index), count in detailed.items():
        for scope in scopes_for(index, year):
            expected[scope][(age, race, leaf)] += count
    for scope in SCOPES:
        compare("year_index_rollup_" + scope, rollup(result["grouped"][scope], (0, 2, 3)), expected[scope])
    for name, parent in [("iccc_recode_age_group_race_counts.csv", PARENTS[0]),
                         ("iccc_major_age_group_race_counts.csv", PARENTS[1])]:
        actual = rollup(result["grouped"]["all"], (0, 2, 3), lambda k: (*k[:2], hierarchy[k[2]][parent]))
        compare(name, actual, read_counts(analysis / name, ["age_code", "race_code", "iccc_code"]))
    return comparisons


def emit(result, out, rule, domains):
    tables = {}
    for scope in SCOPES:
        tables[scope + ".csv"] = write_counts(out / (scope + ".csv"), AXES, result["counts"][scope])
        name = "grouped_age_" + scope + ".csv"
        tables[name] = write_counts(out / name, [GROUP, SEX, RACE, LEAF], result["grouped"][scope])
    name = "through2022_schema_p16.csv"
    tables[name] = write_counts(out / name, [YEAR, SCHEMA, P16], result["p16_counts"])
    reasons = Counter({(scope, status, reason): count for scope, values in result["p16_reasons"].items()
                       for (status, reason), count in values.items()})
    tables["p16_reason_counts.csv"] = write_counts(out / "p16_reason_counts.csv", ["scope", "status", "reason"], reasons)
    all_schemas = sorted(set(rule["recognized_schema_codes"]) | set(result["marginals"][SCHEMA]))
    all_years = sorted({str(y) for y in range(rule["supported_diagnosis_years"]["minimum"], rule["supported_diagnosis_years"]["maximum"] + 1)} | set(result["marginals"][YEAR]))
    coverage = result["p16_coverage"]
    with (out / "p16_schema_year_coverage.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow([YEAR, SCHEMA, "source_rows", "passed_rows", "finding_rows", "not_evaluated_rows", "retained_through2022_rows"])
        for year in all_years:
            for schema in all_schemas:
                c = coverage.get((year, schema), Counter())
                writer.writerow([year, schema, sum(c.values()), c["passed"], c["findings"], c["not_evaluated"], c["passed"] if through2022(year) else 0])
    retained = sum(result["p16_counts"].values())
    through = result["p16_reasons"]["through2022"]
    status_totals = Counter()
    for (status, _), n in through.items():
        status_totals[status] += n
    require(retained == status_totals["passed"], "p16 retained denominator mismatch")
    p16_report = {"rule_version": "3.1.0", "all_source": result["p16_check"],
                  "through2022": {"rows": sum(through.values()), "passed_rows": retained,
                                  "finding_rows": status_totals["findings"], "not_evaluated_rows": status_totals["not_evaluated"],
                                  "reasons": [{"status": s, "reason": r, "count": n} for (s, r), n in sorted(through.items())]},
                  "outside_through2022_rows": result["validation"]["rows"] - sum(through.values()),
                  "passed_table_p16_counts": {k[0]: v for k, v in sorted(rollup(result["p16_counts"], (2,)).items())},
                  "explicit_blank": "Valid category stored as an empty CSV field; read with NA inference disabled.",
                  "excluded_rows_policy": "Findings and not_evaluated excluded only from p16 table, with exact reason counts; no source repair.",
                  "coverage": {"path": "p16_schema_year_coverage.csv", "schemas": len(all_schemas), "years": all_years,
                               "observed_schema_year_pairs": len(coverage), "grid_cells": len(all_schemas) * len(all_years),
                               "zero_meaning": "No observed support, never a biological exclusion; grid includes all recognized and observed schemas/years."}}
    save(out / "p16_report.json", p16_report)
    support = {"domains": {k: sorted(v) for k, v in domains.items()},
               "observed_marginals": {k: dict(sorted(v.items())) for k, v in result["marginals"].items()},
               "scopes": {scope: {"age_counts": {k[0]: v for k, v in sorted(rollup(c, (0,)).items())},
                                  "age_sex_race_strata": len(rollup(c, (0, 1, 2))),
                                  "age_40_plus_rows": sum(n for (age, _, _, _), n in c.items() if age != "99" and int(age) >= 40)}
                          for scope, c in result["counts"].items()},
               "missing_cells": "Unobserved, not structural impossibilities. Unknown/unclassified source codes retained."}
    save(out / "support.json", support)
    return tables, p16_report


def build(args):
    started, clock = now(), time.monotonic()
    out = args.out.resolve()
    require(not out.exists() or not any(out.iterdir()), "Refusing nonempty output directory")
    if args.replay_bundle:
        require(args.replay_manifest_sha256 is not None, "Replay requires manifest pin")
        replay = args.replay_bundle.resolve()
        previous = verify_inventory(replay, args.replay_manifest_sha256)
        reference = replay / "inputs/reference"
        rules_path, checker_path = replay / "inputs/rules-v3.1.0.json", replay / "provenance/p16_rules.py"
        evidence_root = replay / "inputs/p16-evidence"
        expected = previous["source"]["expected"]
    else:
        require(all((args.reference, args.p16_rules, args.p16_checker, args.evidence_root)), "Initial build requires explicit reference/rules/checker/evidence-root")
        reference, rules_path, checker_path = args.reference.resolve(), args.p16_rules.resolve(), args.p16_checker.resolve()
        evidence_root = args.evidence_root.resolve()
        expected = {"sha256": SOURCE_SHA, "size_bytes": 842668671, "rows": 1614685, "patients": 1483916}
    require(expected == {"sha256": SOURCE_SHA, "size_bytes": 842668671, "rows": 1614685, "patients": 1483916}, "Unexpected authorized source contract")
    source = args.source_csv.resolve()
    before = pin(source)
    require(before == {k: expected[k] for k in ("sha256", "size_bytes")}, "Authorized source hash/size mismatch")
    source_stat = source.stat()
    reference_manifest = verify_inventory(reference, REFERENCE_SHA)
    domains, hierarchy, rule, checker = load_contracts(reference, rules_path, checker_path)
    for item in rule["evidence"]:
        checked(safe_member(evidence_root, item["path"]), item["sha256"])
    out.mkdir(parents=True, exist_ok=True)
    copy_exact(reference / "manifest.json", out / "inputs/reference/manifest.json")
    for entry in reference_manifest["files"]:
        copy_exact(safe_member(reference, entry["path"]), safe_member(out / "inputs/reference", entry["path"]))
    copy_exact(rules_path, out / "inputs/rules-v3.1.0.json")
    copy_exact(checker_path, out / "provenance/p16_rules.py")
    copy_exact(Path(__file__), out / "provenance/build_g1_snapshot.py")
    for entry in rule["evidence"]:
        copy_exact(safe_member(evidence_root, entry["path"]), safe_member(out / "inputs/p16-evidence", entry["path"]))
    # From this point onward, only archived reference and checker inputs are used.
    reference = out / "inputs/reference"
    domains, hierarchy, rule, checker = load_contracts(reference, out / "inputs/rules-v3.1.0.json", out / "provenance/p16_rules.py")
    try:
        result = aggregate(source, domains, hierarchy, rule, checker, expected["rows"], expected["patients"])
    finally:
        after = pin(source)
        source_validation = {"before": before, "after": after, "hash_size_unchanged": before == after,
                             "mtime_unchanged": source.stat().st_mtime_ns == source_stat.st_mtime_ns,
                             "checked_at": now()}
        save(out / "source_verification.json", source_validation)
        require(before == after and source_validation["mtime_unchanged"], "Source changed during read")
    comparisons = compare_reference(result, reference, hierarchy)
    save(out / "validation.json", {"source_validation": result["validation"], "independent_frozen_comparisons": comparisons})
    tables, p16_report = emit(result, out, rule, domains)
    verify_inventory(reference, REFERENCE_SHA)
    save(out / "schema.json", {"g1_columns": AXES + ["count"], "grain": "tumor_row", "code_type": "exact string",
                               "count_type": "positive integer", "sort": "lexicographic code tuples", "through2022": "1995..2022 inclusive",
                               "index": ["1", "2"], "nonindex": ["0"], "unknowns": "preserved; no imputation",
                               "zero_counts": "omitted in count tables; not biological exclusions"})
    (out / "README.md").write_text("""# G1 file-derived candidate v2

Source provenance is STAGE_TEST_ONLY. Authenticity, clinical validity and clinical
representativeness are unverified. These are counts of the authorized file, not
clinical or public population estimates. No identifiers or source rows are exported.

The six unprefixed G1 CSVs retain tumor counts by single-year age, sex, race/origin
and extended ICCC. Grouped-age companions use the exact source encoding: 00 is
under 1, 01 is 1-4, 02 is 5-9, through 19 for 90+; 99 is unknown. Index codes are
1/2, nonindex is 0; through2022 means 1995 through 2022 inclusive. No profile-based
changes are applied. All G1 counts include rows excluded from the p16-only table.

support.json records descriptive domains, marginals, age counts and stratum
cardinalities for every view. No observed zero is a biological exclusion.
The snapshot provides counts and provenance only. Method settings and table
selection belong to generator profiles.

through2022_schema_p16.csv contains only rows passed by the exact archived v3.1
p16 rule/checker. Its schema is yearOfDiagnosis,schemaId,p16Recode,count. Explicit
blank p16 is a category: disable CSV NA inference and read code columns as strings.
The table describes the joint counts of year, schema and p16; it does not
establish relationships to G1, site/histology, or other fields.
p16_report.json and p16_reason_counts.csv retain excluded findings/not_evaluated
counts. p16_schema_year_coverage.csv includes the complete recognized/observed
schema/year grid, including zero-support contexts. No source repair is performed.

validation.json compares sex-summed G1 against the independent frozen reference,
including grouped/index/year/parent rollups. Metadata-unlisted codes remain visible;
the confirmed hierarchy supplies ICCC membership despite known metadata gaps.

Replay from this directory, with a new empty destination and explicit raw path:
python provenance/build_g1_snapshot.py --replay-bundle . --replay-manifest-sha256
<sha256 of manifest.json> --source-csv <authorized raw CSV> --out <new directory>
Join the command onto one line. Replay uses only this archive, Python's standard
library, and the explicit raw source. Historical paths in copied evidence are
provenance, not runtime dependencies. A successful manifest is written last;
partial directories without it are not completed candidates. No shared reference
or generator profile is changed; activation/promotion is separate.
""", encoding="utf-8")
    files = [{"path": str(p.relative_to(out)), **pin(p)} for p in sorted(out.rglob("*")) if p.is_file()]
    manifest = {"snapshot_schema_version": 1, "snapshot_id": "g1-candidate-v2", "kind": "file_derived_joint_counts",
                "status": "candidate_verified_structural_counts", "started_at": started, "completed_at": now(),
                "elapsed_seconds": time.monotonic() - clock, "builder_version": VERSION,
                "runtime": {"python": sys.version, "platform": platform.platform(), "dependencies": "Python standard library only"},
                "command": [sys.executable, *sys.argv], "contains_patient_identifiers": False,
                "source": {"path": str(source), "expected": expected, "before": before, "after": after,
                           "filename_provenance": "STAGE_TEST_ONLY", "authenticity": "unverified", "clinical_validity": "unverified", "clinical_representativeness": "unverified"},
                "pins": {"frozen_reference_manifest": REFERENCE_SHA, "metadata": METADATA_SHA,
                         "rules_v2.3.0": HIERARCHY_SHA, "rules_v3.1.0": P16_RULES_SHA, "p16_checker": P16_CHECKER_SHA},
                "tables": tables, "files": files,
                "replay_of": args.replay_manifest_sha256, "source_validation": result["validation"],
                "p16_through2022": p16_report["through2022"]}
    save(out / "manifest.json", manifest)
    verify_inventory(out, digest(out / "manifest.json"))
    return {"out": str(out), "manifest_sha256": digest(out / "manifest.json"), "totals": {s: tables[s + ".csv"]["count_total"] for s in SCOPES}, "p16": p16_report["through2022"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source-csv", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("reference", "p16-rules", "p16-checker", "evidence-root", "replay-bundle"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--replay-manifest-sha256")
    args = parser.parse_args(argv)
    try:
        result = build(args)
    except (ValueError, OSError, csv.Error, KeyError, StopIteration) as error:
        # Errors deliberately omit source cell values and row content.
        print(json.dumps({"status": "failed", "error_type": type(error).__name__, "message": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps({"status": "passed", **result}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
