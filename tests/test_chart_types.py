import pytest
from datasette.app import Datasette

from datasette_charts import engine
from datasette_charts.config import get_config
from datasette_charts.profiler import profile_source
from datasette_charts.sources import Source
from datasette_charts.sqlgen import SpecError, log_edges, nice_bins, time_bucket_sql


def table(name):
    return Source(database="shop", kind="table", table=name)


async def run(ds, source, spec):
    "Compile a spec and run it, the way the data endpoint does"
    chart_types = engine.get_chart_types(ds)
    profile = await profile_source(ds, source)
    compiled = chart_types[spec["type"]].compile(spec, source, profile)
    results = await ds.get_database("shop").execute(compiled.sql, compiled.params)
    rows = [dict(zip([c.name for c in results.columns], row)) for row in results.rows]
    assert len(rows) <= compiled.max_rows
    return rows, compiled


def spec(type_, options=None, **encoding):
    return {"v": 1, "type": type_, "encoding": encoding, "options": options or {}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ("orders", "places", "readings", "paid_orders", "codes")
)
async def test_every_suggestion_compiles_runs_and_stays_bounded(ds, name):
    source = table(name)
    chart_types = engine.get_chart_types(ds)
    profile = await profile_source(ds, source)
    suggestions = engine.suggest(profile, chart_types, get_config(ds))
    for suggestion in suggestions:
        assert suggestion["title"]
        assert suggestion["reason"]
        assert 0 < suggestion["score"] <= 1
        rows, compiled = await run(ds, source, suggestion["spec"])
        assert rows, suggestion["title"]


@pytest.mark.asyncio
async def test_suggestions_for_orders(ds):
    profile = await profile_source(ds, table("orders"))
    suggestions = engine.suggest(profile, engine.get_chart_types(ds), get_config(ds))
    titles = [s["title"] for s in suggestions]
    assert titles[0] == "Rows per month of created_at"
    assert "Rows by status" in titles
    assert "Distribution of price" in titles
    # Identifiers and free text are never charted
    for suggestion in suggestions:
        for channel in suggestion["spec"]["encoding"].values():
            assert channel.get("column") not in ("id", "note")
    reasons = {s["title"]: s["reason"] for s in suggestions}
    assert reasons["Rows per month of created_at"] == (
        "created_at spans 3 years; monthly buckets give 31 points"
    )
    assert reasons["Rows by status"] == "status has 3 values"


@pytest.mark.asyncio
async def test_map_and_panel_suggestions_come_first(ds):
    chart_types = engine.get_chart_types(ds)
    config = get_config(ds)
    places = engine.suggest(
        await profile_source(ds, table("places")), chart_types, config
    )
    assert places[0]["spec"]["type"] == "map"
    # One row per sensor per day: the reading over time, one line per
    # sensor, is the chart
    readings = engine.suggest(
        await profile_source(ds, table("readings")), chart_types, config
    )
    assert readings[0]["title"] == "Average reading per week of day by sensor"


@pytest.mark.asyncio
async def test_panel_suggestions_without_a_primary_key(ds):
    "A query has no primary key, but one row per sensor per day still shows"
    chart_types = engine.get_chart_types(ds)
    config = get_config(ds)
    source = Source(
        database="shop",
        kind="query",
        sql="select day, sensor, reading from readings",
    )
    suggestions = engine.suggest(await profile_source(ds, source), chart_types, config)
    assert suggestions[0]["title"] == "Average reading per week of day by sensor"


@pytest.mark.asyncio
async def test_panel_suggestions_for_one_row_per_decade(ds):
    "Issue #2: a summary with one row per decade per type"
    chart_types = engine.get_chart_types(ds)
    config = get_config(ds)
    source = Source(
        database="shop",
        kind="query",
        sql="""
        with recursive decades(n) as (
          select 1800 union all select n + 10 from decades where n < 2020
        )
        select printf('%04d-01-01', n) as decade, type, (n - 1800) / 10.0 + length(type) as pct
        from decades, (select 'rep' as type union all select 'sen')
        """,
    )
    suggestions = engine.suggest(await profile_source(ds, source), chart_types, config)
    titles = [s["title"] for s in suggestions]
    line = [s for s in suggestions if s["spec"]["type"] == "line"]
    # One line per type, rather than the two averaged together or a count
    # of the same 2 rows per decade
    assert line[0]["title"] == "Average pct per year of decade by type"
    counts = [t for t in titles if t.startswith("Rows per")]
    assert all(
        titles.index(t) > titles.index("Average pct per year of decade by type")
        for t in counts
    )


