import json

import pytest
from datasette.app import Datasette

pytest.importorskip("datasette_agent")

from datasette_agent.tools import get_agent_tools  # noqa: E402

from datasette_charts import api  # noqa: E402
from urllib.parse import unquote  # noqa: E402

LINE = {
    "type": "line",
    "encoding": {
        "x": {"column": "created_at", "timeUnit": "year"},
        "y": {"aggregate": "count"},
    },
}


async def call(ds, name, actor=None, **arguments):
    tools = {tool.name: tool for tool in await get_agent_tools(ds)}
    return json.loads(await tools[name].fn(datasette=ds, actor=actor, **arguments))


@pytest.mark.asyncio
async def test_tools_are_registered_and_describe_every_chart_type(ds):
    tools = {tool.name: tool for tool in await get_agent_tools(ds)}
    assert {"chart", "suggest_charts"} <= set(tools)
    description = tools["chart"].description
    types = (await ds.client.get("/-/charts/types.json")).json()["types"]
    for chart_type in types:
        assert f"- {chart_type['name']}: " in description
    assert "option layout: stacked|grouped (default stacked)" in description
    assert "__" not in description


@pytest.mark.asyncio
async def test_suggest_charts(ds):
    result = await call(ds, "suggest_charts", database="shop", table="orders")
    assert result["ok"] is True
    assert result["row_count"] == 600
    assert {"name": "status", "role": "category", "secondary_roles": []} in result[
        "columns"
    ]
    first = result["suggestions"][0]
    assert set(first) == {"title", "reason", "spec"}
    # A suggested spec can be passed straight to chart
    drawn = await call(ds, "chart", database="shop", table="orders", spec=first["spec"])
    assert drawn["ok"] is True
    assert drawn["title"] == first["title"]


@pytest.mark.asyncio
async def test_chart_returns_rows_a_link_and_an_embed(ds):
    result = await call(ds, "chart", database="shop", table="orders", spec=LINE)
    assert result["ok"] is True
    assert result["title"] == "Rows per year of created_at"
    assert result["columns"] == ["bucket", "value"]
    assert sum(row[1] for row in result["rows"]) == 600
    # v is filled in, so the link holds a complete spec
    path, _, fragment = result["chart_path"].partition("#chart=")
    assert path == "/shop/orders"
    from urllib.parse import unquote

    assert json.loads(unquote(fragment)) == {"v": 1, **LINE}
    html = result["_html"]
    assert "<datasette-chart " in html
    assert "/-/static-plugins/datasette-charts/embed.js" in html
    # The chart page it links to renders
    assert (await ds.client.get(path)).status_code == 200


@pytest.mark.asyncio
async def test_chart_filters(ds):
    result = await call(
        ds,
        "chart",
        database="shop",
        table="orders",
        filters={"status": "paid", "quantity__gt": 2},
        spec=LINE,
    )
    expected = (
        await ds.get_database("shop").execute(
            "select count(*) from orders where status = 'paid' and quantity > 2"
        )
    ).first()[0]
    assert sum(row[1] for row in result["rows"]) == expected
    assert result["chart_path"].startswith(
        "/shop/orders?quantity__gt=2&status=paid#chart="
    )


@pytest.mark.asyncio
async def test_chart_sql_with_params(ds):
    bar = {
        "type": "bar",
        "encoding": {"x": {"column": "status"}, "y": {"aggregate": "count"}},
    }
    result = await call(
        ds,
        "chart",
        database="shop",
        sql="select * from orders where price > :low",
        # The shape datasette-agent's own sql_query tool takes
        params=[{"name": "low", "value": 50}],
        spec=bar,
    )
    assert result["ok"] is True
    expected = (
        await ds.get_database("shop").execute(
            "select count(*) from orders where price > 50"
        )
    ).first()[0]
    assert sum(row[-1] for row in result["rows"]) == expected
    assert result["chart_path"].startswith("/shop/-/query?sql=select")
    assert "&low=50#chart=" in result["chart_path"]


