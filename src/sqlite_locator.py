#!/usr/bin/env python3
"""SQLite data locator. Python >= 3.11, standard library only.

Usage:
    python src/sqlite_locator.py --frontend data/demo/frontend.sqlite \
        --source data/demo/source.sqlite --report reports/demo/report.html

Layers in one file: model -> SQLite reader -> probing -> structure matching
-> reporting. No runtime AI or access to the demo ground-truth manifest.
Match validity and search completeness are separate properties.
"""

from __future__ import annotations

import argparse
import fnmatch
import html
import itertools
import json
import logging
import math
import random
import re
import sqlite3
import sys
import tempfile
import time
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Hashable, Iterable, Iterator

Value = Any  # SQLite: None, int, float, str, bytes.
Key = tuple[str, Hashable]
Ref = tuple[str, int]  # table name, column position (not its name or declared type)
LOGGER = logging.getLogger("sqlite_locator")
VERSION = "1.0.0"


def qi(name: str) -> str:
    """Identifiers come from the catalog; values are always bound as parameters."""
    return '"' + name.replace('"', '""') + '"'


def sql_literal(value: Value) -> str:
    """For SQL export only; executed queries use bound parameters."""
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    if isinstance(value, str):
        if "\x00" in value:
            return "CAST(X'" + value.encode("utf-8").hex() + "' AS TEXT)"
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, float) and not math.isfinite(value):
        return "9e999" if value > 0 else "-9e999"
    return str(value)


def shown(value: Value, limit: int = 180) -> str:
    if value is None:
        return "NULL"
    text = "0x" + value.hex() if isinstance(value, bytes) else str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def json_value(value: Value) -> Value:
    if isinstance(value, bytes):
        return {"sqlite_blob_hex": value.hex()}
    if isinstance(value, float) and not math.isfinite(value):
        return {"sqlite_real": str(value)}
    return value


def json_tree(value: Value) -> Value:
    """The JSON default hook cannot handle float('inf'); convert nested values too."""
    if isinstance(value, dict):
        return {k: json_tree(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_tree(v) for v in value]
    return json_value(value)


class ValueCodec:
    """Conservative normalization: '001' remains a text identifier.

    numeric: 12, 12.0 and '12.00' are equivalent, without guessing currencies,
    dates, thousands separators, case, whitespace or floating-point tolerance.
    strict: text is distinct from numbers; INTEGER and REAL compare numerically.
    """

    number = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")

    def __init__(self, mode: str):
        self.mode = mode

    def key(self, value: Value) -> Key | None:
        if value is None:
            return None
        if isinstance(value, bytes):
            return ("blob", value)
        numeric = isinstance(value, (int, float))
        if isinstance(value, str):
            numeric = (
                self.mode == "numeric"
                and len(value) <= 200
                and self.number.fullmatch(value) is not None
            )
        if numeric:
            try:
                decimal = Decimal(str(value))
                if decimal.is_finite() and abs(decimal.adjusted()) <= 1000:
                    return ("number", decimal)
            except InvalidOperation:
                pass
        return (type(value).__name__, value)

    def variants(self, value: Value) -> list[Value]:
        """A finite set of index probes, not every possible numeric representation."""
        values = [value]
        key = self.key(value)
        if key and key[0] == "number":
            number = key[1]
            if number == number.to_integral_value() and -(2**63) <= number < 2**63:
                values.append(int(number))
            floating = float(number)
            if math.isfinite(floating) and Decimal(str(floating)) == number:
                values.append(floating)
            if self.mode == "numeric":
                fixed = format(number, "f")
                values.extend([fixed, str(floating)])
                if "." in fixed:
                    values.append(fixed.rstrip("0").rstrip("."))
        unique = {(type(v).__name__, v): v for v in values}
        return list(unique.values())


@dataclass
class Config:
    mode: str = "bounded"
    comparison: str = "numeric"
    sample_rows: int = 160
    random_seeks: int = 24
    witnesses: int = 32
    max_evidence_rows: int = 12000
    max_evidence_bytes: int = 67108864
    max_row_bytes: int = 4194304
    max_hit_rows: int = 20000
    max_tables: int = 10000
    max_columns: int = 256
    max_frontend_rows: int = 100
    max_frontend_cells: int = 400
    max_value_bytes: int = 65536
    candidates_per_column: int = 8
    max_mappings: int = 128
    max_join_hops: int = 2
    max_join_paths: int = 80
    max_eav_columns: int = 16
    max_findings: int = 100
    min_containment: float = 0.6
    query_seconds: float = 0.5
    query_vm_steps: int = 1000000
    total_seconds: float = 120.0
    seed: int = 42


@dataclass
class Column:
    name: str
    declared_type: str
    pk_order: int = 0
    index: str | None = None  # None = no usable index, '' = INTEGER PRIMARY KEY.


@dataclass
class Table:
    name: str
    columns: list[Column]
    locator: list[str]
    rowid: str | None
    foreign_keys: list[tuple[str, list[str], list[str | None]]] = field(
        default_factory=list
    )

    def select_list(self, alias: str = "") -> str:
        prefix = alias + "." if alias else ""
        return ", ".join(
            prefix + qi(c) for c in self.locator + [c.name for c in self.columns]
        )


@dataclass
class FrontTable:
    name: str
    columns: list[str]
    rows: list[tuple[Value, ...]]

    @property
    def cells(self) -> int:
        return sum(v is not None for row in self.rows for v in row)


@dataclass
class Coverage:
    table: str
    columns: int
    sample_rows: int = 0
    index_probes: int = 0
    retained_rows: int = 0
    full_scan_finished: bool = False
    truncated_probes: int = 0
    interrupted: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class Witness:
    locator: tuple[Value, ...]
    values: tuple[Value, ...]


@dataclass
class CellProof:
    row: int
    column: int
    table: str
    source_column: str
    locator: dict[str, Value]
    actual: Value


@dataclass
class Finding:
    frontend: str
    kind: str
    tables: list[str]
    total_cells: int
    proofs: list[CellProof]
    mapping: list[str]
    note: str = ""
    relation_sql: str = ""
    replay_sql: str = ""

    @property
    def matched_cells(self) -> int:
        return len({(p.row, p.column) for p in self.proofs})

    @property
    def full(self) -> bool:
        return self.matched_cells == self.total_cells and self.total_cells > 0


class Deadline:
    def __init__(self, seconds: float):
        self.started = time.monotonic()
        self.until = self.started + seconds

    def expired(self) -> bool:
        return time.monotonic() >= self.until


@dataclass
class QueryResult:
    rows: list[tuple] = field(default_factory=list)
    complete: bool = True
    reason: str = ""
    count: int = 0


class SQLiteReader:
    """Single database gateway: read-only access, parameters, time and VM limits.

    The progress handler provides approximate limits, not a hard operating-system
    I/O deadline. Rows received before an interrupted query are retained.
    """

    def __init__(self, path: Path, config: Config, deadline: Deadline):
        if not path.is_file():
            raise ValueError(f"Database file not found: {path}")
        self.config, self.deadline = config, deadline
        self.connection = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=min(config.query_seconds, 1.0),
        )
        self.connection.execute("PRAGMA query_only=ON")
        self.connection.execute("PRAGMA trusted_schema=OFF")
        self.connection.execute("PRAGMA automatic_index=OFF")
        self.connection.execute("PRAGMA cache_size=-8192")
        if hasattr(self.connection, "setlimit"):
            self.connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, config.max_row_bytes)
        self.connection.execute("BEGIN")  # A consistent snapshot within each database.
        self.queries = 0
        self.returned_rows = 0
        self.interruptions = 0
        self.errors: list[str] = []

    def run(
        self,
        sql: str,
        params: Iterable[Value] = (),
        *,
        limit: int = 10000,
        consume: Callable[[tuple], None] | None = None,
    ) -> QueryResult:
        result = QueryResult()
        if self.deadline.expired():
            return QueryResult(complete=False, reason="global time limit")
        end = min(self.deadline.until, time.monotonic() + self.config.query_seconds)
        ticks = 0

        def progress() -> int:
            nonlocal ticks
            ticks += 1000
            return int(ticks >= self.config.query_vm_steps or time.monotonic() >= end)

        self.connection.set_progress_handler(progress, 1000)
        cursor = None
        self.queries += 1
        try:
            cursor = self.connection.execute(sql, tuple(params))
            for row in cursor:
                if result.count >= limit:
                    result.complete, result.reason = False, "returned row limit"
                    break
                result.count += 1
                if consume:
                    consume(row)
                else:
                    result.rows.append(row)
                if time.monotonic() >= end:
                    result.complete, result.reason = False, "query time limit"
                    break
        except sqlite3.Error as exc:
            result.complete, result.reason = False, str(exc)
            if len(self.errors) < 50:
                self.errors.append(f"{str(exc)} | {sql[:180]}")
        finally:
            if cursor:
                cursor.close()
            self.connection.set_progress_handler(None, 0)
        self.returned_rows += result.count
        if not result.complete:
            self.interruptions += 1
        return result

    def close(self) -> None:
        self.connection.close()


