# SQLite Locator

Find the source of a small table copied from an application interface inside an unfamiliar SQLite database. SQLite Locator searches by values, verifies row relationships, and produces an analyst-facing HTML report with exact table, column, and record locations.

The application is a single standalone Python file. A separate standalone script generates reproducible validation databases. Both use only the Python standard library; no AI service, network access, or package installation is required at runtime.

## Project layout

```text
sqlite-locator/
├── README.md
├── .gitignore
├── src/
│   └── sqlite_locator.py      # Discovery engine, CLI, and report generation
├── scripts/
│   └── generate_demo.py       # Synthetic datasets and independent ground truth
├── tests/
│   └── test_locator.py        # Integration tests
├── data/
│   ├── demo/                 # Small generated databases
│   └── large/                # Large generated databases
├── reports/
│   ├── demo/                 # HTML, text, and JSON reports
│   └── large/
└── logs/                     # Optional run logs
```

Run the commands below from the repository root. Relative CLI paths are resolved against the current working directory. Both Python scripts can also be copied and used independently.

Generated databases, reports, and logs are excluded from version control. The scripts create their output directories when needed.

## Requirements

- Python 3.11 or newer, including the `sqlite3` module.
- SQLite 3.33 or newer, including the [`sqlite_schema` catalog](https://www.sqlite.org/schematab.html). Check with `python3 -c "import sqlite3; print(sqlite3.sqlite_version)"`.
- Read access to both input databases and write access to the report directory.
- A modern browser for the self-contained HTML report. No external assets or network requests are used.

On Windows, replace `python3` with `py -3`. The Copy SQL button depends on browser clipboard permissions; SQL can always be selected and copied manually.

## Quick start

```bash
python3 scripts/generate_demo.py --profile small
python3 src/sqlite_locator.py \
  --frontend data/demo/frontend.sqlite \
  --source data/demo/source.sqlite \
  --report reports/demo/report.html
```

Open `reports/demo/report.html` in your browser. The same run writes `report.txt` and `report.json` alongside it.

To analyze your own databases:

```bash
python3 src/sqlite_locator.py \
  --frontend /path/to/frontend.sqlite \
  --source /path/to/source.sqlite \
  --report reports/analysis.html
```

Without `--report`, outputs are written to `reports/report.html`, `reports/report.txt`, and `reports/report.json`. A subsequent run replaces these files. Each output file is written through a unique temporary file and an atomic rename; the three formats are not committed as one filesystem transaction.

Select individual frontend tables or restrict the source catalog:

```bash
python3 src/sqlite_locator.py \
  --frontend data/frontend.sqlite --frontend-table "Orders" \
  --source data/source.sqlite \
  --table-pattern 'archive_*' --table-pattern 'records_*' \
  --report reports/orders.html
```

A source filter also restricts the available join graph. Excluding an intermediate table prevents discovery of joins through that table.

## Preparing the frontend database

Store each GUI table as an ordinary SQLite table. Preserve the relationship between values in the same row. Use column labels as column names, not as an extra data row. Multiple frontend tables with different schemas are supported.

```python
import sqlite3
from contextlib import closing
from pathlib import Path

Path("data").mkdir(exist_ok=True)
with closing(sqlite3.connect("data/frontend.sqlite")) as db, db:
    db.execute('CREATE TABLE "Products" ("Name", "Color", "Quantity")')
    db.executemany(
        'INSERT INTO "Products" VALUES (?, ?, ?)',
        [("Russet R-927", "green", 137), ("Gala G-418", "red", 263)],
    )
```

Omitting declared types in this example preserves mixed value types. Store identifiers such as `0012` as text. SQLite does not guarantee screen order without `ORDER BY`; report row numbers follow the frontend query result order. The engine does not use source row order to infer relationships.

The default frontend limits are 100 rows per table and 400 cells in total. Exceeding them produces an error rather than silently truncating the sample. NULL means unavailable evidence: it is excluded from matching and from the coverage denominator.

## Interpreting results

| Result | Meaning |
| --- | --- |
| Value found | A specific source cell contains a matching value. Row relationships are not established by this alone. |
| Complete sample / partial sample | The engine also verified a consistent column mapping, shared records, transposition, EAV grouping, or a join condition. |
| Not found within budget | No witness was found in the inspected scope. This is not proof of absence. |

A complete match proves sample agreement, not historical provenance. Copies, archives, and materialized results can all agree with the same sample. Alternative matches are retained rather than claiming one uniquely proven origin.

The HTML report provides filters, field mappings, source values, exact record locators, copyable SQL, and search coverage. Technical diagnostics and configuration are also available in TXT and JSON. Coordinates in `findings[].proofs` are zero-based; coordinates in `frontends[].cells` and displayed report row numbers are one-based.

Replay SQL reproduces confirmed sample rows using their recorded locators. It is not the original application query or a generalized query for future data. Source `rowid` values can change after database maintenance. Numeric comparison may return a different source type or textual representation from the GUI. GUI NULL cells are emitted as NULL placeholders without asserting that the source cell is NULL.

## Large validation dataset

The generator defaults to the `large` profile:

```bash
python3 scripts/generate_demo.py --profile large --output-dir data/large --seed 42
python3 src/sqlite_locator.py \
  --frontend data/large/frontend.sqlite \
  --source data/large/source.sqlite \
  --report reports/large/report.html \
  --total-seconds 600
```

| Parameter | `small` | `large` |
| --- | ---: | ---: |
| Noise tables | 30 | 3,000 |
| Rows per noise table | 180–700 | 18,000–70,000 |
| Columns per noise table | 4–12 | 4–12 |
| Additional scenario tables | 9 | 9 |
| Expected total rows | About 15,000 | About 132 million |

The large profile increases both table count and rows per table by 100 times, producing roughly 10,000 times more rows overall. Plan for tens of GiB of free disk space depending on parameters and indexes. The generator estimates disk requirements before starting and stops when free space falls below 2 GiB. The estimate is not a guaranteed upper bound.

Noise is generated in batches inside SQLite using reproducible pseudorandom sequences. It is synthetic test data, not a model of business data distributions. The seed controls schemas, names, sizes, indexes, and planted record locations.

Scenarios include ordinary rows with reordered columns, numbers stored as text, mixed-type readings, transposition, EAV with opaque attribute identifiers, declared FK joins, inferred joins without FKs, isolated cells across tables, and a decoy with complete column value sets but incorrect row pairings.

`expected.json` is independent ground truth for tests and inspection. The discovery engine never reads it. Planted values are indexed by default; noise columns are partially indexed. Fast discovery on this fixture does not imply the same performance on an unindexed source.

Custom scale and an unindexed example:

```bash
python3 scripts/generate_demo.py \
  --noise-tables 5000 --min-rows 20000 --max-rows 90000 --output-dir data/larger

python3 scripts/generate_demo.py \
  --profile small --no-target-indexes --output-dir data/unindexed
python3 src/sqlite_locator.py \
  --frontend data/unindexed/frontend.sqlite --source data/unindexed/source.sqlite \
  --mode exhaustive --query-seconds 3 --query-vm-steps 10000000 \
  --report reports/unindexed/report.html
```

`--force` allows the generator to replace its three output files. Databases are first built as `.building` files, then promoted after generation succeeds. Existing WAL/SHM/journal sidecars prevent replacement. A hard process failure can leave working files that must be removed before retrying. This is a fixture generator, not a production database migration tool.

### Recorded pre-release benchmark

A local run on November 16, 2026, before English localization, used profile `large`, seed 42:

| Measurement | Result |
| --- | ---: |
| Source tables | 3,009 |
| Source rows, recorded by the generator | 133,196,564 |
| Source database size | 14,083,543,040 bytes / 13.12 GiB |
| Generation and indexing | 273.5 s |
| Bounded discovery, 600 s budget | 10.71 s |
| Source queries, including catalog and query plans | 227,397 |
| Returned rows, including repeats and metadata | 810,522 |
| Expected complete layouts recovered | 6/6 |
| Failed or interrupted queries | 0 |

Every replay query was executed and compared with its frontend sample. The shuffled-row decoy was not accepted as a complete layout. This measurement used local NVMe storage after database creation, existing indexes, and an uncleared operating-system cache. It is not a cold-disk benchmark or a performance guarantee for other databases.

## Discovery strategy

The engine automates the same sequence an analyst would follow manually: find distinctive values, keep their locations, and then check whether the surrounding records explain the sample. A **witness** is an actual source record retained as evidence. A **candidate** is a possible mapping that still needs verification.

| Stage | Question answered | Method |
| --- | --- | --- |
| Inspect the catalog | Which tables, columns, indexes, and declared relationships exist? | SQLite schema and PRAGMA metadata. |
| Locate values | Where can the sample values be observed within the budget? | Indexed equality probes, limited samples, or explicitly enabled scans. |
| Rank columns | Which columns contain enough of the requested values? | Set containment and observed-frequency weighting. |
| Verify structure | Do the values occur together in the required records? | Constrained mappings, bipartite matching, EAV grouping, and SQL join checks. |
| Report evidence | Which exact cells support each finding, and what was inspected? | Record locators, replay SQL, and coverage diagnostics. |

For example, suppose the GUI contains `(Gala, red, 263)` and `(Russet, green, 137)`. A source column containing both names has name containment `2/2`. That makes it a candidate. To accept a row layout, the engine must also find `red` and `263` in the same source record as `Gala`, and `green` and `137` in the record containing `Russet`, under one consistent column mapping. Finding every value somewhere in the table is insufficient. The generator includes a shuffled-row decoy to exercise this distinction.

If the source stores each product in a separate column, the engine repeats the structural check with the GUI matrix transposed. If values are spread across an EAV table or related tables, it verifies shared entity identifiers or join conditions instead. Each search has explicit limits, so the report separates verified matches from incomplete coverage.

### Catalog and bounded reads

The engine reads `sqlite_schema`, column metadata, index metadata, and foreign keys. It considers ordinary indexes whose leading column matches the predicate and INTEGER PRIMARY KEY access. Partial and expression indexes are not treated as universal access paths.

Declared types do not eliminate candidates: SQLite supports dynamic typing. Optimizer statistics are not used as exact, current extrema or as proof that a value is absent. See [SQLite datatypes](https://www.sqlite.org/datatype3.html) and [ANALYZE](https://www.sqlite.org/lang_analyze.html).

### Witness discovery

`bounded` mode reads small head and tail samples and performs indexed rowid seeks. These samples are biased by key distribution and do not provide a statistical absence probability. WITHOUT ROWID tables use head and tail primary-key samples.

GUI values are then probed through eligible indexes. `EXPLAIN QUERY PLAN` must confirm a SEARCH access path before an index probe runs. Parameter variants are prepared locally instead of applying normalization functions to indexed columns. A matching row also supplies evidence for its other columns.

`exhaustive` mode permits one scan per table using a local SQLite callback to match values. Only rows containing evidence reach the result cursor, but inspecting all cells still requires work inside SQLite and the Python callback. Time, VM-instruction, and returned-row limits remain active; selecting this mode does not guarantee every scan will finish.

The implementation does not use `ORDER BY random()` or large OFFSET scans. SQLite does not provide PostgreSQL-style `TABLESAMPLE SYSTEM`. See [Query Planning](https://www.sqlite.org/queryplanner.html) and [EXPLAIN QUERY PLAN](https://www.sqlite.org/eqp.html).

### Inverted evidence index and containment

An in-memory index maps matching values to source columns and record locators. It stores bounded witnesses, not a full index or copy of the source database.

```text
containment = distinct sample values found / distinct sample values requested
```

Single-field rankings also use IDF weights computed from observed hits. These are prioritization heuristics, not global database frequencies or calibrated probabilities. Structural coverage counts confirmed cells. Acceptance depends on actual witnesses rather than a similarity score alone.

Pearson correlation is unsuitable as the primary filter because record pairs are unknown and values may be categorical. The application does not build full-database MinHash, LSH, or JOSIE indexes: that would require a separate profiling and maintenance phase. Small samples and existing database indexes favor direct probes and bounded verification.

### Structural verification

- **Rows:** one injective mapping from GUI fields to source columns; values belonging to one GUI record must share a source record.
- **Transpose:** the same verification engine processes the transposed GUI matrix, without assuming source column order or neighboring rows.
- **Duplicates:** an augmenting-path bipartite matching assigns repeated GUI records to distinct source witnesses.
- **Alternative mappings:** backtracking explores constrained fields first, with explicit mapping and state limits. One arbitrary column matching is insufficient to establish transposition.
- **EAV:** a shared entity identifier, attribute column, and value column; attribute assignments remain consistent across matched records. Attribute labels may be opaque. Text identifiers are not normalized into numeric identities.
- **JOIN:** bounded exploration of simple FK paths, including composite keys. Additional candidate edges can be inferred from overlapping witness identifiers, requiring at least two shared values. Inferred relations are labeled explicitly. SQL verifies the relation on concrete locators, including intermediate tables without GUI values.

Structural verification uses retained witnesses. It does not automatically recover rows skipped by sampling or occurrence limits.

## Value comparison

| Mode | Behavior |
| --- | --- |
| `--comparison numeric` | Default. Numeric 12, 12.0, and text `12.00` can match. Numeric comparison uses Decimal representations without an arbitrary epsilon. |
| `--comparison strict` | Text and numbers remain distinct. INTEGER and REAL are still compared numerically. |

Case, whitespace, decimal commas, date formats, currencies, and leading zeros are not normalized. BLOBs are compared byte-for-byte. Index probes enumerate a finite set of numeric text variants; other representations may only be found by sampling or scanning.

## Operational parameters

Complete reference: `python3 src/sqlite_locator.py --help`.

| Parameter | Default | Purpose |
| --- | ---: | --- |
| `--mode` | `bounded` | Indexes and samples, or permission to scan entire tables. |
| `--total-seconds` | 120 | Search budget, excluding report serialization. |
| `--query-seconds` | 0.5 | Approximate per-query time limit. |
| `--query-vm-steps` | 1,000,000 | VM instruction limit, checked approximately every 1,000 instructions. |
| `--sample-rows` | 160 | Combined head and tail sample size per table. |
| `--random-seeks` | 24 | Additional rowid seeks, up to four rows each. |
| `--witnesses` | 32 | Retained occurrences per value/column and rows per probe. |
| `--max-evidence-rows` | 12,000 | Global retained source-record limit. |
| `--max-evidence-bytes` | 67,108,864 | Approximate witness payload limit, excluding indexes and interpreter overhead. |
| `--max-row-bytes` | 4,194,304 | SQLite limit on one value or row. |
| `--max-hit-rows` | 20,000 | Returned matches per full scan. |
| `--max-tables` | 10,000 | Catalog table limit. |
| `--max-columns` | 256 | Wider tables are skipped and reported. |
| `--candidates-per-column` | 8 | Candidate columns per field in a tested layout. |
| `--min-containment` | 0.6 | Minimum observed column coverage for structural verification. |
| `--max-mappings` | 128 | Mapping limit per table layout. |
| `--max-join-hops` | 2 | Up to three tables on a join path; zero disables joins. |
| `--max-join-paths` | 80 | Join-path limit per frontend table. |
| `--max-eav-columns` | 16 | Candidate EAV identifier columns. |
| `--max-findings` | 100 | Retained structural findings. |
| `--quiet` | Off | Suppress progress messages. |

Time limits use the [SQLite progress handler](https://www.sqlite.org/c3ref/progress_handler.html) and checks between operations. They are not hard limits on disk latency, operating-system stalls, or process RSS. Larger budgets increase recall at the cost of time, memory, and I/O.

For empty results, inspect coverage and diagnostics first. Then consider larger samples, more witnesses, or a narrower source scope. Full scans are an explicit operator choice, not a hidden fallback.

## Complexity and limitations

Let `M` be the number of columns, `K` the distinct GUI values, `I` the indexed candidate columns, `N_t` the row count of table `t`, `C_t` its width, `S` the sample limit, `W` the witness limit per value/column, and `R` the retained-record limit.

- Catalog work is at least linear in the inspected metadata, plus index and FK descriptions.
- Index probing is approximately `O(I × K × (log N + W))`, plus fetching and comparing witness columns. Collation, query plans, and cache affect actual cost.
- Sample comparison costs approximately `O(sum_t(S × C_t))`, plus rowid/PK seeks. Rare values can be missed.
- Exact unindexed search has worst-case cost `Theta(sum_t(N_t × C_t))` cell comparisons. I/O also depends on page layout. EXISTS cannot remove this cost when a value is absent.
- Column mappings and join paths have potentially combinatorial search spaces. Limits bound the effort; they do not make the algorithm complete.
- Memory holds metadata, the frontend sample, bounded witnesses, and findings. The payload byte limit excludes some Python and index overhead. The whole source database is never loaded into RAM.

Without an index, an existing full profile, or a scan, no general algorithm can guarantee discovery of a unique value at an unknown location. Global sketches and indexes move work into their construction and maintenance phases.

Supported cases include ordinary tables, mixed types, duplicates, quoted SQL identifiers, rowid, WITHOUT ROWID, and composite PK/FK relationships. HTML values and identifiers are escaped. Executed queries use bound values and quoted catalog identifiers.

Aggregation, arbitrary formulas, exchange-rate conversion, text concatenation, JSON extraction, arbitrary pivots, self-joins, and general join graphs are not reconstructed. Views and virtual tables are skipped. Tables without a supported unambiguous locator are skipped. A structure usually requires two matched records and four cells, with lower thresholds for smaller samples. Isolated value locations can still be reported when no complete structure is found.

Both inputs use `mode=ro` and `query_only=ON`, without `immutable=1`. Discovery does not create indexes, run ANALYZE, or modify source tables. Each database has its own read transaction; there is no atomic cross-file snapshot. Long-running reads can affect concurrent SQLite users, including WAL checkpoints. Consistent local copies are preferable for analysis.

Reports contain source data fragments and should be handled with the same access controls as those data. Interrupting a search leaves the previous report in place; a new partial report is not written on KeyboardInterrupt.

## Code organization and verification

| Component | Responsibility |
| --- | --- |
| `Config` and dataclasses | Explicit configuration and data models. |
| `ValueCodec` | Comparison rules and parameter variants. |
| `SQLiteReader` | Read-only connections, budgets, and partial query results. |
| `Catalog` | Schema, indexes, record locators, and foreign keys. |
| `ProbeEngine` | Index probes, samples, and scans. |
| `EvidenceStore` | Bounded witnesses and inverted hit index. |
| `StructureMatcher` | Rows, transpose, EAV, and joins. |
| `ReportWriter` | Shared HTML/TXT/JSON report model. |
| `main` | Argument validation and resource lifecycle. |

Run the integration suite:

```bash
python3 -m unittest discover -s tests -v
```

Save its output without writing bytecode caches:

```bash
mkdir -p logs
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v > logs/validation.log 2>&1
```

Tests cover fixture seeds, replay SQL, unchanged input files, shuffled-row decoys, duplicates, alternative transpose mappings, BLOBs, Unicode, quoted identifiers, HTML escaping, infinities, NULL, dynamic types, strict comparison, composite keys, multi-hop joins, unindexed discovery, query interruption, and evidence/findings limits.

Exit codes: `0` means reports were written, possibly with incomplete coverage; `2` indicates an input or operation error; `130` indicates operator interruption. Inspect report coverage rather than relying on the process exit code alone.