@pytest.mark.asyncio
async def test_bar_top_n_plus_other_keeps_null_distinct(ds):
    rows, compiled = await run(
        ds,
        table("orders"),
        spec(
            "bar",
            x={"column": "status", "limit": 2, "other": True},
            y={"aggregate": "count"},
        ),
    )
    assert rows == [
        {"key": "paid", "is_other": 0, "value": 348},
        {"key": "pending", "is_other": 0, "value": 120},
        # refunded and the real NULL category folded together
        {"key": None, "is_other": 1, "value": 132},
    ]
    # With enough room, NULL is its own bar and is_other tells it from Other
    rows, _ = await run(
        ds,
        table("orders"),
        spec(
            "bar",
            x={"column": "status", "limit": 10, "other": True},
            y={"aggregate": "count"},
        ),
    )
    assert {"key": None, "is_other": 0, "value": 12} in rows


@pytest.mark.asyncio
async def test_bar_with_measure_and_foreign_key_label(ds):
    rows, compiled = await run(
        ds,
        table("orders"),
        spec(
            "bar",
            x={"column": "customer_id", "sort": "key"},
            y={"column": "price", "aggregate": "avg"},
        ),
    )
    assert [row["key"] for row in rows] == [1, 2, 3, 4, 5]
    assert [row["label"] for row in rows] == [f"Customer {i}" for i in range(1, 6)]
    assert all(5 <= row["value"] <= 105 for row in rows)
    assert compiled.meta["other"] is False  # averages cannot be added up


@pytest.mark.asyncio
async def test_columns_named_like_chart_aliases(ds):
    # The source has a column called "value": it must not shadow the
    # chart's own aliases
    rows, _ = await run(
        ds,
        table("orders"),
        spec(
            "line",
            x={"column": "created_at", "timeUnit": "year"},
            y={"column": "value", "aggregate": "max"},
        ),
    )
    assert rows == [
        {"bucket": "2023", "value": 8.0},
        {"bucket": "2024", "value": 8.0},
        {"bucket": "2025", "value": 8.0},
    ]


@pytest.mark.asyncio
async def test_line_with_series_and_unix_timestamps(ds):
    rows, compiled = await run(
        ds,
        table("orders"),
        spec(
            "line",
            x={"column": "shipped_at", "timeUnit": "week"},
            y={"aggregate": "count"},
            series={"column": "status", "limit": 1, "other": True},
        ),
    )
    assert compiled.meta["temporalKind"] == "unix_seconds"
    assert {row["series"] for row in rows} == {"paid", None}
    assert sum(row["value"] for row in rows) == 600
    assert rows[0]["bucket"] == "2023-11-13"  # the Monday of that week


@pytest.mark.asyncio
async def test_top_series_are_the_largest_totals_when_values_add_up(ds):
    # Every sensor has the same number of rows, so only the totals can
    # tell them apart: north and south read higher than east
    for aggregate, top in (("sum", "north"), ("avg", "east")):
        rows, _ = await run(
            ds,
            table("readings"),
            spec(
                "line",
                x={"column": "day", "timeUnit": "month"},
                y={"column": "reading", "aggregate": aggregate},
                series={"column": "sensor", "limit": 1, "other": True},
            ),
        )
        assert {row["series"] for row in rows} == {top, None}, aggregate
    rows, _ = await run(
        ds,
        table("readings"),
        spec(
            "stacked_bar",
            x={"column": "sensor", "limit": 1, "other": False},
            series={"column": "sensor", "limit": 1, "other": False},
            y={"column": "reading", "aggregate": "sum"},
        ),
    )
    assert [(row["key"], row["series"]) for row in rows] == [("north", "north")]


@pytest.mark.asyncio
async def test_line_needs_a_temporal_column(ds):
    with pytest.raises(SpecError, match="does not hold dates or times"):
        await run(
            ds,
            table("orders"),
            spec("line", x={"column": "status"}, y={"aggregate": "count"}),
        )


@pytest.mark.asyncio
async def test_histogram_clamps_values_outside_its_bins(ds):
    rows, compiled = await run(
        ds,
        table("orders"),
        spec(
            "histogram",
            x={"column": "price", "bin": {"start": 20, "step": 10, "count": 3}},
        ),
    )
    assert [row["bucket"] for row in rows] == [-1, 0, 1, 2, 3]
    assert sum(row["n"] for row in rows) == 600
    # maxbins alone is resolved from the profile
    rows, compiled = await run(
        ds,
        table("orders"),
        spec("histogram", x={"column": "price", "bin": {"maxbins": 10}}),
    )
    assert compiled.meta["bin"] == {"start": 0, "step": 10, "count": 11}


@pytest.mark.asyncio
async def test_histogram_log_scale_for_long_tails(ds):
    profile = await profile_source(ds, table("places"))
    suggestions = engine.suggest(profile, engine.get_chart_types(ds), get_config(ds))
    capacity = next(
        s for s in suggestions if s["title"] == "Distribution of capacity_mw"
    )
    assert capacity["spec"]["options"] == {"scale": "log"}
    rows, compiled = await run(ds, table("places"), capacity["spec"])
    assert compiled.meta["edges"][0] == 1
    assert sum(row["n"] for row in rows) == 300
    with pytest.raises(SpecError, match="log scale"):
        await run(
            ds,
            table("places"),
            spec("histogram", {"scale": "log"}, x={"column": "latitude"}),
        )


