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
                    for a, b in zip(row[1:], expected):
                        if b is not None and not isinstance(b, dict):
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
