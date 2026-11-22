"""
Role assignment: deterministic and rule-based.

Every rule, threshold and name pattern the profiler uses to decide what a
column means lives in this module, so changes to them are easy to review.
"""

from __future__ import annotations

import re

from .model import TEMPORAL_KINDS, ColumnProfile, Profile, TemporalInfo

# --- Thresholds -------------------------------------------------------------

# A text column is a category when it has at most this many distinct values...
CATEGORY_MAX_DISTINCT = 500
# ...and values repeat: distinct / non-null is at most this
CATEGORY_MAX_DISTINCT_RATIO = 0.5
# Small sources get a looser ratio, since a few rows cannot repeat much
CATEGORY_SMALL_SOURCE_ROWS = 40
CATEGORY_SMALL_SOURCE_RATIO = 0.75
# An integer column with this few distinct values is a category first
NUMERIC_CATEGORY_MAX_DISTINCT = 12
# ...and up to this many it is a measure that can also act as a category
NUMERIC_SECONDARY_CATEGORY_MAX_DISTINCT = 50
# Share of non-null values that must have a storage type for it to count
TYPE_MAJORITY = 0.9
# Share of text values that must match for url and json detection
STRUCTURED_TEXT_RATE = 0.95
# Share of numeric values that must fall in range for a latitude or longitude
COORDINATE_MIN_IN_RANGE = 0.95
# Mean length above which text is treated as free text without counting
# its distinct values
LONG_TEXT_MEAN_LENGTH = 256

# --- Value windows (spec section 7.5) ---------------------------------------

UNIX_SECONDS_WINDOW = (315532800, 4102444800)  # 1980-01-01 to 2100-01-01
UNIX_MS_WINDOW = (315532800000, 4102444800000)
YEAR_WINDOW = (1000, 2200)

# --- Name patterns ----------------------------------------------------------

TEMPORAL_NAME_TOKENS = {
    "date",
    "time",
    "timestamp",
    "ts",
    "created",
    "updated",
    "modified",
    "dob",
    "datetime",
}
TEMPORAL_NAME_SUFFIXES = {"at", "on", "date", "time"}
YEAR_NAME_TOKENS = {"year", "yr"}
IDENTIFIER_NAME_TOKENS = {
    "id",
    "ids",
    "rowid",
    "pk",
    "uuid",
    "guid",
    "hash",
    "digest",
    "md5",
    "sha",
    "sha1",
    "sha256",
    "token",
}
# Columns with exactly these names identify the row, however often they repeat
ROW_IDENTITY_NAMES = {"id", "rowid", "_rowid_", "oid", "pk"}
# Numbers that are labels, not quantities
CODE_NAME_TOKENS = {
    "zip",
    "zipcode",
    "postcode",
    "postal",
    "phone",
    "fax",
    "code",
    "ssn",
    "fips",
    "isbn",
}
LATITUDE_NAME_TOKENS = {"lat", "latitude"}
LONGITUDE_NAME_TOKENS = {"lon", "lng", "long", "longitude"}
BOOLEAN_TEXT_VALUES = {"true", "false", "yes", "no", "t", "f", "y", "n", "0", "1"}


def name_tokens(name: str) -> list:
    "created_at -> [created, at]; createdAt -> [created, at]"
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name or "")
    return [t for t in re.split(r"[^a-z0-9]+", spaced.lower()) if t]


def has_temporal_name_hint(name: str) -> bool:
    tokens = name_tokens(name)
    if not tokens:
        return False
    return bool(TEMPORAL_NAME_TOKENS.intersection(tokens)) or (
        len(tokens) > 1 and tokens[-1] in TEMPORAL_NAME_SUFFIXES
    )


def has_year_name_hint(name: str) -> bool:
    return bool(YEAR_NAME_TOKENS.intersection(name_tokens(name)))


def has_temporal_type_hint(declared_type: str | None) -> bool:
    declared = (declared_type or "").upper()
    return "DATE" in declared or "TIME" in declared


def has_identifier_name_hint(name: str) -> bool:
    return bool(IDENTIFIER_NAME_TOKENS.intersection(name_tokens(name)))


def has_code_name_hint(name: str) -> bool:
    return bool(CODE_NAME_TOKENS.intersection(name_tokens(name)))


def affinity_for(declared_type: str | None) -> str:
    "SQLite's column affinity rules"
    declared = (declared_type or "").upper()
    if "INT" in declared:
        return "integer"
    if "CHAR" in declared or "CLOB" in declared or "TEXT" in declared:
        return "text"
    if "BLOB" in declared or not declared:
        return "blob"
    if "REAL" in declared or "FLOA" in declared or "DOUB" in declared:
        return "real"
    return "numeric"


def _fmt_count(n) -> str:
    return f"{n:,}"


def _plural(n, noun: str) -> str:
    return f"{n:,} {noun}" if n == 1 else f"{n:,} {noun}s"


def _pct(rate: float) -> str:
    return f"{round(rate * 100)}%"


# --- Temporal detection -----------------------------------------------------


