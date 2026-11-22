# Datasette chart suggestions: specification

- **Status:** Draft 1
- **Date:** 2026-11-22
- **Working name:** `datasette-charts` (placeholder; not yet checked against existing plugin names)
- **Datasette baseline:** commit `1d629fb` (2026-11-21, after 1.0a43). File and line references in this document are to that commit.

## 1. Summary

A Datasette plugin that looks at a table, view or arbitrary SQL query and suggests a ranked list of charts for it. Each suggestion is a small JSON document in our own chart DSL. The user sees the suggestions as thumbnails or icons, picks one, and can then adjust it in a manual builder that edits the same JSON.

The work is split into a pipeline:

```
source ─► profile ─► propose ─► verify ─► rank ─► suggestions (JSON)
            │        (per chart type)     (central)      │
            ▼                                            ▼
   internal database                        compile (per chart type)
   (stored profiles)                             │
                                                 ▼
                                    SQL ─► bounded rows ─► render (JavaScript)
```

Profiling is shared and runs once per source. Each chart type bundles its own heuristics (`propose`), its own SQL generation (`compile`) and its own renderer. A central ranker orders candidates across chart types. Charts never fetch every row: each chart type compiles to SQL that returns a bounded, aggregated result.

## 2. Goals and non-goals

### Goals

1. **Fast path to a useful chart.** Get someone from a freshly imported table to a useful visualization as quickly as possible. They should not need to know which chart types exist or which fit their data.
2. **Any source.** Work for any table, view or arbitrary SQL query, with dozens of columns and thousands to millions of rows.
3. **Bounded results.** Never retrieve every row. Aggregation, binning and downsampling happen in SQL, including window functions where they help.
4. **Extensible chart types.** A chart type is a self-contained bundle of heuristics, SQL generation and rendering. Third-party plugins can add chart types and use any charting library.
5. **Deterministic.** The same data and the same plugin versions give the same suggestions in the same order.
6. **Works without `execute-sql`.** The logic runs in Python, so users who can view a table but cannot run arbitrary SQL from JavaScript still get suggestions and charts.
7. **Manual parity.** Anything the system can suggest, a user can build by hand.

### Non-goals for the first version

- LLM-based ranking or explanations. This may come later; section 10.5 notes where it would attach.
- Databases other than SQLite.
- Saved charts, dashboards and embedding. These have not been discussed yet.
- Interactive re-suggestion. Suggestions are produced once per source; refinement is manual.
- Solving expensive arbitrary queries, for example by materializing them. We will deal with that when it happens.
- Implementing date and timestamp detection. This spec assumes that system exists (section 7.5).

## 3. Decisions so far

These were settled in discussion. Everything else in this document is a proposal that follows from them, and section 19 lists what is still open.

| # | Topic | Decision |
|---|-------|----------|
| D1 | Form | A Datasette plugin. Most of the work happens in Python so it works when arbitrary SQL is not available to the user's JavaScript. |
| D2 | Suggestion format | JSON in our own DSL. |
| D3 | Previews | Ideally a thumbnail per suggestion. Where that is too expensive, show a chart type icon and load the preview on hover. |
| D4 | Sources | Design for arbitrary SQL queries from the start. Expensive queries are a problem for later. |
| D5 | Determinism | Suggestions are deterministic. An LLM may be in scope later. |
| D6 | Extensibility | Design the chart type interface for plugin authors, and make sure the default chart set covers the most important cases. |
| D7 | Interaction | Suggestions are one-shot. Users can also build any suggestable chart manually. |
| D8 | Column detection | Detect everything useful: temporal columns, latitude/longitude, identifiers, foreign keys, categories versus free text, and more. |
| D9 | Large tables | Profile large or expensive tables in a Datasette background task and store the results in a table in the internal database. |
| D10 | Time limits | Datasette core can be changed so `execute()` can raise the time limit beyond the default. |
| D11 | Rendering | Built-in charts use Observable Plot. The extension mechanism lets plugins use any charting library, or their own code. |
| D12 | Default chart set | Histogram, category bar, time-series line, scatter or binned heatmap, map, and grouped or stacked bar. |
| D13 | Reasons | Each suggestion carries a plain-English reason, shown to the user. |
| D14 | Staleness | A stored profile goes stale after a time limit, or when the row count changes by more than a set percentage. |
| D15 | Temporal detection | Assume a separate system reports likely date and timestamp columns. |

Two questions were explicitly left open for prototyping: the profiling budget, and how to rank across chart types. Sections 7.3 and 10.3 propose starting points for each.

## 4. Terminology

- **Source:** the thing being charted: a table, a view, an arbitrary SQL query or a stored query.
- **Profile:** the facts the profiler has gathered about a source and its columns.
- **Role:** the semantic kind of a column, such as temporal, measure or category.
- **Chart type:** a registered bundle of Python heuristics, SQL generation and a JavaScript renderer.
- **Spec:** a JSON document in the chart DSL that fully describes one chart of one source.
- **Candidate:** a spec proposed by a chart type, with a score and a reason.
- **Suggestion:** a candidate that survived ranking and is shown to the user.
- **Tier:** how much work went into a profile: schema only, sample, or full.

## 5. Architecture

| Stage | Owned by | Database access | Output |
|-------|----------|-----------------|--------|
| Profile | Shared profiler | Yes: a small number of aggregate queries | `Profile` |
| Propose | Each chart type | None | Candidates |
| Verify (optional) | Each chart type | Yes: budgeted, cheap queries | Adjusted or dropped candidates |
| Rank | Central ranker | None | Ordered suggestions |
| Compile | Each chart type | None (produces SQL) | SQL with a bounded result |
| Render | Each chart type (JavaScript) | None | A drawn chart |

Design consequences:

- **One DSL for everything.** Suggestions and the manual builder both produce specs. The server only ever compiles specs, so there is one code path to secure and test.
- **`propose` is a pure function of the profile.** That makes heuristics cheap, deterministic and unit-testable without a database.
- **The DSL describes intent, not presentation.** It names a chart type, columns, aggregations and binning. It contains no SQL and no styling.

