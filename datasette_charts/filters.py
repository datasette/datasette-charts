"""
Table filters: charts on a filtered table page cover the same rows the page
shows.

A table source carries the page's filters as (key, value) pairs from its query
string, never as SQL. The WHERE clause is rebuilt on the server every time,
with the same machinery the table page uses: Datasette's ``Filters`` class for
column filters such as ``?state=CA`` and ``?price__gt=5``, and the
``filters_from_request`` plugin hook for ``?_where=``, ``?_search=`` and
``?_through=``. That keeps their permission checks too: ``_where`` still
needs execute-sql.
"""

from __future__ import annotations

import urllib.parse

from datasette.filters import Filters
from datasette.plugins import pm
from datasette.utils import await_me_maybe
from datasette.utils.asgi import Request

# Query string keys starting with _ that select rows. Everything else that
# starts with _, such as _sort, _size and _next, only changes how rows are
# shown, so it is left out and does not fragment the profile cache.
SPECIAL_FILTER_KEYS = {"_where", "_through", "_searchmode", "_fts_table", "_fts_pk"}
MAX_FILTERS = 100


def is_column_filter(key: str) -> bool:
    "The rule the table view uses to tell ?column=value from ?_special=value"
    return not (key.startswith("_") and "__" not in key)


def is_filter_key(key: str) -> bool:
    return (
        is_column_filter(key) or key in SPECIAL_FILTER_KEYS or key.startswith("_search")
    )


def filters_from_args(args) -> tuple:
    "The filters in a request's query string, as sorted (key, value) pairs"
    pairs = []
    for key in args:
        if is_filter_key(key):
            for value in args.getlist(key):
                pairs.append((key, value))
    return tuple(sorted(pairs))


def clean_filters(value) -> tuple:
    "Validate filters that arrived in a source descriptor from the browser"
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_FILTERS:
        raise ValueError("filters must be a list of [key, value] pairs")
    pairs = []
    for pair in value:
        if (
            not isinstance(pair, (list, tuple))
            or len(pair) != 2
            or not all(isinstance(part, str) for part in pair)
        ):
            raise ValueError("filters must be a list of [key, value] pairs")
        if is_filter_key(pair[0]):
            pairs.append((pair[0], pair[1]))
    return tuple(sorted(pairs))


async def build_where(datasette, request, database: str, table: str, filters) -> tuple:
    """
    Returns (where_sql, params) for a table's filters, or (None, {}) if they
    select every row. Raises whatever the table page itself would raise, for
    example when ``_where`` is used without permission.
    """
    # The hooks read the query string and the actor from a request, so they
    # get one that has this source's filters and the real request's actor
    scope = dict(request.scope)
    scope["query_string"] = urllib.parse.urlencode(list(filters)).encode("latin-1")
    filter_request = Request(scope, request.receive)

    # This mirrors _table_filters() in datasette/views/table.py
    column_filters = sorted(pair for pair in filters if is_column_filter(pair[0]))
    where_clauses, params = Filters(column_filters).build_where_clauses(table)
    for hook in pm.hook.filters_from_request(
        request=filter_request, table=table, database=database, datasette=datasette
    ):
        filter_arguments = await await_me_maybe(hook)
        if filter_arguments:
            where_clauses.extend(filter_arguments.where_clauses)
            params.update(filter_arguments.params)
    if not where_clauses:
        return None, {}
    # Joined exactly as the table view joins them, so a chart can never
    # cover different rows from the ones the page lists
    return " and ".join(where_clauses), params