class Catalog:
    def __init__(self, db: SQLiteReader):
        self.db = db
        self.notes: list[str] = []

    def read(self, patterns: list[str] | None = None) -> dict[str, Table]:
        cfg = self.db.config
        result = self.db.run(
            "SELECT name, sql FROM sqlite_schema WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'",
            limit=cfg.max_tables,
        )
        if not result.complete:
            self.notes.append("Catalog read incomplete: " + result.reason)
        tables = {}
        for name, ddl in result.rows:
            if self.db.deadline.expired():
                self.notes.append("Time limit reached while reading the schema.")
                break
            if patterns and not any(fnmatch.fnmatchcase(name, p) for p in patterns):
                continue
            if re.search(r"CREATE\s+VIRTUAL\s+TABLE", ddl or "", re.I):
                self.notes.append(f"{name}: virtual table skipped.")
                continue
            info = self.db.run(
                "SELECT name, type, pk, hidden FROM pragma_table_xinfo(?)",
                [name],
                limit=cfg.max_columns,
            )
            if not info.complete:
                self.notes.append(
                    f"{name}: incomplete column metadata ({info.reason}); skipped."
                )
                continue
            columns = [
                Column(n, t or "", pk) for n, t, pk, hidden in info.rows if hidden != 1
            ]
            if not columns:
                continue
            names = {c.name.casefold() for c in columns}
            without = bool(re.search(r"\bWITHOUT\s+ROWID\b", ddl or "", re.I))
            rowid = next(
                (x for x in ["_rowid_", "rowid", "oid"] if x not in names), None
            )
            if without:
                rowid = None
            primary = [
                c.name for c in sorted(columns, key=lambda c: c.pk_order) if c.pk_order
            ]
            locator = [rowid] if rowid else primary if without else []
            if not locator:
                self.notes.append(f"{name}: no unambiguous row locator; skipped.")
                continue
            table = Table(name, columns, locator, rowid)
            indices = self.db.run(
                "SELECT name, origin, partial FROM pragma_index_list(?)", [name]
            )
            if not indices.complete:
                self.notes.append(f"{name}: incomplete index metadata.")
            for index_name, origin, partial in indices.rows:
                if partial:
                    continue
                idx = self.db.run(
                    "SELECT name, coll FROM pragma_index_xinfo(?) "
                    "WHERE key=1 ORDER BY seqno",
                    [index_name],
                )
                if (
                    idx.complete
                    and idx.rows
                    and idx.rows[0][1] in ("BINARY", "NOCASE", "RTRIM")
                ):
                    for c in columns:
                        if c.name == idx.rows[0][0] and c.index is None:
                            c.index = index_name
            # INTEGER PRIMARY KEY DESC may have its own index; verify the plan later.
            if len(primary) == 1 and rowid:
                for c in columns:
                    if (
                        c.pk_order
                        and c.declared_type.upper() == "INTEGER"
                        and c.index is None
                    ):
                        c.index = ""
            fks = self.db.run(
                'SELECT id, seq, "table", "from", "to" '
                "FROM pragma_foreign_key_list(?) ORDER BY id, seq",
                [name],
            )
            groups: dict[int, list] = defaultdict(list)
            for fk_id, seq, target, source_col, target_col in fks.rows:
                groups[fk_id].append((target, source_col, target_col))
            table.foreign_keys = [
                (rows[0][0], [r[1] for r in rows], [r[2] for r in rows])
                for rows in groups.values()
            ]
            if not fks.complete:
                self.notes.append(f"{name}: incomplete foreign-key metadata.")
            tables[name] = table
        return tables


def load_frontend(db: SQLiteReader, names: list[str] | None) -> list[FrontTable]:
    catalog = Catalog(db)
    tables = catalog.read()
    if catalog.notes:
        raise ValueError("Frontend schema read incomplete: " + "; ".join(catalog.notes))
    selected = names or list(tables)
    missing = set(selected) - set(tables)
    if missing:
        raise ValueError("Frontend tables not found: " + ", ".join(sorted(missing)))
    fronts, total = [], 0
    for name in selected:
        table = tables[name]
        columns = [c.name for c in table.columns]
        result = db.run(
            "SELECT " + ", ".join(map(qi, columns)) + " FROM " + qi(name),
            limit=db.config.max_frontend_rows,
        )
        if not result.complete:
            raise ValueError(
                f"Frontend {name}: sample read incomplete ({result.reason})."
            )
        total += len(result.rows) * len(columns)
        if total > db.config.max_frontend_cells:
            raise ValueError(
                "--max-frontend-cells exceeded. Provide a smaller sample or increase the limit."
            )
        for row in result.rows:
            for v in row:
                if isinstance(v, (str, bytes)) and len(v) > db.config.max_value_bytes:
                    raise ValueError("Frontend value exceeds --max-value-bytes.")
        if result.rows:
            fronts.append(FrontTable(name, columns, result.rows))
    if not fronts or not any(f.cells for f in fronts):
        raise ValueError("The frontend is empty or contains only NULL values.")
    return fronts


class EvidenceStore:
    """Inverted index of retained evidence, not a copy of the source database."""

    def __init__(self, codec: ValueCodec, fronts: list[FrontTable], config: Config):
        self.codec, self.config = codec, config
        self.targets: dict[Key, Value] = {}
        for front in fronts:
            for row in front.rows:
                for value in row:
                    key = codec.key(value)
                    if key is not None:
                        self.targets.setdefault(key, value)
        self.rows: dict[str, dict[tuple, Witness]] = defaultdict(dict)
        self.hits: dict[Ref, dict[Key, set[tuple]]] = defaultdict(
            lambda: defaultdict(set)
        )
        self.count = 0
        self.payload_bytes = 0
        self.capped = False
        self.oversized = False

    def add(self, table: Table, raw: tuple) -> None:
        n = len(table.locator)
        loc, values = tuple(raw[:n]), tuple(raw[n:])
        if loc in self.rows[table.name]:
            return
        if any(
            isinstance(v, (str, bytes)) and len(v) > self.config.max_value_bytes
            for v in values
        ):
            self.oversized = True
            return
        keys = [self.codec.key(v) for v in values]
        useful = [(i, key) for i, key in enumerate(keys) if key in self.targets]
        if not useful:
            return
        size = (
            sys.getsizeof(values)
            + sys.getsizeof(loc)
            + sum(sys.getsizeof(v) for v in values + loc)
        )
        if (
            self.count >= self.config.max_evidence_rows
            or self.payload_bytes + size > self.config.max_evidence_bytes
        ):
            self.capped = True
            return
        # A per-value/column cap prevents a common '1' from exhausting memory.
        if all(
            len(self.hits[(table.name, i)][k]) >= self.config.witnesses
            for i, k in useful
        ):
            self.capped = True
            return
        self.rows[table.name][loc] = Witness(loc, values)
        self.count += 1
        self.payload_bytes += size
        for i, key in useful:
            bucket = self.hits[(table.name, i)][key]
            if len(bucket) < self.config.witnesses:
                bucket.add(loc)
            else:
                self.capped = True

    def coverage(self, ref: Ref, values: Iterable[Value]) -> float:
        keys = {self.codec.key(v) for v in values if v is not None}
        return len(keys & self.hits.get(ref, {}).keys()) / len(keys) if keys else 0.0


