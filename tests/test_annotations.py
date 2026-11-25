import json
from urllib.parse import unquote

import pytest

from datasette_charts import annotations, dsl
from datasette_charts.chart_types import BUILTIN_CHART_TYPES

CHART_TYPES = {cls.name: cls() for cls in BUILTIN_CHART_TYPES}
COLUMNS = ["created_at", "status", "price", "quantity", "latitude", "longitude"]

BAR = {
    "v": 1,
    "type": "bar",
    "encoding": {"x": {"column": "status"}, "y": {"aggregate": "count"}},
    "options": {},
}
LINE = {
    "v": 1,
    "type": "line",
    "encoding": {
        "x": {"column": "created_at", "timeUnit": "year"},
        "y": {"aggregate": "count"},
    },
    "options": {},
}
SCATTER = {
    "v": 1,
    "type": "scatter",
    "encoding": {"x": {"column": "price"}, "y": {"column": "quantity"}},
}
MAP = {
    "v": 1,
    "type": "map",
    "encoding": {
        "latitude": {"column": "latitude"},
        "longitude": {"column": "longitude"},
    },
}


def errors(spec, *items):
    return dsl.validate_spec({**spec, "annotations": list(items)}, CHART_TYPES, COLUMNS)


def test_every_annotation_type_validates():
    assert (
        errors(
            BAR,
            {"type": "line", "x": "paid", "position": "after", "text": "Rule"},
            {"type": "line", "y": 100, "text": "Target", "stroke": "solid"},
            {"type": "range", "x": "paid", "x2": "refunded", "text": "Band"},
            {"type": "range", "y": 10, "y2": 20.5},
            {"type": "point", "x": "pending", "text": "Look", "anchor": "top-right"},
            {
                "type": "text",
                "left": 0.02,
                "top": 1,
                "text": "Note",
                "color": "#d03b3b",
            },
        )
        == []
    )
    assert (
        errors(
            LINE,
            {"type": "line", "x": 2009, "position": "after", "text": "Rule"},
            {"type": "line", "x": "2009-06-15", "color": "red", "stroke": "dotted"},
            {"type": "range", "x": "2008-Q4", "x2": "2009-06", "text": "Recession"},
            {"type": "point", "x": "2024-03", "series": "paid", "text": "Spike"},
            {"type": "point", "x": "2024-03-01 10:30", "y": 5, "text": "Exact"},
        )
        == []
    )
    assert errors(MAP, {"type": "text", "left": 0.5, "top": 0.5, "text": "Hi"}) == []
    assert errors(BAR) == []  # an empty list is fine anywhere


def test_bar_orientation_option():
    assert (
        dsl.validate_spec(
            {**BAR, "options": {"orientation": "vertical"}}, CHART_TYPES, COLUMNS
        )
        == []
    )
    assert dsl.validate_spec(
        {**BAR, "options": {"orientation": "sideways"}}, CHART_TYPES, COLUMNS
    ) == ["orientation must be one of horizontal, vertical"]