Proposed package layout:

```
datasette_charts/
    __init__.py          # Datasette hook implementations
    hookspecs.py         # register_chart_types
    sources.py           # Source
    profiler/            # tiers, SQL generation, roles, staleness
    worker.py            # background profile worker
    engine.py            # propose, verify, rank
    dsl.py               # spec validation
    chart_types/         # built-in chart types (Python halves)
    static/              # Observable Plot, panel UI, built-in renderers
    views.py             # HTTP endpoints
    cli.py               # `datasette charts ...` commands
```

## 6. Sources

```python
@dataclass(frozen=True)
class Source:
    database: str
    kind: str                 # "table" | "query" | "stored_query"
    table: str | None = None  # table or view name
    sql: str | None = None
    params: dict | None = None
    query_name: str | None = None
```

- Every profiling and chart query has the shape `with source as (...) select ... from source`. The CTE body is `select * from "table"` for tables and views, or the query's own SQL.
- `Source.key` identifies the source for stored profiles: the table name, or a SHA-256 of the SQL plus canonical JSON params.
- Table sources with a rowid allow cheap tricks that queries and views do not: evenly spaced sampling, chunked scans and `max(rowid)` as a growth signal.
- Arbitrary queries are re-executed by every profiling and chart query that wraps them. That is accepted for now (D4).
- Query SQL must pass Datasette's existing read-only validation before it is wrapped.

Filtered table pages (for example `?state=CA`) are an open question; see section 19.

## 7. Profiling

### 7.1 Facts

Per source:

- `row_count` and whether it is exact
- `rows_profiled` and `basis` (`"sample"` or `"full"`)
- primary keys, foreign keys and indexes (table sources)
- detected column pairs, such as latitude with longitude
- bookkeeping: `profiled_at`, `schema_version`, `max_rowid`, `profiler_version`

Per column:

- name, position, declared type and SQLite affinity
- counts by observed storage type (`typeof()`): integer, real, text, blob, null
- null rate
- distinct count, capped (exact up to a cap, otherwise "more than the cap")
- whether values are unique, where known cheaply (primary key, unique index, or sample evidence)
- numeric: min, max, mean
- text: min, max and mean length
- top values with counts, for columns with low distinct counts
- temporal: format, earliest and latest value (from the temporal detector, section 7.5)
- the Datasette column type, if one is assigned
- role, secondary roles and the evidence for them

Illustrative shape:

```json
{
  "row_count": 1843022,
  "row_count_exact": true,
  "basis": "full",
  "columns": [
    {
      "name": "created_at",
      "role": "temporal",
      "temporal": {"format": "iso_datetime", "min": "2023-11-02", "max": "2026-11-20"},
      "null_rate": 0.0,
      "distinct": {"count": 1000, "capped": true}
    },
    {
      "name": "status",
      "role": "category",
      "null_rate": 0.02,
      "distinct": {"count": 7, "capped": false},
      "top_values": [["paid", 1203311], ["pending", 301220]]
    }
  ]
}
```

### 7.2 Roles

Each column gets one primary role and may get secondary roles. A `year` integer is both temporal and a category; a 1 to 5 rating is both a measure and a category.

| Role | Meaning | Typical evidence |
|------|---------|------------------|
| `temporal` | Date, datetime, Unix timestamp or year | Temporal detector, assigned column type |
| `measure` | Numeric quantity worth aggregating | Numeric storage, many distinct values, not an identifier |
| `category` | Small set of repeated values | Low distinct count relative to rows |
| `boolean` | Two values | 0/1, true/false, yes/no |
| `identifier` | Row identity, not worth charting | Primary key, unique, names like `id`, `*_id`, `uuid` |
| `foreign_key` | Reference to another table | Declared foreign key; chart as a category using the referenced label |
| `latitude`, `longitude` | Geographic coordinates | Value range plus name patterns, found as a pair |
| `text` | Free text | High distinct count, long values |
| `url`, `email`, `json` | Structured text | Datasette column types |
| `blob` | Binary data | Storage type |
| `constant` | One distinct value | Nothing to chart |
| `unknown` | None of the above | |

Role assignment is deterministic and rule-based. The rules, thresholds and name patterns live in one module so the eval harness (section 17) can track changes to them.

### 7.3 Tiers and flow

| Tier | Runs | Cost | Provides |
|------|------|------|----------|
| 0: Schema | Always, inline | No table scan | Names, declared types, keys, indexes, column types, name-based hints |
| 1: Sample | Inline, under the normal SQL time limit | Bounded by sample size | Approximate facts for every column |
| 2: Full | Inline if it fits the budget, otherwise in the background | One or more full scans | Exact facts |

When suggestions are requested:

1. Look up a stored profile for the source. If one exists and is fresh (section 7.8), use it.
2. Otherwise run tiers 0 and 1 inline and return suggestions immediately, marked `"basis": "sample"`.
3. If the source is small (row count under `inline_row_limit`), the sample is the whole source. The facts are exact and nothing needs to be stored.
4. Otherwise enqueue a full profile for the background worker (section 7.7). The page polls for status and offers refreshed suggestions when the full profile lands.
5. If a stored profile exists but is stale, serve suggestions from it immediately and enqueue a refresh.

The right budgets, sample sizes and thresholds are an open question that we agreed to answer by experiment. The defaults in section 16 are starting points for those experiments.

### 7.4 Profiling SQL

The patterns in this section and in section 11 were run against a 200,000-row SQLite test table (SQLite 3.53.1) to confirm that they execute and return the expected shapes. They have not been benchmarked at the millions-of-rows scale.

**One scan for many columns.** Basic facts for every column come from a single aggregate query, with one group of expressions per column:

