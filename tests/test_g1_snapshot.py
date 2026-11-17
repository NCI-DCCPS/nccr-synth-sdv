"""Invented rows only; all temporary writes remain in the authorized candidate dir."""
from collections import Counter
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("g1_builder", ROOT / "experiments/gen-sdv/scripts/build_g1_snapshot.py")
g1 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(g1)
REFERENCE = ROOT / "reference/nccr-derived/2026-11-13-stage-test-v30-iccc-age-race-v1"
P16_CHECKER = ROOT / "reference/nccr-derived/2026-11-13-stage-test-v30-g1-v1/provenance/p16_rules.py"
SCRATCH = ROOT / "experiments/gen-sdv/work/reference-inputs/g1-candidate-v2"


class G1SnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.domains, cls.hierarchy, cls.rule, cls.checker = g1.load_contracts(
            REFERENCE, ROOT / "reference/generation-rules/v3.1.0/rules.json", P16_CHECKER)

    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix="fixture-", dir=SCRATCH)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.rows = [
            self.row("private-a", "1", "00", "1", "1", "001", "1995", "1", "00360", ""),
            self.row("private-a", "2", "40", "1", "1", "060", "2022", "0", "09520", "9"),
            self.row("private-b", "1", "99", "9", "9", "999", "2021", "2", "00360", ""),
            self.row("private-c", "1", "90", "2", "5", "061", "2023", "1", "09210", "1"),
            self.row("private-d", "1", "39", "2", "1", "001", "2021", "1", "09520", ""),
            self.row("private-d", "2", "40", "2", "1", "001", "2022", "0", "00360", "8"),
        ]

    def row(self, patient, tumor, age, sex, race, leaf, year, index, schema, p16):
        return {g1.PID: patient, g1.TUMOR: tumor, g1.AGE: age, g1.GROUP: g1.grouped_age(age),
                g1.SEX: sex, g1.RACE: race, g1.LEAF: leaf, g1.YEAR: year, g1.INDEX: index,
                g1.SCHEMA: schema, g1.P16: p16, **self.hierarchy[leaf]}

    def source(self, rows=None, header=None):
        path = self.root / "source.csv"
        with path.open("w", newline="") as stream:
            writer = csv.writer(stream)
            columns = g1.REQUIRED if header is None else header
            writer.writerow(columns)
            for row in self.rows if rows is None else rows:
                writer.writerow([row.get(k, "") for k in columns])
        return path

    def aggregate(self, rows=None):
        rows = self.rows if rows is None else rows
        return g1.aggregate(self.source(rows), self.domains, self.hierarchy, self.rule, self.checker,
                            len(rows), len({r[g1.PID] for r in rows}))

    def test_scopes_unknowns_and_p16_denominators(self):
        result = self.aggregate()
        self.assertEqual({k: sum(c.values()) for k, c in result["counts"].items()},
                         {"all": 6, "index": 4, "nonindex": 2, "through2022_all": 5,
                          "through2022_index": 3, "through2022_nonindex": 2})
        self.assertEqual(result["p16_counts"], Counter({("1995", "00360", ""): 1,
                         ("2022", "09520", "9"): 1, ("2021", "00360", ""): 1}))
        self.assertEqual(result["validation"]["metadata_unlisted_codes"]["sex"], {"9": 1})
        self.assertEqual(result["validation"]["metadata_unlisted_codes"][g1.LEAF], {"060": 1})
        self.assertEqual(result["p16_check"]["finding_rows"], 1)
        self.assertEqual(result["p16_check"]["not_evaluated_rows"], 1)
        self.assertIn(("99", "9", "9", "999"), result["counts"]["all"])

    def test_grouped_age_boundaries_exact_encoding(self):
        for age, group in [("00", "00"), ("01", "01"), ("04", "01"), ("05", "02"),
                           ("39", "08"), ("40", "09"), ("89", "18"), ("90", "19"), ("99", "99")]:
            self.assertEqual(g1.grouped_age(age), group)
        for bad in ("0", "090", "91", " 01", "０１", "", 1, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                g1.grouped_age(bad)

    def test_cutoff_exact_ascii_years(self):
        self.assertTrue(g1.through2022("1995"))
        self.assertTrue(g1.through2022("2022"))
        for bad in ("1994", "2023", "２０２２", "2022 ", "2022.0", "", None):
            self.assertFalse(g1.through2022(bad))

    def test_errors_do_not_print_identifiers_or_rows(self):
        cases = [(0, g1.PID, "", "Missing patient"), (1, g1.TUMOR, "1", "Duplicate patient"),
                 (1, g1.SEX, "2", "sex/race inconsistency"), (1, g1.RACE, "5", "sex/race inconsistency"),
                 (1, g1.INDEX, "1", "Multiple index"), (0, g1.INDEX, "0", "without exactly one"),
                 (0, g1.INDEX, "9", "Invalid index"), (0, g1.GROUP, "01", "Grouped-age"),
                 (0, g1.AGE, "0", "age encoding"), (0, g1.LEAF, "1", "Unmapped ICCC"),
                 (0, "icccMajor", "02", "hierarchy mismatch")]
        for i, col, value, message in cases:
            rows = [r.copy() for r in self.rows]
            rows[i][col] = value
            with self.subTest(col=col, value=value):
                with self.assertRaisesRegex(ValueError, message) as raised:
                    self.aggregate(rows)
                self.assertNotIn("private-", str(raised.exception))

    def test_bad_headers_width_and_totals(self):
        for header in [g1.REQUIRED + [g1.PID], [k for k in g1.REQUIRED if k != g1.P16]]:
            with self.assertRaises(ValueError):
                g1.aggregate(self.source(header=header), self.domains, self.hierarchy, self.rule, self.checker, 6, 4)
        source = self.source()
        with source.open("a") as stream:
            stream.write("truncated,row\n")
        with self.assertRaisesRegex(ValueError, "row width"):
            g1.aggregate(source, self.domains, self.hierarchy, self.rule, self.checker, 6, 4)
        for rows, patients in [(7, 4), (6, 5)]:
            with self.assertRaisesRegex(ValueError, "total mismatch"):
                g1.aggregate(self.source(), self.domains, self.hierarchy, self.rule, self.checker, rows, patients)

    def test_p16_checker_v31_exact_categories_and_reasons(self):
        cases = [("2021", "09520", "", "findings", "p16_blank_when_applicable"),
                 ("2022", "09520", "9", "passed", "p16_applicable_result"),
                 ("2022", "00360", "", "passed", "p16_not_applicable_blank"),
                 ("2022", "00360", "8", "not_evaluated", "p16_collection_requirement_unbound"),
                 ("2021", "12345", "", "not_evaluated", "p16_unknown_schema"),
                 ("2022", "99999", "", "passed", "p16_not_applicable_blank"),
                 ("2022", "09210", "", "findings", "p16_inconsistent_schema_year"),
                 ("2022", "00360", "Blank", "findings", "p16_invalid_value"),
                 ("9999", "00360", "", "not_evaluated", "p16_missing_or_unsupported_year")]
        for year, schema, value, status, reason in cases:
            with self.subTest(year=year, schema=schema, value=value):
                self.assertEqual(self.checker(self.rule).add({g1.YEAR: year, g1.SCHEMA: schema, g1.P16: value}), (status, reason))

    def test_independent_reference_rollups_and_tamper(self):
        result = self.aggregate()
        analysis = self.root / "reference/analysis"
        analysis.mkdir(parents=True)
        # Independent fixture recount in reference column order from invented rows.
        views = [("iccc_extended_age_race_counts.csv", g1.AGE, g1.LEAF, lambda r: True),
                 ("iccc_extended_age_group_race_counts.csv", g1.GROUP, g1.LEAF, lambda r: True),
                 ("index_iccc_extended_age_group_race_counts.csv", g1.GROUP, g1.LEAF, lambda r: r[g1.INDEX] in ["1", "2"]),
                 ("through_2022_iccc_extended_age_group_race_counts.csv", g1.GROUP, g1.LEAF, lambda r: r[g1.YEAR] != "2023"),
                 ("iccc_recode_age_group_race_counts.csv", g1.GROUP, "iccc2017Recode", lambda r: True),
                 ("iccc_major_age_group_race_counts.csv", g1.GROUP, "icccMajor", lambda r: True)]
        for name, age, leaf, select in views:
            counts = Counter((r[age], r[g1.RACE], r[leaf]) for r in self.rows if select(r))
            g1.write_counts(analysis / name, ["age_code", "race_code", "iccc_code"], counts)
        columns = [g1.GROUP, g1.RACE, g1.LEAF, g1.YEAR, g1.INDEX]
        g1.write_counts(analysis / "iccc_extended_age_group_race_year_index_counts.csv", columns,
                        Counter(tuple(r[k] for k in columns) for r in self.rows))
        self.assertEqual(len(g1.compare_reference(result, analysis.parent, self.hierarchy)), 12)
        result["counts"]["all"][("00", "1", "1", "001")] += 1
        with self.assertRaisesRegex(ValueError, "Frozen reference rollup mismatch"):
            g1.compare_reference(result, analysis.parent, self.hierarchy)

    def test_outputs_support_blank_and_identifier_free(self):
        tables, report = g1.emit(self.aggregate(), self.root, self.rule, self.domains)
        self.assertEqual(tables["through2022_schema_p16.csv"]["count_total"], 3)
        self.assertEqual(report["passed_table_p16_counts"], {"": 2, "9": 1})
        support = g1.load(self.root / "support.json")
        self.assertEqual(set(support), {"domains", "observed_marginals", "scopes", "missing_cells"})
        self.assertEqual(support["scopes"]["through2022_all"]["age_40_plus_rows"], 2)
        self.assertEqual(support["scopes"]["through2022_index"]["age_40_plus_rows"], 0)
        # Do not scan source.csv: it is an invented fixture, never a bundle artifact.
        for path in self.root.iterdir():
            if path.name != "source.csv":
                self.assertNotIn("private-", path.read_text())
        with (self.root / "p16_schema_year_coverage.csv").open() as stream:
            coverage = list(csv.DictReader(stream))
        self.assertEqual(sum(int(r["source_rows"]) for r in coverage), 6)
        self.assertEqual(sum(int(r["retained_through2022_rows"]) for r in coverage), 3)
        self.assertTrue(any(r["source_rows"] == "0" for r in coverage))

    def test_counts_errors_and_inventory_tamper(self):
        path = self.root / "counts.csv"
        for text in ["code,count\n001,0\n", "code,count\n001,1.0\n", "code,count\n001,1\n001,2\n", "code,count\n002,1\n001,1\n"]:
            path.write_text(text)
            with self.assertRaises(ValueError): g1.read_counts(path, ["code"], True)
        path.write_text("code,count\n001,1\n")
        g1.save(self.root / "manifest.json", {"files": [{"path": "counts.csv", **g1.pin(path)}]})
        manifest_pin = g1.digest(self.root / "manifest.json")
        g1.verify_inventory(self.root, manifest_pin)
        path.write_text("code,count\n001,2\n")
        with self.assertRaisesRegex(ValueError, "inventory mismatch"):
            g1.verify_inventory(self.root, manifest_pin)
        with self.assertRaises(ValueError): g1.safe_member(self.root, "../outside")

    def test_metadata_pin_and_no_overwrite(self):
        metadata = g1.load(REFERENCE / "inputs/ctc-normalized.json")
        metadata["fields"][g1.AGE]["permissible_values"].pop()
        with self.assertRaisesRegex(ValueError, "age metadata"):
            g1.metadata_contract(metadata)
        path = self.source()
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            g1.checked(path, "0" * 64)
        with self.assertRaisesRegex(ValueError, "overwrite"):
            g1.copy_exact(path, path)
        args = g1.argparse.Namespace(out=self.root)
        with self.assertRaisesRegex(ValueError, "nonempty"):
            g1.build(args)


if __name__ == "__main__":
    unittest.main()
