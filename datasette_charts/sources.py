from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

SOURCE_KINDS = ("table", "query", "stored_query")


def quote(identifier: str) -> str:
    "Quote a SQLite identifier"
    return '"' + str(identifier).replace('"', '""') + '"'


def clean_sql(sql: str) -> str:
    "Strip the whitespace and trailing semicolons that would break a CTE body"
    sql = (sql or "").strip()
    while sql.endswith(";"):
        sql = sql[:-1].rstrip()
    return sql


@dataclass(frozen=True)
class Source:
    """
    The thing being charted: a table, a view, an arbitrary SQL query
    or a stored query.

    Every profiling and chart query has the shape
    ``with source as (...) select ... from source``.
    """

    database: str
    kind: str  # "table" | "query" | "stored_query"
    table: str | None = None  # table or view name
    sql: str | None = None
    params: dict | None = None
    query_name: str | None = None

    def __post_init__(self):
        if self.kind not in SOURCE_KINDS:
            raise ValueError(f"Unknown source kind: {self.kind!r}")
        if self.kind == "table" and not self.table:
            raise ValueError("Table sources need a table")
        if self.kind != "table" and not self.sql:
            raise ValueError("Query sources need SQL")

    @property
    def key(self) -> str:
        "Identifies the source: the table name, or a hash of the SQL and params"
        if self.kind == "table":
            return self.table
        payload = json.dumps(
            [clean_sql(self.sql), self.params or {}],
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if self.kind == "stored_query":
            return f"{self.query_name}:{digest}"
        return digest

    @property
    def body(self) -> str:
        "The SQL that goes inside the ``source`` CTE"
        if self.kind == "table":
            return f"select * from {quote(self.table)}"
        return clean_sql(self.sql)

    def with_clause(self) -> str:
        # The newlines mean a trailing "-- comment" in a query cannot
        # swallow the closing parenthesis
        return f"with source as (\n{self.body}\n)"

    @property
    def label(self) -> str:
        if self.kind == "table":
            return self.table
        if self.kind == "stored_query":
            return self.query_name
        return "query"

    def to_dict(self) -> dict:
        "The descriptor the browser sends back to identify this source"
        d = {"database": self.database, "kind": self.kind}
        if self.kind == "table":
            d["table"] = self.table
        elif self.kind == "stored_query":
            d["query_name"] = self.query_name
            if self.params:
                d["params"] = self.params
        else:
            d["sql"] = self.sql
            if self.params:
                d["params"] = self.params
        return d