class ProbeEngine:
    def __init__(self, db: SQLiteReader, store: EvidenceStore):
        self.db, self.store, self.cfg = db, store, db.config
        self.rng = random.Random(self.cfg.seed)
        self.coverage: list[Coverage] = []

    def run(self, tables: dict[str, Table]) -> None:
        # Value complexity is a priority heuristic, not a measurement of rarity.
        targets = sorted(
            self.store.targets.values(),
            key=lambda v: (isinstance(v, (str, bytes)), len(str(v))),
            reverse=True,
        )
        for number, table in enumerate(tables.values(), 1):
            coverage = Coverage(table.name, len(table.columns))
            self.coverage.append(coverage)
            if self.db.deadline.expired():
                coverage.notes.append("Not started: global time limit.")
                continue
            if number == 1 or number % 25 == 0:
                LOGGER.info("Probing table %s/%s: %s", number, len(tables), table.name)
            if self.cfg.mode == "exhaustive":
                self.full_scan(table, coverage)
            else:
                self.sample(table, coverage)
                for i, col in enumerate(table.columns):
                    if col.index is None or self.db.deadline.expired():
                        continue
                    force = " INDEXED BY " + qi(col.index) if col.index else ""
                    prototype = (
                        "SELECT "
                        + table.select_list()
                        + " FROM "
                        + qi(table.name)
                        + force
                        + " WHERE "
                        + qi(col.name)
                        + "=?"
                    )
                    plan = self.db.run("EXPLAIN QUERY PLAN " + prototype, [targets[0]])
                    if not plan.complete or not any(
                        "SEARCH " in str(r[-1]) for r in plan.rows
                    ):
                        coverage.notes.append(
                            f"{col.name}: indexed search could not be confirmed."
                        )
                        continue
                    for value in targets:
                        if self.db.deadline.expired():
                            break
                        key = self.store.codec.key(value)
                        if (
                            len(self.store.hits.get((table.name, i), {}).get(key, ()))
                            >= self.cfg.witnesses
                        ):
                            continue
                        variants = self.store.codec.variants(value)
                        sql = (
                            "SELECT "
                            + table.select_list()
                            + " FROM "
                            + qi(table.name)
                            + force
                            + " WHERE "
                            + qi(col.name)
                            + " IN ("
                            + ",".join("?" for _ in variants)
                            + ") LIMIT ?"
                        )
                        result = self.db.run(
                            sql,
                            variants + [self.cfg.witnesses + 1],
                            limit=self.cfg.witnesses,
                            consume=lambda row: self.store.add(table, row),
                        )
                        coverage.index_probes += 1
                        if not result.complete:
                            coverage.truncated_probes += 1
                            if result.reason != "returned row limit":
                                coverage.interrupted += 1
            coverage.retained_rows = len(self.store.rows.get(table.name, {}))

    def sample(self, table: Table, coverage: Coverage) -> None:
        if self.cfg.sample_rows == 0:
            return
        order = ", ".join(map(qi, table.locator))
        prefix = "SELECT " + table.select_list() + " FROM " + qi(table.name)
        # LIMIT without random sorting; both ends of the rowid/PRIMARY KEY index.
        head_n = (self.cfg.sample_rows + 1) // 2
        tail_n = self.cfg.sample_rows // 2
        head = self.db.run(
            prefix + " ORDER BY " + order + " LIMIT ?", [head_n], limit=head_n
        )
        tail_order = ", ".join(qi(c) + " DESC" for c in table.locator)
        tail = self.db.run(
            prefix + " ORDER BY " + tail_order + " LIMIT ?", [tail_n], limit=tail_n
        )
        unique = set()
        for result in (head, tail):
            if not result.complete:
                coverage.interrupted += 1
            for row in result.rows:
                unique.add(tuple(row[: len(table.locator)]))
                self.store.add(table, row)
        if table.rowid and head.rows and tail.rows:
            low, high = head.rows[0][0], tail.rows[0][0]
            if high > low:
                for _ in range(self.cfg.random_seeks):
                    position = self.rng.randint(low, high)
                    result = self.db.run(
                        prefix
                        + " WHERE "
                        + qi(table.rowid)
                        + ">=? ORDER BY "
                        + qi(table.rowid)
                        + " LIMIT 4",
                        [position],
                        limit=4,
                    )
                    for row in result.rows:
                        unique.add((row[0],))
                        self.store.add(table, row)
                    if not result.complete:
                        coverage.interrupted += 1
        elif not table.rowid:
            coverage.notes.append(
                "WITHOUT ROWID: samples from both ends of the primary key, without random seeks."
            )
        coverage.sample_rows = len(unique)
        coverage.notes.append(
            "Sampling is not uniform; a miss does not establish absence."
        )

    def full_scan(self, table: Table, coverage: Coverage) -> None:
        # One scan per table, rather than k separate full scans for k values.
        # The local deterministic function returns only rows containing evidence.
        self.db.connection.create_function(
            "_detective_hit",
            1,
            lambda v: int(self.store.codec.key(v) in self.store.targets),
            deterministic=True,
        )
        where = " OR ".join("_detective_hit(" + qi(c.name) + ")" for c in table.columns)
        sql = (
            "SELECT "
            + table.select_list()
            + " FROM "
            + qi(table.name)
            + " WHERE "
            + where
        )
        result = self.db.run(
            sql,
            limit=self.cfg.max_hit_rows,
            consume=lambda row: self.store.add(table, row),
        )
        coverage.full_scan_finished = result.complete
        if not result.complete:
            coverage.interrupted += 1
            coverage.notes.append("Scan incomplete: " + result.reason)


def assignments(
    options: dict[int, list[Hashable]], limit: int, deadline: Deadline
) -> Iterator[dict[int, Hashable]]:
    """Bounded backtracking over alternative injective assignments."""
    order = sorted(options, key=lambda key: len(options[key]))
    chosen: dict[int, Hashable] = {}
    used: set[Hashable] = set()
    emitted = 0
    visited = 0

    def walk(depth: int) -> Iterator[dict[int, Hashable]]:
        nonlocal emitted, visited
        visited += 1
        if (
            emitted >= limit
            or visited > limit * max(10, len(order) * 10)
            or deadline.expired()
        ):
            return
        if depth == len(order):
            emitted += 1
            yield dict(chosen)
            return
        key = order[depth]
        for candidate in options[key]:
            if candidate in used:
                continue
            used.add(candidate)
            chosen[key] = candidate
            yield from walk(depth + 1)
            used.remove(candidate)
            chosen.pop(key, None)

    yield from walk(0)


def distinct_witnesses(options: dict[int, list[Hashable]]) -> dict[int, Hashable]:
    """Small bipartite matching: duplicate GUI rows require distinct source records."""
    owner: dict[Hashable, int] = {}

    def augment(row: int, seen: set[Hashable]) -> bool:
        for candidate in options.get(row, []):
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate not in owner or augment(owner[candidate], seen):
                owner[candidate] = row
                return True
        return False

    for row in sorted(options, key=lambda key: len(options[key])):
        augment(row, set())
    return {row: witness for witness, row in owner.items()}


@dataclass(frozen=True)
class Edge:
    left: str
    right: str
    pairs: tuple[tuple[str, str], ...]
    inferred: bool = False

    def other(self, node: str) -> str:
        return self.right if node == self.left else self.left


