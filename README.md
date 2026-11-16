# SQLite Locator

An educational SQLite example of finding where a table from an application screen is stored. The search starts with the **values you can see**. It uses them to identify candidate columns, verify which values belong together, and report the source tables, columns, and records. The same sample can be checked against ordinary rows, a transposed table, entity–attribute–value (EAV) records, or joined tables.

The repository includes a standalone search script and a reproducible data generator. You can inspect the evidence in a browser, rerun the SQL behind a finding, and reproduce the examples locally with the Python standard library.

## Start with two rows

Suppose an application displays this sample:

| Variety | Color | Quantity |
| --- | --- | ---: |
| Gala G-418 | red | 263 |
| Russet R-927 | green | 137 |

A source column named `c_72a3` contains both `Gala G-418` and `Russet R-927`. Its **containment** for Variety is `2 / 2`, even if the source column has millions of other values. A different column that contains both colors also scores `2 / 2`. If the source pairs Gala with green and Russet with red, the score is still the same. To verify the layout, the program checks the pairs and quantities **on the same records**, with one consistent mapping from GUI fields to source columns.

This is the distinction between finding a value and explaining a row. A retained source record is called a **witness**. A complete finding supplies a witness for every non-NULL cell under one layout. Another table can also pass the test if it contains a copy of the same records, so sample agreement cannot establish which historical query produced the screen.

## How the search works

### 1. Read the schema and choose a search order

The program reads `sqlite_schema` and looks up column, index, and foreign-key metadata in batches. Table and column names are used to address records and report findings. Values supply the evidence. SQLite values have their own storage types, so a column's declared type does not rule it out. The catalog accepts a table count limit after optional name filters. With no explicit count limit, time still limits how far it can get. [SQLite schema table](https://www.sqlite.org/schematab.html) · [Table-valued PRAGMAs](https://www.sqlite.org/pragma.html#pragfunc) · [SQLite datatypes](https://www.sqlite.org/datatype3.html)

In the default bounded mode, one short query identifies empty tables. Across the nonempty tables, the program first checks eligible indexes for up to three priority values. It favors values that repeat less in the frontend sample and uses value type and length to break ties. Those are hints about which checks to run first. The source frequencies are unknown. It then reads limited head and tail samples, makes a few rowid seeks where available, and probes indexes for the remaining values. A small table already covered by the sample needs no further index probes.

Index probes bind values and use the index's collation. If multiple indexes cover a column, `BINARY` is preferred because the value comparison is case-sensitive. The program checks that SQLite reports a `SEARCH` access path before executing an index probe. Actual values are checked again in Python. [SQLite collations](https://www.sqlite.org/datatype3.html#collation) · [EXPLAIN QUERY PLAN](https://www.sqlite.org/eqp.html)

### 2. Keep an index of what was found

The in-memory index maps a normalized value to matching source columns and record locators. It only contains observations from the current run. For ranking columns:

```text
containment = distinct requested values observed in a column
              / distinct values requested for the field
```

Containment ignores how many extra values are in the source column. That is useful when the sample is tiny relative to the source. The report also gives more weight to values observed in fewer columns, using inverse document frequency (IDF). These scores rank candidates. They are not probabilities.

A common color can fill its occurrence quota long before a rare product name is found. When a record is retained because of the name, **every matching cell on that record** is indexed, including its color. The matcher starts with the shortest list of retained records for the requested cells and verifies the other values on those records. The per-value quota still limits which new records can be admitted. The global row and byte limits stop discovery when evidence capacity is reached.

The default comparison treats `12`, `12.0`, and simple numeric text such as `"12.00"` as equivalent. `--comparison strict` keeps text distinct from numbers. Leading zeros, case, whitespace, currency, and date formats retain their meaning. BLOBs compare byte by byte. An index probe can try only a finite list of text representations of a number. Samples and scans can encounter additional forms. NULL supplies no evidence.

### 3. Test how the records are arranged

| Layout | What the program checks |
| --- | --- |
| Rows | One mapping from GUI fields to distinct source columns. The requested values for each checked record must occur together. |
| Transpose | The same check with GUI rows and columns exchanged. Source rows and source columns can appear in any order. |
| EAV | One column contains values, another identifies attributes, and a third groups records by entity. For example, two records can encode `(P42, a7, Gala G-418)` and `(P42, a8, red)`. The program tries consistent attribute mappings and can look up additional cells by entity ID when an index is available or the table is known to be small. |
| Join | Foreign keys supply paths between candidate tables, including paths through tables with no matching cells. The program tries declared paths first. It can also propose relationships from shared identifiers in retained records and then check them with SQL. The report labels inferred relationships. |

Column candidates come from observed hits. In a join search, the foreign-key graph is considered before column caps are applied to each table in a path. That lets a related table with a common value survive even if many unrelated tables contain the same value.

The program tries alternative field mappings under explicit limits. For repeated GUI rows, it uses **bipartite matching** to assign distinct source records. Think of a graph with GUI rows on the left and eligible source records on the right. A link means that record agrees with the row under the mapping. The algorithm can reassign an earlier row when doing so lets both rows use different valid records. Reusing one record twice would overstate the evidence.