```sql
with source as (select * from "orders")
select
  count(*) as row_count,
  count("price") as price__non_null,
  sum(typeof("price") = 'integer') as price__n_integer,
  sum(typeof("price") = 'real') as price__n_real,
  sum(typeof("price") = 'text') as price__n_text,
  min("price") as price__min,
  max("price") as price__max,
  avg(case when typeof("price") in ('integer', 'real') then "price" end) as price__mean
  -- ...repeated for each column
from source
```

**Capped distinct counts.** Chart heuristics mostly need to know whether a column has few distinct values, not the exact count. This stops as soon as the cap is passed:

```sql
with source as (select * from "orders")
select count(*) from (select distinct "status" from source limit 1001)
```

A high-cardinality column returns almost instantly. A low-cardinality column without an index still needs a full scan, so in the sample tier this runs against the sample.

**Top values.** For columns under the distinct cap only:

```sql
select "status", count(*) as n from source group by 1 order by n desc limit 20
```

**Sampling.** For rowid tables, the proposed sample is evenly spaced rowid probes. It is deterministic, spans the whole table, and costs one index lookup per probe:

```sql
with recursive probes(i, r) as (
  select 0, :lo
  union all
  select i + 1, :lo + (i + 1) * :step from probes where i + 1 < :n
)
select t.* from probes join "orders" t on t.rowid = probes.r
```

`:lo` and `:step` come from `min(rowid)` and `max(rowid)`, which are both instant. Deleted rows produce misses, so the sample can be smaller than `:n`. For views, queries and tables without a rowid, the fallback is the first N rows (`select * from source limit :n`), which is cheap but biased toward whatever order the source produces.

**Chunked full scans.** For rowid tables, the full tier runs the one-scan query in rowid ranges (`where rowid >= :lo and rowid < :hi`) and merges results in Python. Counts, sums, minimums and maximums merge exactly. This keeps each query short, frees the SQL thread between chunks, and gives a natural progress measure. Distinct counts and top values do not merge across chunks, so they run afterwards as separate per-column queries, each under a time limit; a column whose query times out keeps its sample-tier estimate.

Queries and views cannot be chunked. Their full profile is one aggregate query under the extended time limit (section 15). If it times out, the sample-tier profile stays in place and the stored profile records that the full tier was not reached.

### 7.5 Temporal detection (assumed input)

This spec assumes a separate system reports likely date and timestamp columns (D15). The profiler needs this interface from it:

```python
async def detect_temporal_columns(datasette, source) -> dict[str, TemporalInfo]: ...

@dataclass
class TemporalInfo:
    kind: str          # "date" | "datetime" | "unix_seconds" | "unix_ms" | "year"
    confidence: float  # 0 to 1
```

Notes:

- `kind` determines the SQL expression chart types use to bucket the column, for example `strftime('%Y-%m', col)` versus `strftime('%Y-%m', col, 'unixepoch')`.
- Datasette's column type system is a natural place for users to confirm or correct detections on tables. Core has a `column_types` table and a `register_column_types` hook, but its built-in types are only `url`, `email`, `json` and `textarea`; there are no date types and no automatic detection. An assigned column type should override detection.
- Query sources have no table to attach column types to, so detection for them has to work from names and values.

### 7.6 Storage

Profiles and jobs live in the internal database, in two tables. Keeping them separate lets the last good profile keep serving suggestions while a refresh is running.

```sql
create table datasette_charts_profiles (
    id integer primary key,
    database_name text not null,
    source_kind text not null,       -- 'table' | 'query' | 'stored_query'
    source_key text not null,        -- table name, query name, or hash of sql + params
    basis text not null,             -- 'sample' | 'full'
    profile_json text not null,
    row_count integer,
    row_count_exact integer,
    max_rowid integer,
    schema_version integer,          -- PRAGMA schema_version when profiled
    columns_hash text,               -- hash of column names and declared types
    profiler_version integer not null,
    profiled_at text not null,
    duration_ms integer,
    unique (database_name, source_kind, source_key)
);

create table datasette_charts_profile_jobs (
    id integer primary key,
    database_name text not null,
    source_kind text not null,
    source_key text not null,
    source_json text not null,       -- full Source, including SQL for queries
    status text not null,            -- 'pending' | 'running' | 'complete' | 'failed' | 'cancelled'
    progress real,                   -- 0 to 1
    error text,
    requested_by text,               -- actor id
    cancel_requested integer not null default 0,
    created_at text not null,
    started_at text,
    finished_at text
);
```

Constraints from Datasette that shape this:

- The internal database is a temporary file that is deleted at exit unless Datasette is started with `--internal path.db` or `DATASETTE_INTERNAL` is set. Stored profiles are therefore a cache: everything in them must be rebuildable, and nothing may depend on them existing.
- Datasette asks plugins to prefix internal table names (`datasette_charts_*`), keep writes short and avoid exposing other plugins' data.
- Tables are created in the `startup` hook.
- Profiles contain data values (top values, minimums, maximums). The internal database is not exposed by default, but the profile endpoint (section 12) must apply the same permission checks as the source itself.

### 7.7 Background worker

Datasette's background task mechanism (new in 1.0a40) runs an `async def task(datasette)` as an asyncio task on the server's event loop. It keeps state in memory only: no queue, progress, results, deduplication or resume after a restart. It also never prunes finished task handles, and its docstring warns against registering one task per job.

So the plugin registers **one long-lived worker** at startup and keeps all job state in its own table:

```python
@hookimpl
def startup(datasette):
    async def inner():
        await create_tables(datasette)
        await requeue_interrupted_jobs(datasette)
        datasette.add_background_task(profile_worker, name="datasette-charts-profiler")
    return inner
```

Worker behaviour:

1. Wait for a wake-up signal (an `asyncio.Event` set when a job is enqueued), with a slow poll as a fallback.
2. Take the oldest `pending` job. Process one job at a time, so background profiling never holds more than one SQL thread.
3. Run the full tier in chunks (section 7.4). After each chunk, write `progress` with a short `execute_write` call and check `cancel_requested`.
4. On success, upsert the profile and mark the job `complete`. On failure, record the error and mark it `failed`.