class StructureMatcher:
    def __init__(
        self, db: SQLiteReader, tables: dict[str, Table], store: EvidenceStore
    ):
        self.db, self.tables, self.store = db, tables, store
        self.cfg, self.codec = db.config, store.codec
        self.findings: list[Finding] = []
        self._finding_signatures: set[tuple] = set()
        self.notes: set[str] = set()

    def view(self, front: FrontTable, transpose: bool) -> tuple[list[tuple], Callable]:
        if transpose:
            return list(zip(*front.rows)), lambda r, c: (c, r)
        return front.rows, lambda r, c: (r, c)

    def options(self, rows: list[tuple], names: list[str]) -> dict[int, list[Ref]]:
        result = {}
        for j in range(len(rows[0])):
            values = [r[j] for r in rows if r[j] is not None]
            if not values:
                continue  # NULL provides no evidence and is not used as a search key.
            candidates = []
            for name in names:
                for i in range(len(self.tables[name].columns)):
                    ref = (name, i)
                    score = self.store.coverage(ref, values)
                    if score >= self.cfg.min_containment:
                        candidates.append((score, ref))
            candidates.sort(key=lambda x: (-x[0], x[1]))
            if len(candidates) > self.cfg.candidates_per_column:
                self.notes.add(
                    "Column candidates capped; increase --candidates-per-column."
                )
            result[j] = [ref for _, ref in candidates[: self.cfg.candidates_per_column]]
        return result

    def run(self, fronts: list[FrontTable]) -> None:
        for front in fronts:
            LOGGER.info("Verifying structures for: %s", front.name)
            for transpose in (False, True):
                rows, coords = self.view(front, transpose)
                for name in self.tables:
                    if self.db.deadline.expired():
                        break
                    options = self.options(rows, [name])
                    if not options or any(not c for c in options.values()):
                        continue
                    count = 0
                    for mapping in assignments(
                        options, self.cfg.max_mappings, self.db.deadline
                    ):
                        count += 1
                        self.verify(
                            front,
                            rows,
                            coords,
                            [name],
                            [],
                            mapping,
                            "transpose" if transpose else "rows",
                        )
                    if count >= self.cfg.max_mappings:
                        self.notes.add(
                            "Column mapping limit reached; results are not exhaustive."
                        )
            if not self.db.deadline.expired():
                self.eav(front)
            if not self.db.deadline.expired():
                self.joins(front)
        self.findings.sort(
            key=lambda f: (
                -f.matched_cells / max(1, f.total_cells),
                len(f.tables),
                f.kind,
                f.tables,
            )
        )
        if len(self.findings) > self.cfg.max_findings:
            self.notes.add("Report limited to --max-findings best structures.")
            self.findings = self.findings[: self.cfg.max_findings]

    def from_sql(self, names: list[str], edges: list[Edge]) -> str:
        aliases = {n: f"t{i}" for i, n in enumerate(names)}
        sql = qi(names[0]) + " AS t0"
        for i, edge in enumerate(edges, 1):
            condition = " AND ".join(
                aliases[edge.left]
                + "."
                + qi(l)
                + "="
                + aliases[edge.right]
                + "."
                + qi(r)
                for l, r in edge.pairs
            )
            sql += "\nJOIN " + qi(names[i]) + f" AS t{i} ON " + condition
        return sql

    def candidate_locations(
        self, name: str, row: tuple, mapping: dict[int, Ref]
    ) -> list[tuple]:
        constraints = [
            (j, i) for j, (n, i) in mapping.items() if n == name and row[j] is not None
        ]
        if not constraints:
            return []
        sets = [
            self.store.hits.get((name, i), {}).get(self.codec.key(row[j]), set())
            for j, i in constraints
        ]
        common = set.intersection(*(set(s) for s in sets))
        return sorted(common, key=repr)[: self.cfg.witnesses]

    def verify(
        self,
        front: FrontTable,
        rows: list[tuple],
        coords: Callable,
        names: list[str],
        edges: list[Edge],
        mapping: dict[int, Ref],
        kind: str,
    ) -> None:
        choices: dict[int, list[Hashable]] = {}
        evidence: dict[tuple[int, Hashable], list[CellProof]] = {}
        from_sql = self.from_sql(names, edges)
        aliases = {name: f"t{i}" for i, name in enumerate(names)}
        ordered = sorted(mapping)
        for r, row in enumerate(rows):
            if self.db.deadline.expired():
                break
            if not any(row[j] is not None for j in mapping):
                continue
            locs = {
                name: self.candidate_locations(name, row, mapping) for name in names
            }
            constrained = {
                name for j, (name, _) in mapping.items() if row[j] is not None
            }
            if any(not locs[name] for name in constrained):
                continue
            if not edges:
                name = names[0]
                candidates = []
                for loc in locs[name]:
                    witness = self.store.rows[name][loc]
                    values = [witness.values[mapping[j][1]] for j in ordered]
                    candidates.append((loc, values))
            else:
                select = [
                    aliases[n] + "." + qi(c)
                    for n in names
                    for c in self.tables[n].locator
                ]
                select += [
                    aliases[mapping[j][0]]
                    + "."
                    + qi(self.tables[mapping[j][0]].columns[mapping[j][1]].name)
                    for j in ordered
                ]
                where, params = [], []
                for name in names:
                    if name not in constrained:
                        continue
                    alternatives = []
                    for loc in locs[name]:
                        alternatives.append(
                            "("
                            + " AND ".join(
                                aliases[name] + "." + qi(c) + " IS ?"
                                for c in self.tables[name].locator
                            )
                            + ")"
                        )
                        params.extend(loc)
                    where.append("(" + " OR ".join(alternatives) + ")")
                sql = (
                    "SELECT "
                    + ", ".join(select)
                    + " FROM "
                    + from_sql
                    + "\nWHERE "
                    + " AND ".join(where)
                    + " LIMIT ?"
                )
                result = self.db.run(
                    sql, params + [self.cfg.witnesses + 1], limit=self.cfg.witnesses
                )
                if not result.complete:
                    self.notes.add(
                        "Some JOIN checks were interrupted or truncated; review the limits."
                    )
                loc_size = sum(len(self.tables[n].locator) for n in names)
                candidates = [
                    (tuple(raw[:loc_size]), list(raw[loc_size:])) for raw in result.rows
                ]
            for identity, values in candidates:
                # Ignore GUI NULL values; verify every remaining cell after retrieval.
                if any(
                    row[j] is not None
                    and self.codec.key(values[p]) != self.codec.key(row[j])
                    for p, j in enumerate(ordered)
                ):
                    continue
                locations, offset = {}, 0
                for name in names:
                    size = len(self.tables[name].locator)
                    locations[name] = dict(
                        zip(self.tables[name].locator, identity[offset : offset + size])
                    )
                    offset += size
                proofs = []
                for p, j in enumerate(ordered):
                    if row[j] is None:
                        continue
                    name, c = mapping[j]
                    fr, fc = coords(r, j)
                    proofs.append(
                        CellProof(
                            fr,
                            fc,
                            name,
                            self.tables[name].columns[c].name,
                            locations[name],
                            values[p],
                        )
                    )
                choices.setdefault(r, []).append(identity)
                evidence[(r, identity)] = proofs
        matched = distinct_witnesses(choices)
        min_rows = min(2, sum(any(v is not None for v in row) for row in rows))
        if len(matched) < min_rows:
            return
        proofs = [
            p
            for r, identity in sorted(matched.items())
            for p in evidence[(r, identity)]
        ]
        if len(proofs) < min(4, front.cells):
            return
        descriptions = []
        for j, (name, c) in sorted(mapping.items()):
            label = f"GUI row {j + 1}" if kind == "transpose" else front.columns[j]
            descriptions.append(f"{label} → {name}.{self.tables[name].columns[c].name}")
        note = "Shared records verified; duplicate GUI rows mapped to distinct source records."
        if any(e.inferred for e in edges):
            note += " Join inferred from values without a declared foreign key; agreement applies to the sample."
        self.add_finding(
            Finding(
                front.name,
                kind,
                names,
                front.cells,
                proofs,
                descriptions,
                note,
                "SELECT * FROM " + from_sql + ";" if edges else "",
            ),
            front,
        )

    def add_finding(self, finding: Finding, front: FrontTable) -> None:
        def signature(f: Finding) -> tuple:
            return (
                f.frontend,
                f.kind,
                tuple(
                    (p.row, p.column, p.table, p.source_column, repr(p.locator))
                    for p in f.proofs
                ),
            )

        def quality(f: Finding) -> tuple:
            return (f.matched_cells / max(1, f.total_cells), -len(f.tables))

        identity = signature(finding)
        if identity in self._finding_signatures:
            return
        if len(self.findings) >= self.cfg.max_findings:
            self.notes.add(
                "Retained structures capped by --max-findings; additional structures omitted."
            )
            weakest = min(self.findings, key=quality)
            if quality(finding) <= quality(weakest):
                return
            self.findings.remove(weakest)
            self._finding_signatures.remove(signature(weakest))
        self._finding_signatures.add(identity)
        finding.replay_sql = self.replay(finding, front)
        self.findings.append(finding)

    @staticmethod
    def replay(finding: Finding, front: FrontTable) -> str:
        by_cell = {(p.row, p.column): p for p in finding.proofs}
        selects = []
        for r, row in enumerate(front.rows):
            if any(v is not None and (r, c) not in by_cell for c, v in enumerate(row)):
                continue
            if not any((r, c) in by_cell for c in range(len(row))):
                continue
            expressions = [f'{r + 1} AS "__gui_row"']
            for c, label in enumerate(front.columns):
                proof = by_cell.get((r, c))
                if proof:
                    where = " AND ".join(
                        qi(k) + " IS " + sql_literal(v)
                        for k, v in proof.locator.items()
                    )
                    expr = (
                        "(SELECT "
                        + qi(proof.source_column)
                        + " FROM "
                        + qi(proof.table)
                        + " WHERE "
                        + where
                        + ")"
                    )
                else:
                    expr = "NULL"  # A GUI NULL means missing evidence, not proof of a database NULL.
                expressions.append(expr + " AS " + qi(label))
            selects.append("SELECT " + ",\n       ".join(expressions))
        return (
            "\nUNION ALL\n".join(selects) + "\nORDER BY 1;"
            if selects
            else "-- No complete GUI rows in this fragment; see the cell locations."
        )

    def eav(self, front: FrontTable) -> None:
        identity_codec = ValueCodec(
            "strict"
        )  # The text ID '42' is distinct from the numeric ID 42.
        active = [
            j
            for j in range(len(front.columns))
            if any(r[j] is not None for r in front.rows)
        ]
        if len(active) < 2:
            return
        all_values = [v for row in front.rows for v in row if v is not None]
        for name, table in self.tables.items():
            witnesses = list(self.store.rows.get(name, {}).values())
            if not witnesses or self.db.deadline.expired():
                continue
            value_cols = [
                i
                for i in range(len(table.columns))
                if self.store.coverage((name, i), all_values)
                >= self.cfg.min_containment
            ]
            for vi in value_cols:
                relevant = [
                    w
                    for w in witnesses
                    if self.codec.key(w.values[vi]) in self.store.targets
                ]
                # EAV repeats entity/attribute IDs; GUI attribute names are not required.
                candidates = []
                for i in range(len(table.columns)):
                    if i == vi:
                        continue
                    counts = Counter(
                        self.codec.key(w.values[i])
                        for w in relevant
                        if w.values[i] is not None
                    )
                    if any(v > 1 for v in counts.values()):
                        candidates.append(i)
                if len(candidates) > self.cfg.max_eav_columns:
                    self.notes.add("EAV key columns capped by --max-eav-columns.")
                    candidates = candidates[: self.cfg.max_eav_columns]
                for entity, attribute in itertools.permutations(candidates, 2):
                    if self.db.deadline.expired():
                        return
                    groups: dict[Key, dict[Key, list[Witness]]] = defaultdict(
                        lambda: defaultdict(list)
                    )
                    value_attrs: dict[Key, set[Key]] = defaultdict(set)
                    for w in relevant:
                        ek = identity_codec.key(w.values[entity])
                        ak = identity_codec.key(w.values[attribute])
                        vk = self.codec.key(w.values[vi])
                        if ek is not None and ak is not None:
                            groups[ek][ak].append(w)
                            value_attrs[vk].add(ak)
                    options = {}
                    for j in active:
                        sets = [
                            value_attrs[self.codec.key(r[j])]
                            for r in front.rows
                            if r[j] is not None
                        ]
                        common = set.intersection(*(set(s) for s in sets))
                        options[j] = sorted(common, key=repr)[
                            : self.cfg.candidates_per_column
                        ]
                    if any(not options[j] for j in active):
                        continue
                    for mapping in assignments(
                        options, self.cfg.max_mappings, self.db.deadline
                    ):
                        choices, proofs_by_entity = {}, {}
                        for r, row in enumerate(front.rows):
                            for ek, attrs in groups.items():
                                proofs = []
                                for j, ak in mapping.items():
                                    if row[j] is None:
                                        continue
                                    w = next(
                                        (
                                            w
                                            for w in attrs.get(ak, [])
                                            if self.codec.key(w.values[vi])
                                            == self.codec.key(row[j])
                                        ),
                                        None,
                                    )
                                    if w is None:
                                        break
                                    proofs.append(
                                        CellProof(
                                            r,
                                            j,
                                            name,
                                            table.columns[vi].name,
                                            dict(zip(table.locator, w.locator)),
                                            w.values[vi],
                                        )
                                    )
                                else:
                                    if proofs:
                                        choices.setdefault(r, []).append(ek)
                                        proofs_by_entity[(r, ek)] = proofs
                        matched = distinct_witnesses(choices)
                        if len(matched) < min(2, len(front.rows)):
                            continue
                        proofs = [
                            p
                            for r, ek in sorted(matched.items())
                            for p in proofs_by_entity[(r, ek)]
                        ]
                        descriptions = [
                            f"entity: {table.columns[entity].name}; attribute: "
                            f"{table.columns[attribute].name}; value: {table.columns[vi].name}"
                        ]
                        for j, ak in sorted(mapping.items()):
                            raw_attr = next(
                                w.values[attribute]
                                for w in relevant
                                if identity_codec.key(w.values[attribute]) == ak
                            )
                            descriptions.append(
                                f"{front.columns[j]} → attribute {shown(raw_attr)}"
                            )
                        self.add_finding(
                            Finding(
                                front.name,
                                "EAV",
                                [name],
                                front.cells,
                                proofs,
                                descriptions,
                                "Values for each GUI row share an entity identifier; "
                                "the attribute mapping is shared across all matched rows.",
                            ),
                            front,
                        )

    def declared_edges(self) -> list[Edge]:
        edges = []
        name_lookup = {name.casefold(): name for name in self.tables}
        for table in self.tables.values():
            for target, lefts, rights in table.foreign_keys:
                target = name_lookup.get(target.casefold(), "")
                if not target or target == table.name:
                    continue
                if any(c is None for c in rights):
                    rights = [
                        c.name
                        for c in sorted(
                            self.tables[target].columns, key=lambda c: c.pk_order
                        )
                        if c.pk_order
                    ]
                if len(lefts) == len(rights) and all(rights):
                    edges.append(Edge(table.name, target, tuple(zip(lefts, rights))))
        return edges

    def inferred_edges(self, names: set[str], existing: list[Edge]) -> list[Edge]:
        """A small candidate graph only; matching IDs do not prove relationship semantics."""
        fields = []
        if len(names) > 24:
            self.notes.add("Inferred joins limited to 24 tables containing hits.")
        for name in sorted(names)[:24]:
            witnesses = list(self.store.rows[name].values())
            for c, col in enumerate(self.tables[name].columns):
                values = [
                    self.codec.key(w.values[c])
                    for w in witnesses
                    if w.values[c] is not None
                ]
                keys = set(values) - self.store.targets.keys()
                if len(keys) >= 2 and len(keys) >= len(values) * 0.5:
                    fields.append((name, c, keys))
        postings: dict[Key, list[int]] = defaultdict(list)
        for i, (_, _, keys) in enumerate(fields):
            for key in keys:
                postings[key].append(i)
        counts: Counter = Counter()
        for locations in postings.values():
            if (
                len(locations) > 32
            ):  # Skip overly common values to avoid a combinatorial number of pairs.
                continue
            for a, b in itertools.combinations(locations, 2):
                if fields[a][0] != fields[b][0]:
                    counts[(a, b)] += 1
        result = []
        for (a, b), overlap in counts.most_common():
            if self.db.deadline.expired():
                break
            ln, lc, ls = fields[a]
            rn, rc, rs = fields[b]
            if overlap < 2 or overlap / min(len(ls), len(rs)) < 0.6:
                continue
            pair = (
                (self.tables[ln].columns[lc].name, self.tables[rn].columns[rc].name),
            )
            if any(
                e.left == ln and e.right == rn and e.pairs == pair for e in existing
            ):
                continue
            result.append(Edge(ln, rn, pair, True))
            if len(result) == 32:
                self.notes.add(
                    "Inferred relationships without foreign keys capped at 32."
                )
                break
        return result

    def joins(self, front: FrontTable) -> None:
        if self.cfg.max_join_hops == 0:
            return
        all_options = self.options(front.rows, list(self.tables))
        names = {ref[0] for refs in all_options.values() for ref in refs}
        if len(names) < 2:
            return
        edges = self.declared_edges()
        edges += self.inferred_edges(names, edges)
        graph: dict[str, list[Edge]] = defaultdict(list)
        for edge in edges:
            graph[edge.left].append(edge)
            graph[edge.right].append(edge)
        seen, paths, expanded = set(), 0, 0
        for start in sorted(names):
            queue = deque([([start], [])])
            while queue and not self.db.deadline.expired():
                nodes, path = queue.popleft()
                expanded += 1
                if (
                    paths >= self.cfg.max_join_paths
                    or expanded > self.cfg.max_join_paths * 20
                ):
                    self.notes.add(
                        "JOIN path search capped; increase --max-join-paths."
                    )
                    return
                if len(nodes) > 1 and nodes[-1] in names:
                    signature = tuple(sorted(repr(e) for e in path))
                    if signature not in seen:
                        seen.add(signature)
                        paths += 1
                        options = self.options(front.rows, nodes)
                        if options and all(options.values()):
                            count = 0
                            for mapping in assignments(
                                options, self.cfg.max_mappings, self.db.deadline
                            ):
                                if len({ref[0] for ref in mapping.values()}) < 2:
                                    continue
                                count += 1
                                self.verify(
                                    front,
                                    front.rows,
                                    lambda r, c: (r, c),
                                    nodes,
                                    path,
                                    mapping,
                                    "JOIN",
                                )
                            if count >= self.cfg.max_mappings:
                                self.notes.add(
                                    "Mapping limit reached during JOIN verification."
                                )
                if len(path) < self.cfg.max_join_hops:
                    for edge in graph[nodes[-1]]:
                        other = edge.other(nodes[-1])
                        if other not in nodes:
                            queue.append((nodes + [other], path + [edge]))