A layout can be partial if a displayed field has no viable source column or some rows could not be verified. A structural finding generally needs at least two sample records and four confirmed cells, with lower thresholds for smaller samples. The individual-value section lists evidence that does not meet those thresholds.

## Time and space complexity

Use the following quantities when estimating a run:

| Symbol | Meaning |
| --- | --- |
| `T`, `M`, `I` | Number of source tables, columns, and eligible indexed columns |
| `N_t`, `C_t` | Row count and column count of table `t` |
| `K`, `S`, `W` | Distinct GUI values, sample rows per table, and witness limit |
| `R`, `H` | Retained source records and matching cells indexed across those records |
| `U`, `A` | GUI records and eligible GUI-to-source record pairs for one mapping |

| Stage | Cost and what drives it |
| --- | --- |
| Catalog | Work grows at least linearly with the table, column, index, and foreign-key entries inspected. Batching reduces SQL calls while SQLite still has to examine the schema. Empty tables require one data check each in bounded mode. |
| Indexed search | Up to `I × K` logical probes, plus an access-plan check per probed column. In table `t`, a B-tree seek is approximately `O(log N_t)`. Each probe may fetch up to `W` candidate rows, with `O(W × C_t)` cell checks in the worst case. Index collation, selectivity, and disk cache affect elapsed time. |
| Bounded sampling | With `J` additional rowid seeks per table, up to `S + 4J` sampled records per table. Comparing their cells costs approximately `O(sum_t((S + J) × C_t))`, plus the seeks. Confirmation for an indexed EAV entity adds the cost of an index seek per ID and the matching records visited. |
| Exhaustive search | The initial discovery pass visits each row. Worst-case cell work is `Theta(sum_t(N_t × C_t))`, even when very few result rows reach Python. Query limits can interrupt that pass. |
| Mapping and row checks | If a layout has `P` positions with up to `B` candidates each, there can be up to `B^P` combinations before injectivity and other constraints. The program caps the number of mappings and search states. For each mapping, it verifies records from the shortest posting list for each row. The augmenting-path matcher takes up to `O(U × A)` time after the eligible pairs have been found. |
| Join search | A graph with branching factor `D` can yield up to `O(D^h)` paths of length at most `h` from one starting table. The engine limits path length, expansion, and SQL checks. Inference uses identifier postings and skips IDs shared by more than 32 candidate columns to bound pair generation. |

The source database stays on disk. The catalog uses memory proportional to its inspected metadata and tables. Witness storage contains up to `R` source records (at worst `O(R × max(C_t))` stored cells), plus `O(H)` matching positions and temporary search state. The configurable byte limit estimates the witness payload. Python objects, indexes, query results, and report rendering add overhead. Report serialization and the TXT/JSON coverage lists grow with the number of tables.

An index can often answer an absence query from a few pages. In an unindexed table, a unique target might be on the last page. An algorithm that must prove absence in that case needs a scan or an existing profile. Sampling caps the work and can miss a rare value. Similarly, removing the catalog count limit lets the tool attempt larger schemas, but query cost and the time budget still determine coverage. There is no constant-time or million-table guarantee.

## Run the example

From the repository root, use Python 3.11 or newer with `sqlite3` and SQLite 3.33 or newer. No packages or network access are required. A modern browser opens the self-contained HTML report.

```bash
python3 scripts/generate_demo.py --profile small
python3 src/sqlite_locator.py \
  --frontend data/demo/frontend.sqlite \
  --source data/demo/source.sqlite \
  --report reports/demo/report.html
```

The generator creates `frontend.sqlite`, `source.sqlite`, and `expected.json`. The last file records the placement of the synthetic examples so you can compare them with the findings. The search script reads only the two databases. Try the decoy in the manifest to see the difference between finding every individual value and matching their row relationships.

The source and frontend files are opened read-only, with a consistent snapshot within each file. Save a real GUI sample as one ordinary SQLite table per screen or extract, with GUI labels as column names. Keep each record on one row and preserve text identifiers such as `0012`. Without `ORDER BY`, SQLite does not guarantee screen order. Report row numbers follow the frontend query order.

```bash
python3 src/sqlite_locator.py \
  --frontend data/frontend.sqlite --frontend-table "Orders" \
  --source data/source.sqlite --table-pattern 'archive_*' \
  --report reports/orders.html
```

Repeat `--frontend-table` or `--table-pattern` to select multiple tables. Source filters also limit the tables available for join paths.

### Read the report

The HTML displays verified layouts, coverage, the GUI sample, and individual value locations. Each proof identifies the exact source column and rowid or primary key. The TXT and JSON files include the full inspected catalog. The HTML shows up to 1,000 tables in its coverage panel, prioritizing those with evidence or interruptions.

Replay SQL uses the recorded locations and emits verified cells in GUI order. Unverified cells become NULL placeholders. The SQL is a recipe for checking the sample. Record locators may change after source maintenance. Keep reports under the same access controls as the source data.

