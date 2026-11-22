import json

from click.testing import CliRunner
from datasette.cli import cli


def test_charts_suggest(db_path):
    result = CliRunner().invoke(cli, ["charts", "suggest", db_path, "orders"])
    assert result.exit_code == 0, result.output
    assert "orders: 600 rows" in result.output
    assert "Rows per month of created_at" in result.output
    assert "signals: raw_score=" in result.output


def test_charts_suggest_json_for_sql(db_path):
    result = CliRunner().invoke(
        cli,
        [
            "charts", "suggest", db_path,
            "--sql", "select status, price from orders where quantity = :q",
            "-p", "q", "2",
            "--json",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["source"]["kind"] == "query"
    assert data["profile"]["row_count"] == 150
    assert data["suggestions"][0]["signals"]["raw_score"] > 0


def test_charts_profile(db_path):
    result = CliRunner().invoke(cli, ["charts", "profile", db_path, "places"])
    assert result.exit_code == 0, result.output
    assert "'latitude': 'latitude', 'longitude': 'longitude'" in result.output
    assert "built_year" in result.output


def test_charts_needs_a_table_or_sql(db_path):
    result = CliRunner().invoke(cli, ["charts", "suggest", db_path])
    assert result.exit_code == 1
    assert "Provide a table name or --sql" in result.output
    result = CliRunner().invoke(cli, ["charts", "suggest", db_path, "nope"])
    assert result.exit_code == 1
    assert "No such table or view: nope" in result.output