class ReportWriter:
    """One report model supplies HTML, plain text and machine-readable JSON."""

    LABELS = {
        "rows": "Shared rows",
        "transpose": "Transpose",
        "EAV": "Entity · attribute · value",
        "JOIN": "Joined tables",
    }

    def __init__(
        self,
        fronts: list[FrontTable],
        tables: dict[str, Table],
        store: EvidenceStore,
        matcher: StructureMatcher,
        coverage: list[Coverage],
        metadata: dict,
    ):
        self.fronts, self.tables, self.store = fronts, tables, store
        self.findings, self.coverage, self.metadata = (
            matcher.findings,
            coverage,
            metadata,
        )

    def locations(self, value: Value, limit: int = 8) -> tuple[list[dict], int]:
        key = self.store.codec.key(value)
        locations, count = [], 0
        if key is None:
            return locations, count
        for (name, c), keys in self.store.hits.items():
            locs = keys.get(key, ())
            count += len(locs)
            for loc in sorted(locs, key=repr):
                if len(locations) < limit:
                    locations.append(
                        {
                            "table": name,
                            "column": self.tables[name].columns[c].name,
                            "locator": dict(zip(self.tables[name].locator, loc)),
                            "actual": self.store.rows[name][loc].values[c],
                        }
                    )
        return locations, count

    def column_matches(self) -> list[dict]:
        result = []
        # IDF weights cover observed columns only, not the entire database.
        df = Counter(k for keys in self.store.hits.values() for k in keys)
        column_count = max(1, len(self.store.hits))
        for front in self.fronts:
            for j, label in enumerate(front.columns):
                keys = {
                    self.store.codec.key(r[j]) for r in front.rows if r[j] is not None
                }
                if not keys:
                    continue
                weight = {
                    k: 1 + math.log((column_count + 1) / (df[k] + 1)) for k in keys
                }
                candidates = []
                for ref, hits in self.store.hits.items():
                    overlap = keys & hits.keys()
                    if overlap:
                        candidates.append(
                            {
                                "frontend": front.name,
                                "frontend_column": label,
                                "table": ref[0],
                                "column": self.tables[ref[0]].columns[ref[1]].name,
                                "hits": len(overlap),
                                "distinct": len(keys),
                                "containment": len(overlap) / len(keys),
                                "weighted_score": sum(weight[k] for k in overlap)
                                / sum(weight.values()),
                            }
                        )
                candidates.sort(
                    key=lambda c: (-c["weighted_score"], c["table"], c["column"])
                )
                result.extend(
                    candidates[: self.metadata["config"]["candidates_per_column"]]
                )
        return result

    def payload(self) -> dict:
        fronts = []
        for front in self.fronts:
            cells = []
            for r, row in enumerate(front.rows):
                for c, value in enumerate(row):
                    locations, total = self.locations(value)
                    cells.append(
                        {
                            "row": r + 1,
                            "column": c + 1,
                            "value": value,
                            "status": (
                                "NULL excluded"
                                if value is None
                                else (
                                    "value found"
                                    if locations
                                    else "not found within budget"
                                )
                            ),
                            "retained_location_count": total,
                            "locations": locations,
                        }
                    )
            fronts.append(
                {
                    "name": front.name,
                    "columns": front.columns,
                    "rows": front.rows,
                    "cells": cells,
                }
            )
        return {
            "metadata": self.metadata,
            "frontends": fronts,
            "findings": [
                dict(asdict(f), matched_cells=f.matched_cells, full=f.full)
                for f in self.findings
            ],
            "column_matches": self.column_matches(),
            "coverage": [asdict(c) for c in self.coverage],
        }

    def text(self, payload: dict) -> str:
        m = self.metadata
        out = [
            "SQLite Locator",
            "=" * 70,
            f'Source: {m["source"]}',
            f'Frontend: {m["frontend"]}',
            f'Mode: {m["config"]["mode"]}; comparison: {m["config"]["comparison"]}',
            f'Tables: {len(self.tables)}; source queries: {m["source_queries"]}; '
            f'search time: {m["elapsed_seconds"]:.2f} s',
            "",
            "Sample agreement does not prove the historical origin of the GUI data.",
            "NULL provides no evidence. 100% means non-NULL sample agreement, not a probability.",
            "A miss in a bounded search means: NOT DETERMINED.",
            "",
        ]
        out += ["NOTES:"] + ["- " + n for n in m["notes"]]
        for i, f in enumerate(self.findings, 1):
            front = next(front for front in self.fronts if front.name == f.frontend)
            out += [
                "",
                "=" * 70,
                f"{i}. {f.frontend} | {self.LABELS[f.kind]} | "
                f"{f.matched_cells}/{f.total_cells} cells",
                f.note,
            ]
            out += f.mapping
            for p in sorted(f.proofs, key=lambda p: (p.row, p.column)):
                loc = ", ".join(k + "=" + shown(v) for k, v in p.locator.items())
                out.append(
                    f"  GUI [{p.row + 1}, {front.columns[p.column]}] {shown(front.rows[p.row][p.column])}"
                    f" -> {p.table}.{p.source_column} [{loc}] = {shown(p.actual)}"
                )
            if f.relation_sql:
                out += [
                    "",
                    "Relationship template (without sample filters):",
                    f.relation_sql,
                ]
            out += [
                "",
                "SQL replaying the verified complete sample rows:",
                f.replay_sql,
            ]
        out += ["", "=" * 70, "INDIVIDUAL VALUES — do not establish a shared row"]
        for front in payload["frontends"]:
            out.append(front["name"])
            for cell in front["cells"]:
                out.append(
                    f'  [{cell["row"]}, {front["columns"][cell["column"] - 1]}] '
                    f'{shown(cell["value"])}: {cell["status"]}'
                )
                for loc in cell["locations"]:
                    out.append(
                        f'    {loc["table"]}.{loc["column"]}, {loc["locator"]!r}'
                    )
        out += ["", "SEARCH COVERAGE"]
        for c in self.coverage:
            out.append(
                f"{c.table}: sample={c.sample_rows}, index probes={c.index_probes}, "
                f"retained rows={c.retained_rows}, full scan={c.full_scan_finished}, "
                f"interruptions={c.interrupted}, truncated probes={c.truncated_probes}"
            )
            out += ["  " + n for n in c.notes]
        return "\n".join(out) + "\n"

    def html(self, payload: dict) -> str:
        def esc(value: Value) -> str:
            return html.escape(shown(value, 500))

        def grid(headers: list[str], rows: list[list[str]], name: str = "") -> str:
            return (
                '<div class="scroll"><table'
                + (' id="' + name + '"' if name else "")
                + "><thead><tr>"
                + "".join("<th>" + esc(h) + "</th>" for h in headers)
                + "</tr></thead><tbody>"
                + "".join(
                    "<tr>" + "".join("<td>" + cell + "</td>" for cell in row) + "</tr>"
                    for row in rows
                )
                + "</tbody></table></div>"
            )

        m = self.metadata
        total = sum(f.cells for f in self.fronts)
        located = sum(
            bool(self.locations(v)[0])
            for f in self.fronts
            for row in f.rows
            for v in row
            if v is not None
        )
        complete = sum(f.full for f in self.findings)
        retained = sum(c.retained_rows > 0 for c in self.coverage)
        notices = m.get("operational_notes", [])
        status = "Partial results" if notices else "Analysis complete"
        scope = (
            "Indexes and samples"
            if m["config"]["mode"] == "bounded"
            else "Full scans with limits"
        )
        parts = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>SQLite Locator — analysis report</title>
