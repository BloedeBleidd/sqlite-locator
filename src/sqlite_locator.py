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
import heapq
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
    max_tables: int = 0
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
    collation: str = "BINARY"

    @property
    def index_expression(self) -> str:
        return qi(self.name) + " COLLATE " + self.collation


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

    def select_sql(self, index: str | None = None) -> str:
        hint = " INDEXED BY " + qi(index) if index else ""
        return "SELECT " + self.select_list() + " FROM " + qi(self.name) + hint


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
    started: bool = False
    empty: bool = False
    all_rows_sampled: bool = False
    sample_rows: int = 0
    index_probes: int = 0
    entity_probes: int = 0
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
        consume: Callable[[tuple], bool] | None = None,
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
                    if not consume(row):
                        result.complete, result.reason = (
                            False,
                            "evidence capacity reached",
                        )
                        break
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
    BATCH_SIZE = 128

    def __init__(self, db: SQLiteReader):
        self.db = db
        self.notes: list[str] = []

    def read(
        self, patterns: list[str] | None = None, *, limit: int = 0
    ) -> dict[str, Table]:
        tables = {}
        after = None
        selected = 0
        while not self.db.deadline.expired():
            lower_bound = "" if after is None else " AND rowid>?"
            result = self.db.run(
                "SELECT rowid, name, sql FROM sqlite_schema WHERE type='table' "
                "AND substr(name,1,7)<>'sqlite_'"
                + lower_bound
                + " ORDER BY rowid LIMIT ?",
                [self.BATCH_SIZE] if after is None else [after, self.BATCH_SIZE],
                limit=self.BATCH_SIZE,
            )
            batch = {}
            limited = False
            for after, name, ddl in result.rows:
                if patterns and not any(fnmatch.fnmatchcase(name, p) for p in patterns):
                    continue
                if re.search(r"^\s*CREATE\s+VIRTUAL\s+TABLE\b", ddl or "", re.I):
                    self.notes.append(f"{name}: virtual table skipped.")
                    continue
                if limit and selected >= limit:
                    limited = True
                    break
                batch[name] = ddl
                selected += 1
            if batch:
                tables.update(self.read_batch(batch))
            if limited:
                self.notes.append(
                    f"Catalog stopped at --max-tables={limit}. Additional matching tables were not inspected."
                )
                break
            if not result.complete:
                self.notes.append("Catalog read incomplete: " + result.reason)
                break
            if len(result.rows) < self.BATCH_SIZE:
                break
        if self.db.deadline.expired():
            self.notes.append(
                "Time limit reached while reading the source or frontend schema."
            )
        return tables

    def read_batch(self, batch: dict[str, str]) -> dict[str, Table]:
        cfg = self.db.config
        names = list(batch)
        prefix = "WITH batch(name) AS (VALUES " + ",".join("(?)" for _ in names) + ") "
        columns_result = self.db.run(
            prefix + "SELECT batch.name, c.cid, c.name, c.type, c.pk, c.hidden "
            "FROM batch CROSS JOIN pragma_table_xinfo(batch.name) AS c "
            "WHERE c.cid<=?",
            [*names, cfg.max_columns],
            limit=len(names) * (cfg.max_columns + 1),
        )
        if not columns_result.complete:
            if len(names) > 1 and not self.db.deadline.expired():
                middle = len(names) // 2
                return self.read_batch(
                    {n: batch[n] for n in names[:middle]}
                ) | self.read_batch({n: batch[n] for n in names[middle:]})
            self.notes.append(
                f"Column metadata incomplete for a batch of {len(names)} tables "
                f"starting at {names[0]}: {columns_result.reason}. Batch skipped."
            )
            return {}
        column_rows: dict[str, list[tuple]] = defaultdict(list)
        for name, *info in columns_result.rows:
            column_rows[name].append(info)
        tables = {}
        for name, ddl in batch.items():
            info = column_rows[name]
            if len(info) > cfg.max_columns:
                self.notes.append(f"{name}: exceeds --max-columns; skipped.")
                continue
            columns = [
                Column(n, t or "", pk)
                for _, n, t, pk, hidden in sorted(info)
                if hidden != 1
            ]
            if not columns:
                continue
            column_names = {c.name.casefold() for c in columns}
            without = bool(re.search(r"\bWITHOUT\s+ROWID\b", ddl or "", re.I))
            rowid = next(
                (x for x in ["_rowid_", "rowid", "oid"] if x not in column_names), None
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
            # INTEGER PRIMARY KEY DESC may have its own index; verify the plan later.
            if len(primary) == 1 and rowid:
                for c in columns:
                    if c.pk_order and c.declared_type.upper() == "INTEGER":
                        c.index = ""
            tables[name] = Table(name, columns, locator, rowid)
        if not tables:
            return tables

        indices = self.db.run(
            prefix + "SELECT batch.name, i.name, x.name, x.coll FROM batch "
            "CROSS JOIN pragma_index_list(batch.name) AS i "
            "CROSS JOIN pragma_index_xinfo(i.name) AS x "
            'WHERE i.partial=0 AND x.seqno=0 AND x."key"=1',
            names,
        )
        if not indices.complete:
            self.notes.append(
                f"Incomplete index metadata starting at {names[0]}: {indices.reason}."
            )
        columns_by_name = {
            (table.name, c.name): c for table in tables.values() for c in table.columns
        }
        for name, index_name, leading, collation in indices.rows:
            col = columns_by_name.get((name, leading))
            collation = (collation or "").upper()
            if col is None or collation not in ("BINARY", "NOCASE", "RTRIM"):
                continue
            if col.index in (None, "") or (
                collation == "BINARY" and col.collation != "BINARY"
            ):
                col.index, col.collation = index_name, collation

        if cfg.max_join_hops:
            fks = self.db.run(
                prefix + 'SELECT batch.name, f.id, f.seq, f."table", f."from", f."to" '
                "FROM batch CROSS JOIN pragma_foreign_key_list(batch.name) AS f",
                names,
            )
            if not fks.complete:
                self.notes.append(
                    f"Incomplete foreign-key metadata starting at {names[0]}: {fks.reason}. "
                    "Foreign keys in this batch were skipped."
                )
            else:
                groups: dict[tuple[str, int], list[tuple]] = defaultdict(list)
                for name, fk_id, seq, target, left, right in fks.rows:
                    if name in tables:
                        groups[(name, fk_id)].append((seq, target, left, right))
                for (name, _), rows in groups.items():
                    ordered = sorted(rows)
                    tables[name].foreign_keys.append(
                        (
                            ordered[0][1],
                            [r[2] for r in ordered],
                            [r[3] for r in ordered],
                        )
                    )
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
        self.target_frequency: Counter[Key] = Counter()
        self.frontend_keys: list[set[Key]] = []
        for front in fronts:
            keys: set[Key] = set()
            for row in front.rows:
                for value in row:
                    key = codec.key(value)
                    if key is not None:
                        self.targets.setdefault(key, value)
                        self.target_frequency[key] += 1
                        keys.add(key)
            self.frontend_keys.append(keys)
        self.rows: dict[str, dict[tuple, Witness]] = defaultdict(dict)
        self.hits: dict[Ref, dict[Key, set[tuple]]] = defaultdict(
            lambda: defaultdict(set)
        )
        self.references: dict[Key, set[Ref]] = defaultdict(set)
        self.count = 0
        self.payload_bytes = 0
        self.occurrences_limited = False
        self.capacity_limit: str | None = None
        self.oversized = False

    def add(self, table: Table, raw: tuple, *, entity_match: bool = False) -> bool:
        """Admit a witness and index all of its matching cells."""
        if self.capacity_limit:
            return False
        n = len(table.locator)
        loc, values = tuple(raw[:n]), tuple(raw[n:])
        if loc in self.rows.get(table.name, {}):
            return True
        if any(
            isinstance(v, (str, bytes)) and len(v) > self.config.max_value_bytes
            for v in values
        ):
            self.oversized = True
            return True
        keys = [self.codec.key(v) for v in values]
        useful = [(i, key) for i, key in enumerate(keys) if key in self.targets]
        if not useful:
            return True
        # An already retained row must appear in every posting for its matching
        # cells. Use the occurrence limit for admission, never for verification.
        if not entity_match and all(
            self.bucket_full((table.name, i), key) for i, key in useful
        ):
            self.occurrences_limited = True
            return True
        size = (
            sys.getsizeof(values)
            + sys.getsizeof(loc)
            + sum(sys.getsizeof(v) for v in values + loc)
        )
        if self.payload_bytes + size > self.config.max_evidence_bytes:
            self.capacity_limit = "--max-evidence-bytes"
            return False
        self.rows[table.name][loc] = Witness(loc, values)
        self.count += 1
        self.payload_bytes += size
        for i, key in useful:
            ref = table.name, i
            self.hits[ref][key].add(loc)
            self.references[key].add(ref)
        if self.count == self.config.max_evidence_rows:
            self.capacity_limit = "--max-evidence-rows"
        elif self.payload_bytes == self.config.max_evidence_bytes:
            self.capacity_limit = "--max-evidence-bytes"
        return self.capacity_limit is None

    def bucket_full(self, ref: Ref, key: Key) -> bool:
        return len(self.hits.get(ref, {}).get(key, ())) >= self.config.witnesses

    def matches(self, value: Value) -> int:
        return int(self.codec.key(value) in self.targets)


class ProbeEngine:
    PRIORITY_VALUES_PER_FRONTEND = 3

    def __init__(self, db: SQLiteReader, store: EvidenceStore):
        self.db, self.store, self.cfg = db, store, db.config
        self.rng = random.Random(self.cfg.seed)
        self.coverage: dict[str, Coverage] = {}
        self.plans: dict[Ref, str | None] = {}
        self.stop_reason: str | None = None

    def run(self, tables: dict[str, Table]) -> None:
        if self.cfg.mode == "exhaustive":
            for table in tables.values():
                if self.stopped():
                    break
                coverage = self.start(table)
                self.full_scan(table, coverage)
            return

        priority, remaining, repeated = self.index_probe_groups()
        params = {
            key: self.store.codec.variants(value)
            for key, value in self.store.targets.items()
        }
        self.probe_width = max(map(len, params.values()))
        for variants in params.values():
            variants.extend([None] * (self.probe_width - len(variants)))

        active = []
        for number, table in enumerate(tables.values(), 1):
            if self.stopped():
                return
            indices = [
                i for i, col in enumerate(table.columns) if col.index is not None
            ]
            if not indices and not self.cfg.sample_rows:
                continue
            if number == 1 or number % 25 == 0:
                LOGGER.info(
                    "Checking priority values in table %s/%s: %s",
                    number,
                    len(tables),
                    table.name,
                )
            coverage = self.start(table)
            check = self.db.run(f"SELECT 1 FROM {qi(table.name)} LIMIT 1", limit=1)
            if not check.complete:
                coverage.interrupted += 1
            elif not check.rows:
                coverage.empty = True
                continue
            active.append((table, coverage, indices))
            self.probe_indices(table, coverage, indices, priority, params)

        # Try the remaining values seen once before broad samples can fill the
        # store with repeated values.
        if remaining:
            for table, coverage, indices in active:
                if self.stopped():
                    return
                self.probe_indices(table, coverage, indices, remaining, params)

        for table, coverage, _ in active:
            if self.stopped():
                return
            if self.cfg.sample_rows:
                self.sample(table, coverage)

        for table, coverage, indices in active:
            if self.stopped():
                return
            if not coverage.all_rows_sampled:
                self.probe_indices(table, coverage, indices, repeated, params)

    def index_probe_groups(self) -> tuple[list[Key], list[Key], list[Key]]:
        # Frontend frequency and value length only suggest selectivity. The
        # actual frequency in the source is unknown.
        def priority(key: Key) -> tuple:
            value = self.store.targets[key]
            is_text_or_blob = isinstance(value, (str, bytes))
            return (
                self.store.target_frequency[key],
                not is_text_or_blob,
                -(len(value) if is_text_or_blob else len(str(value))),
            )

        ranked = sorted(self.store.targets, key=priority)
        selective = {
            key: rank
            for rank, key in enumerate(ranked)
            if self.store.target_frequency[key] == 1
        }
        by_frontend = [
            sorted(keys & selective.keys(), key=selective.__getitem__)[
                : self.PRIORITY_VALUES_PER_FRONTEND
            ]
            for keys in self.store.frontend_keys
        ]
        first = [
            key
            for group in itertools.zip_longest(*by_frontend)
            for key in group
            if key is not None
        ]
        first_set = set(first)
        return (
            first,
            [key for key in selective if key not in first_set],
            [key for key in ranked if key not in selective],
        )

    def stopped(self) -> bool:
        if self.store.capacity_limit:
            self.stop_reason = "Evidence capacity reached."
        elif self.db.deadline.expired():
            self.stop_reason = "Discovery time limit reached; remaining time reserved for verification."
        return self.stop_reason is not None

    def start(self, table: Table) -> Coverage:
        coverage = Coverage(table.name, len(table.columns), started=True)
        self.coverage[table.name] = coverage
        return coverage

    def probe_indices(
        self,
        table: Table,
        coverage: Coverage,
        indices: list[int],
        keys: list[Key],
        params: dict[Key, list[Value]],
    ) -> None:
        for i in indices:
            ref = table.name, i
            col = table.columns[i]
            for key in keys:
                if self.stopped():
                    return
                if self.store.bucket_full(ref, key):
                    continue
                bindings = [*params[key], self.cfg.witnesses + 1]
                if ref not in self.plans:
                    sql = (
                        table.select_sql(col.index)
                        + " WHERE "
                        + col.index_expression
                        + " IN ("
                        + ",".join("?" for _ in range(self.probe_width))
                        + ") LIMIT ?"
                    )
                    self.plans[ref] = (
                        sql if confirms_search(self.db, sql, bindings) else None
                    )
                    if self.plans[ref] is None:
                        coverage.notes.append(
                            f"{col.name}: indexed search could not be confirmed."
                        )
                sql = self.plans[ref]
                if sql is None:
                    break
                result = self.db.run(
                    sql,
                    bindings,
                    limit=self.cfg.witnesses,
                    consume=lambda row: self.store.add(table, row),
                )
                coverage.index_probes += 1
                if not result.complete:
                    coverage.truncated_probes += 1
                    if result.reason == "returned row limit":
                        self.store.occurrences_limited = True
                    elif result.reason != "evidence capacity reached":
                        coverage.interrupted += 1

    def sample(self, table: Table, coverage: Coverage) -> None:
        order = ", ".join(map(qi, table.locator))
        prefix = table.select_sql()
        unique = set()

        def retain(result: QueryResult) -> bool:
            if not result.complete:
                coverage.interrupted += 1
            for row in result.rows:
                unique.add(tuple(row[: len(table.locator)]))
                if not self.store.add(table, row):
                    break
            coverage.sample_rows = len(unique)
            return result.complete and not self.store.capacity_limit

        # LIMIT without random sorting; both ends of the rowid/PRIMARY KEY index.
        head_n = (self.cfg.sample_rows + 1) // 2
        tail_n = self.cfg.sample_rows // 2
        head_sql = prefix + ((" ORDER BY " + order) if table.rowid else "") + " LIMIT ?"
        head = self.db.run(head_sql, [head_n], limit=head_n)
        head_examined = retain(head)
        coverage.all_rows_sampled = head_examined and head.count < head_n
        if coverage.all_rows_sampled or not tail_n or self.stopped():
            return
        tail_order = ", ".join(qi(c) + " DESC" for c in table.locator)
        tail_sql = prefix + " ORDER BY " + tail_order + " LIMIT ?"
        if not table.rowid:
            plan = self.db.run("EXPLAIN QUERY PLAN " + tail_sql, [tail_n])
            if not plan.complete or any("TEMP B-TREE" in row[-1] for row in plan.rows):
                coverage.notes.append(
                    "Tail sample skipped because its order could require a sort."
                )
                return
        tail = self.db.run(tail_sql, [tail_n], limit=tail_n)
        tail_examined = retain(tail)
        if table.rowid and head_examined and tail_examined and head.rows and tail.rows:
            coverage.all_rows_sampled = head.rows[-1][0] >= tail.rows[-1][0]
        if coverage.all_rows_sampled:
            return
        if table.rowid and head.rows and tail.rows:
            low, high = head.rows[0][0], tail.rows[0][0]
            if high > low:
                for _ in range(self.cfg.random_seeks):
                    if self.stopped():
                        return
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
                    retain(result)

    def full_scan(self, table: Table, coverage: Coverage) -> None:
        # One scan per table, rather than k separate full scans for k values.
        # The local deterministic function returns only rows containing evidence.
        where = " OR ".join("_locator_hit(" + qi(c.name) + ")" for c in table.columns)
        sql = table.select_sql() + " WHERE " + where
        result = self.db.run(
            sql,
            limit=self.cfg.max_hit_rows,
            consume=lambda row: self.store.add(table, row),
        )
        coverage.full_scan_finished = result.complete
        if not result.complete:
            coverage.interrupted += 1
            coverage.notes.append("Scan incomplete: " + result.reason)


def confirms_search(db: SQLiteReader, sql: str, params: list[Value]) -> bool:
    plan = db.run("EXPLAIN QUERY PLAN " + sql, params)
    return plan.complete and any(
        str(row[-1]).startswith("SEARCH ") for row in plan.rows
    )


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
        self,
        db: SQLiteReader,
        tables: dict[str, Table],
        store: EvidenceStore,
        coverage: dict[str, Coverage],
    ):
        self.db, self.tables, self.store, self.coverage = db, tables, store, coverage
        self.cfg, self.codec = db.config, store.codec
        self.findings: list[Finding] = []
        self._finding_signatures: set[tuple] = set()
        self._declared_edges: list[Edge] | None = None
        self._small_tables: dict[str, bool] = {}
        self.notes: set[str] = set()

    def view(self, front: FrontTable, transpose: bool) -> tuple[list[tuple], Callable]:
        if transpose:
            return list(zip(*front.rows)), lambda r, c: (c, r)
        return front.rows, lambda r, c: (r, c)

    def column_candidates(
        self, rows: list[tuple]
    ) -> dict[str, dict[int, list[tuple[float, Ref]]]]:
        candidates = defaultdict(lambda: defaultdict(list))
        for j in range(len(rows[0])):
            keys = {self.codec.key(row[j]) for row in rows if row[j] is not None}
            counts = Counter(
                ref for key in keys for ref in self.store.references.get(key, ())
            )
            for ref, count in counts.items():
                if count / len(keys) >= self.cfg.min_containment:
                    candidates[ref[0]][j].append((count / len(keys), ref))
        return candidates

    def options(
        self,
        candidates: dict[str, dict[int, list[tuple[float, Ref]]]],
        names: list[str],
    ) -> dict[int, list[Ref]]:
        combined = defaultdict(list)
        for name in names:
            for j, refs in candidates.get(name, {}).items():
                if len(refs) > self.cfg.candidates_per_column:
                    self.notes.add(
                        "Column candidates capped; increase --candidates-per-column."
                    )
                combined[j].extend(
                    sorted(refs, key=lambda x: (-x[0], x[1][1]))[
                        : self.cfg.candidates_per_column
                    ]
                )
        return {
            j: [ref for _, ref in sorted(refs, key=lambda x: -x[0])]
            for j, refs in sorted(combined.items())
        }

    def run(self, fronts: list[FrontTable]) -> None:
        for front in fronts:
            if self.db.deadline.expired():
                break
            LOGGER.info("Verifying structures for: %s", front.name)
            for transpose in (False, True):
                rows, coords = self.view(front, transpose)
                min_fields = min(
                    2, sum(any(v is not None for v in col) for col in zip(*rows))
                )
                candidates = self.column_candidates(rows)
                for name in sorted(candidates):
                    if self.db.deadline.expired():
                        break
                    options = self.options(candidates, [name])
                    if len(options) < min_fields:
                        continue
                    if sum(row[j] is not None for row in rows for j in options) < min(
                        4, front.cells
                    ):
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
                            min_fields=min_fields,
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
            (i, self.codec.key(row[j]))
            for j, (n, i) in mapping.items()
            if n == name and row[j] is not None
        ]
        if not constraints:
            return []
        buckets = (
            self.store.hits.get((name, i), {}).get(key, ()) for i, key in constraints
        )
        anchors = min(buckets, key=len)
        witnesses = self.store.rows.get(name, {})
        matches = (
            loc
            for loc in sorted(anchors, key=repr)
            if all(
                self.codec.key(witnesses[loc].values[i]) == key
                for i, key in constraints
            )
        )
        return list(itertools.islice(matches, self.cfg.witnesses))

    def verify(
        self,
        front: FrontTable,
        rows: list[tuple],
        coords: Callable,
        names: list[str],
        edges: list[Edge],
        mapping: dict[int, Ref],
        kind: str,
        *,
        min_fields: int = 2,
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
        proven_fields = {p.row if kind == "transpose" else p.column for p in proofs}
        if len(proven_fields) < min_fields:
            return
        descriptions = []
        for j, (name, c) in sorted(mapping.items()):
            if j not in proven_fields:
                continue
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
                    expr = "NULL"  # An unverified cell has no source expression.
                expressions.append(expr + " AS " + qi(label))
            selects.append("SELECT " + ",\n       ".join(expressions))
        return (
            "\nUNION ALL\n".join(selects) + "\nORDER BY 1;"
            if selects
            else "-- No verified cells to replay."
        )

    def eav(self, front: FrontTable) -> None:
        field_keys = {
            j: {self.codec.key(row[j]) for row in front.rows if row[j] is not None}
            for j in range(len(front.columns))
        }
        field_keys = {j: keys for j, keys in field_keys.items() if keys}
        if len(field_keys) < 2:
            return
        front_keys = set().union(*field_keys.values())
        identity = ValueCodec("strict")
        for name in sorted(self.store.rows):
            if self.db.deadline.expired():
                return
            table = self.tables[name]
            for vi in range(len(table.columns)):
                if self.db.deadline.expired():
                    return
                hits = self.store.hits.get((name, vi), {})
                if (
                    sum(
                        len(keys & hits.keys()) / len(keys) >= self.cfg.min_containment
                        for keys in field_keys.values()
                    )
                    < 2
                ):
                    continue
                relevant = [
                    w
                    for w in self.store.rows[name].values()
                    if self.codec.key(w.values[vi]) in front_keys
                ]
                candidates = []
                single_pk = sum(column.pk_order > 0 for column in table.columns) == 1
                for i, column in enumerate(table.columns):
                    if i == vi or (column.pk_order and single_pk):
                        continue
                    ids = {
                        identity.key(w.values[i])
                        for w in relevant
                        if w.values[i] is not None
                    }
                    if not ids:
                        continue
                    candidates.append(
                        (column.index is None, len(ids) == len(relevant), i)
                    )
                if len(candidates) > self.cfg.max_eav_columns:
                    self.notes.add("EAV entity columns capped by --max-eav-columns.")
                for _, _, entity in sorted(candidates)[: self.cfg.max_eav_columns]:
                    if self.db.deadline.expired():
                        return
                    self.fetch_entity_witnesses(
                        table, vi, entity, front, front_keys, relevant, identity
                    )
                    self.match_entities(
                        front, table, vi, entity, field_keys, front_keys, identity
                    )

    def fetch_entity_witnesses(
        self,
        table: Table,
        vi: int,
        entity: int,
        front: FrontTable,
        front_keys: set[Key],
        relevant: list[Witness],
        identity: ValueCodec,
    ) -> None:
        if self.store.capacity_limit:
            return
        coverage = self.coverage[table.name]
        column = table.columns[entity]
        if column.index is None and not self.small_enough_for_entity_scan(
            table, coverage
        ):
            return
        hits = self.store.hits[(table.name, vi)]
        ordered = sorted(
            relevant, key=lambda w: len(hits[self.codec.key(w.values[vi])])
        )
        ids = {}
        limit = min(
            max(self.cfg.witnesses, len(front.rows)),
            self.db.connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) - 1,
        )
        for w in ordered:
            value = w.values[entity]
            if value is not None:
                ids.setdefault(identity.key(value), value)
            if len(ids) >= limit:
                break
        if not ids:
            return
        if len(ids) == limit:
            self.notes.add("EAV entity lookups reached the anchor limit.")
        row_limit = min(
            self.cfg.max_hit_rows,
            max(front.cells * 2, len(ids) * len(front.columns) * 2),
        )
        sql = (
            table.select_sql(column.index)
            + " WHERE "
            + column.index_expression
            + " IN ("
            + ",".join("?" for _ in ids)
            + ") AND _locator_hit("
            + qi(table.columns[vi].name)
            + ") LIMIT ?"
        )
        params = [*ids.values(), row_limit + 1]
        if column.index is not None and not confirms_search(self.db, sql, params):
            self.notes.add(
                f"{table.name}.{column.name}: EAV indexed search could not be confirmed."
            )
            return
        offset = len(table.locator)

        def retain(raw: tuple) -> bool:
            if (
                identity.key(raw[offset + entity]) in ids
                and self.codec.key(raw[offset + vi]) in front_keys
            ):
                return self.store.add(table, raw, entity_match=True)
            return True

        result = self.db.run(sql, params, limit=row_limit, consume=retain)
        coverage.entity_probes += 1
        if not result.complete and result.reason != "evidence capacity reached":
            self.notes.add(
                "Some EAV entity lookups were interrupted or truncated; review the limits."
            )

    def small_enough_for_entity_scan(self, table: Table, coverage: Coverage) -> bool:
        if coverage.all_rows_sampled:
            return True
        if not (
            coverage.full_scan_finished
            and self.store.occurrences_limited
            and self.cfg.sample_rows
        ):
            return False
        if table.name not in self._small_tables:
            limit = self.cfg.sample_rows + 1
            result = self.db.run(
                f"SELECT 1 FROM {qi(table.name)} LIMIT ?", [limit], limit=limit
            )
            self._small_tables[table.name] = result.complete and result.count < limit
        return self._small_tables[table.name]

    def match_entities(
        self,
        front: FrontTable,
        table: Table,
        vi: int,
        entity: int,
        field_keys: dict[int, set[Key]],
        front_keys: set[Key],
        identity: ValueCodec,
    ) -> None:
        relevant = [
            (w, identity.key(w.values[entity]), self.codec.key(w.values[vi]))
            for w in self.store.rows[table.name].values()
            if w.values[entity] is not None
            and self.codec.key(w.values[vi]) in front_keys
        ]
        active_rows = sum(any(v is not None for v in row) for row in front.rows)
        candidates = []
        for attribute in range(len(table.columns)):
            if attribute in (entity, vi):
                continue
            counts = Counter(
                identity.key(w.values[attribute])
                for w, _, _ in relevant
                if w.values[attribute] is not None
            )
            repeated = sum(count > 1 for count in counts.values())
            if counts and (repeated or active_rows == 1):
                candidates.append((-repeated, attribute))
        if len(candidates) > self.cfg.max_eav_columns:
            self.notes.add("EAV attribute columns capped by --max-eav-columns.")
        for _, attribute in sorted(candidates)[: self.cfg.max_eav_columns]:
            if self.db.deadline.expired():
                return
            groups: dict[Key, dict[tuple[Key, Key], Witness]] = defaultdict(dict)
            entities_by_cell: dict[tuple[Key, Key], set[Key]] = defaultdict(set)
            value_attrs: dict[Key, set[Key]] = defaultdict(set)
            attributes = {}
            for w, ek, vk in relevant:
                ak = identity.key(w.values[attribute])
                if ak is not None:
                    groups[ek].setdefault((ak, vk), w)
                    entities_by_cell[(ak, vk)].add(ek)
                    value_attrs[vk].add(ak)
                    attributes.setdefault(ak, w.values[attribute])
            options = {}
            for j, keys in field_keys.items():
                counts = Counter(ak for key in keys for ak in value_attrs.get(key, ()))
                viable = [
                    ak
                    for ak in sorted(counts, key=lambda ak: (-counts[ak], repr(ak)))
                    if counts[ak] / len(keys) >= self.cfg.min_containment
                ]
                if len(viable) > self.cfg.candidates_per_column:
                    self.notes.add(
                        "EAV attribute candidates capped by --candidates-per-column."
                    )
                if viable:
                    options[j] = viable[: self.cfg.candidates_per_column]
            if len(options) < 2:
                continue
            for mapping in assignments(
                options, self.cfg.max_mappings, self.db.deadline
            ):
                choices: dict[int, list[Key]] = {}
                checks = {}
                for r, row in enumerate(front.rows):
                    if self.db.deadline.expired():
                        return
                    cells = {
                        j: (ak, self.codec.key(row[j]))
                        for j, ak in mapping.items()
                        if row[j] is not None
                    }
                    if not cells:
                        continue
                    anchors = min(
                        (entities_by_cell.get(c, ()) for c in cells.values()), key=len
                    )
                    matching = (
                        ek
                        for ek in sorted(anchors, key=repr)
                        if all(cell in groups[ek] for cell in cells.values())
                    )
                    choices[r] = list(itertools.islice(matching, self.cfg.witnesses))
                    checks[r] = cells
                matched = distinct_witnesses(choices)
                if len(matched) < min(2, active_rows):
                    continue
                proofs = []
                for r, ek in sorted(matched.items()):
                    for j, key in checks[r].items():
                        w = groups[ek][key]
                        proofs.append(
                            CellProof(
                                r,
                                j,
                                table.name,
                                table.columns[vi].name,
                                dict(zip(table.locator, w.locator)),
                                w.values[vi],
                            )
                        )
                if len(proofs) < min(4, front.cells):
                    continue
                descriptions = [
                    f"entity: {table.columns[entity].name}; attribute: "
                    f"{table.columns[attribute].name}; value: {table.columns[vi].name}"
                ]
                descriptions.extend(
                    f"{front.columns[j]} → attribute {shown(attributes[ak])}"
                    for j, ak in sorted(mapping.items())
                )
                self.add_finding(
                    Finding(
                        front.name,
                        "EAV",
                        [table.name],
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
        """Propose edges from shared witness IDs, independent of catalog order."""
        fields = []
        for name in sorted(names):
            if self.db.deadline.expired():
                return []
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
                bucket = postings[key]
                if len(bucket) <= 32:
                    bucket.append(i)
        counts: Counter = Counter()
        for locations in postings.values():
            if self.db.deadline.expired():
                return []
            if (
                len(locations) > 32
            ):  # Skip overly common values to avoid a combinatorial number of pairs.
                continue
            for a, b in itertools.combinations(locations, 2):
                if fields[a][0] != fields[b][0]:
                    counts[(a, b)] += 1
        result = []
        declared = set(existing)
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
            reverse = tuple((r, l) for l, r in pair)
            if Edge(ln, rn, pair) in declared or Edge(rn, ln, reverse) in declared:
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
        candidates = self.column_candidates(front.rows)
        if len(candidates) < 2:
            return
        if self._declared_edges is None:
            self._declared_edges = self.declared_edges()
        frequency = Counter(j for fields in candidates.values() for j in fields)
        support = {
            name: sum(
                max(score for score, _ in refs) / frequency[j]
                for j, refs in fields.items()
            )
            for name, fields in candidates.items()
        }
        rank = lambda name: (-support.get(name, 0), name)
        graph: dict[str, list[Edge]] = defaultdict(list)
        seen: set[frozenset[Edge]] = set()
        paths = 0
        expanded = 0
        expansion_limit = 20 * self.cfg.max_join_paths
        # Declared paths are tested first. Tables without an edge consume no
        # path budget, even if their columns match common frontend values.
        for inferred in (False, True):
            edges = (
                self.inferred_edges(set(candidates), self._declared_edges)
                if inferred
                else self._declared_edges
            )
            if not edges:
                continue
            for edge in edges:
                graph[edge.left].append(edge)
                graph[edge.right].append(edge)
            for start in sorted(candidates.keys() & graph.keys(), key=rank):
                queue = deque([([start], [])])
                while queue and not self.db.deadline.expired():
                    nodes, path = queue.popleft()
                    if paths >= self.cfg.max_join_paths or expanded >= expansion_limit:
                        self.notes.add(
                            "JOIN path search capped; increase --max-join-paths."
                        )
                        return
                    end = nodes[-1]
                    if path:
                        expanded += 1
                    if path and end in candidates:
                        signature = frozenset(path)
                        if signature not in seen:
                            seen.add(signature)
                            if self.verify_path(front, candidates, nodes, path):
                                paths += 1
                    if len(path) >= self.cfg.max_join_hops:
                        continue
                    adjacent = sorted(graph[end], key=lambda e: rank(e.other(end)))
                    for edge in adjacent:
                        other = edge.other(end)
                        if other in nodes:
                            continue
                        if len(queue) + expanded >= expansion_limit:
                            self.notes.add(
                                "JOIN path search capped; increase --max-join-paths."
                            )
                            break
                        queue.append((nodes + [other], path + [edge]))

    def verify_path(
        self,
        front: FrontTable,
        candidates: dict[str, dict[int, list[tuple[float, Ref]]]],
        nodes: list[str],
        path: list[Edge],
    ) -> bool:
        options = self.options(candidates, nodes)
        if len(options) < 2:
            return False
        if sum(row[j] is not None for row in front.rows for j in options) < min(
            4, front.cells
        ):
            return False
        endpoints = {nodes[0], nodes[-1]}
        mapped = 0
        for mapping in assignments(options, self.cfg.max_mappings, self.db.deadline):
            mapped += 1
            if endpoints <= {ref[0] for ref in mapping.values()}:
                self.verify(
                    front, front.rows, lambda r, c: (r, c), nodes, path, mapping, "JOIN"
                )
        if mapped >= self.cfg.max_mappings:
            self.notes.add("Mapping limit reached during JOIN verification.")
        return mapped > 0


class ReportWriter:
    """One report model supplies HTML, plain text and machine-readable JSON."""

    LABELS = {
        "rows": "Shared rows",
        "transpose": "Transpose",
        "EAV": "Entity · attribute · value",
        "JOIN": "Joined tables",
    }
    HTML_COVERAGE_ROWS = 1000

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
        for name, c in sorted(self.store.references.get(key, ())):
            locs = self.store.hits[(name, c)][key]
            count += len(locs)
            if len(locations) < limit:
                for loc in itertools.islice(
                    sorted(locs, key=repr), limit - len(locations)
                ):
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
        column_count = max(1, len(self.store.hits))
        for front in self.fronts:
            for j, label in enumerate(front.columns):
                keys = {
                    self.store.codec.key(r[j]) for r in front.rows if r[j] is not None
                }
                if not keys:
                    continue
                weight = {
                    k: 1
                    + math.log(
                        (column_count + 1) / (len(self.store.references.get(k, ())) + 1)
                    )
                    for k in keys
                }
                candidates = []
                refs = {ref for k in keys for ref in self.store.references.get(k, ())}
                for ref in refs:
                    hits = self.store.hits[ref]
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
            f'Tables inspected: {m["tables_started"]}/{len(self.tables)}; source queries: {m["source_queries"]}; '
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
                "SQL replay of verified cells (NULL for unverified cells):",
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
                f"{c.table}: started={c.started}, empty={c.empty}, "
                f"sample={c.sample_rows}, index probes={c.index_probes}, entity lookups={c.entity_probes}, "
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
</style></head><body><header><div><div class="brand">SQLite Locator</div><small>Data analysis report</small></div><div class="run-date">{esc(m['created_at'])}</div></header>
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
                '<details><summary>Sample SQL</summary><div class="sqlbar"><span class="note">'
                "Verified cells in GUI order. Unverified cells are returned as NULL.</span>"
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
            '<div class="card"><div class="source"><span>Tables inspected: <strong>'
            + str(m["tables_started"])
            + "/"
            + str(len(self.coverage))
            + "</strong></span><span>Queries: <strong>"
            + str(m["source_queries"])
            + "</strong></span><span>Interrupted / truncated queries: <strong>"
            + str(m["interrupted_queries"])
            + '</strong></span></div><details><summary>Coverage by table</summary><label>Filter tables<input id="coverage-search" type="search" placeholder="Table name"></label>'
        )
        coverage_rows = []
        visible_tables = heapq.nsmallest(
            self.HTML_COVERAGE_ROWS,
            self.coverage,
            key=lambda c: (-c.retained_rows, not c.interrupted, c.table),
        )
        if len(visible_tables) < len(self.coverage):
            parts.append(
                f'<p class="note">Showing {len(visible_tables):,} of {len(self.coverage):,} tables. '
                "See the TXT or JSON report for the full list.</p>"
            )
        for c in visible_tables:
            if c.empty:
                state = "Empty"
            elif c.full_scan_finished or c.all_rows_sampled:
                state = "All rows examined"
            elif c.interrupted:
                state = "Interrupted"
            elif c.started:
                state = "Checked"
            else:
                state = "Not checked"
            coverage_rows.append(
                [
                    "<code>" + esc(c.table) + "</code>",
                    str(c.columns),
                    str(c.retained_rows),
                    str(c.sample_rows),
                    str(c.index_probes),
                    str(c.entity_probes),
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
                    "Index probes",
                    "Entity lookups",
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
        "witnesses": "Occurrence threshold for admitting new evidence and row limit per index probe.",
        "max_evidence_rows": "Global limit on retained rows containing evidence.",
        "max_evidence_bytes": "Approximate evidence payload limit, excluding indexes and interpreter overhead.",
        "max_row_bytes": "SQLite limit on the size of a single row/value, in bytes.",
        "max_hit_rows": "Maximum matching rows returned by a scan of one table.",
        "max_tables": "Maximum source tables after filtering; 0 has no table count limit.",
        "max_columns": "Wider tables are skipped and reported.",
        "max_frontend_rows": "Maximum rows per GUI sample; exceeding the limit is an error.",
        "max_frontend_cells": "Maximum total number of frontend cells.",
        "max_value_bytes": "Retained value size limit: text characters or BLOB bytes.",
        "candidates_per_column": "Maximum candidate columns per table and layout position.",
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
        zero_allowed = name in (
            "random_seeks",
            "sample_rows",
            "max_join_hops",
            "max_tables",
        )
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
    discovery_deadline = Deadline(
        config.total_seconds - min(10, config.total_seconds * 0.1)
    )
    connections = []
    try:
        frontend_db = SQLiteReader(args.frontend, config, deadline)
        connections.append(frontend_db)
        fronts = load_frontend(frontend_db, args.frontend_table)
        source = SQLiteReader(args.source, config, discovery_deadline)
        connections.append(source)
        catalog = Catalog(source)
        tables = catalog.read(args.table_pattern, limit=config.max_tables)
        codec = ValueCodec(config.comparison)
        store = EvidenceStore(codec, fronts, config)
        source.connection.create_function(
            "_locator_hit", 1, store.matches, deterministic=True
        )
        probe = ProbeEngine(source, store)
        probe.run(tables)
        source.deadline = deadline
        matcher = StructureMatcher(source, tables, store, probe.coverage)
        matcher.run(fronts)
        notes = list(catalog.notes) + sorted(matcher.notes)
        if deadline.expired():
            notes.append("Global time limit reached. Results are partial.")
        if probe.stop_reason and not store.capacity_limit:
            notes.append(probe.stop_reason)
        if store.capacity_limit:
            notes.append(
                f"Evidence discovery stopped at {store.capacity_limit}. Search coverage is partial."
            )
        if store.occurrences_limited:
            notes.append(
                "The occurrence limit (--witnesses) restricted some value locations and layouts."
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
        coverage = [
            probe.coverage.get(table.name) or Coverage(table.name, len(table.columns))
            for table in tables.values()
        ]
        for c in coverage:
            c.retained_rows = len(store.rows.get(c.table, {}))
        metadata = {
            "source": str(args.source.resolve()),
            "frontend": str(args.frontend.resolve()),
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "elapsed_seconds": round(time.monotonic() - deadline.started, 4),
            "source_queries": source.queries,
            "tables_started": len(probe.coverage),
            "returned_rows": source.returned_rows,
            "interrupted_queries": source.interruptions,
            "query_errors": source.errors,
            "config": asdict(config),
            "notes": notes,
            "operational_notes": operational_notes,
            "evidence_payload_bytes": store.payload_bytes,
        }
        writer = ReportWriter(fronts, tables, store, matcher, coverage, metadata)
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
