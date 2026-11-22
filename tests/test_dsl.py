import pytest

from datasette_charts import dsl
from datasette_charts.chart_types import BUILTIN_CHART_TYPES

CHART_TYPES = {cls.name: cls() for cls in BUILTIN_CHART_TYPES}
COLUMNS = ["created_at", "status", "price"]


def errors(spec):
    return dsl.validate_spec(spec, CHART_TYPES, COLUMNS)


def line(**encoding):
    return {"v": 1, "type": "line", "encoding": encoding, "options": {}}


def test_valid_specs():
    assert (
        errors(
            line(
                x={"column": "created_at", "timeUnit": "month"},
                y={"aggregate": "count"},
            )
        )
        == []
    )
    assert (
        errors(
            line(
                x={"column": "created_at"},
                y={"column": "price", "aggregate": "avg"},
                series={"column": "status", "limit": 5, "other": True},
            )
        )
        == []
    )
    assert (
        errors(
            {
                "v": 1,
                "type": "histogram",
                "encoding": {"x": {"column": "price", "bin": {"start": 0, "step": 50}}},
            }
        )
        == []
    )


@pytest.mark.parametrize(
    "spec,expected",
    (
        ("not a spec", "The spec must be a JSON object"),
        ({"v": 2, "type": "line", "encoding": {}}, "v must be 1"),
        ({"v": 1, "type": "pie", "encoding": {}}, "Unknown chart type: 'pie'"),
        ({"v": 1, "type": "line", "encoding": []}, "encoding must be an object"),
        (
            {"v": 1, "type": "line", "encoding": {}, "sql": "select 1"},
            "Unknown spec keys: sql",
        ),
        (line(y={"aggregate": "count"}), "x is required"),
        (
            line(x={"column": "nope"}, y={"aggregate": "count"}),
            "x: no such column: nope",
        ),
        (
            line(
                x={"column": "created_at"},
                y={"aggregate": "count"},
                color={"column": "status"},
            ),
            "line has no color channel",
        ),
        (
            line(
                x={"column": "created_at", "timeUnit": "fortnight"},
                y={"aggregate": "count"},
            ),
            "x: timeUnit must be one of year, quarter, month, week, day, hour, minute",
        ),
        (
            line(
                x={"column": "created_at"}, y={"column": "price", "aggregate": "median"}
            ),
            "y: aggregate must be one of count, sum, avg, min, max, count_distinct",
        ),
        (
            line(x={"column": "created_at"}, y={"aggregate": "sum"}),
            "y: sum needs a column",
        ),
        (
            line(x={"column": "created_at"}, y={"column": "price"}),
            "y needs an aggregate",
        ),
        (
            line(
                x={"column": "created_at", "expr": "1; drop table x"},
                y={"aggregate": "count"},
            ),
            "x: unknown keys: expr",
        ),
        (
            line(
                x={"column": "created_at"},
                y={"aggregate": "count"},
                series={"column": "status", "limit": 5000},
            ),
            "series: limit must be a whole number from 1 to 100",
        ),
        (
            {
                "v": 1,
                "type": "histogram",
                "encoding": {"x": {"column": "price", "bin": {"start": 0, "step": 0}}},
            },
            "x: bin step must be a number above zero",
        ),
        (
            {
                "v": 1,
                "type": "histogram",
                "encoding": {"x": {"column": "price", "bin": {"maxbins": 100000}}},
            },
            "x: maxbins must be a whole number from 1 to 200",
        ),
        (
            {
                "v": 1,
                "type": "histogram",
                "encoding": {"x": {"column": "price"}},
                "options": {"scale": "sideways"},
            },
            "scale must be one of linear, log",
        ),
    ),
)
def test_invalid_specs(spec, expected):
    assert expected in errors(spec)