def detect_temporal(col: ColumnProfile, min_match_rate: float, override=None):
    """
    Returns (kind, match_rate, origin, is_candidate_only).

    kind is None when the column is not temporal. A candidate matched on
    values but lacked the name or declared-type hint a numeric kind needs.
    """
    tests = col.tests
    present = col.present
    if override is False:
        return None, 0.0, "config", False
    if override in TEMPORAL_KINDS:
        rate = _kind_rate(override, tests, present)
        return override, rate, "config", False
    if not present:
        return None, 0.0, "detected", False

    # ISO text is specific enough to be trusted on values alone. A column
    # that mixes dates and datetimes is treated as iso_datetime.
    n_date = tests.get("iso_date", 0)
    n_datetime = tests.get("iso_datetime", 0)
    iso_rate = (n_date + n_datetime) / present
    if iso_rate >= min_match_rate:
        return (
            ("iso_datetime" if n_datetime else "iso_date"),
            iso_rate,
            "detected",
            False,
        )

    # Numbers need a hint as well as matching values
    time_hint = has_temporal_name_hint(col.name) or has_temporal_type_hint(
        col.declared_type
    )
    for kind in ("unix_seconds", "unix_ms"):
        rate = tests.get(kind, 0) / present
        if rate >= min_match_rate:
            return kind, rate, "detected", not time_hint
    rate = tests.get("year", 0) / present
    if rate >= min_match_rate:
        return "year", rate, "detected", not has_year_name_hint(col.name)
    return None, 0.0, "detected", False


def _kind_rate(kind: str, tests: dict, present: int) -> float:
    if not present:
        return 0.0
    if kind == "iso_datetime":
        return (tests.get("iso_date", 0) + tests.get("iso_datetime", 0)) / present
    return tests.get(kind, 0) / present


# --- Per-column roles -------------------------------------------------------


def assign_role(
    col: ColumnProfile,
    *,
    min_match_rate: float = 0.95,
    temporal_override=None,
) -> None:
    "Sets role, secondary_roles, evidence, temporal and temporal_candidate"
    col.role = "unknown"
    col.secondary_roles = []
    col.evidence = []
    col.temporal = None
    col.temporal_candidate = None

    counts = col.type_counts
    non_null = col.non_null
    distinct = col.distinct_count  # None when unknown or over the cap
    n_numeric = counts.get("integer", 0) + counts.get("real", 0)
    mostly = lambda n: bool(non_null) and n / non_null >= TYPE_MAJORITY  # noqa: E731
    is_numeric = mostly(n_numeric)
    is_text = mostly(counts.get("text", 0))

    # 1. Nothing to chart. constant takes precedence over temporal.
    if non_null == 0:
        col.role = "constant"
        col.evidence.append("every value is null")
        return
    if distinct == 1 and col.null_rate == 0:
        col.role = "constant"
        col.evidence.append("one distinct value")
        return
    if mostly(counts.get("blob", 0)):
        col.role = "blob"
        col.evidence.append("binary data")
        return

    # 2. Temporal. Takes precedence over identifier: a created_at column
    #    that happens to be unique is still a time axis.
    kind, rate, origin, candidate_only = detect_temporal(
        col, min_match_rate, temporal_override
    )
    if kind and candidate_only:
        col.temporal_candidate = {"kind": kind, "match_rate": round(rate, 4)}
    elif kind:
        col.role = "temporal"
        col.temporal = TemporalInfo(
            kind=kind,
            match_rate=round(rate, 4),
            min=None,
            max=None,
            has_offset=False,
            origin=origin,
        )
        if origin == "config":
            col.evidence.append(f"configured as {kind}")
        else:
            col.evidence.append(f"{_pct(rate)} of values are {kind}")
        if kind == "year" and distinct is not None:
            col.secondary_roles.append("category")
        return

    id_name = has_identifier_name_hint(col.name)
    code_name = has_code_name_hint(col.name)
    category_like = _is_category(col)

    # 3. Foreign keys chart as categories
    if col.foreign_key:
        col.role = "foreign_key"
        col.evidence.append(f"references {col.foreign_key['table']}")
        if category_like:
            col.secondary_roles.append("category")
        return

    # 4. Booleans
    if distinct is not None and distinct <= 2 and _is_boolean(col):
        col.role = "boolean"
        col.secondary_roles.append("category")
        col.evidence.append("two values" if distinct == 2 else "one value or null")
        return

    # 5. Identifiers: row identity, not worth charting
    if col.is_pk:
        col.role = "identifier"
        col.evidence.append("primary key")
        return
    if col.unique_declared:
        col.role = "identifier"
        col.evidence.append("unique index")
        return
    if col.unique and id_name:
        col.role = "identifier"
        col.evidence.append("unique values; name looks like an identifier")
        return
    if id_name and not category_like:
        col.role = "identifier"
        col.evidence.append("name looks like an identifier")
        return
    if col.name.lower() in ROW_IDENTITY_NAMES:
        col.role = "identifier"
        col.evidence.append("name is a row identifier")
        return

    # 6. Structured text
    if col.column_type in ("url", "email", "json"):
        col.role = col.column_type
        col.evidence.append(f"column type is {col.column_type}")
        return
    n_text = counts.get("text", 0)
    if is_text and n_text:
        for role in ("url", "json"):
            if col.tests.get(role, 0) / n_text >= STRUCTURED_TEXT_RATE:
                col.role = role
                col.evidence.append(f"values look like {role.upper()}")
                return

    # 7. Numbers
    if is_numeric:
        mostly_integer = counts.get("integer", 0) >= counts.get("real", 0)
        if id_name or code_name:
            # A number that is a label: a category if it repeats, never a measure
            if category_like:
                col.role = "category"
                col.evidence.append(
                    f"{_fmt_count(distinct)} distinct values; name suggests a label"
                )
            else:
                col.role = "identifier"
                col.evidence.append("name suggests a label, not a quantity")
            return
        if (
            mostly_integer
            and distinct is not None
            and distinct <= NUMERIC_CATEGORY_MAX_DISTINCT
            and category_like
        ):
            col.role = "category"
            col.secondary_roles.append("measure")
            col.evidence.append(f"{_fmt_count(distinct)} distinct integer values")
            return
        col.role = "measure"
        col.evidence.append("numeric values")
        if (
            distinct is not None
            and distinct <= NUMERIC_SECONDARY_CATEGORY_MAX_DISTINCT
            and category_like
        ):
            col.secondary_roles.append("category")
        return

    # 8. Text
    if is_text:
        if category_like:
            col.role = "category"
            col.evidence.append(
                f"{_plural(distinct, 'distinct value')} in "
                f"{_plural(non_null, 'row')}"
            )
        else:
            col.role = "text"
            if distinct is None:
                col.evidence.append("many distinct values")
            elif distinct > CATEGORY_MAX_DISTINCT:
                col.evidence.append(
                    f"{_fmt_count(distinct)} distinct values: too many for a category"
                )
            else:
                col.evidence.append("values rarely repeat")
        return

    col.evidence.append("mixed storage types")


