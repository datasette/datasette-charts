import json
import urllib.parse

import pytest
from datasette.app import Datasette

LINE = {
    "v": 1,
    "type": "line",
    "encoding": {
        "x": {"column": "created_at", "timeUnit": "year"},
        "y": {"aggregate": "count"},
    },
    "options": {},
}
TABLE = {"kind": "table", "table": "orders"}


def query(**params):
    return "?" + urllib.parse.urlencode(
        {k: v if isinstance(v, str) else json.dumps(v) for k, v in params.items()}
    )


@pytest.mark.asyncio
async def test_types_json(ds):
    response = await ds.client.get("/-/charts/types.json")
    assert response.status_code == 200
    data = response.json()
    assert [t["name"] for t in data["types"]] == [
        "histogram",
        "bar",
        "line",
        "scatter",
        "map",
        "stacked_bar",
    ]
    line = data["types"][2]
    assert [c["name"] for c in line["channels"]] == ["x", "y", "series"]
    assert line["js_url"].startswith(
        "/-/static-plugins/datasette-charts/renderers/line.js"
    )
    assert line["icon"].startswith("<svg")
    # Every renderer is a static file that is really there
    for chart_type in data["types"]:
        assert (await ds.client.get(chart_type["js_url"])).status_code == 200


@pytest.mark.asyncio
async def test_suggest_json_for_a_table(ds):
    response = await ds.client.get("/shop/orders/-/charts/suggest.json")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["source"] == {"database": "shop", "kind": "table", "table": "orders"}
    assert data["profile"]["basis"] == "full"
    assert data["profile"]["row_count"] == 600
    assert {
        "name": "created_at",
        "role": "temporal",
        "secondary_roles": [],
        "temporal": "iso_datetime",
    } in data["columns"]
    first = data["suggestions"][0]
    assert set(first) == {"spec", "title", "reason", "score", "basis", "preview"}
    assert first["preview"] == "eager"
    assert first["spec"]["v"] == 1


@pytest.mark.asyncio
async def test_suggest_json_for_a_query(ds):
    sql = "select status, price from orders where quantity = :q"
    by_sql = await ds.client.get("/shop/-/charts/suggest.json" + query(sql=sql, q="2"))
    by_source = await ds.client.get(
        "/shop/-/charts/suggest.json"
        + query(source={"kind": "query", "sql": sql, "params": {"q": "2"}})
    )
    assert by_sql.status_code == by_source.status_code == 200
    assert by_sql.json() == by_source.json()
    assert by_sql.json()["profile"]["row_count"] == 150
    assert [c["name"] for c in by_sql.json()["columns"]] == ["status", "price"]


@pytest.mark.asyncio
async def test_suggestions_are_deterministic(db_path):
    bodies = []
    for _ in range(2):
        ds = Datasette([db_path])
        await ds.invoke_startup()
        for path in (
            "/shop/orders/-/charts/suggest.json",
            "/shop/places/-/charts/suggest.json",
            "/shop/readings/-/charts/suggest.json",
        ):
            bodies.append((await ds.client.get(path)).text)
    assert bodies[:3] == bodies[3:]


@pytest.mark.asyncio
async def test_data_json(ds):
    response = await ds.client.get(
        "/shop/-/charts/data.json" + query(source=TABLE, spec=LINE)
    )
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["title"] == "Rows per year of created_at"
    assert data["columns"] == ["bucket", "value"]
    assert data["rows"] == [["2023", 237], ["2024", 237], ["2025", 126]]
    assert data["truncated"] is False
    assert data["meta"]["timeUnit"] == "year"
    # The compiled SQL is there for actors who could have run it themselves
    assert data["sql"].startswith("with source as (")
    sql_page = await ds.client.get(data["sql_url"] + "&_shape=array")
    assert sql_page.status_code == 200