@pytest.mark.asyncio
async def test_scatter_switches_to_bins_for_large_sources(db_path):
    ds = Datasette([db_path])
    await ds.invoke_startup()
    scatter = spec("scatter", x={"column": "price"}, y={"column": "quantity"})
    rows, compiled = await run(ds, table("orders"), scatter)
    assert compiled.meta["mode"] == "points"
    assert len(rows) == 600
    binned = spec(
        "scatter",
        x={"column": "price", "bin": {"maxbins": 5}},
        y={"column": "quantity", "bin": {"maxbins": 4}},
    )
    rows, compiled = await run(ds, table("orders"), binned)
    assert compiled.meta["mode"] == "bins"
    assert sum(row["n"] for row in rows) == 600


@pytest.mark.asyncio
async def test_stacked_bar_over_categories_and_time(ds):
    rows, _ = await run(
        ds,
        table("orders"),
        spec(
            "stacked_bar",
            {"layout": "stacked"},
            x={"column": "quantity"},
            series={"column": "status", "limit": 2, "other": True},
            y={"aggregate": "count"},
        ),
    )
    assert sum(row["value"] for row in rows) == 600
    assert {row["key"] for row in rows} == {1, 2, 3, 4}
    rows, compiled = await run(
        ds,
        table("orders"),
        spec(
            "stacked_bar",
            x={"column": "created_at", "timeUnit": "year"},
            series={"column": "customer_id"},
            y={"column": "price", "aggregate": "sum"},
        ),
    )
    assert compiled.meta["timeUnit"] == "year"
    assert [row["key"] for row in rows][:5] == ["2023"] * 5
    assert rows[0]["series_label"].startswith("Customer ")


@pytest.mark.asyncio
async def test_map_points_and_grid(db_path):
    ds = Datasette([db_path])
    await ds.invoke_startup()
    map_spec = spec(
        "map", latitude={"column": "latitude"}, longitude={"column": "longitude"}
    )
    rows, compiled = await run(ds, table("places"), map_spec)
    assert compiled.meta["mode"] == "points"
    assert len(rows) == 300
    assert set(rows[0]) == {"lat", "lon"}


def test_nice_bins():
    assert nice_bins(1, 22500) == {"start": 0, "step": 1000, "count": 23}
    assert nice_bins(1, 5, 30, integer=True) == {"start": 1, "step": 1, "count": 5}
    assert nice_bins(0.0, 1.0, 10) == {"start": 0, "step": 0.1, "count": 11}
    assert nice_bins(7, 7) == {"start": 7, "step": 1, "count": 1}


def test_log_edges():
    assert log_edges({"min": 1, "max": 600}) == [
        1,
        2,
        5,
        10,
        20,
        50,
        100,
        200,
        500,
        1000,
    ]
    assert log_edges({"min": -1, "max": 600}) is None
    # Zero has no place on a log scale, so the first edge comes from the quantiles
    edges = log_edges(
        {
            "min": 0,
            "max": 90,
            "quantiles": {"p01": 0, "p25": 0, "p50": 3, "p75": 9, "p99": 80},
        }
    )
    assert edges == [2, 5, 10, 20, 50, 100]


@pytest.mark.parametrize(
    "kind,value,unit,expected",
    (
        ("iso_datetime", "2026-11-22T10:30:15Z", "quarter", "2026-Q4"),
        ("iso_datetime", "2026-11-22 10:30:15", "minute", "2026-11-22T10:30"),
        ("iso_date", "2026-11-22", "week", "2026-11-16"),
        ("iso_date", "2026-11-22", "month", "2026-11"),
        ("unix_seconds", 1795364019, "day", "2026-11-22"),
        ("unix_ms", 1795364019670, "hour", "2026-11-22T16"),
        ("year", 2026, "year", 2026),
        # Offsets are converted to UTC by SQLite
        ("iso_datetime", "2026-11-22 23:30:00-08:00", "day", "2026-11-23"),
        # A value SQLite cannot parse gives a NULL bucket, which charts drop
        ("iso_date", "22 Nov 2026", "month", None),
    ),
)
def test_time_buckets(kind, value, unit, expected):
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("create table t (c)")
    conn.execute("insert into t values (?)", [value])
    sql = "select {} from t".format(time_bucket_sql("c", kind, unit))
    assert conn.execute(sql).fetchone()[0] == expected


def test_year_columns_only_bucket_by_year():
    with pytest.raises(SpecError):
        time_bucket_sql("c", "year", "month")
    with pytest.raises(SpecError):
        time_bucket_sql("c", "iso_date", "hour")
