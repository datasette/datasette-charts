"""
The calendar heatmap, box plot and active count chart types, against a database of small
tables, each built to trigger (or not trigger) one of them. Everything is
generated from the row number, so it is deterministic.
"""

import collections
import datetime
import json
import sqlite3
from types import SimpleNamespace

import pytest
import pytest_asyncio
from datasette.app import Datasette

from datasette_charts import dsl, engine
from datasette_charts.chart_types.calendar_heatmap import fit_years
from datasette_charts.chart_types.active_count import is_start_end_pair
from datasette_charts.config import get_config
from datasette_charts.profiler import profile_source
from datasette_charts.sources import Source
from datasette_charts.sqlgen import SpecError

TYPES = ("calendar_heatmap", "box_plot", "active_count")
KINDS = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
EVENTS_START = datetime.datetime(2024, 1, 1, 8, 0, 0)
PACKAGES = ["one", "two", "three"]


def event_times():
    "Every 7 hours from 2024-01-01 08:00"
    return [EVENTS_START + datetime.timedelta(hours=7 * i) for i in range(600)]


def build_database(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        create table events (
            id integer primary key, created_at text, updated_at text,
            kind text, amount real
        );
        create table boxes (id integer primary key, cat text, v real);
        create table terms (
            id integer primary key, person text, party text,
            start_date text, end_date text
        );
        create table spans (
            id integer primary key, party text, start_date text, end_date text
        );
        create table survey (id integer primary key, submitted text, name text);
        create table downloads (
            package text, day text, downloads integer,
            primary key (package, day)
        );
        create table people (id integer primary key, name text, birthday text);
        create table plain (id integer primary key, name text, size real);
        """)
    conn.executemany(
        "insert into events values (?, ?, ?, ?, ?)",
        [
            (
                i + 1,
                moment.strftime("%Y-%m-%d %H:%M:%S"),
                (moment + datetime.timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"),
                KINDS[(i * i) % len(KINDS)],
                float((i * 37) % 101 + 10 * ((i * i) % len(KINDS))),
            )
            for i, moment in enumerate(event_times())
        ],
    )
    # Quartiles worked out by hand: a is 1..5, b is 1..4, c is 10
    boxes = [("a", v) for v in (5, 3, 1, 4, 2)] + [("b", v) for v in (4, 1, 3, 2)]
    boxes += [("c", 10)]
    conn.executemany("insert into boxes (cat, v) values (?, ?)", boxes)
    # Terms of two years each, staggered by month, for 120 people
    terms = []
    for i in range(240):
        start = datetime.date(1990 + i // 12, 1 + i % 12, 1)
        end = None if i >= 230 else start.replace(year=start.year + 2)
        terms.append(
            (
                i + 1,
                f"person {i % 120}",
                ["Red", "Blue", "Green"][(i * 7) % 5 % 3],
                start.isoformat(),
                end.isoformat() if end else None,
            )
        )
    conn.executemany("insert into terms values (?, ?, ?, ?, ?)", terms)
    # Running totals worked out by hand: see test_active_count_running_totals
    conn.executemany(
        "insert into spans (party, start_date, end_date) values (?, ?, ?)",
        [
            ("x", "2020-01-15", "2022-06-01"),
            ("y", "2021-03-01", "2021-09-01"),
            ("x", "2021-05-01", None),
            ("y", "2022-03-01", "2022-01-01"),  # ends before it starts
            ("x", "2022-06-01", "2023-01-01"),
        ],
    )
    conn.executemany(
        "insert into survey values (?, ?, ?)",
        [
            (
                i + 1,
                (
                    datetime.date(2020, 1, 1) + datetime.timedelta(days=i * 3)
                ).isoformat(),
                f"Person {i}",
            )
            for i in range(300)
        ],
    )
    # One row per package per day, for 200 days
    conn.executemany(
        "insert into downloads values (?, ?, ?)",
        [
            (
                package,
                (datetime.date(2025, 1, 1) + datetime.timedelta(days=d)).isoformat(),
                (d * 13 + p * 7) % 50 + 1,
            )
            for d in range(200)
            for p, package in enumerate(PACKAGES)
        ],
    )
    conn.executemany(
        "insert into people values (?, ?, ?)",
        [
            (
                i + 1,
                f"Person {i}",
                (
                    datetime.date(1950, 1, 1) + datetime.timedelta(days=i * 11)
                ).isoformat(),
            )
            for i in range(400)
        ],
    )
    conn.executemany(
        "insert into plain values (?, ?, ?)",
        [(i + 1, f"Thing {i}", float(i * 3 % 97)) for i in range(200)],
    )
    conn.commit()
    conn.close()


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("dbs") / "more.db"
    build_database(path)
    return str(path)


@pytest_asyncio.fixture
async def ds(db_path):
    datasette = Datasette([db_path])
    await datasette.invoke_startup()
    return datasette


def table(name):
    return Source(database="more", kind="table", table=name)


def spec(type_, options=None, **encoding):
    return {"v": 1, "type": type_, "encoding": encoding, "options": options or {}}


async def run(ds, source, chart):
    "Compile a spec and run it, the way the data endpoint does"
    chart_types = engine.get_chart_types(ds)
    profile = await profile_source(ds, source)
    compiled = chart_types[chart["type"]].compile(chart, source, profile)
    results = await ds.get_database("more").execute(compiled.sql, compiled.params)
    rows = [dict(zip([c.name for c in results.columns], row)) for row in results.rows]
    assert len(rows) <= compiled.max_rows
    return rows, compiled


async def proposed(ds, name, type_):
    "Candidates from one chart type for a table, straight from propose()"
    profile = await profile_source(ds, table(name))
    return engine.get_chart_types(ds)[type_].propose(profile)


async def suggested(ds, name):
    profile = await profile_source(ds, table(name))
    return engine.suggest(profile, engine.get_chart_types(ds), get_config(ds))


async def validate(ds, name, chart):
    profile = await profile_source(ds, table(name))
    return dsl.validate_spec(chart, engine.get_chart_types(ds), profile.column_names)


async def get_data(ds, name, chart):
    return await ds.client.get(
        "/more/-/charts/data.json",
        params={
            "source": json.dumps({"kind": "table", "table": name}),
            "spec": json.dumps(chart),
        },
    )


# --- Registration -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_types_are_registered(ds):
    response = await ds.client.get("/-/charts/types.json")
    assert response.status_code == 200
    types = {t["name"]: t for t in response.json()["types"]}
    for name in TYPES:
        chart_type = types[name]
        assert chart_type["label"]
        assert chart_type["icon"].startswith("<svg")
        assert "/-/static-plugins/datasette-charts/renderers/" in chart_type["js_url"]
        assert chart_type["max_rows"] <= 5000
        response = await ds.client.get(chart_type["js_url"])
        assert response.status_code == 200
        assert "export function render" in response.text
    assert types["box_plot"]["annotations"]["types"] == [
        "line",
        "range",
        "point",
        "text",
    ]
    assert types["box_plot"]["annotations"]["axes"] == {"x": "category", "y": "number"}
    assert types["active_count"]["annotations"]["axes"] == {"x": "time", "y": "number"}
    # No straight time axis: free-floating notes only
    assert types["calendar_heatmap"]["annotations"]["types"] == ["text"]
    assert list(types["calendar_heatmap"]["options"]) == ["years"]


# --- Every suggestion compiles, runs and stays bounded ------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    ("events", "boxes", "terms", "spans", "survey", "downloads", "people", "plain"),
)
async def test_every_candidate_compiles_and_runs(ds, name):
    source = table(name)
    chart_types = engine.get_chart_types(ds)
    profile = await profile_source(ds, source)
    for type_ in TYPES:
        chart_type = chart_types[type_]
        for candidate in chart_type.propose(profile):
            assert candidate.reason
            assert 0 < candidate.score < 1
            assert chart_type.title(candidate.spec)
            assert not await validate(ds, name, candidate.spec), candidate.spec
            rows, compiled = await run(ds, source, candidate.spec)
            assert rows, candidate.spec
            assert compiled.max_rows <= chart_type.max_rows


@pytest.mark.asyncio
async def test_none_for_a_plain_table(ds):
    for type_ in TYPES:
        assert await proposed(ds, "plain", type_) == [], type_


@pytest.mark.asyncio
async def test_they_do_not_crowd_out_the_others(ds):
    "On an ordinary table of events the line chart still comes first"
    types = [s["spec"]["type"] for s in await suggested(ds, "events")]
    assert types[0] == "line"
    assert sum(1 for t in types if t in TYPES) <= 3


# --- calendar heatmap ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calendar_proposals(ds):
    candidates = await proposed(ds, "events", "calendar_heatmap")
    columns = [c.spec["encoding"]["x"]["column"] for c in candidates]
    assert columns == ["created_at", "updated_at"]
    # A second calendar, of a weaker date column, scores well under the first
    assert candidates[1].score <= candidates[0].score - 0.15
    assert candidates[0].spec["options"] == {"years": "1"}
    assert candidates[0].reason == "created_at spans 6 months; one cell per day of 2024"
    survey = await proposed(ds, "survey", "calendar_heatmap")
    assert [c.spec["options"] for c in survey] == [{"years": "3"}]
    assert survey[0].reason == (
        "submitted spans 2 years; one cell per day, 2020 to 2022"
    )


@pytest.mark.asyncio
async def test_calendar_not_proposed_when_mostly_empty(ds):
    # A term starts once a month: a calendar of days would be almost empty
    assert await proposed(ds, "terms", "calendar_heatmap") == []
    # A calendar of the most recent years of birthdays shows nothing
    assert await proposed(ds, "people", "calendar_heatmap") == []


@pytest.mark.asyncio
async def test_calendar_of_a_panel_shows_the_measure(ds):
    "One row per package per day: a count is the same every day"
    candidates = await proposed(ds, "downloads", "calendar_heatmap")
    assert len(candidates) == 1
    assert candidates[0].spec["encoding"]["y"] == {
        "column": "downloads",
        "aggregate": "sum",
    }
    assert candidates[0].reason.startswith("Total downloads for each day: ")


@pytest.mark.parametrize(
    "start,end,expected",
    [
        ("2024-01-05", "2024-11-30", "1"),
        ("2023-06-01", "2024-02-01", "2"),
        ("2022-03-01", "2024-08-01", "3"),
        ("2021-01-01", "2024-12-31", "5"),
        ("2019-01-01", "2024-12-31", "10"),
        ("2015-06-03T17:23:01+00:00", "2026-11-21T23:06:29+00:00", "10"),
    ],
)
def test_calendar_fit_years(start, end, expected):
    assert fit_years(SimpleNamespace(min=start, max=end)) == expected


@pytest.mark.asyncio
async def test_calendar_days(ds):
    rows, compiled = await run(
        ds,
        table("events"),
        spec("calendar_heatmap", {"years": "5"}, x={"column": "created_at"}),
    )
    expected = collections.Counter(
        moment.date().isoformat() for moment in event_times()
    )
    assert {r["day"]: r["value"] for r in rows} == dict(expected)
    assert compiled.max_rows == 5 * 366
    assert compiled.meta["years"] == 5


@pytest.mark.asyncio
async def test_calendar_with_a_measure(ds):
    rows, _ = await run(
        ds,
        table("events"),
        spec(
            "calendar_heatmap",
            x={"column": "created_at"},
            y={"column": "amount", "aggregate": "sum"},
        ),
    )
    assert sum(r["value"] for r in rows) == pytest.approx(
        sum(float((i * 37) % 101 + 10 * ((i * i) % 6)) for i in range(600))
    )


@pytest.mark.asyncio
async def test_calendar_keeps_only_recent_years(ds):
    rows, _ = await run(
        ds,
        table("terms"),
        spec("calendar_heatmap", {"years": "2"}, x={"column": "start_date"}),
    )
    # Terms start from 1990 to 2009: only 2008 and 2009 are shown
    assert {r["day"][:4] for r in rows} == {"2008", "2009"}


@pytest.mark.asyncio
@pytest.mark.parametrize("years,first", [("1", 2009), ("5", 2005), ("10", 2000)])
async def test_calendar_years_count_back_from_the_latest_date(ds, years, first):
    """
    The years are the ones up to the latest date in the data, not up to today:
    terms stopped starting in 2009, and still get a full calendar
    """
    rows, _ = await run(
        ds,
        table("terms"),
        spec("calendar_heatmap", {"years": years}, x={"column": "start_date"}),
    )
    assert {int(r["day"][:4]) for r in rows} == set(range(first, 2010))


@pytest.mark.asyncio
async def test_calendar_needs_dates(ds):
    profile = await profile_source(ds, table("plain"))
    chart = spec("calendar_heatmap", x={"column": "name"})
    with pytest.raises(SpecError, match="^A calendar heatmap needs dates: "):
        engine.get_chart_types(ds)["calendar_heatmap"].compile(
            chart, table("plain"), profile
        )


@pytest.mark.asyncio
async def test_calendar_annotations(ds):
    chart = spec("calendar_heatmap", x={"column": "created_at"})
    chart["annotations"] = [{"type": "text", "left": 0.5, "top": 0.1, "text": "Hi"}]
    assert await validate(ds, "events", chart) == []
    chart["annotations"] = [{"type": "line", "x": "2024-03-01", "text": "Launch"}]
    assert await validate(ds, "events", chart) == [
        "annotations[0]: calendar_heatmap charts take text annotations, not line"
    ]


# --- box --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_box_plot_quartiles(ds):
    rows, compiled = await run(
        ds,
        table("boxes"),
        spec(
            "box_plot",
            {"order": "name"},
            x={"column": "cat", "limit": 12},
            y={"column": "v"},
        ),
    )
    by_key = {r["key"]: r for r in rows}
    # The same as numpy's default, linear interpolation
    assert {
        k: by_key["a"][k] for k in ("count", "min", "q1", "median", "q3", "max")
    } == {"count": 5, "min": 1, "q1": 2, "median": 3, "q3": 4, "max": 5}
    assert [by_key["b"][k] for k in ("min", "q1", "median", "q3", "max")] == [
        1,
        1.75,
        2.5,
        3.25,
        4,
    ]
    assert [by_key["c"][k] for k in ("min", "q1", "median", "q3", "max")] == [10] * 5
    assert by_key["b"]["mean"] == 2.5
    assert [r["key"] for r in rows] == ["a", "b", "c"]
    assert compiled.max_rows == 13


@pytest.mark.asyncio
async def test_box_plot_top_n_order_and_other(ds):
    chart = spec(
        "box_plot",
        {"order": "count"},
        x={"column": "cat", "limit": 1},
        y={"column": "v"},
    )
    rows, _ = await run(ds, table("boxes"), chart)
    # a is the most common; Other is left out unless asked for
    assert [r["key"] for r in rows] == ["a"]
    chart["encoding"]["x"]["other"] = True
    rows, _ = await run(ds, table("boxes"), chart)
    assert [(r["key"], r["is_other"], r["count"]) for r in rows] == [
        ("a", 0, 5),
        (None, 1, 5),
    ]
    # Other is b (1..4) and c (10)
    assert rows[1]["median"] == 3 and rows[1]["max"] == 10


@pytest.mark.asyncio
async def test_box_plot_orders_by_median(ds):
    rows, _ = await run(
        ds, table("boxes"), spec("box_plot", {}, x={"column": "cat"}, y={"column": "v"})
    )
    assert [r["key"] for r in rows] == ["c", "a", "b"]


@pytest.mark.asyncio
async def test_box_plot_proposals(ds):
    candidates = await proposed(ds, "events", "box_plot")
    assert candidates
    encoding = candidates[0].spec["encoding"]
    assert encoding["x"]["column"] == "kind"
    assert encoding["y"] == {"column": "amount"}
    assert candidates[0].signals["name_hint"] is True
    # Too few rows per box to be worth it
    assert await proposed(ds, "boxes", "box_plot") == []


@pytest.mark.asyncio
async def test_box_plot_log_scale_needs_positive_values(ds):
    chart = spec("box_plot", {"scale": "log"}, x={"column": "cat"}, y={"column": "v"})
    _, compiled = await run(ds, table("boxes"), chart)
    assert compiled.meta["scale"] == "log"
    chart = spec(
        "box_plot", {"scale": "log"}, x={"column": "name"}, y={"column": "size"}
    )
    with pytest.raises(SpecError, match="values of zero or less"):
        await run(ds, table("plain"), chart)


@pytest.mark.asyncio
async def test_box_plot_validation(ds):
    errors = await validate(
        ds,
        "boxes",
        spec(
            "box_plot",
            x={"column": "cat", "limit": 60},
            y={"column": "v", "aggregate": "sum"},
        ),
    )
    assert "y does not take an aggregate" in errors
    assert "x: limit must be at most 50 on this chart" in errors
    errors = await validate(
        ds, "boxes", spec("box_plot", {"order": "size"}, x={"column": "cat"})
    )
    assert "y is required" in errors
    assert "order must be one of median, count, name" in errors
    chart = spec("box_plot", x={"column": "cat"}, y={"column": "v"})
    chart["annotations"] = [
        {"type": "line", "y": 3, "text": "Target"},
        {"type": "range", "x": "a", "x2": "b", "text": "These two"},
        # Points at a's median
        {"type": "point", "x": "a", "text": "Hi"},
    ]
    assert await validate(ds, "boxes", chart) == []


# --- active count --------------------------------------------------------------------


def test_start_end_names():
    for start, end in (
        ("start", "end"),
        ("start_date", "end_date"),
        ("valid_from", "valid_to"),
        ("opened_at", "closed_at"),
        ("startedAt", "endedAt"),
        ("begin", "end"),
        ("term_start", "term_end"),
    ):
        assert is_start_end_pair(start, end), (start, end)
    for start, end in (
        ("end", "start"),
        ("created", "updated"),
        ("start_date", "finish_time"),
        ("start", "start"),
    ):
        assert not is_start_end_pair(start, end), (start, end)


@pytest.mark.asyncio
async def test_active_count_proposals(ds):
    candidates = await proposed(ds, "terms", "active_count")
    assert [c.spec["encoding"]["start"]["column"] for c in candidates] == [
        "start_date",
        "start_date",
    ]
    assert candidates[0].spec["encoding"]["end"] == {"column": "end_date"}
    assert candidates[1].spec["encoding"]["series"]["column"] == "party"
    assert "count as still active" in candidates[0].reason
    suggestions = await suggested(ds, "terms")
    assert suggestions[0]["title"] == (
        "Rows active each quarter from start_date to end_date"
    )
    # created_at and updated_at are not a start and an end
    assert await proposed(ds, "events", "active_count") == []


@pytest.mark.asyncio
async def test_active_count_running_totals(ds):
    rows, _ = await run(
        ds,
        table("spans"),
        spec(
            "active_count",
            start={"column": "start_date", "timeUnit": "year"},
            end={"column": "end_date"},
        ),
    )
    # 2020-01-15 to 2022-06-01, 2021-03-01 to 2021-09-01, 2021-05-01 to (no
    # end), 2022-06-01 to 2023-01-01; one row ends before it starts. A row
    # counts up to, not including, the bucket it ends in, unless it ends in
    # the bucket it starts in.
    assert [(r["bucket"], r["value"], r["after"]) for r in rows] == [
        ("2020", 1, 1),
        ("2021", 3, 2),
        ("2022", 2, 2),
        ("2023", 1, 1),
    ]


@pytest.mark.asyncio
async def test_active_count_by_series(ds):
    rows, compiled = await run(
        ds,
        table("spans"),
        spec(
            "active_count",
            start={"column": "start_date", "timeUnit": "year"},
            end={"column": "end_date"},
            series={"column": "party", "limit": 1, "other": True},
        ),
    )
    assert [
        (r["bucket"], r["series"], r["is_other"], r["value"], r["after"]) for r in rows
    ] == [
        ("2020", "x", 0, 1, 1),
        ("2021", "x", 0, 2, 2),
        ("2021", None, 1, 1, 0),
        ("2022", "x", 0, 2, 2),
        ("2023", "x", 0, 1, 1),
    ]
    assert compiled.max_rows == 500 * 2


@pytest.mark.asyncio
async def test_active_count_terms_add_up(ds):
    "Two-year terms starting every month: 24 active at once, once under way"
    rows, _ = await run(
        ds,
        table("terms"),
        spec(
            "active_count",
            start={"column": "start_date", "timeUnit": "month"},
            end={"column": "end_date"},
        ),
    )
    values = {r["bucket"]: r["value"] for r in rows}
    assert values["1990-01"] == 1
    assert values["1995-06"] == 24
    # The last ten terms have no end, so they are still going at the end
    assert rows[-1]["after"] == 10


@pytest.mark.asyncio
async def test_active_count_validation_and_bounds(ds):
    errors = await validate(
        ds,
        "terms",
        spec(
            "active_count", start={"column": "start_date"}, end={"column": "start_date"}
        ),
    )
    assert "start and end must be different columns" in errors
    errors = await validate(
        ds, "terms", spec("active_count", start={"column": "start_date"})
    )
    assert "end is required" in errors
    with pytest.raises(SpecError):
        await run(
            ds,
            table("terms"),
            spec(
                "active_count",
                start={"column": "start_date", "timeUnit": "day"},
                end={"column": "end_date"},
            ),
        )
    chart = spec(
        "active_count", start={"column": "start_date"}, end={"column": "end_date"}
    )
    chart["annotations"] = [
        {"type": "line", "x": "2000", "text": "Election"},
        {"type": "point", "x": "1995-06", "text": "Full"},
    ]
    assert await validate(ds, "terms", chart) == []


# --- data.json end to end -----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,chart",
    (
        ("events", spec("calendar_heatmap", x={"column": "created_at"})),
        ("events", spec("box_plot", x={"column": "kind"}, y={"column": "amount"})),
        (
            "spans",
            spec(
                "active_count",
                start={"column": "start_date"},
                end={"column": "end_date"},
            ),
        ),
    ),
)
async def test_data_json(ds, name, chart):
    response = await get_data(ds, name, chart)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ok"]
    assert data["rows"]
    assert not data["truncated"]
    assert data["title"]


@pytest.mark.asyncio
async def test_data_json_rejects_bad_specs(ds):
    response = await get_data(
        ds, "plain", spec("calendar_heatmap", x={"column": "name"})
    )
    assert response.status_code == 400
    assert "A calendar heatmap needs dates" in response.json()["error"]
    response = await get_data(
        ds, "boxes", spec("box_plot", x={"column": "nope"}, y={"column": "v"})
    )
    assert response.status_code == 400
    assert response.json()["errors"] == ["x: no such column: nope"]


@pytest.mark.asyncio
async def test_suggest_json(ds):
    response = await ds.client.get("/more/terms/-/charts/suggest.json")
    suggestions = response.json()["suggestions"]
    assert suggestions[0]["spec"]["type"] == "active_count"