@pytest.mark.parametrize(
    "spec,annotation,expected",
    (
        (BAR, "nope", "annotations[0]: must be an object"),
        (
            BAR,
            {"type": "arrow"},
            "annotations[0]: type must be one of line, range, point, text",
        ),
        (
            BAR,
            {"type": "line", "x": "paid", "y": 3},
            "annotations[0]: a line needs x (a vertical line on most charts) or y "
            "(a horizontal line), not both",
        ),
        (BAR, {"type": "line"}, "a line needs x"),
        (BAR, {"type": "line", "y": "lots"}, "annotations[0]: y must be a number"),
        (BAR, {"type": "line", "x": True}, "x must be a category value"),
        (
            BAR,
            {"type": "line", "x": "paid", "position": "between"},
            "position must be one of before, center, after",
        ),
        (
            BAR,
            {"type": "line", "y": 3, "position": "after"},
            "position only applies to a line at an x",
        ),
        (
            SCATTER,
            {"type": "line", "x": 3, "position": "after"},
            "position only applies",
        ),
        (
            BAR,
            {"type": "line", "y": 3, "stroke": "wavy"},
            "stroke must be one of dashed, solid, dotted",
        ),
        (
            BAR,
            {"type": "line", "y": 3, "color": "url(#x)"},
            "color must be one of default, blue",
        ),
        (
            BAR,
            {"type": "line", "y": 3, "anchor": "middle"},
            "anchor must be one of top-left",
        ),
        (BAR, {"type": "line", "y": 3, "text": 5}, "text must be a string"),
        (
            BAR,
            {"type": "line", "y": 3, "text": "x" * 201},
            "text must be at most 200 characters",
        ),
        (
            BAR,
            {"type": "line", "y": 3, "left": 0.5},
            "annotations[0]: a line annotation has no left - it takes anchor, color, "
            "position, stroke, text, x, y",
        ),
        (BAR, {"type": "range", "x": "paid"}, "a range needs both x and x2"),
        (
            BAR,
            {"type": "range", "x": "paid", "y2": 3},
            "a range needs x and x2, or y and y2",
        ),
        (
            LINE,
            {"type": "range", "x": "June 2009", "x2": "2010"},
            'x must be a date or time such as 2009, "2009-06"',
        ),
        (LINE, {"type": "line", "x": "2009-13-45x"}, "x must be a date or time"),
        (BAR, {"type": "point", "x": "paid"}, "a point annotation needs text"),
        (BAR, {"type": "point", "text": "hi"}, "a point needs x"),
        (
            BAR,
            {"type": "point", "x": "paid", "text": "hi", "series": "a"},
            "bar charts have no series",
        ),
        (
            BAR,
            {"type": "point", "x": "paid", "text": "hi", "marker": "yes"},
            "marker must be true or false",
        ),
        (
            SCATTER,
            {"type": "point", "x": 3, "text": "hi"},
            "a point on a scatter chart needs y",
        ),
        (
            BAR,
            {"type": "text", "text": "hi", "top": 0.5},
            "a text annotation needs left",
        ),
        (
            BAR,
            {"type": "text", "text": "hi", "left": 2, "top": 0.5},
            "left must be a number from 0 to 1",
        ),
        (
            BAR,
            {"type": "text", "left": 0.1, "top": 0.5, "text": "  "},
            "a text annotation needs text",
        ),
        (MAP, {"type": "line", "y": 3}, "map charts take text annotations, not line"),
    ),
)
def test_annotation_errors(spec, annotation, expected):
    found = errors(spec, annotation)
    assert any(expected in error for error in found), found


def test_annotations_must_be_a_short_list():
    assert dsl.validate_spec({**BAR, "annotations": {}}, CHART_TYPES, COLUMNS) == [
        "annotations must be a list"
    ]
    many = [{"type": "line", "y": i} for i in range(21)]
    assert errors(BAR, *many) == ["annotations: at most 20 are allowed"]
    # Each error says which annotation it is about
    assert errors(BAR, {"type": "line", "y": 1}, {"type": "line", "y": "x"}) == [
        "annotations[1]: y must be a number"
    ]


def test_chart_types_without_annotations_reject_them():
    from datasette_charts.chart_types import ChartType

    class Sankey(ChartType):
        name = "sankey"
        channels = []

    types = {**CHART_TYPES, "sankey": Sankey()}
    spec = {"v": 1, "type": "sankey", "encoding": {}, "annotations": [{"type": "line"}]}
    assert dsl.validate_spec(spec, types, COLUMNS) == [
        "sankey charts do not take annotations"
    ]
    assert dsl.validate_spec({**spec, "annotations": []}, types, COLUMNS) == []


def test_describe():
    described = annotations.describe(CHART_TYPES)
    assert "bar: line, range, point, text" in described
    assert "map: text" in described


@pytest.mark.asyncio
async def test_types_json_says_what_each_chart_takes(ds):
    types = {
        t["name"]: t
        for t in (await ds.client.get("/-/charts/types.json")).json()["types"]
    }
    assert types["bar"]["annotations"] == {
        "types": ["line", "range", "point", "text"],
        "axes": {"x": "category", "y": "number"},
        "series": False,
        "lookup": True,
    }
    assert types["line"]["annotations"]["axes"]["x"] == "time"
    assert types["line"]["annotations"]["series"] is True
    assert types["scatter"]["annotations"]["lookup"] is False
    assert types["map"]["annotations"]["types"] == ["text"]
    assert types["bar"]["options"]["orientation"]["default"] == "horizontal"