**Not found within budget** means the search found no witness within its scope. Check the coverage for uninspected tables, empty tables, truncated probes, and other limits before interpreting the result.

### Change the search budget

Run `python3 src/sqlite_locator.py --help` for every option.

| Option | Default | Effect |
| --- | ---: | --- |
| `--mode` | `bounded` | Use indexes and samples. `exhaustive` permits full discovery scans. |
| `--total-seconds` | 120 | Search budget excluding report writing. Up to 10% (at most 10 s) is reserved for verification when discovery uses its allocation. |
| `--query-seconds`, `--query-vm-steps` | 0.5, 1,000,000 | Approximate limits per query. |
| `--max-tables` | 0 | No table count limit. A positive value caps the source catalog after filters and reports if more matching tables remain. |
| `--max-columns` | 256 | Skip and report wider tables. |
| `--sample-rows`, `--random-seeks` | 160, 24 | Set sample size and additional rowid seeks. A sample size of zero disables bounded sampling. |
| `--witnesses` | 32 | Set the occurrence admission threshold and the maximum records per index probe or row verification. |
| `--max-evidence-rows`, `--max-evidence-bytes` | 12,000, 67,108,864 | Stop discovery at the witness capacity. |
| `--max-frontend-rows`, `--max-frontend-cells` | 100, 400 | Reject a GUI sample over these limits. |
| `--min-containment` | 0.6 | Minimum observed fraction of distinct values for a structural column candidate. |
| `--candidates-per-column`, `--max-mappings` | 8, 128 | Limit alternatives for a field within a table and mappings per layout. |
| `--max-join-hops`, `--max-join-paths` | 2, 80 | Limit join length and path exploration. Zero hops disables joins. |
| `--max-findings` | 100 | Keep the best verified findings within this limit. |

The progress handler checks time and instruction limits approximately. Disk stalls and report writing can extend wall time. The engine handles ordinary tables, rowid and composite primary keys, and simple join paths. Queries that use aggregates, transformations, or general join graphs are outside the example. [SQLite progress handler](https://www.sqlite.org/c3ref/progress_handler.html)

### Generate at larger scale

The generator defaults to the large profile. Set aside tens of GiB of disk space for it.

| Profile | Noise tables | Rows per noise table | Additional scenario tables |
| --- | ---: | ---: | ---: |
| `small` | 30 | 180–700 | 9 |
| `large` | 3,000 | 18,000–70,000 | 9 |

```bash
python3 scripts/generate_demo.py --profile large --output-dir data/large --seed 42
python3 src/sqlite_locator.py \
  --frontend data/large/frontend.sqlite --source data/large/source.sqlite \
  --report reports/large/report.html
```

The large profile increases both the number of tables and the row count per noise table by 100 times. Planted values are indexed by default. To study how indexes change the search, use `--no-target-indexes` and compare bounded mode with `--mode exhaustive` on the small fixture. The generator estimates disk needs before it starts. `--force` replaces only its generated outputs.

### Example measurements

These measurements used local SQLite files and exclude report writing. The large benchmark reused an existing synthetic fixture with indexes on planted values.

| Dataset | Source queries | Search time | Verified outcome |
| --- | ---: | ---: | --- |
| 3,000 empty indexed tables, before batching and empty-table checks | 33,001 | 0.235 s | Baseline |
| Same 3,000 tables with batching and empty-table checks | 3,096 | 0.078 s | All empty tables checked, zero value probes |
| 12,000 empty indexed tables | 12,376 | 0.340 s | All tables checked at default settings |
| 3,009 tables, 133,196,564 rows, 13.12 GiB | 217,099 | 5.59 s | All six expected complete layouts found |

The empty-table comparison uses the same two-by-two sample. The large number of rows is the fixture size. Bounded mode visits a small fraction of those records. Cache state, schema width, index selectivity, and the data distribution all affect the timings.

## Source and tests

| File | Role |
| --- | --- |
| [`src/sqlite_locator.py`](src/sqlite_locator.py) | Read-only database gateway, evidence collection, layout verification, CLI, and reports |
| [`scripts/generate_demo.py`](scripts/generate_demo.py) | Reproducible SQLite fixtures and ground truth |
| [`tests/test_locator.py`](tests/test_locator.py) | Regression cases and checks of replay SQL |

```bash
python3 -m unittest discover -s tests -v
```

The test cases include a page of repeated colors preceding the target records, common EAV values recovered through entity IDs, join candidates among 30 lookalike tables, binary and case-insensitive indexes on the same column, and partial evidence. Generated databases and reports are ignored by Git.

## License

The project source is under the [BSD Zero Clause License](LICENSE) (`0BSD`). It permits use, modification, and redistribution without an attribution condition or a requirement to place derived work under the same license. The project uses the installed Python standard library and SQLite and bundles neither runtime. [SPDX license entry](https://spdx.org/licenses/0BSD.html) · [Python license](https://docs.python.org/3/license.html) · [SQLite public-domain dedication](https://www.sqlite.org/copyright.html)
