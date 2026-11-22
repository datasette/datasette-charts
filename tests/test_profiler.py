import sqlite3

import pytest
from datasette.app import Datasette

from datasette_charts.profiler import profile_source
from datasette_charts.profiler.roles import (
    affinity_for,
    has_temporal_name_hint,
    has_year_name_hint,
    name_tokens,
)
from datasette_charts.sources import Source


def table(name):
    return Source(database="shop", kind="table", table=name)


def roles(profile):
    return {column.name: column.role for column in profile.columns}


@pytest.mark.asyncio
async def test_roles_for_orders(ds):
    profile = await profile_source(ds, table("orders"))
    assert profile.row_count == 600
    assert profile.row_count_exact
    assert profile.basis == "full"
    assert roles(profile) == {
        "id": "identifier",
        "created_at": "temporal",
        "status": "category",
        "price": "measure",
        "quantity": "category",
        "customer_id": "foreign_key",
        "note": "text",
        "shipped_at": "temporal",
        "is_gift": "boolean",
        "value": "measure",
    }
    created = profile.column("created_at")
    assert created.temporal.kind == "iso_datetime"
    assert created.temporal.min == "2023-01-01 08:00:00"
    assert created.temporal.origin == "detected"
    # Numeric timestamps need a name or declared-type hint as well as values
    shipped = profile.column("shipped_at")
    assert shipped.temporal.kind == "unix_seconds"
    assert shipped.temporal.min == "2023-11-14 22:13:20"
    # A 1 to 4 quantity is both a category and a measure
    assert profile.column("quantity").secondary_roles == ["measure"]
    status = profile.column("status")
    assert status.distinct == {"count": 3, "capped": False}
    assert status.top_values[0] == ["paid", 348]
    assert status.null_rate == 0.02
    fk = profile.column("customer_id").foreign_key
    assert fk == {"table": "customers", "column": "id", "label_column": "name"}
    price = profile.column("price")
    assert price.numeric["min"] == 5.0
    assert set(price.numeric["quantiles"]) == {"p01", "p25", "p50", "p75", "p99"}


@pytest.mark.asyncio
async def test_latitude_longitude_pair_and_temporal_candidate(ds):
    profile = await profile_source(ds, table("places"))
    assert profile.pairs == [
        {"kind": "latlon", "latitude": "latitude", "longitude": "longitude"}
    ]
    assert profile.column("latitude").role == "latitude"
    assert profile.column("longitude").role == "longitude"
    # Integers from 1000 to 2200 with no year in the name are not a time axis...
    elevation = profile.column("elevation")
    assert elevation.role == "measure"
    assert elevation.temporal_candidate == {"kind": "year", "match_rate": 1.0}
    # ...but with a name hint they are
    built = profile.column("built_year")
    assert built.role == "temporal"
    assert built.temporal.kind == "year"
    assert (built.temporal.min, built.temporal.max) == ("1950-01-01", "2019-12-31")


@pytest.mark.asyncio
async def test_views_and_queries_are_profiled_through_the_source_cte(ds):
    view = await profile_source(ds, table("paid_orders"))
    assert view.is_view
    assert view.row_count == 348
    assert roles(view)["created_at"] == "temporal"

    # A trailing semicolon or comment must not break the wrapping CTE
    for sql in (
        "select status, price from orders where quantity = :q;",
        "select status, price from orders where quantity = :q -- by quantity",
    ):
        source = Source(database="shop", kind="query", sql=sql, params={"q": 2})
        query = await profile_source(ds, source)
        assert query.row_count == 150
        assert roles(query) == {"status": "category", "price": "measure"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,expected_method",
    (
        ("orders", "rowid_probes"),  # dense rowids
        ("paid_orders", "first_n"),  # views have no rowid
        ("codes", "first_n"),  # nor do WITHOUT ROWID tables
    ),
)
async def test_sampling_methods(db_path, name, expected_method):
    ds = Datasette(
        [db_path],
        config={
            "plugins": {"datasette-charts": {"inline_row_limit": 10, "sample_rows": 30}}
        },
    )
    await ds.invoke_startup()
    profile = await profile_source(ds, table(name))
    assert profile.basis == "sample"
    assert profile.sample["method"] == expected_method
    assert 0 < profile.rows_profiled <= 31
    assert profile.rows_profiled < profile.row_count


