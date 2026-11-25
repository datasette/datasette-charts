"""
A chart open on a SQL query page goes with edited SQL: charts.js puts it in
the #chart= fragment of the SQL form's action. These tests check the parts
of that which Datasette's pages provide.
"""

import pytest


@pytest.mark.asyncio
async def test_query_page_form_is_a_get_form_with_charts_js(ds):
    response = await ds.client.get(
        "/shop/-/query", params={"sql": "select * from orders"}
    )
    assert response.status_code == 200
    assert '<form class="sql core" action="/shop" method="get"' in response.text
    assert "/-/static-plugins/datasette-charts/charts.js" in response.text


@pytest.mark.asyncio
async def test_sql_error_page_still_has_the_form_and_charts_js(ds):
    # Fixing the SQL there and running it again brings the chart back
    response = await ds.client.get(
        "/shop/-/query", params={"sql": "select nope from orders"}
    )
    assert "no such column: nope" in response.text
    assert '<form class="sql core" action="/shop" method="get"' in response.text
    assert "/-/static-plugins/datasette-charts/charts.js" in response.text


@pytest.mark.asyncio
async def test_form_redirect_keeps_the_fragment(ds):
    # The form goes to /db?sql=, which redirects. A Location with no fragment
    # of its own keeps the one the browser asked with.
    response = await ds.client.get("/shop", params={"sql": "select 1"})
    assert response.status_code == 302
    assert response.headers["location"].startswith("/shop/-/query?sql=")
    assert "#" not in response.headers["location"]


@pytest.mark.asyncio
async def test_charts_js_carries_the_open_chart(ds):
    js = (await ds.client.get("/-/static-plugins/datasette-charts/charts.js")).text
    # Set on the form's action, so form.submit() from the editor carries it too
    assert 'form.setAttribute("action", fragment ? action + fragment : action)' in js
    assert "carryThroughSqlForm(fragment);" in js
    assert "carryThroughSqlForm(null);" in js
