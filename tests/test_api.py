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
    assert data["sql"].startswith("with _chart_source as (")
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
async def test_no_charts_for_a_charts_own_sql(db_path):
    "Charting a chart's SQL would clash with the chart_ parameters it uses"
    ds = Datasette(
        [db_path],
        config={
            "databases": {
                "shop": {
                    "queries": {
                        "saved_chart": {
                            "sql": "select status from orders limit :chart_x_limit"
                        }
                    }
                }
            }
        },
    )
    await ds.invoke_startup()
    histogram = {
        "v": 1,
        "type": "histogram",
        "encoding": {"x": {"column": "price"}},
        "options": {},
    }
    data = await ds.client.get(
        "/shop/-/charts/data.json" + query(source=TABLE, spec=histogram)
    )
    sql_url = data.json()["sql_url"]
    assert ":chart_x_start" in data.json()["sql"]
    for path in (sql_url, "/shop/saved_chart?chart_x_limit=5"):
        page = await ds.client.get(path)
        assert page.status_code == 200
        assert 'id="datasette-charts"' not in page.text


def bootstrap_from(html):
    raw = html.split('data-bootstrap="')[1].split('"')[0]
    return json.loads(raw.replace("&#34;", '"').replace("&amp;", "&"))


@pytest.mark.asyncio
async def test_table_page_passes_its_filters_to_the_panel(ds):
    plain = bootstrap_from((await ds.client.get("/shop/orders")).text)
    assert "filters" not in plain["source"]
    # Sorting and paging change how rows are shown, not which rows there are
    sorted_page = bootstrap_from(
        (await ds.client.get("/shop/orders?_sort_desc=price&_size=10")).text
    )
    assert "filters" not in sorted_page["source"]
    filtered = bootstrap_from(
        (
            await ds.client.get(
                "/shop/orders?status=paid&quantity__gt=1&_sort=price&_facet=status"
            )
        ).text
    )
    assert filtered["source"] == {
        "database": "shop",
        "kind": "table",
        "table": "orders",
        "filters": [["quantity__gt", "1"], ["status", "paid"]],
    }


@pytest.mark.asyncio
async def test_charts_cover_the_rows_the_filtered_page_shows(ds):
    # The same query string means the same rows on the table page and here
    page = (
        await ds.client.get("/shop/orders.json?status=paid&quantity__gt=1&_extra=count")
    ).json()
    assert page["count"] == 264
    by_query_string = await ds.client.get(
        "/shop/orders/-/charts/suggest.json?status=paid&quantity__gt=1&_sort=price"
    )
    source = {
        "kind": "table",
        "table": "orders",
        "filters": [["status", "paid"], ["quantity__gt", "1"]],
    }
    by_source = await ds.client.get(
        "/shop/-/charts/suggest.json" + query(source=source)
    )
    assert by_query_string.status_code == by_source.status_code == 200
    assert by_query_string.json() == by_source.json()
    suggested = by_source.json()
    assert suggested["source"]["filters"] == [["quantity__gt", "1"], ["status", "paid"]]
    # The profile is of the filtered rows, so suggestions suit them:
    # status is now a single value, which is not worth a chart
    assert suggested["profile"]["row_count"] == 264
    roles = {column["name"]: column["role"] for column in suggested["columns"]}
    assert roles["status"] == "constant"
    assert "Rows by status" not in [s["title"] for s in suggested["suggestions"]]

    data = await ds.client.get(
        "/shop/-/charts/data.json" + query(source=suggested["source"], spec=LINE)
    )
    assert data.status_code == 200
    assert sum(row[1] for row in data.json()["rows"]) == 264
    # The SQL link runs the filtered query too
    sql_url = data.json()["sql_url"].replace("/-/query?", "/-/query.json?")
    sql_page = await ds.client.get(sql_url + "&_shape=array")
    assert sum(row["value"] for row in sql_page.json()) == 264
    # Unfiltered charts are unaffected
    unfiltered = await ds.client.get(
        "/shop/-/charts/data.json" + query(source=TABLE, spec=LINE)
    )
    assert sum(row[1] for row in unfiltered.json()["rows"]) == 600


@pytest.mark.asyncio
async def test_filters_that_select_every_row_share_the_plain_table_source(ds):
    # orders has no full-text index, so the table page ignores ?_search=
    response = await ds.client.get(
        "/shop/orders/-/charts/suggest.json?_search=anything"
    )
    assert response.status_code == 200
    assert response.json()["source"] == {
        "database": "shop",
        "kind": "table",
        "table": "orders",
    }