@pytest.mark.asyncio
async def test_data_json_enforces_the_hard_row_cap(db_path):
    ds = Datasette(
        [db_path], config={"plugins": {"datasette-charts": {"hard_max_rows": 2}}}
    )
    await ds.invoke_startup()
    data = (
        await ds.client.get("/shop/-/charts/data.json" + query(source=TABLE, spec=LINE))
    ).json()
    assert len(data["rows"]) == 2
    assert data["truncated"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params,status,expected",
    (
        ({"source": TABLE}, 400, "A spec is required"),
        ({"spec": LINE}, 400, "A source is required"),
        ({"source": TABLE, "spec": "{not json"}, 400, "spec is not valid JSON"),
        (
            {"source": TABLE, "spec": LINE, "size": "huge"},
            400,
            "size must be thumbnail or full",
        ),
        (
            {"source": {"kind": "table", "table": "nope"}, "spec": LINE},
            404,
            "Table not found",
        ),
        (
            {"source": {"kind": "magic"}, "spec": LINE},
            400,
            "source kind must be table, query or stored_query",
        ),
        (
            {"source": {"kind": "query", "sql": "delete from orders"}, "spec": LINE},
            400,
            "Statement must be a SELECT",
        ),
        (
            {"source": {"kind": "query", "sql": "select * from nope"}, "spec": LINE},
            400,
            "no such table: nope",
        ),
    ),
)
async def test_data_json_errors(ds, params, status, expected):
    response = await ds.client.get("/shop/-/charts/data.json" + query(**params))
    assert response.status_code == status
    assert response.json() == {"ok": False, "error": expected}


@pytest.mark.asyncio
async def test_data_json_rejects_invalid_specs(ds):
    spec = {
        "v": 1,
        "type": "line",
        "encoding": {
            "x": {"column": 'created_at" from orders; --'},
            "y": {"aggregate": "nope"},
        },
    }
    response = await ds.client.get(
        "/shop/-/charts/data.json" + query(source=TABLE, spec=spec)
    )
    assert response.status_code == 400
    data = response.json()
    assert data["error"] == "Invalid spec"
    assert data["errors"] == [
        'x: no such column: created_at" from orders; --',
        "y: aggregate must be one of count, sum, avg, min, max, count_distinct",
    ]


@pytest.mark.asyncio
async def test_unknown_database_is_a_404(ds):
    response = await ds.client.get("/nope/-/charts/suggest.json" + query(source=TABLE))
    assert response.status_code == 404
    assert response.json() == {"ok": False, "error": "Database not found"}


@pytest.mark.asyncio
async def test_tables_work_without_execute_sql(db_path):
    # Goal 6: someone who can view a table but not run SQL still gets charts
    ds = Datasette([db_path], config={"allow_sql": False})
    await ds.invoke_startup()
    suggest = await ds.client.get("/shop/orders/-/charts/suggest.json")
    assert suggest.status_code == 200
    data = await ds.client.get(
        "/shop/-/charts/data.json" + query(source=TABLE, spec=LINE)
    )
    assert data.status_code == 200
    assert data.json()["rows"][0] == ["2023", 237]
    # ...but they are not shown the SQL, and cannot chart their own
    assert "sql" not in data.json()
    assert "sql_url" not in data.json()
    for path in ("suggest.json", "data.json", "profile.json"):
        response = await ds.client.get(
            f"/shop/-/charts/{path}"
            + query(source={"kind": "query", "sql": "select * from orders"}, spec=LINE)
        )
        assert response.status_code == 403
        assert response.json() == {"ok": False, "error": "Permission denied"}


@pytest.mark.asyncio
async def test_table_permissions_apply_to_every_endpoint(db_path):
    ds = Datasette(
        [db_path],
        config={
            "databases": {"shop": {"tables": {"orders": {"allow": {"id": "root"}}}}}
        },
    )
    await ds.invoke_startup()
    for path in (
        "/shop/orders/-/charts/suggest.json",
        "/shop/-/charts/suggest.json" + query(source=TABLE),
        "/shop/-/charts/data.json" + query(source=TABLE, spec=LINE),
        "/shop/-/charts/profile.json" + query(source=TABLE),
    ):
        response = await ds.client.get(path)
        assert response.status_code == 403, path
    cookies = {"ds_actor": ds.client.actor_cookie({"id": "root"})}
    response = await ds.client.get(
        "/shop/orders/-/charts/suggest.json", cookies=cookies
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_foreign_key_labels_need_permission_on_the_other_table(db_path):
    ds = Datasette(
        [db_path],
        config={
            "databases": {"shop": {"tables": {"customers": {"allow": {"id": "root"}}}}}
        },
    )
    await ds.invoke_startup()
    spec = {
        "v": 1,
        "type": "bar",
        "encoding": {"x": {"column": "customer_id"}, "y": {"aggregate": "count"}},
    }
    path = "/shop/-/charts/data.json" + query(source=TABLE, spec=spec)
    anonymous = (await ds.client.get(path)).json()
    assert anonymous["columns"] == ["key", "is_other", "value"]
    cookies = {"ds_actor": ds.client.actor_cookie({"id": "root"})}
    root = (await ds.client.get(path, cookies=cookies)).json()
    assert root["columns"] == ["key", "is_other", "value", "label"]
    assert root["rows"][0][3].startswith("Customer ")


@pytest.mark.asyncio
async def test_profile_json(ds):
    response = await ds.client.get("/shop/-/charts/profile.json" + query(source=TABLE))
    assert response.status_code == 200
    profile = response.json()["profile"]
    assert profile["row_count"] == 600
    assert profile["columns"][1]["name"] == "created_at"
    assert profile["columns"][1]["temporal"]["kind"] == "iso_datetime"


@pytest.mark.asyncio
async def test_timeout_is_reported(db_path):
    ds = Datasette([db_path], settings={"sql_time_limit_ms": 1})
    await ds.invoke_startup()
    slow = (
        "with recursive c(x) as (select 1 union all select x + 1 from c where x < 5000000)"
        " select x from c"
    )
    response = await ds.client.get(
        "/shop/-/charts/suggest.json" + query(source={"kind": "query", "sql": slow})
    )
    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "timeout"}