<style>
:root{{--ink:#182e2b;--muted:#62736e;--line:#dce4df;--paper:#f4f6f4;--green:#17664f;--light:#e7f4ec;--amber:#8a6217}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:14px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}}
header{{background:#123b30;color:white;padding:22px max(24px,calc((100vw - 1280px)/2));display:flex;justify-content:space-between;gap:20px;align-items:center}}
.brand{{font-size:21px;font-weight:750;letter-spacing:-.4px}}header small{{display:block;color:#bed9ce;font-size:12px}}.run-date{{text-align:right;color:#d0e7dc;font-size:12px}}
nav{{position:sticky;top:0;z-index:3;background:#fff;border-bottom:1px solid var(--line);padding:13px 24px;display:flex;justify-content:center;gap:28px;flex-wrap:wrap}}
a{{color:var(--green);text-decoration:none}}nav a{{font-weight:650}}a:hover{{text-decoration:underline}}main{{max-width:1328px;padding:28px 24px 54px;margin:auto}}
h1{{font-size:28px;letter-spacing:-.5px;line-height:1.2;margin:0 0 6px}}h2{{font-size:22px;margin:0;letter-spacing:-.4px}}h3{{font-size:17px;margin:4px 0 8px}}p{{margin:8px 0}}.muted,small{{color:var(--muted)}}
section{{margin:32px 0;scroll-margin-top:80px}}.section-head,.card-head,.overview-head{{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:16px}}
.metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:22px 0}}.metric,.card{{background:white;border:1px solid var(--line);border-radius:10px}}
.metric{{padding:20px 22px}}.metric b{{display:block;font-size:30px;font-variant-numeric:tabular-nums;font-weight:700;line-height:1.3}}.metric span{{color:var(--muted);font-size:12px}}
.card{{padding:22px;margin:14px 0}}.status,.pill{{font-size:11px;display:inline-block;padding:4px 9px;border-radius:5px;font-weight:650;background:#edf0ed;color:#53645a}}
.ok{{background:var(--light);color:var(--green)}}.warn{{background:#fff3d8;color:var(--amber)}}.source{{background:#eaf0ec;border-radius:6px;padding:12px 16px;display:flex;gap:22px;flex-wrap:wrap;font-size:12px}}
.filterbar{{display:grid;grid-template-columns:2fr 1fr 1fr;gap:12px;margin:18px 0}}label{{font-size:12px;color:var(--muted)}}input,select{{display:block;width:100%;margin-top:5px;border:1px solid #c6d2c9;border-radius:6px;padding:10px 12px;background:white;color:var(--ink);font:inherit}}
input:focus,select:focus,button:focus{{outline:2px solid #3c987a;outline-offset:2px}}.counter{{color:var(--muted);font-size:12px}}.score{{font-weight:750;font-size:22px;white-space:nowrap}}.score small{{font-size:12px;font-weight:500}}
.sources{{font:12px/1.7 ui-monospace,SFMono-Regular,Consolas,monospace;color:var(--muted);overflow-wrap:anywhere}}.mapping{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:16px 0}}
.mapping>div{{padding:10px 12px;background:#f6f8f6;border-radius:5px;overflow-wrap:anywhere}}.mapping strong{{font-weight:600;font-size:12px}}.mapping code{{display:block;margin-top:2px}}
.scroll{{overflow:auto;max-height:480px;border:1px solid var(--line);border-radius:6px;margin:12px 0}}table{{border-collapse:collapse;width:100%;text-align:left;font-size:12px}}th,td{{padding:11px 12px;border-bottom:1px solid var(--line);vertical-align:top}}th{{background:#edf2ee;position:sticky;top:0;white-space:nowrap;font-weight:600}}tr:last-child td{{border-bottom:0}}td small{{display:block;margin-top:3px}}code{{font:11px/1.65 ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}}
summary{{cursor:pointer;padding:10px 0;font-size:12px;font-weight:650;color:var(--green)}}details{{border-top:1px solid var(--line);margin-top:12px}}details details{{border:0}}pre{{padding:18px;background:#142e26;color:#e0efe7;font:12px/1.65 ui-monospace,monospace;overflow:auto;max-height:400px;border-radius:6px}}
button{{padding:7px 12px;border:1px solid #b9cdbf;border-radius:5px;background:white;color:var(--green);cursor:pointer;font:600 12px system-ui}}.sqlbar{{display:flex;align-items:center;justify-content:space-between;gap:12px}}
.found{{border-left:3px solid #77b495;padding-left:8px}}.unknown{{border-left:3px solid #d6aa4c;padding-left:8px}}.null{{color:#87928a}}.note{{font-size:12px;color:var(--muted)}}.notice{{background:#fff7e9;border:1px solid #ead4a7;padding:12px 16px;border-radius:6px;margin:14px 0}}
.empty{{padding:24px;border:1px dashed #b6c7ba;border-radius:8px;color:var(--muted)}}footer{{border-top:1px solid var(--line);padding-top:20px;color:var(--muted);font-size:11px}}[hidden]{{display:none!important}}
@media(max-width:760px){{.metrics{{grid-template-columns:repeat(2,1fr)}}.filterbar{{grid-template-columns:1fr}}.mapping{{grid-template-columns:1fr}}main{{padding:20px 14px}}.card{{padding:16px}}nav{{gap:16px;font-size:12px;padding:10px}}header{{padding:18px 16px;align-items:start}}.run-date{{font-size:10px;max-width:110px}}}}
@media print{{nav,.filterbar,button,input{{display:none}}body{{background:white}}header{{background:white;color:black;padding:10px}}header small,.run-date{{color:#555}}.scroll,pre{{max-height:none;overflow:visible}}.card{{break-inside:avoid}}}}
</style></head><body><header><div><div class="brand">SQLite Locator</div><small>Data analysis report</small></div><div class="run-date">{esc(m['created_at'])}<br>v{esc(m.get('version', VERSION))}</div></header>
<nav aria-label="Report navigation"><a href="#results">Matches</a><a href="#sample">Sample</a><a href="#locations">Value locations</a><a href="#coverage">Analysis coverage</a></nav><main>
<div class="overview-head"><div><h1>Search results</h1><span class="muted">{esc(Path(m['source']).name)}</span></div><span class="status {'warn' if notices else 'ok'}">{status}</span></div>
<div class="metrics"><div class="metric"><b>{complete}</b><span>complete sample matches</span></div><div class="metric"><b>{located}/{total}</b><span>cells with a located value</span></div><div class="metric"><b>{retained} / {len(self.tables)}</b><span>tables with hits / analyzed</span></div><div class="metric"><b>{m['elapsed_seconds']:.2f} s</b><span>search time</span></div></div>
<div class="source"><span>Source: <strong>{esc(Path(m['source']).name)}</strong></span><span>Sample: <strong>{esc(Path(m['frontend']).name)}</strong></span><span>Coverage: <strong>{scope}</strong></span></div>
<p class="note">Matches apply to the sample. A miss within a limited search does not establish absence. NULL cells are excluded.</p>
<section id="results"><div class="section-head"><h2>Matches</h2><span class="counter" id="count">{len(self.findings)} results</span></div>
<div class="filterbar"><label>Search tables or columns<input id="search" type="search" placeholder="Table, column or field name" autocomplete="off"></label>
<label>Sample<select id="front-filter"><option value="">All samples</option>"""]
        for front in self.fronts:
            parts.append(
                '<option value="'
                + html.escape(front.name, quote=True)
                + '">'
                + esc(front.name)
                + "</option>"
            )
        parts.append(
            '</select></label><label>Layout<select id="kind-filter"><option value="">All layouts</option>'
        )
        for kind, label in self.LABELS.items():
            parts.append(
                '<option value="' + esc(kind) + '">' + esc(label) + "</option>"
            )
        parts.append(
            '</select></label></div><div class="empty" id="no-results"'
            + (" hidden" if self.findings else "")
            + ">No matches for the current filters.</div>"
        )
        for index, f in enumerate(self.findings, 1):
            front = next(x for x in self.fronts if x.name == f.frontend)
            parts.append(
                '<article class="card finding" data-front="'
                + html.escape(f.frontend, quote=True)
                + '" data-kind="'
                + esc(f.kind)
                + f'" id="result-{index}"><div class="card-head"><div><span class="pill '
                + ("ok" if f.full else "warn")
                + '">'
                + ("Complete sample match" if f.full else "Partial sample match")
                + "</span>"
                + "<h3>"
                + esc(f.frontend)
                + " · "
                + esc(self.LABELS[f.kind])
                + "</h3></div>"
                + f'<div class="score">{f.matched_cells}/{f.total_cells} <small>cells</small></div></div>'
                + '<div class="sources">'
                + " &nbsp; ↔ &nbsp; ".join(esc(t) for t in f.tables)
                + "</div>"
            )
            if f.kind == "JOIN":
                parts.append(
                    '<p><span class="pill">'
                    + (
                        "Relationship inferred from values"
                        if "inferred" in f.note
                        else "Declared foreign-key relationship"
                    )
                    + "</span></p>"
                )
            parts.append('<div class="mapping">')
            for description in f.mapping:
                label, _, target = description.partition(" → ")
                parts.append(
                    "<div><strong>"
                    + esc(label)
                    + "</strong><code>"
                    + esc(target)
                    + "</code></div>"
                )
            parts.append("</div>")
            proofs = []
            for p in sorted(f.proofs, key=lambda p: (p.row, p.column)):
                locator = ", ".join(k + "=" + shown(v) for k, v in p.locator.items())
                proofs.append(
                    [
                        str(p.row + 1),
                        esc(front.columns[p.column]),
                        esc(front.rows[p.row][p.column]),
                        esc(p.actual)
                        + "<small>"
                        + esc(type(p.actual).__name__)
                        + "</small>",
                        "<code>"
                        + esc(p.table)
                        + "<br>"
                        + esc(p.source_column)
                        + "</code>",
                        "<code>" + esc(locator) + "</code>",
                    ]
                )
            parts.append(
                "<details><summary>Values and source records</summary>"
                + grid(
                    [
                        "GUI row",
                        "GUI field",
                        "GUI value",
                        "Database value",
                        "Table / column",
                        "Record",
                    ],
                    proofs,
                )
                + "</details>"
            )
            parts.append(
                '<details><summary>Sample SQL</summary><div class="sqlbar"><span class="note">Verified rows in GUI order.</span>'
                + f'<button type="button" data-copy="sql-{index}">Copy SQL</button></div><pre id="sql-{index}">'
                + html.escape(f.replay_sql)
                + "</pre></details>"
            )
            if f.relation_sql:
                parts.append(
                    '<details><summary>Join condition</summary><p class="note">Relationship template, without sample filters.</p><pre>'
                    + html.escape(f.relation_sql)
                    + "</pre></details>"
                )
            parts.append("</article>")
        parts.append(
            '</section><section id="sample"><div class="section-head"><h2>Input sample</h2><span class="note">Green: value found · amber: not determined</span></div>'
        )
        for front in self.fronts:
            rows = []
            for r, row in enumerate(front.rows):
                cells = [str(r + 1)]
                for value in row:
                    _, n = self.locations(value)
                    cls = "null" if value is None else "found" if n else "unknown"
                    cells.append('<div class="' + cls + '">' + esc(value) + "</div>")
                rows.append(cells)
            parts.append(
                '<article class="card"><h3>'
                + esc(front.name)
                + "</h3>"
                + grid(["#"] + front.columns, rows)
                + "</article>"
            )
        parts.append('</section><section id="locations"><h2>Value locations</h2>')
        for front in payload["frontends"]:
            rows = []
            for cell in front["cells"]:
                locations = []
                for loc in cell["locations"]:
                    identity = ", ".join(
                        k + "=" + shown(v) for k, v in loc["locator"].items()
                    )
                    locations.append(
                        "<code>"
                        + esc(loc["table"])
                        + "."
                        + esc(loc["column"])
                        + "</code><small>"
                        + esc(identity)
                        + "</small>"
                    )
                state = (
                    "NULL — excluded"
                    if cell["value"] is None
                    else "Found" if locations else "Not determined"
                )
                rows.append(
                    [
                        str(cell["row"]),
                        esc(front["columns"][cell["column"] - 1]),
                        esc(cell["value"]),
                        state,
                        "<br>".join(locations) or "—",
                    ]
                )
            parts.append(
                '<div class="card"><h3>'
                + esc(front["name"])
                + '</h3><details><summary>Open cell locations</summary><p class="note">Up to 8 retained locations per cell.</p>'
                + grid(["GUI row", "GUI field", "Value", "Status", "Locations"], rows)
                + "</details></div>"
            )
        matches = [
            [
                esc(x["frontend"]) + "<small>" + esc(x["frontend_column"]) + "</small>",
                "<code>" + esc(x["table"]) + "</code>",
                "<code>" + esc(x["column"]) + "</code>",
                f'{x["hits"]}/{x["distinct"]}',
            ]
            for x in payload["column_matches"]
        ]
        parts.append(
            "<details><summary>Individual field matches</summary>"
            + grid(["Sample / field", "Table", "Column", "Distinct values"], matches)
            + "</details></section>"
        )
        parts.append('<section id="coverage"><h2>Analysis coverage</h2>')
        for note in notices:
            parts.append('<div class="notice">' + esc(note) + "</div>")
        parts.append(
            '<div class="card"><div class="source"><span>Tables: <strong>'
            + str(len(self.coverage))
            + "</strong></span><span>Queries: <strong>"
            + str(m["source_queries"])
            + "</strong></span><span>Interrupted / truncated queries: <strong>"
            + str(m["interrupted_queries"])
            + '</strong></span></div><details><summary>Coverage by table</summary><label>Filter tables<input id="coverage-search" type="search" placeholder="Table name"></label>'
        )
        coverage_rows = []
        for c in sorted(self.coverage, key=lambda x: (-x.retained_rows, x.table)):
            state = (
                "Full scan complete"
                if c.full_scan_finished
                else (
                    "Interrupted"
                    if c.interrupted
                    else (
                        "Indexes / sample"
                        if c.sample_rows or c.index_probes
                        else "Not checked"
                    )
                )
            )
            coverage_rows.append(
                [
                    "<code>" + esc(c.table) + "</code>",
                    str(c.columns),
                    str(c.retained_rows),
                    str(c.sample_rows),
                    str(c.index_probes),
                    state,
                ]
            )
        parts.append(
            grid(
                [
                    "Table",
                    "Columns",
                    "Rows with hits",
                    "Sample rows",
                    "Probes",
                    "Coverage",
                ],
                coverage_rows,
                "coverage-table",
            )
            + '</details><details><summary>Input files</summary><p class="sources">'
            + esc(m["source"])
            + "<br>"
            + esc(m["frontend"])
            + "</p></details></div></section>"
        )
        parts.append(
            """<footer>SQLite Locator · Local report · Read-only source access · Diagnostics and settings in TXT / JSON files</footer></main><script>
const cards=Array.from(document.querySelectorAll('.finding'));
function filterResults(){const q=document.getElementById('search').value.toLocaleLowerCase('en'),front=document.getElementById('front-filter').value,kind=document.getElementById('kind-filter').value;let count=0;cards.forEach(card=>{card.hidden=!!((q&&!card.textContent.toLocaleLowerCase('en').includes(q))||(front&&card.dataset.front!==front)||(kind&&card.dataset.kind!==kind));if(!card.hidden)count++;});document.getElementById('count').textContent=count+' / '+cards.length+' results';document.getElementById('no-results').hidden=count>0;}
['search','front-filter','kind-filter'].forEach(id=>document.getElementById(id).addEventListener('input',filterResults));
document.getElementById('coverage-search').addEventListener('input',function(){const q=this.value.toLowerCase();document.querySelectorAll('#coverage-table tbody tr').forEach(row=>row.hidden=!row.cells[0].textContent.toLowerCase().includes(q));});
document.querySelectorAll('[data-copy]').forEach(button=>button.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(document.getElementById(button.dataset.copy).textContent);button.textContent='Copied';}catch(e){button.textContent='Select the SQL and copy manually';}}));
</script></body></html>"""
        )
        return "".join(parts)

    def write(self, path: Path) -> list[Path]:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.payload()
        files = [
            (path.with_suffix(".html"), self.html(payload)),
            (path.with_suffix(".txt"), self.text(payload)),
            (
                path.with_suffix(".json"),
                json.dumps(
                    json_tree(payload), ensure_ascii=False, indent=2, allow_nan=False
                ),
            ),
        ]
        for destination, content in files:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=destination.parent,
                    prefix="." + destination.name + ".",
                    delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(content)
                temporary.replace(destination)
            finally:
                if temporary and temporary.exists():
                    temporary.unlink()
        return [f for f, _ in files]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--version", action="version", version="SQLite Locator " + VERSION
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress progress messages on stderr."
    )
    parser.add_argument(
        "--source",
        required=True,
        type=Path,
        help="Source SQLite database, opened read-only.",
    )
    parser.add_argument(
        "--frontend",
        required=True,
        type=Path,
        help="SQLite database containing GUI samples, opened read-only.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/report.html"),
        help="Report path; writes HTML, TXT and JSON. Default: reports/report.html.",
    )
    parser.add_argument(
        "--frontend-table",
        action="append",
        help="Select a GUI table; repeatable. Default: all tables.",
    )
    parser.add_argument(
        "--table-pattern",
        action="append",
        help="Optional source table glob, such as archive_*; repeatable.",
    )
    parser.add_argument(
        "--mode",
        choices=["bounded", "exhaustive"],
        default="bounded",
        help="bounded: indexes and samples; exhaustive: allows full scans, still subject to limits.",
    )
    parser.add_argument(
        "--comparison",
        choices=["numeric", "strict"],
        default="numeric",
        help="numeric matches numbers to simple numeric text; strict keeps text distinct.",
    )
    defaults = Config()
    help_text = {
        "sample_rows": "Combined size of samples from the beginning and end of each table.",
        "random_seeks": "Additional rowid seeks; up to 4 rows per seek, without random sorting.",
        "witnesses": "Maximum retained locations per value/column and rows per index probe.",
        "max_evidence_rows": "Global limit on retained rows containing evidence.",
        "max_evidence_bytes": "Approximate evidence payload limit, excluding indexes and interpreter overhead.",
        "max_row_bytes": "SQLite limit on the size of a single row/value, in bytes.",
        "max_hit_rows": "Maximum matching rows returned by a scan of one table.",
        "max_tables": "Maximum number of catalog tables to read.",
        "max_columns": "Wider tables are skipped and reported.",
        "max_frontend_rows": "Maximum rows per GUI sample; exceeding the limit is an error.",
        "max_frontend_cells": "Maximum total number of frontend cells.",
        "max_value_bytes": "Retained value size limit: text characters or BLOB bytes.",
        "candidates_per_column": "Maximum candidate columns per layout position.",
        "max_mappings": "Maximum alternative mappings for each table layout.",
        "max_join_hops": "Maximum JOIN path length in edges; 0 disables joins.",
        "max_join_paths": "Maximum JOIN paths evaluated per GUI sample.",
        "max_eav_columns": "Maximum candidate entity and attribute columns for EAV.",
        "max_findings": "Maximum structures included in the report.",
        "min_containment": "Minimum observed column-value coverage required for structure matching.",
        "query_seconds": "Approximate time limit per query, in seconds.",
        "query_vm_steps": "Approximate SQLite VM instruction limit per query, checked every 1000 steps.",
        "total_seconds": "Search time budget in seconds, excluding report writing.",
        "seed": "Seed for reproducible sampling.",
    }
    for name, help_message in help_text.items():
        default = getattr(defaults, name)
        parser.add_argument(
            "--" + name.replace("_", "-"),
            type=type(default),
            default=default,
            help=f"{help_message} Default: {default}.",
        )
    args = parser.parse_args(argv)
    for name in help_text:
        value = getattr(args, name)
        if name == "seed":
            continue
        zero_allowed = name in ("random_seeks", "sample_rows", "max_join_hops")
        if (
            value < 0
            or (not zero_allowed and value == 0)
            or (isinstance(value, float) and not math.isfinite(value))
        ):
            parser.error("--" + name.replace("_", "-") + ": invalid value.")
    if not 0 < args.min_containment <= 1:
        parser.error("--min-containment must be in (0, 1].")
    if args.max_frontend_cells > 800:
        parser.error(
            "This tool supports small samples: --max-frontend-cells cannot exceed 800."
        )
    protected = {args.source.resolve(), args.frontend.resolve()}
    if len(protected) != 2:
        parser.error("Frontend and source must be different files.")
    for suffix in (".html", ".txt", ".json"):
        destination = args.report.with_suffix(suffix).resolve()
        if (
            destination in protected
            or destination.with_name(destination.name + ".tmp") in protected
        ):
            parser.error("A report must not overwrite an input database.")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    config = Config(
        **{name: getattr(args, name) for name in Config.__dataclass_fields__}
    )
    deadline = Deadline(config.total_seconds)
    connections = []
    try:
        frontend_db = SQLiteReader(args.frontend, config, deadline)
        connections.append(frontend_db)
        fronts = load_frontend(frontend_db, args.frontend_table)
        source = SQLiteReader(args.source, config, deadline)
        connections.append(source)
        catalog = Catalog(source)
        tables = catalog.read(args.table_pattern)
        codec = ValueCodec(config.comparison)
        store = EvidenceStore(codec, fronts, config)
        probe = ProbeEngine(source, store)
        probe.run(tables)
        matcher = StructureMatcher(source, tables, store)
        matcher.run(fronts)
        notes = list(catalog.notes) + sorted(matcher.notes)
        if deadline.expired():
            notes.append("Global time limit reached. Results are partial.")
        if store.capped:
            notes.append(
                "Evidence retention was capped by witnesses / max-evidence-rows / max-evidence-bytes. "
                "Additional occurrences and some structures may have been omitted."
            )
        if store.oversized:
            notes.append(
                "Rows containing values larger than --max-value-bytes were skipped."
            )
        operational_notes = list(notes)
        notes.extend(
            [
                "bounded checks indexes and limited samples; exhaustive allows full scans. "
                "Interruptions, limits and missing indexes restrict completeness but do not invalidate retained evidence.",
                "SQLite uses dynamic typing. Columns are not excluded solely by their declared types, "
                "and optimizer statistics are not treated as proof that values are absent.",
                "In numeric mode, a number and its simple text representation can match. Index probes "
                "do not enumerate all possible text representations; 001, whitespace, commas, dates and letter case are not normalized.",
                "NULL is excluded as missing evidence. A 100% match covers the remaining cells only. "
                "Column coverage counts distinct values; structure matching accounts for duplicate rows.",
                "Supported structures: one table in either orientation, EAV and simple JOIN paths of up to "
                f"{config.max_join_hops} edges. No aggregates, text transformations, JSON expansion, self-joins, "
                "arbitrary join graphs or proof of historical data provenance.",
                "Structure verification uses retained evidence. Even a completed full scan cannot guarantee "
                "all structures are found when witness and mapping limits apply.",
                "Database connections are read-only. No indexes or statistics are created in the source. "
                "Rowid sampling is biased by key distribution and does not provide probability estimates.",
            ]
        )
        metadata = {
            "version": VERSION,
            "source": str(args.source.resolve()),
            "frontend": str(args.frontend.resolve()),
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "elapsed_seconds": round(time.monotonic() - deadline.started, 4),
            "source_queries": source.queries,
            "returned_rows": source.returned_rows,
            "interrupted_queries": source.interruptions,
            "query_errors": source.errors,
            "config": asdict(config),
            "notes": notes,
            "operational_notes": operational_notes,
            "evidence_payload_bytes": store.payload_bytes,
        }
        writer = ReportWriter(fronts, tables, store, matcher, probe.coverage, metadata)
        paths = writer.write(args.report)
        print(
            f"Found {len(matcher.findings)} layouts, including "
            f"{sum(f.full for f in matcher.findings)} complete non-NULL sample matches."
        )
        for path in paths:
            print(path.resolve())
        return 0
    except (ValueError, sqlite3.Error, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        LOGGER.warning("Operation interrupted. Input databases were not modified.")
        return 130
    finally:
        for connection in connections:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