@pytest.mark.asyncio
async def test_where_filter_needs_execute_sql_as_on_the_table_page(db_path):
    where = {
        "kind": "table",
        "table": "orders",
        "filters": [["_where", "quantity = 2"]],
    }
    path = "/shop/-/charts/data.json" + query(source=where, spec=LINE)
    ds = Datasette([db_path])
    await ds.invoke_startup()
    allowed = await ds.client.get(path)
    assert allowed.status_code == 200
    assert sum(row[1] for row in allowed.json()["rows"]) == 150

    locked = Datasette([db_path], config={"allow_sql": False})
    await locked.invoke_startup()
    assert (
        await locked.client.get("/shop/orders?_where=quantity+%3D+2")
    ).status_code == 403
    denied = await locked.client.get(path)
    assert denied.status_code == 403
    assert denied.json() == {"ok": False, "error": "_where= is not allowed"}
    # Column filters need no such permission
    column = {"kind": "table", "table": "orders", "filters": [["quantity", "2"]]}
    response = await locked.client.get(
        "/shop/-/charts/data.json" + query(source=column, spec=LINE)
    )
    assert response.status_code == 200
    assert sum(row[1] for row in response.json()["rows"]) == 150


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filters,expected",
    (
        ("status=paid", "filters must be a list of [key, value] pairs"),
        ([["status"]], "filters must be a list of [key, value] pairs"),
        ([["status", 1]], "filters must be a list of [key, value] pairs"),
    ),
)
async def test_bad_filters(ds, filters, expected):
    source = {"kind": "table", "table": "orders", "filters": filters}
    response = await ds.client.get("/shop/-/charts/suggest.json" + query(source=source))
    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": expected}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query_string",
    (
        "status=paid",
        "status__not=paid",
        "status__isnull=1",
        "price__gte=50&quantity__in=1,2",
        "note__contains=order+1",
        "customer_id=3&status=pending",
        # A column that does not exist matches nothing on the table page
        "nope=1",
        "_where=quantity+%3D+2&status=paid",
        # Clauses are joined without brackets, exactly as the table page joins them
        "_where=quantity+%3D+2+or+quantity+%3D+3&status=paid",
    ),
)
async def test_filters_match_the_table_page(ds, query_string):
    page = (
        await ds.client.get(f"/shop/orders.json?{query_string}&_extra=count")
    ).json()
    suggested = (
        await ds.client.get(f"/shop/orders/-/charts/suggest.json?{query_string}")
    ).json()
    assert suggested["ok"] is True
    assert suggested["profile"]["row_count"] == page["count"]
    spec = {
        "v": 1,
        "type": "bar",
        "encoding": {"x": {"column": "is_gift"}, "y": {"aggregate": "count"}},
    }
    data = await ds.client.get(
        "/shop/-/charts/data.json" + query(source=suggested["source"], spec=spec)
    )
    assert sum(row[2] for row in data.json()["rows"]) == page["count"]


@pytest.mark.asyncio
async def test_through_filter(ds):
    # Customers with an order priced at 104.9, found through the foreign key
    # from orders, as the table page finds them
    through = json.dumps({"table": "orders", "column": "price", "value": "104.9"})
    query_string = urllib.parse.urlencode({"_through": through})
    page = (
        await ds.client.get(f"/shop/customers.json?{query_string}&_extra=count")
    ).json()
    assert 0 < page["count"] < 5
    suggested = (
        await ds.client.get(f"/shop/customers/-/charts/suggest.json?{query_string}")
    ).json()
    assert suggested["profile"]["row_count"] == page["count"]
    assert suggested["source"]["filters"] == [["_through", through]]


@pytest.mark.asyncio
async def test_search_filter(tmp_path):
    import sqlite3

    path = tmp_path / "docs.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        create table docs (id integer primary key, title text, kind text, words integer);
        create virtual table docs_fts using fts5(title, content=docs, content_rowid=id);
        """)
    for i in range(60):
        animal = ("pelican", "otter", "heron")[i % 3]
        conn.execute(
            "insert into docs values (?, ?, ?, ?)",
            (i + 1, f"The {animal} report {i}", ("note", "post")[i % 2], 100 + i * 7),
        )
    conn.execute("insert into docs_fts (rowid, title) select id, title from docs")
    conn.commit()
    conn.close()
    ds = Datasette([str(path)])
    await ds.invoke_startup()
    page = (await ds.client.get("/docs/docs.json?_search=pelican&_extra=count")).json()
    assert page["count"] == 20
    response = await ds.client.get("/docs/docs/-/charts/suggest.json?_search=pelican")
    assert response.status_code == 200
    assert response.json()["profile"]["row_count"] == 20
    spec = {
        "v": 1,
        "type": "bar",
        "encoding": {"x": {"column": "kind"}, "y": {"aggregate": "count"}},
    }
    data = await ds.client.get(
        "/docs/-/charts/data.json" + query(source=response.json()["source"], spec=spec)
    )
    assert sorted(row[2] for row in data.json()["rows"]) == [10, 10]


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