@pytest.mark.asyncio
async def test_wide_sources_are_sampled_sooner(db_path):
    # 600 rows of 10 columns is 6,000 cells: over a limit of 5,000
    ds = Datasette(
        [db_path],
        config={
            "plugins": {
                "datasette-charts": {"inline_cell_limit": 5000, "sample_rows": 100}
            }
        },
    )
    await ds.invoke_startup()
    orders = await profile_source(ds, table("orders"))
    assert orders.basis == "sample"
    assert orders.rows_profiled <= 101
    # 360 rows of 3 columns still fits
    readings = await profile_source(ds, table("readings"))
    assert readings.basis == "full"


@pytest.mark.asyncio
async def test_sparse_rowids_are_sampled_by_stride(tmp_path):
    # Rowids in a few tight clusters: evenly spaced probes would mostly
    # land in the gaps and return the same handful of rows
    path = tmp_path / "sparse.db"
    conn = sqlite3.connect(path)
    conn.execute("create table t (id integer primary key, n integer)")
    conn.executemany(
        "insert into t values (?, ?)",
        [(cluster * 1_000_000 + i, i) for cluster in range(4) for i in range(250)],
    )
    conn.commit()
    conn.close()
    ds = Datasette(
        [str(path)],
        config={
            "plugins": {
                "datasette-charts": {"inline_row_limit": 10, "sample_rows": 100}
            }
        },
    )
    await ds.invoke_startup()
    profile = await profile_source(
        ds, Source(database="sparse", kind="table", table="t")
    )
    assert profile.sample == {"method": "rowid_stride", "rows": 100}


@pytest.mark.asyncio
async def test_temporal_config_overrides(db_path):
    ds = Datasette(
        [db_path],
        config={
            "databases": {
                "shop": {
                    "tables": {
                        "places": {
                            "plugins": {
                                "datasette-charts": {
                                    "temporal": {
                                        "elevation": "year",
                                        "built_year": False,
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
    )
    await ds.invoke_startup()
    profile = await profile_source(ds, table("places"))
    assert profile.column("elevation").temporal.origin == "config"
    assert profile.column("built_year").role != "temporal"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "column,values,expected",
    (
        ("d", ["2026-11-22", "2026-01-05"], "iso_date"),
        ("d", ["2026-11-22T10:30:15Z", "2026-01-05 08:00"], "iso_datetime"),
        # A mix of dates and datetimes is treated as iso_datetime
        ("d", ["2026-11-22", "2026-01-05 08:00:00"], "iso_datetime"),
        ("created_ms", [1700000000123, 1700000500123], "unix_ms"),
        ("event_ts", [1700000000, 1700000500], "unix_seconds"),
        # Formats SQLite's date functions cannot parse are not detected
        ("d", ["11/22/2026", "01/05/2026"], None),
        ("d", ["2026-11", "2026-12"], None),
        ("d", ["20261122", "20260105"], None),
        # A number near 1.7 billion with no hint could be a population
        ("population", [1700000000, 1700000500], None),
        # julianday() alone would accept these
        ("d", ["2026", "12:00"], None),
    ),
)
async def test_temporal_kinds(tmp_path, column, values, expected):
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.execute(f"create table t (id integer primary key, {column})")
    conn.executemany(f"insert into t ({column}) values (?)", [(v,) for v in values * 5])
    conn.commit()
    conn.close()
    ds = Datasette([str(path)])
    await ds.invoke_startup()
    profile = await profile_source(ds, Source(database="t", kind="table", table="t"))
    temporal = profile.column(column).temporal
    assert (temporal.kind if temporal else None) == expected


def test_name_helpers():
    assert name_tokens("createdAt") == ["created", "at"]
    assert name_tokens("upload_time_iso_8601") == ["upload", "time", "iso", "8601"]
    assert has_temporal_name_hint("created_at")
    assert has_temporal_name_hint("lastModified")
    assert not has_temporal_name_hint("category")
    assert not has_temporal_name_hint("at")
    assert has_year_name_hint("year_of_capacity_data")
    assert not has_year_name_hint("yearly_total")
    assert affinity_for("VARCHAR(20)") == "text"
    assert affinity_for("BIGINT") == "integer"
    assert affinity_for(None) == "blob"
    assert affinity_for("DOUBLE") == "real"