async def data(ds, spec, table="orders"):
    return await ds.client.get(
        "/shop/-/charts/data.json",
        params={
            "source": json.dumps({"kind": "table", "table": table}),
            "spec": json.dumps(spec),
        },
    )


@pytest.mark.asyncio
async def test_data_json_accepts_annotations_and_they_do_not_change_the_rows(ds):
    shop_line = {
        **LINE,
        "annotations": [
            {"type": "line", "x": 2024, "position": "after", "text": "Rule changed"},
            {"type": "range", "x": "2023-06", "x2": "2023-09", "text": "Summer"},
        ],
    }
    plain = {key: value for key, value in shop_line.items() if key != "annotations"}
    with_notes = await data(ds, shop_line)
    without = await data(ds, plain)
    assert with_notes.status_code == 200
    assert with_notes.json()["rows"] == without.json()["rows"]
    assert with_notes.json()["sql"] == without.json()["sql"]


@pytest.mark.asyncio
async def test_data_json_reports_annotation_errors(ds):
    response = await data(
        ds, {**BAR, "annotations": [{"type": "line", "x": "paid", "y": 2}]}
    )
    assert response.status_code == 400
    assert response.json()["errors"] == [
        "annotations[0]: a line needs x (a vertical line on most charts) or y (a "
        "horizontal line), not both"
    ]


@pytest.mark.asyncio
async def test_annotations_round_trip_through_a_chart_link_and_embed(ds):
    from datasette_charts import api

    spec = {
        **BAR,
        "options": {"orientation": "vertical"},
        "annotations": [
            {
                "type": "line",
                "x": "paid",
                "position": "after",
                "text": 'A "quoted" <b>note</b>',
            }
        ],
    }
    source = await api.resolve(
        ds, None, {"database": "shop", "kind": "table", "table": "orders"}
    )
    path = api.chart_path(ds, source, spec)
    assert json.loads(unquote(path.partition("#chart=")[2])) == spec
    html = str(api.embed_html(ds, source, spec))
    # The text is escaped in the attribute, and comes back out unchanged
    assert "<b>note</b>" not in html
    import html as html_module
    import re

    config = json.loads(
        html_module.unescape(re.search(r'config="([^"]*)"', html).group(1))
    )
    assert config["spec"]["annotations"] == spec["annotations"]
    # "Open in the chart builder" and "Copy link to this chart" use this: the
    # source's own page, with the whole spec, annotations included
    assert config["chart_url"] == path
    assert json.loads(unquote(config["chart_url"].partition("#chart=")[2])) == spec
    assert (await data(ds, spec)).status_code == 200


@pytest.mark.asyncio
async def test_static_files_are_served(ds):
    for path in ("annotation-editor.js", "renderers/annotations.js"):
        response = await ds.client.get(f"/-/static-plugins/datasette-charts/{path}")
        assert response.status_code == 200, path


@pytest.mark.asyncio
async def test_table_page_loads_datasette_modal(ds):
    # The annotation editor opens in Datasette's own <datasette-modal>
    response = await ds.client.get("/shop/orders")
    assert "modal.js" in response.text
    assert "datasette-charts" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ("/shop/orders", "/shop/paid_orders", "/shop/-/query?sql=select+*+from+orders"),
)
async def test_every_builder_page_loads_datasette_modal(ds, path):
    # The annotation editor opens in Datasette's own <datasette-modal>, which
    # base.html loads; every page with the Charts panel needs it
    response = await ds.client.get(path)
    assert response.status_code == 200
    assert 'class="datasette-charts' in response.text
    assert "/-/static/modal.js" in response.text


@pytest.mark.asyncio
async def test_copy_link_does_not_use_the_address_bar(ds):
    # Inside Datasette Agent's panel the chart page is in a frame, so the
    # address bar holds the conversation. The link is built from the page
    # and the spec, and copying works without the Clipboard API, which plain
    # HTTP pages do not have.
    charts = (await ds.client.get("/-/static-plugins/datasette-charts/charts.js")).text
    assert "() => location.href" not in charts
    assert "chartLink(location.href, this.current.spec)" in charts
    draw = (await ds.client.get("/-/static-plugins/datasette-charts/draw.js")).text
    assert 'document.execCommand("copy")' in draw
    assert "window.isSecureContext" in draw
    embed = (await ds.client.get("/-/static-plugins/datasette-charts/embed.js")).text
    assert "Copy link to this chart" in embed
