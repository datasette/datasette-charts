"""
Tables named like the CTEs in the plugin's own SQL.

Every profile and chart query wraps its source as ``with _chart_source as
(...)`` and adds steps of its own. A table with one of those names must still be
read as the table, not as the step: SQLite would otherwise report a
circular reference.
"""

import datetime
import json
import sqlite3

import pytest
import pytest_asyncio
from datasette.app import Datasette

# The names chart and profile queries used for their own steps before they
# were prefixed
NAMES = [
    "source",
    "base",
    "grouped",
    "ranked",
    "x_rank",
    "series_rank",
    "profiled",
    "probes",
    # The calendar, box and intervals charts, from before they were built in
    "chart_days",
    "chart_groups",
    "chart_spans",
]
KINDS = ["alpha", "beta", "gamma"]
COLORS = ["red", "green", "blue", "gold"]


def build(path, id_step):
    """
    One table per name. Each has a foreign key to the next one, with a
    ``name`` label column, so bar charts look labels up in a table that is
    also named like a step. ``id_step`` spaces out the rowids: 1 gives
    dense rowids (sampled with probes), 10 sparse ones (sampled by stride).
    """
    conn = sqlite3.connect(path)
    start = datetime.datetime(2024, 1, 1, 9, 0)
    for i, name in enumerate(NAMES):
        target = NAMES[(i + 1) % len(NAMES)]
        conn.execute(f"""
            create table "{name}" (
                id integer primary key,
                name text,
                created_at text,
                ended_at text,
                kind text,
                color text,
                amount real,
                ref_id integer references "{target}"(id)
            )
            """)
        conn.executemany(
            f'insert into "{name}" values (?, ?, ?, ?, ?, ?, ?, ?)',
            [
                (
                    1 + n * id_step,
                    f"{name} {n}",
                    (start + datetime.timedelta(hours=29 * n)).isoformat(),
                    (start + datetime.timedelta(hours=29 * n + 300)).isoformat(),
                    KINDS[n % len(KINDS)],
                    COLORS[(n // 2) % len(COLORS)],
                    float((n * 37) % 101),
                    1 + (n % 5) * id_step,
                )
                for n in range(120)
            ],
        )
    conn.commit()
    conn.close()


async def make_datasette(tmp_path, id_step=1, sample=False):
    path = tmp_path / "reserved.db"
    build(str(path), id_step)
    config = {}
    if sample:
        # Small enough limits that every table is profiled from a sample
        config = {
            "plugins": {
                "datasette-charts": {
                    "inline_row_limit": 50,
                    "inline_cell_limit": 300,
                    "sample_rows": 30,
                }
            }
        }
    ds = Datasette([str(path)], config=config)
    await ds.invoke_startup()
    return ds


@pytest_asyncio.fixture
async def ds(tmp_path):
    return await make_datasette(tmp_path)


async def get_json(ds, path, **params):
    response = await ds.client.get(path, params=params)
    assert response.status_code == 200, (path, params, response.text)
    return response.json()


async def check_charts(ds, source, specs):
    for spec in specs:
        data = await get_json(
            ds,
            "/reserved/-/charts/data.json",
            source=json.dumps(source),
            spec=json.dumps(spec),
        )
        assert data["rows"], spec


def handmade_specs():
    "A chart of each type whose SQL has steps of its own"
    return [
        {
            "v": 1,
            "type": "bar",
            "encoding": {"x": {"column": "kind"}, "y": {"aggregate": "count"}},
        },
        {
            "v": 1,
            "type": "bar",
            "encoding": {
                "x": {"column": "ref_id", "limit": 3, "other": True},
                "y": {"column": "amount", "aggregate": "sum"},
            },
        },
        {
            "v": 1,
            "type": "line",
            "encoding": {
                "x": {"column": "created_at", "timeUnit": "month"},
                "y": {"aggregate": "count"},
            },
        },
        {
            "v": 1,
            "type": "line",
            "encoding": {
                "x": {"column": "created_at", "timeUnit": "month"},
                "y": {"aggregate": "count"},
                "series": {"column": "color", "limit": 2, "other": True},
            },
        },
        {
            "v": 1,
            "type": "stacked_bar",
            "encoding": {
                "x": {"column": "kind"},
                "y": {"aggregate": "count"},
                "series": {"column": "color", "limit": 2, "other": True},
            },
        },
        {
            "v": 1,
            "type": "histogram",
            "encoding": {"x": {"column": "amount"}},
        },
        {
            "v": 1,
            "type": "calendar",
            "encoding": {"x": {"column": "created_at"}, "y": {"aggregate": "count"}},
        },
        {
            "v": 1,
            "type": "box",
            "encoding": {
                "x": {"column": "ref_id", "limit": 3, "other": True},
                "y": {"column": "amount"},
            },
        },
        {
            "v": 1,
            "type": "intervals",
            "encoding": {
                "start": {"column": "created_at", "timeUnit": "week"},
                "end": {"column": "ended_at"},
            },
        },
        {
            "v": 1,
            "type": "intervals",
            "encoding": {
                "start": {"column": "created_at", "timeUnit": "week"},
                "end": {"column": "ended_at"},
                "series": {"column": "ref_id", "limit": 2, "other": True},
            },
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", NAMES)
async def test_table_named_like_a_step(ds, name):
    suggest = await get_json(ds, f"/reserved/{name}/-/charts/suggest.json")
    assert suggest["suggestions"]
    source = {"kind": "table", "table": name}
    await check_charts(ds, source, [s["spec"] for s in suggest["suggestions"]])
    await check_charts(ds, source, handmade_specs())


@pytest.mark.asyncio
@pytest.mark.parametrize("name", NAMES)
async def test_filtered_table_named_like_a_step(ds, name):
    suggest = await get_json(ds, f"/reserved/{name}/-/charts/suggest.json", kind="beta")
    assert suggest["suggestions"]
    source = {"kind": "table", "table": name, "filters": [["kind", "beta"]]}
    await check_charts(ds, source, handmade_specs())


@pytest.mark.asyncio
@pytest.mark.parametrize("name", NAMES)
async def test_query_reading_a_table_named_like_a_step(ds, name):
    "A query cannot be qualified with a schema, so this relies on the prefix"
    sql = f'select * from "{name}"'
    suggest = await get_json(ds, "/reserved/-/charts/suggest.json", sql=sql)
    assert suggest["suggestions"]
    source = {"kind": "query", "sql": sql}
    await check_charts(ds, source, [s["spec"] for s in suggest["suggestions"]])
    await check_charts(ds, source, handmade_specs())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "type_,column", [("bar", "label"), ("box", "label"), ("intervals", "series_label")]
)
async def test_foreign_key_labels_from_a_table_named_like_a_step(ds, type_, column):
    "base.ref_id points at grouped, whose labels come from its name column"
    spec = [
        s
        for s in handmade_specs()
        if s["type"] == type_ and "ref_id" in json.dumps(s["encoding"])
    ][0]
    data = await get_json(
        ds,
        "/reserved/-/charts/data.json",
        source=json.dumps({"kind": "table", "table": "base"}),
        spec=json.dumps(spec),
    )
    label = data["columns"].index(column)
    labels = {row[label] for row in data["rows"] if row[label]}
    assert labels and all(text.startswith("grouped ") for text in labels), data


@pytest.mark.asyncio
@pytest.mark.parametrize("id_step,method", [(1, "rowid_probes"), (10, "rowid_stride")])
@pytest.mark.parametrize("name", NAMES)
async def test_sampled_table_named_like_a_step(tmp_path, name, id_step, method):
    ds = await make_datasette(tmp_path, id_step=id_step, sample=True)
    response = await get_json(
        ds,
        "/reserved/-/charts/profile.json",
        source=json.dumps({"kind": "table", "table": name}),
    )
    profile = response["profile"]
    assert profile["basis"] == "sample"
    assert profile["sample"]["method"] == method
    suggest = await get_json(ds, f"/reserved/{name}/-/charts/suggest.json")
    assert suggest["suggestions"]
