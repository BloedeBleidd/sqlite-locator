#!/usr/bin/env python3
"""Generate SQLite fixtures for value-based discovery validation. No dependencies.

    python scripts/generate_demo.py --profile large --output-dir data/large --seed 42
    python scripts/generate_demo.py --profile small --output-dir data/demo --seed 42

Create the frontend first, then generate a randomized source database with
controlled examples at randomized locations. expected.json is independent
ground truth for users and tests; sqlite_locator.py never reads it.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import random
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass
class Sample:
    name: str
    columns: list[str]
    rows: list[tuple[Any, ...]]


@dataclass
class NoiseTable:
    name: str
    columns: list[str]
    rows: int


class DemoBuilder:
    """Separate data generation, SQLite persistence, and validation scenarios."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.rng = random.Random(args.seed)
        self.scenarios: list[dict] = []
        self.table_count = 0
        self.row_count = 0
        self.indexed: set[tuple[str, str]] = set()
        self.started = time.monotonic()
        self.samples = [
            Sample(
                "Apples",
                ["Variety", "Color", "Quantity", "Price per kg"],
                [
                    ("Golden Russet GR-927", "yellow-green", 137, 8.49),
                    ("Gala GA-418", "red", 263, 6.75),
                    ("Brown Russet BR-583", "russet", 419, 9.35),
                    ("Idared ID-762", "red", 571, 5.89),
                    ("Granny Smith GS-346", "green", 683, 7.25),
                ],
            ),
            Sample(
                "Sensors",
                ["Device", "Reading", "Unit"],
                [
                    ("SENSOR-X7/LAB", 12.75, "°C"),
                    ("SENSOR-B4/LAB", "operational", "status"),
                    ("SENSOR-K9/LAB", 87, "%"),
                    ("SENSOR-M2/LAB", "no signal", "status"),
                ],
            ),
        ]

    def name(self, prefix: str) -> str:
        return prefix + "_" + f"{self.rng.getrandbits(48):012x}"

    def value(self, kind: str = "") -> Any:
        if not kind:
            kind = self.rng.choice(["TEXT", "INTEGER", "REAL", "BLOB"])
        if kind == "TEXT":
            return (
                self.rng.choice(["noise-", "archive-", "record-"])
                + f"{self.rng.getrandbits(48):012x}"
            )
        if kind == "INTEGER":
            return self.rng.randint(10000, 99999999)
        if kind == "REAL":
            return round(self.rng.uniform(10000, 90000), 4)
        return self.rng.randbytes(6)

    def create_frontend(self, path: Path) -> None:
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            for sample in self.samples:
                # Unspecified affinity preserves heterogeneous values in the Reading column.
                db.execute(
                    "CREATE TABLE "
                    + qi(sample.name)
                    + "("
                    + ",".join(map(qi, sample.columns))
                    + ")"
                )
                sql = (
                    "INSERT INTO "
                    + qi(sample.name)
                    + " VALUES("
                    + ",".join("?" for _ in sample.columns)
                    + ")"
                )
                db.executemany(sql, sample.rows)

    def new_table(
        self,
        db: sqlite3.Connection,
        *,
        width: int | None = None,
        rows: int | None = None,
        typed: bool = False,
    ) -> NoiseTable:
        width = width or self.rng.randint(self.args.min_columns, self.args.max_columns)
        rows = rows or self.rng.randint(self.args.min_rows, self.args.max_rows)
        name = self.name("t")
        columns = [self.name("c") for _ in range(width)]
        kinds = [
            self.rng.choice(["TEXT", "INTEGER", "REAL", "BLOB"]) if typed else ""
            for _ in columns
        ]
        db.execute(
            "CREATE TABLE "
            + qi(name)
            + "("
            + ",".join(qi(c) + " " + k for c, k in zip(columns, kinds))
            + ")"
        )
        sql = (
            "INSERT INTO "
            + qi(name)
            + " VALUES("
            + ",".join("?" for _ in columns)
            + ")"
        )
        if typed:
            # Generate batches inside SQLite to avoid per-cell Python/SQLite calls.
            # Affine permutations of a 31-bit range produce reproducible test noise.
            expressions = []
            for kind in kinds:
                multiplier = self.rng.randrange(1000001, 100000000, 2)
                offset = self.rng.randrange(2**31)
                number = f"((n*{multiplier}+{offset}) & 2147483647)"
                if kind == "TEXT":
                    expression = f"'noise-' || printf('%08x', {number})"
                elif kind == "INTEGER":
                    expression = f"10000+({number}%90000000)"
                elif kind == "REAL":
                    expression = f"10000.0+({number}%800000000)/10000.0"
                else:
                    expression = f"CAST(printf('%08x', {number}) AS BLOB)"
                expressions.append(expression)
            db.execute(
                "INSERT INTO "
                + qi(name)
                + " SELECT "
                + ",".join(expressions)
                + " FROM temp._demo_sequence WHERE n<=?",
                [rows],
            )
        else:
            batch = []
            for _ in range(rows):
                batch.append(tuple(self.value(k) for k in kinds))
                if len(batch) == 1000:
                    db.executemany(sql, batch)
                    batch.clear()
            if batch:
                db.executemany(sql, batch)
        self.table_count += 1
        self.row_count += rows
        return NoiseTable(name, columns, rows)

    def index(
        self,
        db: sqlite3.Connection,
        table: NoiseTable,
        column: str,
        *,
        target: bool = True,
    ) -> None:
        if (table.name, column) in self.indexed or (
            target and self.args.no_target_indexes
        ):
            return
        db.execute(
            "CREATE INDEX "
            + qi(self.name("i"))
            + " ON "
            + qi(table.name)
            + "("
            + qi(column)
            + ")"
        )
        self.indexed.add((table.name, column))

    def plant(
        self, db: sqlite3.Connection, table: NoiseTable, updates: list[dict[str, Any]]
    ) -> list[int]:
        """Use random locations without assuming neighboring or ordered rows."""
        locations = self.rng.sample(range(1, table.rows + 1), len(updates))
        for rowid, values in zip(locations, updates):
            db.execute(
                "UPDATE "
                + qi(table.name)
                + " SET "
                + ",".join(qi(c) + "=?" for c in values)
                + " WHERE rowid=?",
                [*values.values(), rowid],
            )
        return locations

    def regular(
        self, db: sqlite3.Connection, sample: Sample, *, numeric_text: bool = False
    ) -> None:
        table = self.new_table(db, width=len(sample.columns) + self.rng.randint(2, 6))
        columns = self.rng.sample(table.columns, len(sample.columns))
        updates = []
        for row in sample.rows:
            updates.append(
                {
                    c: str(v) if numeric_text and isinstance(v, (int, float)) else v
                    for c, v in zip(columns, row)
                }
            )
        rowids = self.plant(db, table, updates)
        for c in columns:
            self.index(db, table, c)
        self.scenarios.append(
            {
                "kind": "rows",
                "frontend": sample.name,
                "tables": [table.name],
                "columns_in_gui_order": columns,
                "rowids_in_gui_order": rowids,
                "description": (
                    "Numbers stored as text."
                    if numeric_text
                    else "Matching records with opaque, reordered columns."
                ),
            }
        )

    def transpose(self, db: sqlite3.Connection, sample: Sample) -> None:
        table = self.new_table(db, width=len(sample.rows) + self.rng.randint(2, 5))
        columns = self.rng.sample(table.columns, len(sample.rows))
        updates = [dict(zip(columns, row)) for row in zip(*sample.rows)]
        rowids = self.plant(db, table, updates)
        for c in columns:
            self.index(db, table, c)
        self.scenarios.append(
            {
                "kind": "transpose",
                "frontend": sample.name,
                "tables": [table.name],
                "columns_for_gui_rows": columns,
                "rowids_for_gui_columns": rowids,
                "description": "Transposed GUI: frontend columns become source rows.",
            }
        )

    def eav(self, db: sqlite3.Connection, sample: Sample) -> None:
        table = self.new_table(
            db,
            width=self.rng.randint(5, 9),
            rows=max(self.args.min_rows, len(sample.rows) * len(sample.columns) * 3),
        )
        entity, attribute, value = self.rng.sample(table.columns, 3)
        attributes = [self.name("a") for _ in sample.columns]
        entities = self.rng.sample(range(700000000, 800000000), len(sample.rows))
        updates = [
            {entity: entities[r], attribute: attributes[c], value: v}
            for r, row in enumerate(sample.rows)
            for c, v in enumerate(row)
        ]
        rowids = self.plant(db, table, updates)
        self.index(db, table, value)
        self.index(db, table, entity)
        self.scenarios.append(
            {
                "kind": "EAV",
                "frontend": sample.name,
                "tables": [table.name],
                "entity_column": entity,
                "attribute_column": attribute,
                "value_column": value,
                "attribute_values_in_gui_order": attributes,
                "entity_values_in_gui_order": entities,
                "rowids_in_cell_order": rowids,
                "description": "EAV with opaque attribute identifiers.",
            }
        )

    def join(self, db: sqlite3.Connection, sample: Sample, *, declared: bool) -> None:
        # Create the FK schema before inserting data; randomize names and positions.
        left, right = self.name("t"), self.name("t")
        left_key, right_key = self.name("c"), self.name("c")
        data_cols = [self.name("c") for _ in sample.columns]
        cut = max(1, len(data_cols) // 2)
        left_cols = [left_key, *data_cols[:cut], self.name("c")]
        right_cols = [right_key, *data_cols[cut:], self.name("c")]
        self.rng.shuffle(left_cols)
        self.rng.shuffle(right_cols)
        db.execute(
            "CREATE TABLE "
            + qi(left)
            + "("
            + ",".join(
                qi(c) + (" INTEGER PRIMARY KEY" if c == left_key else "")
                for c in left_cols
            )
            + ")"
        )
        fk = (
            ", FOREIGN KEY("
            + qi(right_key)
            + ") REFERENCES "
            + qi(left)
            + "("
            + qi(left_key)
            + ")"
            if declared
            else ""
        )
        db.execute(
            "CREATE TABLE " + qi(right) + "(" + ",".join(map(qi, right_cols)) + fk + ")"
        )
        n = max(
            len(sample.rows) + 10,
            self.rng.randint(self.args.min_rows, self.args.max_rows),
        )
        ids = self.rng.sample(range(1000000000, 2000000000), n)
        planted = self.rng.sample(range(n), len(sample.rows))
        by_pos = {position: row for position, row in zip(planted, sample.rows)}
        left_rows, right_rows = [], []
        for p, identity in enumerate(ids):
            source = by_pos.get(p)
            lvalues = {c: self.value() for c in left_cols}
            rvalues = {c: self.value() for c in right_cols}
            lvalues[left_key] = identity
            rvalues[right_key] = identity
            if source is not None:
                for c, v in zip(data_cols[:cut], source[:cut]):
                    lvalues[c] = v
                for c, v in zip(data_cols[cut:], source[cut:]):
                    rvalues[c] = v
            left_rows.append(tuple(lvalues[c] for c in left_cols))
            right_rows.append(tuple(rvalues[c] for c in right_cols))
        self.rng.shuffle(right_rows)
        db.executemany(
            "INSERT INTO "
            + qi(left)
            + " VALUES("
            + ",".join("?" for _ in left_cols)
            + ")",
            left_rows,
        )
        db.executemany(
            "INSERT INTO "
            + qi(right)
            + " VALUES("
            + ",".join("?" for _ in right_cols)
            + ")",
            right_rows,
        )
        ltab, rtab = NoiseTable(left, left_cols, n), NoiseTable(right, right_cols, n)
        for c in data_cols[:cut]:
            self.index(db, ltab, c)
        for c in data_cols[cut:] + [right_key]:
            self.index(db, rtab, c)
        self.table_count += 2
        self.row_count += 2 * n
        self.scenarios.append(
            {
                "kind": "JOIN",
                "frontend": sample.name,
                "tables": [left, right],
                "declared_fk": declared,
                "left_key": left_key,
                "right_key": right_key,
                "columns_in_gui_order": data_cols,
                "split_after": cut,
                "ids_in_gui_order": [ids[p] for p in planted],
                "description": (
                    "Join declared through a foreign key."
                    if declared
                    else "No declared FK: infer the relationship from shared identifiers."
                ),
            }
        )

    def decoy(self, db: sqlite3.Connection, sample: Sample) -> None:
        table = self.new_table(db, width=len(sample.columns) + 3)
        columns = self.rng.sample(table.columns, len(sample.columns))
        # Keep variety order but rotate other fields so every complete row is incorrect.
        updates = [
            {
                c: sample.rows[(r + j) % len(sample.rows)][j]
                for j, c in enumerate(columns)
            }
            for r in range(len(sample.rows))
        ]
        rowids = self.plant(db, table, updates)
        for c in columns:
            self.index(db, table, c)
        self.scenarios.append(
            {
                "kind": "decoy",
                "frontend": sample.name,
                "tables": [table.name],
                "columns_in_gui_order": columns,
                "rowids": rowids,
                "description": "Complete coverage of every column but no correct complete row. Must not produce a complete structural match.",
            }
        )

    def fragments(
        self, db: sqlite3.Connection, sample: Sample, noise: list[NoiseTable]
    ) -> None:
        cells = self.rng.sample(
            [
                (r, c)
                for r in range(len(sample.rows))
                for c in range(len(sample.columns))
            ],
            8,
        )
        placements = []
        # Separate random tables demonstrate isolated evidence without row relationships.
        pool = self.rng.sample(noise, min(8, len(noise)))
        if not pool:
            pool = [self.new_table(db, width=6)]
        for k, (r, c) in enumerate(cells):
            table = pool[k % len(pool)]
            column = self.rng.choice(table.columns)
            value = sample.rows[r][c]
            # Declared affinity in noise tables can convert integer values into text.
            rowid = self.plant(db, table, [{column: value}])[0]
            self.index(db, table, column)
            placements.append(
                {
                    "gui_row": r + 1,
                    "gui_column": c + 1,
                    "table": table.name,
                    "column": column,
                    "rowid": rowid,
                }
            )
        self.scenarios.append(
            {
                "kind": "fragments",
                "frontend": sample.name,
                "placements": placements,
                "description": "Isolated cells without a shared row model.",
            }
        )

    def build_source(self, path: Path) -> None:
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA cache_size=-65536")
            db.execute("CREATE TEMP TABLE _demo_sequence(n INTEGER PRIMARY KEY)")
            db.execute(
                "WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n<?) "
                "INSERT INTO _demo_sequence SELECT n FROM seq",
                [self.args.max_rows],
            )
            db.commit()
            noise = []
            for i in range(self.args.noise_tables):
                if shutil.disk_usage(path.parent).free < 2 * 1024**3:
                    raise OSError(
                        "Less than 2 GiB of free disk space remains. Generation stopped."
                    )
                table = self.new_table(db, typed=True)
                noise.append(table)
                for column in table.columns:
                    if self.rng.random() < self.args.index_probability:
                        self.index(db, table, column, target=False)
                db.commit()  # Keep transactions short; this is still a working database, not the final output.
                if (i + 1) % 25 == 0:
                    elapsed = time.monotonic() - self.started
                    print(
                        f"  {i + 1}/{self.args.noise_tables} noise tables | {self.row_count:,} rows "
                        f"| {elapsed:.1f} s | {path.stat().st_size / 1024**3:.2f} GiB",
                        file=sys.stderr,
                        flush=True,
                    )
            self.regular(db, self.samples[0])
            self.regular(db, self.samples[1], numeric_text=True)
            self.transpose(db, self.samples[0])
            self.eav(db, self.samples[0])
            self.join(db, self.samples[0], declared=True)
            self.join(db, self.samples[1], declared=False)
            self.decoy(db, self.samples[0])
            self.fragments(db, self.samples[0], noise)
            db.commit()
            # Provide optimizer statistics for the fixture. Discovery never runs ANALYZE.
            db.execute("PRAGMA analysis_limit=1000")
            db.execute("ANALYZE")

    def write_manifest(self, path: Path) -> None:
        payload = {
            "seed": self.args.seed,
            "profile": self.args.profile,
            "tables": self.table_count,
            "rows": self.row_count,
            "generation_seconds": round(time.monotonic() - self.started, 3),
            "target_indexes": not self.args.no_target_indexes,
            "scenarios": self.scenarios,
            "note": "Independent ground truth. The discovery engine never reads this file.",
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--profile",
        choices=["small", "large"],
        default="large",
        help="large: 3000 noise tables with 18000–70000 rows; small: 30 tables with 180–700 rows.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Default: data/large for large, data/demo for small.",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--noise-tables",
        type=int,
        help="Noise table count; scenarios add 9 tables, or 10 when the noise table count is zero.",
    )
    parser.add_argument("--min-columns", type=int, default=4)
    parser.add_argument("--max-columns", type=int, default=12)
    parser.add_argument("--min-rows", type=int)
    parser.add_argument("--max-rows", type=int)
    parser.add_argument(
        "--index-probability",
        type=float,
        default=0.18,
        help="Probability of indexing each noise column.",
    )
    parser.add_argument(
        "--no-target-indexes",
        action="store_true",
        help="Do not index planted values; primary keys remain indexed.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace generated frontend.sqlite, source.sqlite, and expected.json.",
    )
    args = parser.parse_args(argv)
    large = args.profile == "large"
    if args.output_dir is None:
        args.output_dir = Path("data") / ("large" if large else "demo")
    if args.noise_tables is None:
        args.noise_tables = 3000 if large else 30
    if args.min_rows is None:
        args.min_rows = 18000 if large else 180
    if args.max_rows is None:
        args.max_rows = 70000 if large else 700
    if args.noise_tables < 0 or not 1 <= args.min_columns <= args.max_columns <= 256:
        parser.error(
            "Required: noise-tables >= 0 and 1 <= min-columns <= max-columns <= 256."
        )
    if not 30 <= args.min_rows <= args.max_rows:
        parser.error(
            "Required: 30 <= min-rows <= max-rows to accommodate the fixtures."
        )
    if not 0 <= args.index_probability <= 1:
        parser.error("--index-probability must be in [0, 1].")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    estimate = (
        args.noise_tables
        * (args.min_rows + args.max_rows)
        / 2
        * (args.min_columns + args.max_columns)
        / 2
        * 18
    )
    if shutil.disk_usage(args.output_dir).free < estimate + 2 * 1024**3:
        parser.error(
            f"Insufficient free space for the estimated budget of {estimate / 1024**3:.1f} GiB plus a 2 GiB reserve."
        )
    destinations = [
        args.output_dir / name
        for name in ("frontend.sqlite", "source.sqlite", "expected.json")
    ]
    existing = [p for p in destinations if p.exists()]
    if any(
        Path(str(p) + suffix).exists()
        for p in destinations[:2]
        for suffix in ("-wal", "-shm", "-journal")
    ):
        parser.error(
            "WAL/SHM/journal sidecars exist for the target databases. Close connections or choose another directory."
        )
    if existing and not args.force:
        parser.error(
            "Output files already exist. Choose another directory or pass --force: "
            + ", ".join(map(str, existing))
        )
    # Build separate working files and promote them only after successful generation.
    temporary = [p.with_name(p.name + ".building") for p in destinations]
    if any(p.exists() for p in temporary):
        parser.error(
            "Working .building files remain from a previous run; remove them or choose another directory."
        )
    try:
        print(
            f"Profile {args.profile}: {args.noise_tables} noise tables, "
            f"{args.min_rows}–{args.max_rows} rows/table; estimated disk budget "
            f"{estimate / 1024**3:.1f} GiB.",
            file=sys.stderr,
            flush=True,
        )
        builder = DemoBuilder(args)
        builder.create_frontend(temporary[0])
        builder.build_source(temporary[1])
        builder.write_manifest(temporary[2])
        for src, dst in zip(temporary, destinations):
            src.replace(dst)
        print(
            f"Completed: {builder.table_count} source tables, {builder.row_count} rows."
        )
        for path in destinations:
            print(path.resolve())
        return 0
    except (OSError, sqlite3.Error) as exc:
        print(f"Generator error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(
            "Generation interrupted. Target files have not been replaced.",
            file=sys.stderr,
        )
        return 130
    finally:
        for path in temporary:
            if path.exists():
                path.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