@pytest.mark.asyncio
async def test_chart_reports_spec_errors_to_the_model(ds):
    result = await call(
        ds,
        "chart",
        database="shop",
        table="orders",
        spec={"type": "line", "encoding": {"x": {"column": "nope"}}},
    )
    assert result == {
        "ok": False,
        "error": "Invalid spec",
        "errors": ["x: no such column: nope", "y is required"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments,error",
    [
        ({}, "Give exactly one of table, sql or query_name"),
        ({"table": "orders", "sql": "select 1"}, "Give exactly one of"),
        ({"table": "nope"}, "Table not found"),
        ({"sql": "select 1", "filters": {"a": "b"}}, "filters only apply to a table"),
        ({"sql": "delete from orders"}, "Statement must be a SELECT"),
    ],
)
async def test_chart_source_errors(ds, arguments, error):
    result = await call(ds, "chart", database="shop", spec=LINE, **arguments)
    assert result["ok"] is False
    assert error in result["error"]


@pytest.mark.asyncio
async def test_chart_caps_the_rows_sent_to_the_model(ds):
    scatter = {
        "type": "scatter",
        "encoding": {"x": {"column": "price"}, "y": {"column": "value"}},
    }
    result = await call(ds, "chart", database="shop", table="orders", spec=scatter)
    assert result["ok"] is True
    assert len(result["rows"]) == 100
    assert "Only the first 100" in result["rows_note"]


@pytest.mark.asyncio
async def test_tools_apply_the_actors_permissions(db_path):
    ds = Datasette(
        [db_path],
        config={
            "databases": {
                "shop": {
                    "allow_sql": {"id": "analyst"},
                    "tables": {"orders": {"allow": {"id": "analyst"}}},
                }
            }
        },
    )
    await ds.invoke_startup()
    for name in ("chart", "suggest_charts"):
        extra = {"spec": LINE} if name == "chart" else {}
        denied = await call(ds, name, database="shop", table="orders", **extra)
        assert denied == {"ok": False, "error": "Permission denied"}
        allowed = await call(
            ds, name, actor={"id": "analyst"}, database="shop", table="orders", **extra
        )
        assert allowed["ok"] is True
    # A table the actor can see needs no execute-sql, and gets no SQL back
    places = {
        "type": "bar",
        "encoding": {"x": {"column": "fuel"}, "y": {"aggregate": "count"}},
    }
    result = await call(ds, "chart", database="shop", table="places", spec=places)
    assert result["ok"] is True
    denied = await call(
        ds, "chart", database="shop", sql="select * from places", spec=places
    )
    assert denied == {"ok": False, "error": "Permission denied"}


@pytest.mark.asyncio
async def test_api_matches_the_http_endpoints(ds):
    source = await api.resolve(
        ds, None, {"database": "shop", "kind": "table", "table": "orders"}
    )
    spec = {"v": 1, **LINE, "options": {}}
    data = await api.chart_data(ds, None, source, spec)
    response = await ds.client.get(
        "/shop/-/charts/data.json",
        params={
            "source": json.dumps({"kind": "table", "table": "orders"}),
            "spec": json.dumps(spec),
        },
    )
    assert response.json() == data
    suggestions = await api.suggest(ds, None, source)
    response = await ds.client.get("/shop/orders/-/charts/suggest.json")
    assert response.json() == suggestions


@pytest.mark.asyncio
async def test_embed_html_escapes_its_config(ds):
    source = await api.resolve(
        ds,
        None,
        {"database": "shop", "kind": "query", "sql": "select '\"><script>' as x"},
    )
    html = str(api.embed_html(ds, source, {"v": 1}))
    assert "<script>'" not in html and '"><script>' not in html
    assert (
        await ds.client.get("/-/static-plugins/datasette-charts/embed.js")
    ).status_code == 200
    assert (
        await ds.client.get("/-/static-plugins/datasette-charts/draw.js")
    ).status_code == 200


# --- Annotations ---------------------------------------------------------------

BAR_BY_YEAR = {
    "type": "bar",
    "encoding": {
        "x": {"column": "built_year", "sort": "key", "limit": 100, "other": False},
        "y": {"aggregate": "count"},
    },
    "options": {"orientation": "vertical"},
}


def test_chart_description_documents_annotations():
    from datasette_charts.agent_tools import chart_description
    from datasette_charts.chart_types import BUILTIN_CHART_TYPES

    description = chart_description({cls.name: cls() for cls in BUILTIN_CHART_TYPES})
    assert (
        '"annotations": [{"type": "line", "x": 2009, "position": "after"' in description
    )
    assert "map: text" in description
    assert "annotate_chart" in description
    # How callouts are laid out, so the model need not place them itself
    assert "in order in a band above the chart" in description
    assert "option orientation: horizontal|vertical (default horizontal)" in description


@pytest.mark.asyncio
async def test_chart_with_annotations(ds):
    spec = {
        **BAR_BY_YEAR,
        "annotations": [
            {"type": "line", "x": 1990, "position": "after", "text": "Rule enacted"}
        ],
    }
    result = await call(ds, "chart", database="shop", table="places", spec=spec)
    assert result["ok"] is True
    assert "annotation_warnings" not in result
    fragment = result["chart_path"].partition("#chart=")[2]
    assert json.loads(unquote(fragment))["annotations"] == spec["annotations"]
    assert (
        "Rule enacted" in unquote(result["_html"]) or "Rule enacted" in result["_html"]
    )


@pytest.mark.asyncio
async def test_chart_reports_invalid_annotations(ds):
    spec = {
        **BAR_BY_YEAR,
        "annotations": [{"type": "line", "x": 1990, "colour": "red"}],
    }
    result = await call(ds, "chart", database="shop", table="places", spec=spec)
    assert result["ok"] is False
    assert result["errors"] == [
        "annotations[0]: a line annotation has no colour - it takes anchor, color, "
        "position, stroke, text, x, y"
    ]


@pytest.mark.asyncio
async def test_chart_warns_about_categories_not_on_the_chart(ds):
    spec = {
        **BAR_BY_YEAR,
        "annotations": [{"type": "range", "x": 1990, "x2": 2031, "text": "Era"}],
    }
    result = await call(ds, "chart", database="shop", table="places", spec=spec)
    assert result["ok"] is True
    (warning,) = result["annotation_warnings"]
    assert warning.startswith(
        "annotations[0]: x2 2031 is not one of the chart's x values, so it is not "
        "drawn. They are: 1950, 1951,"
    )


async def first_chart(ds, spec):
    result = await call(ds, "chart", database="shop", table="places", spec=spec)
    assert result["ok"] is True
    return result["chart_path"]


def annotations_in(path):
    return json.loads(unquote(path.partition("#chart=")[2])).get("annotations")


@pytest.mark.asyncio
async def test_annotate_chart_adds_updates_and_removes(ds):
    path = await first_chart(ds, BAR_BY_YEAR)
    added = await call(
        ds,
        "annotate_chart",
        chart_path=path,
        add=[
            {"type": "line", "x": 1990, "position": "after", "text": "Rule"},
            {"type": "line", "y": 5, "text": "Target"},
        ],
    )
    assert added["ok"] is True
    assert added["annotations"] == [
        {"index": 0, "type": "line", "x": 1990, "position": "after", "text": "Rule"},
        {"index": 1, "type": "line", "y": 5, "text": "Target"},
    ]
    assert annotations_in(added["chart_path"]) == [
        {"type": "line", "x": 1990, "position": "after", "text": "Rule"},
        {"type": "line", "y": 5, "text": "Target"},
    ]
    assert "<datasette-chart " in added["_html"]

    updated = await call(
        ds,
        "annotate_chart",
        chart_path=added["chart_path"],
        update=[{"index": 0, "text": "Rule enacted", "position": None}],
        remove=[1],
        add=[{"type": "text", "left": 0.02, "top": 0.05, "text": "Source: test"}],
    )
    assert updated["ok"] is True
    assert annotations_in(updated["chart_path"]) == [
        {"type": "line", "x": 1990, "text": "Rule enacted"},
        {"type": "text", "left": 0.02, "top": 0.05, "text": "Source: test"},
    ]

    cleared = await call(
        ds, "annotate_chart", chart_path=updated["chart_path"], annotations=[]
    )
    assert cleared["ok"] is True
    assert cleared["annotations"] == []
    assert annotations_in(cleared["chart_path"]) is None


@pytest.mark.asyncio
async def test_annotate_chart_takes_the_page_the_user_has_open(ds):
    # The address of a chart page, as the agent's panel reports it
    path = await first_chart(ds, BAR_BY_YEAR)
    result = await call(
        ds,
        "annotate_chart",
        chart_path="http://localhost:8001" + path,
        add=[{"type": "point", "x": 1960, "text": "Busy year"}],
    )
    assert result["ok"] is True
    assert result["chart_path"].startswith("/shop/places#chart=")


@pytest.mark.asyncio
async def test_annotate_chart_keeps_filters_and_queries(ds):
    filtered = await call(
        ds,
        "chart",
        database="shop",
        table="orders",
        filters={"status": "paid"},
        spec=LINE,
    )
    result = await call(
        ds,
        "annotate_chart",
        chart_path=filtered["chart_path"],
        add=[{"type": "line", "x": "2024-01", "text": "New year"}],
    )
    assert result["ok"] is True
    assert result["rows"] == filtered["rows"]
    assert result["chart_path"].startswith("/shop/orders?status=paid#chart=")

    sql = await call(
        ds,
        "chart",
        database="shop",
        sql="select * from orders where price > :low",
        params={"low": 50},
        spec=LINE,
    )
    result = await call(
        ds,
        "annotate_chart",
        chart_path=sql["chart_path"],
        add=[{"type": "line", "y": 10, "text": "Ten"}],
    )
    assert result["ok"] is True
    assert result["rows"] == sql["rows"]
    assert "&low=50#chart=" in result["chart_path"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments,error",
    [
        ({"chart_path": "/shop/places"}, "chart_path must be a link to a chart"),
        ({"chart_path": "/nope/places#chart={}", "add": []}, "Database not found"),
        ({}, "Give add, update, remove or annotations"),
        (
            {"remove": [3]},
            "remove: 3 is not the index of an annotation. This chart has none",
        ),
        ({"update": [{"text": "x"}]}, "Each update needs an index"),
        (
            {"annotations": [], "add": [{"type": "line", "y": 1}]},
            "Give annotations to replace them all, or add, update and remove - not both",
        ),
        ({"add": [{"type": "line"}]}, "Invalid spec"),
    ],
)
async def test_annotate_chart_errors(ds, arguments, error):
    path = await first_chart(ds, BAR_BY_YEAR)
    arguments = {"chart_path": path, **arguments}
    result = await call(ds, "annotate_chart", **arguments)
    assert result["ok"] is False
    assert error in result["error"]


@pytest.mark.asyncio
async def test_annotate_chart_applies_the_actors_permissions(db_path):
    ds = Datasette(
        [db_path],
        config={
            "databases": {"shop": {"tables": {"places": {"allow": {"id": "analyst"}}}}}
        },
    )
    await ds.invoke_startup()
    path = "/shop/places#chart=" + json.dumps({"v": 1, **BAR_BY_YEAR})
    denied = await call(
        ds, "annotate_chart", chart_path=path, add=[{"type": "line", "y": 1}]
    )
    assert denied == {"ok": False, "error": "Permission denied"}
    allowed = await call(
        ds,
        "annotate_chart",
        actor={"id": "analyst"},
        chart_path=path,
        add=[{"type": "line", "y": 1}],
    )
    assert allowed["ok"] is True