def _is_category(col: ColumnProfile) -> bool:
    distinct = col.distinct_count
    if distinct is None or distinct < 1 or not col.non_null:
        return False
    if distinct > CATEGORY_MAX_DISTINCT:
        return False
    if distinct <= 2 and col.non_null > distinct:
        return True
    ratio = (
        CATEGORY_SMALL_SOURCE_RATIO
        if col.non_null < CATEGORY_SMALL_SOURCE_ROWS
        else CATEGORY_MAX_DISTINCT_RATIO
    )
    return distinct / col.non_null <= ratio


def _is_boolean(col: ColumnProfile) -> bool:
    values = [v for v, _ in (col.top_values or [])]
    if not values or len(values) > 2:
        return False
    if all(
        isinstance(v, int) and not isinstance(v, bool) and v in (0, 1) for v in values
    ):
        return True
    return (
        all(
            isinstance(v, str) and v.strip().lower() in BOOLEAN_TEXT_VALUES
            for v in values
        )
        and len(values) == 2
    )


# --- Pairs ------------------------------------------------------------------


def is_coordinate_name(name: str) -> bool:
    tokens = set(name_tokens(name))
    return bool(tokens & LATITUDE_NAME_TOKENS or tokens & LONGITUDE_NAME_TOKENS)


def _coordinate_candidates(profile: Profile, tokens: set, test: str) -> list:
    found = []
    for col in profile.columns:
        numeric = col.numeric or {}
        if numeric.get("min") is None or numeric["min"] == numeric.get("max"):
            continue
        if not tokens.intersection(name_tokens(col.name)):
            continue
        counts = col.type_counts
        n_numeric = counts.get("integer", 0) + counts.get("real", 0)
        if not col.non_null or n_numeric / col.non_null < TYPE_MAJORITY:
            continue
        # A few bad values should not hide a pair of coordinates
        if col.tests.get(test, 0) / n_numeric < COORDINATE_MIN_IN_RANGE:
            continue
        found.append(col)
    return found


def detect_pairs(profile: Profile) -> None:
    "Finds latitude/longitude pairs and gives both columns their roles"
    profile.pairs = []
    lats = _coordinate_candidates(profile, LATITUDE_NAME_TOKENS, "latitude")
    lons = _coordinate_candidates(profile, LONGITUDE_NAME_TOKENS, "longitude")
    used = set()
    for lat in lats:
        lat_rest = [t for t in name_tokens(lat.name) if t not in LATITUDE_NAME_TOKENS]
        best = None
        for lon in lons:
            if lon.name in used or lon.name == lat.name:
                continue
            lon_rest = [
                t for t in name_tokens(lon.name) if t not in LONGITUDE_NAME_TOKENS
            ]
            # The rest of the two names must match: start_lat with start_lon
            if lat_rest == lon_rest:
                best = lon
                break
        if best is None:
            continue
        used.add(best.name)
        for col, role in ((lat, "latitude"), (best, "longitude")):
            col.role = role
            col.secondary_roles = []
            col.temporal = None
            col.evidence = [f"paired with {best.name if col is lat else lat.name}"]
        profile.pairs.append(
            {"kind": "latlon", "latitude": lat.name, "longitude": best.name}
        )