@pytest.mark.asyncio
async def test_panel_on_table_view_and_query_pages(ds):
    for path, kind in (
        ("/shop/orders", "table"),
        ("/shop/paid_orders", "table"),
        ("/shop/-/query?sql=select+*+from+orders+where+quantity+%3D+%3Aq&q=2", "query"),
    ):
        response = await ds.client.get(path)
        assert response.status_code == 200
        html = response.text
        assert (
            '<div class="datasette-charts" id="datasette-charts" data-bootstrap="'
            in html
        )
        assert "/-/static-plugins/datasette-charts/charts.js" in html
        assert "/-/static-plugins/datasette-charts/charts.css" in html
        bootstrap = json.loads(
            html.split('data-bootstrap="')[1]
            .split('"')[0]
            .replace("&#34;", '"')
            .replace("&amp;", "&")
        )
        assert bootstrap["source"]["kind"] == kind
        assert bootstrap["urls"]["data"] == "/shop/-/charts/data.json"
    assert bootstrap["source"]["params"] == {"q": "2"}
    # Pages that have nothing to chart do not load the plugin's assets
    assert "datasette-charts" not in (await ds.client.get("/shop")).text


@pytest.mark.asyncio
async def test_filtered_table_pages_say_charts_ignore_filters(ds):
    plain = (await ds.client.get("/shop/orders")).text
    filtered = (await ds.client.get("/shop/orders?status=paid")).text
    assert "&#34;filtered&#34;: false" in plain
    assert "&#34;filtered&#34;: true" in filtered


@pytest.mark.asyncio
async def test_stored_queries(db_path):
    ds = Datasette(
        [db_path],
        config={
            "databases": {
                "shop": {
                    "queries": {
                        "by_quantity": {
                            "sql": "select status, price, created_at from orders where quantity = :q"
                        },
                        "add_customer": {
                            "sql": "insert into customers (name) values (:name)",
                            "write": True,
                        },
                    }
                }
            }
        },
    )
    await ds.invoke_startup()
    page = await ds.client.get("/shop/by_quantity?q=2")
    assert page.status_code == 200
    assert "&#34;kind&#34;: &#34;stored_query&#34;" in page.text
    assert "&#34;query_name&#34;: &#34;by_quantity&#34;" in page.text
    assert "&#34;params&#34;: {&#34;q&#34;: &#34;2&#34;}" in page.text

    source = {"kind": "stored_query", "query_name": "by_quantity", "params": {"q": "2"}}
    suggest = await ds.client.get("/shop/-/charts/suggest.json" + query(source=source))
    assert suggest.status_code == 200
    assert suggest.json()["profile"]["row_count"] == 150
    data = await ds.client.get(
        "/shop/-/charts/data.json" + query(source=source, spec=LINE)
    )
    assert data.status_code == 200
    assert sum(row[1] for row in data.json()["rows"]) == 150

    # Queries that write are never charted
    assert (
        'class="datasette-charts"'
        not in (await ds.client.get("/shop/add_customer")).text
    )
    write = {"kind": "stored_query", "query_name": "add_customer"}
    response = await ds.client.get("/shop/-/charts/suggest.json" + query(source=write))
    assert response.status_code == 400
    assert response.json()["error"] == "Queries that write cannot be charted"
    missing = {"kind": "stored_query", "query_name": "nope"}
    response = await ds.client.get(
        "/shop/-/charts/suggest.json" + query(source=missing)
    )
    assert response.status_code == 404
