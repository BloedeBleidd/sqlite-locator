import contextlib
import hashlib
import importlib.util
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "detective", ROOT / "src" / "sqlite_locator.py"
)
d = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = d
spec.loader.exec_module(d)


class Integration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sqlite_detective_")
        self.base = Path(self.tmp.name)
        self.front = self.base / "front ?#.sqlite"
        self.source = self.base / "source ?#.sqlite"
        self.report = self.base / "report.html"

    def tearDown(self):
        self.tmp.cleanup()

    def prepare(self, rows, script, params=None, labels=None):
        labels = labels or [f"GUI {i}" for i in range(len(rows[0]))]
        with contextlib.closing(sqlite3.connect(self.front)) as con, con:
            con.execute("CREATE TABLE gui(" + ",".join(d.qi(x) for x in labels) + ")")
            con.executemany(
                "INSERT INTO gui VALUES(" + ",".join("?" for x in labels) + ")", rows
            )
        with contextlib.closing(sqlite3.connect(self.source)) as con, con:
            con.executescript(script)
            for sql, values in params or []:
                con.executemany(sql, values)

    def run_finder(self, *extra):
        before = {
            p: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [self.front, self.source]
        }
        cmd = [
            "--quiet",
            "--frontend",
            str(self.front),
            "--source",
            str(self.source),
            "--report",
            str(self.report),
            *extra,
        ]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            code = d.main(cmd)
        self.assertEqual(code, 0)
        for p, h in before.items():
            self.assertEqual(h, hashlib.sha256(p.read_bytes()).hexdigest())
        return json.loads(self.report.with_suffix(".json").read_text())

    def assert_replays(self, p):
        codec = d.ValueCodec(p["metadata"]["config"]["comparison"])
        with contextlib.closing(sqlite3.connect(self.source)) as con, con:
            for finding in p["findings"]:
                if not finding["replay_sql"].startswith("SELECT"):
                    continue
                front = next(
                    f for f in p["frontends"] if f["name"] == finding["frontend"]
                )
                rows = con.execute(finding["replay_sql"]).fetchall()
                if finding["full"]:
                    self.assertEqual(len(rows), len(front["rows"]))
                for row in rows:
                    expected = front["rows"][row[0] - 1]
                    verified = {
                        proof["column"]
                        for proof in finding["proofs"]
                        if proof["row"] == row[0] - 1
                    }
                    for j, (a, b) in enumerate(zip(row[1:], expected)):
                        if j not in verified:
                            self.assertIsNone(a)
                        elif not isinstance(b, dict):
                            self.assertEqual(codec.key(a), codec.key(b))

    def test_demo_random_seeds(self):
        for seed in [1, 7, 99]:
            with self.subTest(seed=seed):
                folder = self.base / str(seed)
                subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts" / "generate_demo.py"),
                        "--output-dir",
                        str(folder),
                        "--seed",
                        str(seed),
                        "--noise-tables",
                        "6",
                        "--min-rows",
                        "80",
                        "--max-rows",
                        "160",
                    ],
                    capture_output=True,
                    check=True,
                )
                self.front, self.source = (
                    folder / "frontend.sqlite",
                    folder / "source.sqlite",
                )
                p = self.run_finder()
                truth = json.loads((folder / "expected.json").read_text())
                for s in truth["scenarios"]:
                    if s["kind"] in ["fragments", "decoy"]:
                        continue
                    self.assertTrue(
                        any(
                            f["kind"] == s["kind"]
                            and f["frontend"] == s["frontend"]
                            and set(f["tables"]) == set(s["tables"])
                            and f["full"]
                            for f in p["findings"]
                        ),
                        s,
                    )
                decoy = next(
                    s["tables"][0] for s in truth["scenarios"] if s["kind"] == "decoy"
                )
                self.assertFalse(
                    any(decoy in f["tables"] and f["full"] for f in p["findings"])
                )
                self.assert_replays(p)

    def test_no_indexes_exhaustive(self):
        folder = self.base / "noindex"
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "generate_demo.py"),
                "--output-dir",
                str(folder),
                "--no-target-indexes",
                "--noise-tables",
                "3",
                "--min-rows",
                "1500",
                "--max-rows",
                "2500",
            ],
            capture_output=True,
            check=True,
        )
        self.front, self.source = folder / "frontend.sqlite", folder / "source.sqlite"
        p = self.run_finder(
            "--mode",
            "exhaustive",
            "--query-seconds",
            "3",
            "--query-vm-steps",
            "10000000",
        )
        self.assertEqual(sum(f["full"] for f in p["findings"]), 6)
        self.assertTrue(all(x["full_scan_finished"] for x in p["coverage"]))
        self.assert_replays(p)

    def test_duplicates_require_separate_source_rows(self):
        self.prepare(
            [("x", 11), ("x", 11), ("y", 22)],
            'CREATE TABLE t(a,b); INSERT INTO t VALUES("x",11),("y",22);',
        )
        p = self.run_finder()
        self.assertFalse(any(f["full"] for f in p["findings"] if f["kind"] == "rows"))
        with contextlib.closing(sqlite3.connect(self.source)) as con, con:
            con.execute("INSERT INTO t VALUES(?,?)", ("x", 11))
        p = self.run_finder()
        self.assertTrue(any(f["full"] for f in p["findings"] if f["kind"] == "rows"))
        self.assert_replays(p)

    def test_dynamic_types_quotes_blob_and_html(self):
        rows = [
            ("Unicode Ω </script><script>NO()</script>", b"\x00\x01"),
            ("O'Brian", 2**63 - 1),
        ]
        self.prepare(
            rows,
            'CREATE TABLE "weird""table"("co""l" INTEGER, "select" BLOB);',
            [('INSERT INTO "weird""table" VALUES(?,?)', rows)],
            labels=["description <script>", 'number "'],
        )
        p = self.run_finder()
        self.assertTrue(any(f["full"] for f in p["findings"]))
        text = self.report.read_text()
        self.assertNotIn("<script>NO()", text)
        self.assertIn("&lt;script&gt;NO()", text)
        self.assert_replays(p)

    def test_without_rowid_composite(self):
        rows = [("rare-a", 29), ("rare-b", 31)]
        self.prepare(
            rows,
            "CREATE TABLE t(k1 TEXT,k2 INTEGER,x,y,PRIMARY KEY(k1,k2)) WITHOUT ROWID;",
            [
                (
                    "INSERT INTO t VALUES(?,?,?,?)",
                    [("A", 1, *rows[0]), ("B", 2, *rows[1])],
                )
            ],
        )
        p = self.run_finder()
        self.assertTrue(any(f["full"] for f in p["findings"]))
        for f in p["findings"]:
            for proof in f["proofs"]:
                self.assertEqual(set(proof["locator"]), {"k1", "k2"})
        self.assert_replays(p)

    def test_composite_fk_two_hops(self):
        rows = [("Rare-X", 375), ("Rare-Y", 487), ("Rare-Z", 695)]
        self.prepare(
            rows,
            """CREATE TABLE a(k1 TEXT,k2 INTEGER,x,PRIMARY KEY(k1,k2)) WITHOUT ROWID;
        CREATE TABLE b(id INTEGER PRIMARY KEY, a1 TEXT,a2 INTEGER,FOREIGN KEY(a1,a2) REFERENCES a(k1,k2));
        CREATE TABLE c(z INTEGER PRIMARY KEY,bid INTEGER,y,FOREIGN KEY(bid) REFERENCES b(id));
        CREATE INDEX bx ON b(a1,a2); CREATE INDEX cx ON c(bid);""",
            [
                (
                    "INSERT INTO a VALUES(?,?,?)",
                    [("K", i + 1, r[0]) for i, r in enumerate(rows)],
                ),
                (
                    "INSERT INTO b VALUES(?,?,?)",
                    [(100 + i, "K", i + 1) for i in range(3)],
                ),
                (
                    "INSERT INTO c VALUES(?,?,?)",
                    [(1000 + i, 100 + i, r[1]) for i, r in enumerate(rows)],
                ),
            ],
        )
        p = self.run_finder()
        self.assertTrue(
            any(
                f["full"]
                and f["kind"] == "JOIN"
                and set(f["tables"]) == {"a", "b", "c"}
                for f in p["findings"]
            )
        )
        self.assert_replays(p)

    def test_unknown_is_not_absence(self):
        self.prepare(
            [("needle-x", 17), ("needle-y", 29)],
            "CREATE TABLE t(x,y);",
            [
                (
                    "INSERT INTO t VALUES(?,?)",
                    [("noise-" + str(i), i + 1000) for i in range(1000)],
                ),
                ("INSERT INTO t VALUES(?,?)", [("needle-x", 17), ("needle-y", 29)]),
            ],
        )
        p = self.run_finder("--sample-rows", "0", "--random-seeks", "0")
        self.assertEqual(p["findings"], [])
        self.assertTrue(
            all(
                c["status"] == "not found within budget"
                for c in p["frontends"][0]["cells"]
            )
        )
        p = self.run_finder("--mode", "exhaustive")
        self.assertTrue(any(f["full"] for f in p["findings"]))

    def test_numeric_strict_and_leading_zero(self):
        self.prepare(
            [("A", 12), ("B", 23)],
            'CREATE TABLE t(x,y); INSERT INTO t VALUES("A","12.00"),("B","23.000");',
        )
        p = self.run_finder()
        self.assertTrue(any(f["full"] for f in p["findings"]))
        p = self.run_finder("--comparison", "strict")
        self.assertFalse(any(f["full"] for f in p["findings"]))
        with contextlib.closing(sqlite3.connect(self.source)) as con, con:
            con.execute('UPDATE t SET y="012" WHERE x="A"')
        p = self.run_finder()
        self.assertFalse(any(f["full"] for f in p["findings"]))

    def test_null_omitted_and_infinity_serializable(self):
        self.prepare(
            [("A", None, float("inf")), ("B", 11, 9.5)],
            "CREATE TABLE t(a,b,c);",
            [
                (
                    "INSERT INTO t VALUES(?,?,?)",
                    [("A", "not null", float("inf")), ("B", 11, 9.5)],
                )
            ],
        )
        p = self.run_finder()
        self.assertTrue(any(f["full"] and f["total_cells"] == 5 for f in p["findings"]))
        self.assert_replays(p)

    def test_missing_value_does_not_make_full_match(self):
        self.prepare(
            [("unique-a", 111), ("unique-b", 222)],
            'CREATE TABLE t(a,b);INSERT INTO t VALUES("unique-a",111),("unique-b",333);',
        )
        p = self.run_finder()
        self.assertFalse(any(f["full"] for f in p["findings"]))

    def test_common_value_does_not_hide_retained_rows(self):
        rows = [
            ("Gala GA-418", "red", 6.75),
            ("Idared ID-762", "red", 5.89),
            ("Granny Smith", "green", 7.25),
        ]
        self.prepare(
            rows,
            "CREATE TABLE t(v, c, p); CREATE INDEX by_name ON t(v); "
            "CREATE INDEX by_color ON t(c);",
            [
                (
                    "INSERT INTO t VALUES(?,?,?)",
                    [(f"Other-{i}", "red", i + 100) for i in range(100)] + rows,
                )
            ],
        )
        for mode in ("bounded", "exhaustive"):
            with self.subTest(mode=mode):
                p = self.run_finder("--mode", mode, "--witnesses", "2")
                finding = next(
                    f for f in p["findings"] if f["kind"] == "rows" and f["full"]
                )
                self.assertEqual(
                    {proof["locator"]["_rowid_"] for proof in finding["proofs"]},
                    {101, 102, 103},
                )
                self.assert_replays(p)

    def test_eav_recovers_common_values_via_indexed_entities(self):
        rows = [
            ("Gala GA-418", "red"),
            ("Idared ID-762", "red"),
            ("Granny Smith", "green"),
        ]
        data = [(f"other-{i}", "color-code", "red") for i in range(55)]
        for i, (name, color) in enumerate(rows):
            data.extend(
                [
                    (f"entity-{i}", "name-code", name),
                    (f"entity-{i}", "color-code", color),
                ]
            )
        self.prepare(
            rows,
            "CREATE TABLE t(e, a, v); CREATE INDEX by_value ON t(v); "
            "CREATE INDEX by_entity ON t(e, a);",
            [("INSERT INTO t VALUES(?,?,?)", data)],
        )
        p = self.run_finder("--sample-rows", "0", "--witnesses", "2")
        f = next(f for f in p["findings"] if f["kind"] == "EAV" and f["full"])
        self.assertEqual(
            {proof["locator"]["_rowid_"] for proof in f["proofs"]}, set(range(56, 62))
        )
        self.assert_replays(p)

        with contextlib.closing(sqlite3.connect(self.source)) as con, con:
            con.executescript("DROP INDEX by_value; DROP INDEX by_entity;")
        for mode in ("bounded", "exhaustive"):
            with self.subTest(unindexed_mode=mode):
                p = self.run_finder("--mode", mode, "--witnesses", "2")
                self.assertTrue(
                    any(f["kind"] == "EAV" and f["full"] for f in p["findings"])
                )
                self.assert_replays(p)

    def test_eav_partial_attribute_coverage(self):
        self.prepare(
            [
                ("Gala GA-418", "red"),
                ("Granny Smith", "green"),
                ("Missing apple", "red"),
            ],
            "CREATE TABLE t(e,a,v); INSERT INTO t VALUES "
            "('one','n','Gala GA-418'),('one','c','red'),"
            "('two','n','Granny Smith'),('two','c','green');",
        )
        p = self.run_finder()
        self.assertTrue(
            any(
                f["kind"] == "EAV" and f["matched_cells"] == 4 and not f["full"]
                for f in p["findings"]
            )
        )
        self.assert_replays(p)

    def test_partial_column_mapping_replays_verified_cells(self):
        self.prepare(
            [("Gala GA-418", 2, 149), ("Granny Smith", 3, 398)],
            "CREATE TABLE t(x,y); INSERT INTO t VALUES ('Gala GA-418',2),('Granny Smith',3);",
        )
        p = self.run_finder()
        f = next(f for f in p["findings"] if f["kind"] == "rows")
        self.assertEqual((f["matched_cells"], f["total_cells"]), (4, 6))
        self.assert_replays(p)

    def test_join_candidates_survive_many_earlier_category_tables(self):
        self.prepare(
            [("Gala GA-418", "red"), ("Granny Smith", "green")],
            "".join(
                f"CREATE TABLE a_noise_{i:02}(v); "
                f"INSERT INTO a_noise_{i:02} VALUES ('red'),('green'); "
                for i in range(30)
            )
            + "CREATE TABLE z_colors(k TEXT PRIMARY KEY, v); "
            "INSERT INTO z_colors VALUES ('color-0001','red'),('color-0002','green'); "
            "CREATE TABLE z_apples(x, k REFERENCES z_colors(k)); "
            "INSERT INTO z_apples VALUES ('Gala GA-418','color-0001'),('Granny Smith','color-0002'); "
            "CREATE INDEX apple_name ON z_apples(x); CREATE INDEX color_value ON z_colors(v);",
        )
        for declared in (True, False):
            if not declared:
                with contextlib.closing(sqlite3.connect(self.source)) as con, con:
                    con.executescript(
                        "ALTER TABLE z_apples RENAME TO old; "
                        "CREATE TABLE z_apples AS SELECT * FROM old; DROP TABLE old;"
                    )
            with self.subTest(declared=declared):
                p = self.run_finder("--candidates-per-column", "1")
                f = next(
                    f
                    for f in p["findings"]
                    if f["kind"] == "JOIN"
                    and f["full"]
                    and set(f["tables"]) == {"z_apples", "z_colors"}
                )
                self.assertEqual("Join inferred" in f["note"], not declared)
                self.assert_replays(p)

    def test_binary_index_preferred_to_nocase_on_same_column(self):
        self.prepare(
            [("Gala GA-418", "red"), ("Granny Smith", "green")],
            "CREATE TABLE t(name TEXT COLLATE NOCASE, color);"
            "CREATE INDEX binary_name ON t(name COLLATE BINARY);"
            "CREATE INDEX nocase_name ON t(name);",
            [
                (
                    "INSERT INTO t VALUES(?,?)",
                    [
                        ("GALA GA-418", "red"),
                        ("GRANNY SMITH", "green"),
                        ("Gala GA-418", "red"),
                        ("Granny Smith", "green"),
                    ],
                )
            ],
        )
        p = self.run_finder("--sample-rows", "0", "--witnesses", "1")
        self.assertTrue(any(f["full"] for f in p["findings"]))
        self.assert_replays(p)

    def test_empty_tables_skip_samples_and_index_probes(self):
        self.prepare(
            [("A", 12), ("B", 23)],
            "".join(
                f"CREATE TABLE t{i}(x); CREATE INDEX ix{i} ON t{i}(x); "
                for i in range(128)
            ),
        )
        p = self.run_finder()
        self.assertEqual(len(p["coverage"]), 128)
        self.assertTrue(
            all(c["empty"] and c["index_probes"] == 0 for c in p["coverage"])
        )
        self.assertLess(p["metadata"]["source_queries"], 150)

    def test_table_limit_applies_after_source_filter_and_is_reported(self):
        self.prepare(
            [("Gala GA-418", "red"), ("Granny Smith", "green")],
            "CREATE TABLE ignored(x); CREATE TABLE kept_a(a,b); CREATE TABLE kept_b(x); "
            "INSERT INTO kept_a VALUES ('Gala GA-418','red'),('Granny Smith','green');",
        )
        p = self.run_finder("--max-tables", "1", "--table-pattern", "kept_*")
        self.assertEqual([c["table"] for c in p["coverage"]], ["kept_a"])
        self.assertTrue(any(f["full"] for f in p["findings"]))
        self.assertTrue(
            any("--max-tables" in n for n in p["metadata"]["operational_notes"])
        )

    def test_evidence_capacity_stops_later_discovery_queries(self):
        self.prepare(
            [("Gala GA-418", "red"), ("Granny Smith", "green")],
            "CREATE TABLE first(a,b); CREATE INDEX ix ON first(a); "
            "INSERT INTO first VALUES ('Gala GA-418','red'),('Granny Smith','green');"
            "CREATE TABLE later AS SELECT * FROM first; CREATE INDEX ix2 ON later(a);",
        )
        p = self.run_finder("--max-evidence-rows", "1")
        coverage = {c["table"]: c for c in p["coverage"]}
        self.assertTrue(coverage["first"]["started"])
        self.assertFalse(coverage["later"]["started"])
        self.assertEqual(coverage["first"]["retained_rows"], 1)
        self.assertTrue(
            any("--max-evidence-rows" in n for n in p["metadata"]["operational_notes"])
        )

    def test_alternative_transpose_mapping(self):
        # Lexically first full column-set assignment is wrong; the second is coherent.
        self.prepare(
            [("a", 11), ("b", 22)],
            "CREATE TABLE t(wrong1,wrong2,right1,right2);"
            'INSERT INTO t VALUES("a",22,"a","b"),(11,"b",11,22);',
        )
        p = self.run_finder()
        self.assertTrue(
            any(f["full"] and f["kind"] == "transpose" for f in p["findings"])
        )
        self.assert_replays(p)

    def test_query_interrupt_retains_partial_and_marks_unknown(self):
        self.prepare(
            [("absent", 999999999)],
            "CREATE TABLE t(x);",
            [("INSERT INTO t VALUES(?)", [(str(i),) for i in range(30000)])],
        )
        p = self.run_finder("--mode", "exhaustive", "--query-vm-steps", "3000")
        self.assertTrue(
            any(not x["full_scan_finished"] and x["interrupted"] for x in p["coverage"])
        )
        self.assertGreater(p["metadata"]["interrupted_queries"], 0)

    def test_findings_cap_keeps_valid_proofs_and_reports_limit(self):
        self.prepare(
            [("u1", 111), ("u2", 222)],
            'CREATE TABLE t1(a,b); INSERT INTO t1 VALUES("u1",111),("u2",222);'
            "CREATE TABLE t2 AS SELECT * FROM t1; CREATE TABLE t3 AS SELECT * FROM t1;",
        )
        p = self.run_finder("--max-findings", "1")
        self.assertEqual(len(p["findings"]), 1)
        self.assertTrue(p["findings"][0]["full"])
        self.assertTrue(
            any("max-findings" in n for n in p["metadata"]["operational_notes"])
        )
        self.assert_replays(p)

    def test_evidence_byte_cap_does_not_claim_absence(self):
        self.prepare(
            [("u1", 111), ("u2", 222)],
            'CREATE TABLE t(a,b); INSERT INTO t VALUES("u1",111),("u2",222);',
        )
        p = self.run_finder("--max-evidence-bytes", "1")
        self.assertEqual(p["findings"], [])
        self.assertTrue(
            all(
                c["status"] == "not found within budget"
                for c in p["frontends"][0]["cells"]
            )
        )
        self.assertTrue(
            any("max-evidence-bytes" in n for n in p["metadata"]["operational_notes"])
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
