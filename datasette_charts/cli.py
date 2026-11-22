"`datasette charts ...` commands, for trying heuristics without a browser"

from __future__ import annotations

import asyncio
import json

import click


def _run(db_path, table, sql, params, time_limit_ms, fn):
    from datasette.app import Datasette

    from .config import get_config
    from .profiler import ProfileError, profile_source
    from .sources import Source

    if bool(table) == bool(sql):
        raise click.ClickException("Provide a table name or --sql, but not both")
    settings = {}
    if time_limit_ms:
        settings["sql_time_limit_ms"] = time_limit_ms

    async def inner():
        datasette = Datasette([db_path], settings=settings)
        await datasette.invoke_startup()
        database = next(
            name for name in datasette.databases if not name.startswith("_")
        )
        if table:
            db = datasette.get_database(database)
            if not (await db.table_exists(table) or await db.view_exists(table)):
                raise click.ClickException(f"No such table or view: {table}")
            source = Source(database=database, kind="table", table=table)
        else:
            source = Source(
                database=database, kind="query", sql=sql, params=dict(params)
            )
        try:
            profile = await profile_source(datasette, source, get_config(datasette))
        except ProfileError as e:
            raise click.ClickException(f"Could not profile: {e}")
        return fn(datasette, source, profile)

    return asyncio.run(inner())


def _basis_line(source, profile) -> str:
    rows = (
        "unknown number of" if profile.row_count is None else f"{profile.row_count:,}"
    )
    line = f"{source.label}: {rows} rows, profiled in {profile.duration_ms}ms with {profile.queries} queries"
    if profile.basis == "sample" and profile.sample:
        line += " (sample of {:,} rows, {})".format(
            profile.sample["rows"], profile.sample["method"]
        )
    return line


_common = [
    click.argument("db_path", type=click.Path(exists=True, dir_okay=False)),
    click.argument("table", required=False),
    click.option("--sql", help="Chart this SQL query, not a table"),
    click.option(
        "-p",
        "--param",
        "params",
        multiple=True,
        type=(str, str),
        help="Named parameter for --sql, e.g. -p state CA",
    ),
    click.option(
        "--time-limit-ms",
        type=int,
        help="Run with this sql_time_limit_ms, e.g. 10000",
    ),
    click.option("as_json", "--json", is_flag=True, help="Output JSON"),
]


def _with_common(fn):
    for decorator in reversed(_common):
        fn = decorator(fn)
    return fn


def register(cli):
    @cli.group()
    def charts():
        "Suggest charts for a table, view or SQL query"

    @charts.command()
    @_with_common
    def suggest(db_path, table, sql, params, time_limit_ms, as_json):
        "Print the suggested charts, with the signals behind each score"
        from . import engine
        from .config import get_config

        def output(datasette, source, profile):
            suggestions = engine.suggest(
                profile,
                engine.get_chart_types(datasette),
                get_config(datasette),
                debug=True,
            )
            if as_json:
                click.echo(
                    json.dumps(
                        {
                            "source": source.to_dict(),
                            "profile": profile.summary(),
                            "suggestions": suggestions,
                        },
                        indent=2,
                    )
                )
                return
            click.echo(_basis_line(source, profile))
            for note in profile.notes:
                click.echo(f"  note: {note}")
            if not suggestions:
                click.echo("No suggestions")
            for i, suggestion in enumerate(suggestions, 1):
                click.echo(
                    "\n{:>2}. [{:.3f}] {:<11} {}".format(
                        i,
                        suggestion["score"],
                        suggestion["spec"]["type"],
                        suggestion["title"],
                    )
                )
                click.echo(f"      {suggestion['reason']}")
                click.echo(
                    "      signals: "
                    + ", ".join(f"{k}={v}" for k, v in suggestion["signals"].items())
                )
                click.echo("      spec: " + json.dumps(suggestion["spec"]["encoding"]))

        _run(db_path, table, sql, params, time_limit_ms, output)

    @charts.command()
    @_with_common
    def profile(db_path, table, sql, params, time_limit_ms, as_json):
        "Print the profile: the facts gathered about each column"

        def output(datasette, source, profile):
            if as_json:
                click.echo(json.dumps(profile.to_dict(), indent=2, default=str))
                return
            click.echo(_basis_line(source, profile))
            for note in profile.notes:
                click.echo(f"  note: {note}")
            for pair in profile.pairs:
                click.echo(f"  pair: {pair}")
            width = max((len(column.name) for column in profile.columns), default=4)
            for column in profile.columns:
                roles = column.role
                if column.secondary_roles:
                    roles += " (+{})".format(", ".join(column.secondary_roles))
                distinct = "?"
                if column.distinct:
                    distinct = "{}{}".format(
                        column.distinct["count"],
                        "+" if column.distinct["capped"] else "",
                    )
                extra = ""
                if column.temporal:
                    extra = "  {} {} to {}".format(
                        column.temporal.kind, column.temporal.min, column.temporal.max
                    )
                elif column.numeric:
                    extra = "  {} to {}".format(
                        column.numeric["min"], column.numeric["max"]
                    )
                click.echo(
                    "  {:<{width}}  {:<24} distinct={:<6} null={:<6.1%} {}{}".format(
                        column.name,
                        roles,
                        distinct,
                        column.null_rate,
                        "; ".join(column.evidence),
                        extra,
                        width=width,
                    )
                )

        _run(db_path, table, sql, params, time_limit_ms, output)