Other rules:

- **Deduplication:** enqueueing a source that already has a `pending` or `running` job returns the existing job.
- **Restart:** at startup, jobs left in `running` are set back to `pending`. This only matters when the internal database is persistent.
- **Cancellation:** cancelling the asyncio task does not stop a SQLite query that is already running in a thread. Cancellation therefore takes effect between chunks, and each chunk is bounded by its time limit.
- **Permissions:** background tasks carry no actor. Permission is checked when the job is enqueued, and the actor id is recorded on the job.
- **Thread pool:** SQL reads share a pool of `num_sql_threads` threads (default 3) with page requests. Chunking and single-job concurrency keep the impact down; section 19 lists this as a risk to measure.

### 7.8 Staleness

A stored profile is stale when any of these is true (D14, plus one addition):

1. **Age:** it is older than `stale_after_hours`.
2. **Row count:** the row count has changed by more than `stale_row_change` (a fraction) since it was profiled.
3. **Schema (proposed addition):** the source's columns have changed. Column facts no longer line up, so this invalidates immediately. Core already records `PRAGMA schema_version` per database in `catalog_databases`; when that changes, compare `columns_hash` for the specific source.

Measuring the row count cheaply:

- Core's table counts are capped at 10,000 rows (`Database.count_limit`), so they cannot show a percentage change on a large table.
- For rowid tables, use `select max(rowid)`. It is instant at any size, and its growth approximates the rows inserted since the profile.
- For tables without a rowid, use a time-limited `count(*)`.
- Neither signal sees deletes or in-place updates. The age rule covers those.

How the rule varies by source:

| Source | Rule |
|--------|------|
| Table in a mutable database | Age, row count and schema |
| Table in an immutable database | Never stale |
| Arbitrary or stored query | Age only; counting rows means running the whole query |
| Small source (under `inline_row_limit`) | Not stored; profiled inline each time |

A stale profile is still used: suggestions are served from it at once and a refresh is enqueued. This is safe because charts always run live SQL. A stale profile can only affect which charts are suggested and in what order, never the data a chart shows. One exception: if a spec carries explicit bin parameters taken from the profile (section 9.1), values outside the old range must still be handled, which section 11.2 covers.

## 8. Chart types

A chart type has a Python half and a JavaScript half. The DSL is the contract between them.

### 8.1 Python half

```python
class ChartType:
    name: str            # unique, lowercase, e.g. "line"
    label: str           # "Line chart"
    description: str
    icon: str            # inline SVG, used when no thumbnail is shown
    js_url: str          # URL of the ES module that exports render()
    channels: list[Channel]
    max_rows: int        # upper bound on rows this chart ever needs

    def propose(self, profile: Profile) -> list[Candidate]:
        """Pure function. No database access."""

    async def verify(self, candidate: Candidate, ctx: VerifyContext) -> Candidate | None:
        """Optional. May run cheap, budgeted queries. Return an adjusted
        candidate, or None to drop it."""
        return candidate

    def validate(self, spec: dict, columns: list[str]) -> list[str]:
        """Return a list of error messages. The default implementation
        checks the spec against `channels`."""

    def compile(self, spec: dict, source: Source, profile: Profile | None) -> CompiledQuery:
        """Return SQL and params that select from the `source` CTE and
        return at most `max_rows` rows."""

    def title(self, spec: dict) -> str:
        """Deterministic title, e.g. "Rows per month of created_at"."""
```

```python
@dataclass
class Channel:
    name: str                      # "x", "y", "series", "color", ...
    roles: tuple[str, ...]         # roles a column should have to fit here
    required: bool = True
    aggregates: tuple[str, ...] = ()   # allowed aggregates, if any
    binnable: bool = False
    allows_time_unit: bool = False

@dataclass
class Candidate:
    spec: dict
    score: float                   # 0 to 1, see section 10.3
    reason: str                    # plain English, see section 10.4
    signals: dict                  # the facts behind the score, for debugging and evals

@dataclass
class CompiledQuery:
    sql: str
    params: dict
    max_rows: int
```

`channels` does double duty. It lets a default `propose` enumerate plausible column-to-channel assignments by role, and it lets the manual builder generate its controls without any chart-specific UI code.

### 8.2 JavaScript half

Each chart type's `js_url` is an ES module, loaded with a dynamic `import()` the first time that chart type is needed:

```js
export function render(el, { spec, rows, columns, size, width, height, Plot }) {
  // Draw into `el`. May return a cleanup function.
}
```

- `rows` are the already-aggregated rows from `compile`. The renderer does no data fetching.
- `size` is `"thumbnail"` or `"full"`. Thumbnails omit axes, labels and legends, so one renderer serves both.
- `Plot` is the Observable Plot module shipped with the base plugin, passed in so built-in and third-party renderers can share it. A third-party renderer is free to ignore it and load any other library.
- For built-in charts, Plot only draws. Binning, grouping and top-N all happen in SQL so that results stay bounded, which means Plot's own bin and group transforms are not used.

### 8.3 Registration

The plugin defines its own hook, in the same way `register_column_types` and `register_facet_classes` work in core:

```python
# datasette_charts/hookspecs.py
@hookspec
def register_chart_types(datasette):
    """Return a list of ChartType subclasses"""
```

A third-party chart type is then an ordinary Datasette plugin:

```python
from datasette import hookimpl
from datasette_charts import ChartType

class SankeyChart(ChartType):
    name = "sankey"
    ...

@hookimpl
def register_chart_types(datasette):
    return [SankeyChart]
```

The built-in chart types register through the same hook, so they exercise the same interface third parties get.

### 8.4 The bounded results rule

Every chart type must guarantee an upper bound on the rows it returns, independent of the size of the source. The server enforces this regardless of what a chart type does:

- The data endpoint wraps the compiled SQL as `select * from (...) limit :max_rows + 1` and reports `truncated: true` if the extra row appears.
- A global hard cap (proposed: 5,000 rows) applies on top of any chart type's own `max_rows`.

## 9. The chart DSL

### 9.1 Spec

```json
{
  "v": 1,
  "type": "line",
  "encoding": {
    "x": {"column": "created_at", "timeUnit": "month"},
    "y": {"aggregate": "count"},
    "series": {"column": "status", "limit": 5, "other": true}
  },
  "options": {}
}
```

- `v`: DSL version.
- `type`: a registered chart type name.
- `encoding`: one entry per channel the chart type declares.
- `options`: chart-type-specific settings. Each chart type validates its own.

Channel fields (the vocabulary borrows from Vega-Lite's encoding terms where they fit):

| Field | Meaning |
|-------|---------|
| `column` | Column name in the source |
| `aggregate` | `count`, `sum`, `avg`, `min`, `max`, `count_distinct` |
| `timeUnit` | `year`, `quarter`, `month`, `week`, `day`, `hour`, `minute` |
| `bin` | `{"maxbins": 40}` or explicit `{"start": 0, "step": 50}` |
| `limit`, `other` | Keep the top N values; optionally fold the rest into "Other" |
| `sort` | Ordering for categorical axes |

Suggested specs carry explicit bin parameters computed from the profile, so they compile to a single cheap query. A spec with only `maxbins` is also valid; the compiler then computes bounds itself in a CTE at the cost of an extra scan.

### 9.2 Suggestion envelope

A suggestion wraps a spec with ranking output. These fields are not part of the spec:

```json
{
  "spec": { "v": 1, "type": "line", "encoding": { } },
  "title": "Rows per month of created_at",
  "reason": "created_at spans 3 years; monthly buckets give 37 points",
  "score": 0.82,
  "basis": "sample",
  "preview": "eager"
}
```

`preview` is `"eager"` (load a thumbnail when visible) or `"on_demand"` (show the icon; load on hover or tap). The server decides this from the profile, so the client needs no size thresholds of its own (D3).

### 9.3 Validation and security

Specs arrive from the browser, so the data endpoint treats them as untrusted input:

- The DSL contains no SQL fragments of any kind.
- `type` must be registered. Channels must match the chart type's declaration.
- Every `column` must exist in the source's actual columns. Identifiers are always quoted by the compiler; values always go through parameters.
- Aggregates, time units and numeric parameters are checked against fixed lists and ranges.

## 10. Suggestion engine

### 10.1 Propose

For each registered chart type, call `propose(profile)`. A chart type returns zero or more candidates. Chart types are expected to prune hard: an unhelpful chart should not be proposed at all, not proposed with a low score.

### 10.2 Verify

Optional and budgeted. A chart type can confirm a promising candidate with one cheap query, typically against the sample: is this time series flat, do these two measures correlate, does this category have a dominant value. The engine gives each request a total verify budget (time and query count) and verifies candidates in descending score order until the budget runs out. Unverified candidates keep their original score.

### 10.3 Rank

Ranking across chart types is an open question to explore in prototypes. The proposed starting point:

1. **Shared score rubric.** Chart types score on a documented 0 to 1 scale so that scores are roughly comparable:

   | Score | Meaning |
   |-------|---------|
   | 0.9 and up | Almost certainly something the user wants to see first |
   | 0.6 to 0.9 | A solid, informative chart |
   | 0.3 to 0.6 | Plausible but unremarkable |
   | Under 0.3 | Do not propose |

2. **Central adjustments** for things no single chart type can know:
   - a per-chart-type weight, tunable in one place
   - a diversity pass: greedy selection that penalizes a candidate for sharing its chart type or its columns with suggestions already chosen
   - near-duplicate suppression, such as the same columns with a different aggregate
   - a preference for covering more of the source's interesting columns

3. **Determinism.** Ties break on fixed keys: chart type name, then column position in the source, then column name. No randomness and no wall-clock inputs.

4. **Cutoff.** Return the top `max_suggestions`.

Signals available to chart types when scoring:

| Signal | Effect |
|--------|--------|
| Role fit and confidence | Base score |
| Null rate | High null rates lower the score |
| Category cardinality | A handful to a few dozen values is ideal; one value or thousands is not |
| Category balance | One dominant value, or perfectly uniform counts, is less informative |
| Temporal span | More buckets at a sensible granularity is better; a single bucket is useless |
| Measure spread | Skew and outliers make a histogram more informative |
| Latitude/longitude pair | A map is rarely the wrong suggestion when one exists |
| Column names | Names like `price`, `amount`, `total` and `date` raise the score; `id`, `uuid` and `hash` lower it |
| Identifiers, constants, free text | Excluded from most channels |

The eval harness (section 17) is how these get tuned.

### 10.4 Reasons and titles

Each candidate has a `reason` built from a template plus profile facts, so it is deterministic: "created_at spans 3 years; monthly buckets give 37 points", "status has 7 values; the top 3 cover 81% of rows". Reasons are shown to users (D13). They serve the main goal: they tell someone who is not a visualization expert why this chart might be worth a look.

Titles come from `ChartType.title(spec)` and are also deterministic.

### 10.5 Where an LLM could attach later

Not in scope now (D5). The natural attachment points are re-ranking the deterministic candidates, rewriting reasons and titles, and proposing extra candidates as specs that go through the same validation. None of these needs a change to the chart type interface.

## 11. Default chart set

### 11.1 Overview

| Chart type | Channels | Proposes when | Result bounded by |
|------------|----------|---------------|-------------------|
| Histogram | `x`: measure (binned) | A measure has spread | Number of bins |
| Category bar | `x`: category; `y`: count or aggregated measure | A category has a workable number of values | Top N plus "Other" |
| Time-series line | `x`: temporal; `y`: count or aggregated measure; `series`: optional category | A temporal column spans several buckets | Number of time buckets times series |
| Scatter / binned heatmap | `x`, `y`: measures | Two measures, ideally correlated or clustered | Raw points under a threshold; otherwise a bin grid |
| Map | `latitude`, `longitude`; optional `size` | A latitude/longitude pair exists | Raw points under a threshold; otherwise grid cells |
| Grouped / stacked bar | `x`: category or temporal; `series`: category; `y`: count or aggregated measure | Two categories, or a category over time | Top N by top K series |

### 11.2 SQL patterns

**Histogram.** Suggested specs carry `start` and `step` from the profile:

```sql
with source as (select * from "listings")
select
  cast(("price" - :start) / :step as integer) as bucket,
  count(*) as n
from source
where typeof("price") in ('integer', 'real')
group by 1
order by 1
```

If the data has grown beyond the profiled range, extra buckets appear. The compiler clamps bucket numbers to a maximum so the result stays bounded, and the renderer labels the last bucket as open-ended.

**Top N plus "Other"** (category bar), using a window function:

```sql
with source as (select * from "orders"),
grouped as (
  select "status" as key, count(*) as value from source group by 1
),
ranked as (
  select key, value, row_number() over (order by value desc, key) as rn
  from grouped
)
select
  case when rn <= :limit then key end as key,
  rn > :limit as is_other,
  sum(value) as value
from ranked
group by min(rn, :limit + 1)
order by min(rn, :limit + 1)
```

`is_other` is a separate column so that a real `NULL` category stays distinguishable from the "Other" bucket.

**Time buckets** (time-series line). The time unit is chosen from the temporal span so the bucket count stays in the low hundreds:

```sql
with source as (select * from "orders")
select
  strftime('%Y-%m', "created_at") as bucket,
  count(*) as value
from source
where "created_at" is not null
group by 1
order by 1
limit 501
```

Missing buckets are filled in by the renderer so gaps show as zero and are not skipped.

**Top K series over time** (multi-series line, grouped and stacked bar):

```sql
with source as (select * from "orders"),
series_rank as (
  select "status" as s,
         row_number() over (order by count(*) desc, "status") as rn
  from source
  group by 1
)
select
  strftime('%Y-%m', "created_at") as bucket,
  case when r.rn <= :series_limit then source."status" end as series,
  r.rn > :series_limit as is_other,
  count(*) as value
from source
join series_rank r on r.s is source."status"
group by 1, min(r.rn, :series_limit + 1)
order by 1, min(r.rn, :series_limit + 1)
```

**Downsampling an ordered series** with `ntile`, for a measure plotted against a numeric or sequence axis. Each tile keeps its minimum, maximum and mean, which the renderer draws as a band with a line through it:

```sql
with source as (select * from "readings"),
tiled as (
  select "seq" as t, "value" as v,
         ntile(:buckets) over (order by "seq") as tile
  from source
  where "seq" is not null and "value" is not null
)
select tile, min(t) as t_start, max(t) as t_end,
       min(v) as v_min, max(v) as v_max, avg(v) as v_avg, count(*) as n
from tiled
group by tile
order by tile
```

This needs a sort unless the ordering column is indexed, so time-bucket grouping is preferred whenever the axis is temporal.

**Two-dimensional bins** (scatter on large sources), drawn as a heatmap:

```sql
with source as (select * from "listings")
select
  cast(("price" - :x_start) / :x_step as integer) as bx,
  cast(("sqft" - :y_start) / :y_step as integer) as by,
  count(*) as n
from source
where typeof("price") in ('integer', 'real')
  and typeof("sqft") in ('integer', 'real')
group by 1, 2
```

When the profile shows the source is small enough, scatter returns raw points instead.

**Geographic grid** (map on large sources). `:precision` is chosen from the coordinate extent:

```sql
with source as (select * from "stores")
select
  round("latitude", :precision) as lat,
  round("longitude", :precision) as lon,
  count(*) as n
from source
where "latitude" between -90 and 90
  and "longitude" between -180 and 180
group by 1, 2
order by n desc
limit :max_rows
```

How the map is drawn with Observable Plot, and where a basemap comes from, is an open question (section 19).

## 12. HTTP API

All endpoints return JSON and are registered with `register_routes`. Paths are proposals.

| Endpoint | Purpose |
|----------|---------|
| `GET /-/charts/types.json` | Registered chart types: name, label, icon, `js_url`, channels |
| `GET /<db>/<table>/-/charts/suggest.json` | Suggestions for a table or view |
| `GET /<db>/-/charts/suggest.json?sql=...` | Suggestions for an arbitrary query |
| `GET /<db>/-/charts/data.json?source=...&spec=...&size=...` | Compile a spec and return its rows |
| `GET /<db>/-/charts/profile.json?source=...` | Profile status, progress and facts |
| `POST /<db>/-/charts/profile/refresh` | Enqueue a fresh full profile |

`suggest.json` response:

```json
{
  "ok": true,
  "source": {"database": "shop", "kind": "table", "table": "orders"},
  "profile": {"basis": "sample", "status": "running", "progress": 0.4, "stale": false},
  "suggestions": [ ]
}
```

`data.json` response:

```json
{
  "ok": true,
  "columns": ["bucket", "value"],
  "rows": [["2023-11", 1203], ["2023-12", 1377]],
  "truncated": false
}
```

Notes:

- `data.json` is a `GET` with the spec in the query string so responses are cacheable, including long-lived caching for immutable databases.
- The compiled SQL is included in the `data.json` response only for actors with `execute-sql`, as a "view this SQL" link into Datasette's query page.
- A chart query that hits the time limit returns `{"ok": false, "error": "timeout"}`. What the UI offers next is an open question (section 19).

## 13. User interface

**Entry points.** The `top_table`, `top_query` and `top_stored_query` hooks each emit a container element and a small JSON bootstrap (the source descriptor and endpoint URLs). The plugin's JavaScript, added with `extra_js_urls`, takes over from there. `table_actions` and `query_actions` add a "Chart this data" menu item. Using the plugin's own bootstrap avoids depending on core's undocumented `window._datasetteTableData` and `window._datasetteQueryData` globals.

**Suggestions strip.** A row of cards, in rank order. Each card shows:

- a thumbnail, or the chart type icon when `preview` is `"on_demand"`
- the title
- the reason

Thumbnails load lazily as cards scroll into view, with limited concurrency (`thumbnail_concurrency`) because SQL threads are scarce. On-demand previews load on hover or tap. A failed or timed-out thumbnail falls back to the icon.

**Chart view.** Selecting a card renders the chart at full size with the manual builder beside it.

**Manual builder.** Generated from the `channels` declarations:

- a chart type picker
- one column control per channel, pre-filtered to columns whose role fits, with a way to show all columns
- aggregate, time unit and bin controls where the channel allows them

The builder edits the same spec the suggestion produced. Validation errors from `ChartType.validate` appear inline. Starting from an empty builder, with no suggestion selected, must also work (D7).

**Profile status.** When suggestions are based on a sample, the strip says so ("Based on the first 10,000 rows"). When a full profile finishes, the UI offers the refreshed suggestions and does not reorder cards under the user's cursor.

**Sharing.** The current spec is kept in the URL fragment so a chart can be linked to. Saving charts is out of scope.

**Accessibility.** Each chart has a text alternative made of its title and reason, and a link to the underlying aggregated rows.

## 14. Permissions and security

- **Tables and views:** suggestions, profiles and chart data require `view-table` on the table. `execute-sql` is not required (goal 6).
- **Arbitrary queries:** require `execute-sql` on the database.
- **Stored queries:** require `view-query` on the query.
- **What a user without `execute-sql` gains:** the data endpoint gives them constrained aggregate queries over tables they can already read in full. It reveals nothing they could not get by paging through the table. The cost is CPU, which is bounded by the SQL time limit.
- **Profiles:** contain data values, so the profile endpoint applies the same check as the source.
- **Background jobs:** permission is checked at enqueue time, and the actor is recorded.
- **Read-only:** all profiling and chart SQL runs through `Database.execute()` on Datasette's read connections.
- **Row-level restrictions:** core has none. Plugins that add them are not accounted for; a shared stored profile could leak aggregate facts across users in that setup. Noted as a limitation.

## 15. Changes to Datasette core

### 15.1 Required: let `execute()` exceed the default time limit

Today `Database.execute()` takes `custom_time_limit`, but only applies it when it is shorter than `sql_time_limit_ms` (default 1,000 ms):

```python
time_limit_ms = self.ds.sql_time_limit_ms
timeout_expected = bool(custom_time_limit) and custom_time_limit < time_limit_ms
if timeout_expected:
    time_limit_ms = custom_time_limit
```

That clamp is also a security boundary. The `?_timelimit=` query string parameter is passed straight into `custom_time_limit` (`datasette/views/database.py:826` and `datasette/views/table.py:2556`), so simply removing the clamp would let any visitor raise the limit from the URL.

Proposed change:

- Keep `custom_time_limit` exactly as it is, for request-supplied values.
- Add a separate keyword-only argument for trusted Python callers that can set the limit in either direction. `time_limit_ms` would match the name `execute_write()` already uses.
- Leave the `timeout_expected` telemetry logic tied to `custom_time_limit`.

With this in place the profiler uses `execute()` for its long queries and gets the time budget and query interruption from Datasette's existing progress handler. Without it, the fallback is `Database.execute_fn()`, which has no time limit at all and would need the plugin to install its own progress handler.

### 15.2 Optional

| Change | Why |
|--------|-----|
| Date and datetime column types | A home for temporal detection results that users can correct (section 7.5) |
| A dedicated SQL thread or executor for background work | A long profile query currently occupies one of the three request threads |
| Pruning of finished background task handles | Not needed by this design (one long-lived worker), but would allow simpler per-job tasks |
| Documentation fix | `docs/internals.rst` says the default internal database is "temporary in-memory"; the code creates a temporary file on disk (`datasette/app.py:540`) |

### 15.3 Relationship to the Column statistics dialog

Core 1.0a42 added a Column statistics dialog (`datasette/static/column-stats.js`). It builds its aggregate SQL in the browser and runs it through the query API, so it needs `execute-sql` and is bound by the one-second limit. Its queries (histogram, date histogram, top values, text summary) are a useful reference for the profiler's SQL. Whether the two should share code or stored results is an open question.

## 16. Configuration

Plugin configuration, with proposed defaults. Every number here is a starting point for experiments.

```yaml
plugins:
  datasette-charts:
    max_suggestions: 8
    sample_rows: 10000
    inline_row_limit: 50000           # at or below this, profile inline and do not store
    background_time_limit_ms: 30000   # per chunk or per query in the full tier
    chunk_rows: 250000
    stale_after_hours: 24
    stale_row_change: 0.10
    thumbnail_row_limit: 100000       # above this, previews are on demand
    thumbnail_concurrency: 2
    hard_max_rows: 5000
```

## 17. Evaluation harness

The two open questions, profiling budget and cross-type ranking, will be answered by experiment. The harness exists to make those experiments cheap and their results reviewable.

1. **Corpus.** A set of real SQLite databases covering different shapes: time-series logs, transactional tables, survey data, geographic data, wide tables with dozens of columns, and messy imports.
2. **Snapshot tests.** For each table in the corpus, the top suggestions (spec, score, reason) are stored as a snapshot file. A change to any heuristic shows up as a diff across the whole corpus in code review.
3. **CLI.** `datasette charts suggest data.db orders` prints suggestions and the signals behind each score, registered with `register_commands`. `datasette charts profile data.db orders` prints the profile.
4. **Contact sheet.** A generated HTML page that renders the top suggestions for every table in the corpus, for judging quality by eye.
5. **Benchmarks.** Generated tables at 100 thousand, 1 million and 10 million rows, timing each profile tier, each sampling strategy and each chart query pattern.
6. **Determinism test.** Running the engine twice on the same database must produce byte-identical output.

## 18. Milestones

| Milestone | Contents |
|-----------|----------|
| M0 | Core change: `execute()` time limit (section 15.1) |
| M1 | `Source`; tier 0 and tier 1 profiler; `register_chart_types`; DSL validation; three chart types (histogram, category bar, time-series line); `data.json` and `suggest.json`; suggestions strip; CLI and snapshot harness |
| M2 | Remaining default chart types (scatter and heatmap, grouped and stacked bar, map); manual builder; thumbnails with on-demand previews |
| M3 | Full tier; internal database tables; background worker; staleness; profile status in the UI |
| M4 | Documentation for chart type authors; DSL v1 frozen; tuning from the eval harness |

M1 is deliberately the thinnest slice that exercises every interface: three chart types are enough to find out whether the chart type contract and the ranking approach hold up.

## 19. Open questions

1. **Profiling budget.** Sample size, inline row limit, chunk size and time limits. Is the evenly spaced rowid sample good enough, and how misleading is first-N sampling for queries and views?
2. **Ranking across chart types.** Does a shared rubric plus central weights and a diversity pass produce good orderings, or do chart types need to hand the ranker raw signals instead of scores?
3. **Expensive arbitrary queries.** Every profile and chart query re-runs the wrapped query. Deferred, with materializing to a temporary table as the likely answer.
4. **Slow chart queries.** What should happen when a chart's own query exceeds the time limit on a very large table: chart a sample with a visible label, or run it in the background and cache the result?
5. **Precomputed thumbnails.** Should the background worker also compute and store chart data for the top suggestions, so previews on large tables cost nothing?
6. **Filtered table views.** Should charts on a filtered table page reflect the filters? A proposal: take suggestions from the whole-table profile, and apply the filters inside the `source` CTE when fetching chart data.
7. **Map rendering.** Observable Plot can draw points on a projection but needs basemap geometry from somewhere. The alternative is a tile-based map library for this one chart type.
8. **Thread pool pressure.** How much does background profiling slow page requests with three SQL threads, and is a dedicated executor in core worth it?
9. **Staleness thresholds.** Are 24 hours and 10% right? Should they scale with table size?
10. **Pairwise facts.** Should correlations between measure pairs be part of the full profile, or stay in the scatter chart's `verify` step?
11. **Column statistics overlap.** Should the profiler and core's Column statistics dialog share SQL or stored results?
12. **Names.** The plugin name, the internal table prefix and the new `execute()` argument.
13. **Stored query parameters.** How suggestions work for stored queries that take named parameters.

## Appendix A: Datasette facts this spec relies on

Checked against commit `1d629fb`.

| Fact | Where |
|------|-------|
| `add_background_task(func, name=None)`; `func` is `async def f(datasette)`; added in 1.0a40 | `datasette/app.py:3295`, `docs/internals.rst:1453` |
| Background tasks are plain asyncio tasks; finished handles are not pruned | `datasette/background_tasks.py:105-115`, `:154` |
| `Database.execute()` signature and the shorter-only clamp | `datasette/database.py:751-769` |
| `?_timelimit=` is passed into `custom_time_limit` | `datasette/views/database.py:826`, `datasette/views/table.py:2556` |
| Time limits are enforced with a SQLite progress handler | `datasette/utils/__init__.py:434-454` |
| `sql_time_limit_ms` defaults to 1,000; `num_sql_threads` defaults to 3 | `datasette/app.py:231-235` |
| `execute_write()` has `time_limit_ms=2000` | `datasette/database.py:315` |
| `execute_fn()` runs a function on a read connection with no time limit | `datasette/database.py:702` |
| The default internal database is a temporary file on disk | `datasette/app.py:540` |
| `--internal` and `DATASETTE_INTERNAL`; etiquette for plugin tables | `docs/internals.rst:2966-2999` |
| Table counts are capped at 10,000 | `datasette/database.py:102`, `:910` |
| `PRAGMA schema_version` is tracked in `catalog_databases` | `datasette/database.py:1257`, `datasette/utils/internal_db.py:32` |
| Column types: base class, built-ins (`url`, `email`, `json`, `textarea`), hook | `datasette/column_types.py`, `datasette/default_column_types.py`, `datasette/hookspecs.py:92` |
| `top_table`, `top_query`, `top_stored_query`, `table_actions`, `query_actions` | `datasette/hookspecs.py:170-235` |
| Plugins can define their own hooks with `pm.add_hookspecs` | `docs/writing_plugins.rst:403-427` |
| `datasette.allowed(action=, resource=, actor=)`; `TableResource`, `DatabaseResource`, `QueryResource` | `datasette/app.py:2119`, `datasette/resources.py` |
| Actions `view-table`, `execute-sql`, `view-query` | `datasette/default_actions.py` |
| Column statistics dialog, added in 1.0a42 | `datasette/static/column-stats.js` |

## Appendix B: Prior art

- **Tableau "Show Me"** (Mackinlay, Hanrahan and Stolte, 2007): rule-based chart recommendations from field types. The closest match to the `propose` step.
- **Voyager and CompassQL** (Wongsuphasawat and colleagues): enumerate candidate charts, then rank them, with attention to covering different columns. The model for the central ranker's diversity pass.
- **SeeDB** (Vartak and colleagues, 2015): scores a chart by how much its distribution deviates from a reference. A possible deterministic "interestingness" signal for the verify step.
- **Draco** (Moritz and colleagues, 2018): visualization design knowledge expressed as weighted constraints. Relevant if hand-tuned weights become hard to manage.
- **M4** (Jugel and colleagues, 2014): min/max aggregation per pixel column for time series. The basis for the `ntile` downsampling pattern.
- **Datasette facet suggestions:** core already suggests facets under a small time budget (`facet_suggest_time_limit_ms`), with each facet class supplying its own `suggest()` logic through `register_facet_classes`. This is an existing precedent in Datasette for extension types that bundle their own heuristics.
