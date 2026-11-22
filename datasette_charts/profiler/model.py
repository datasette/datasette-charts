from __future__ import annotations

from dataclasses import asdict, dataclass, field

PROFILER_VERSION = 1

TEMPORAL_KINDS = ("iso_date", "iso_datetime", "unix_seconds", "unix_ms", "year")

ROLES = (
    "temporal",
    "measure",
    "category",
    "boolean",
    "identifier",
    "foreign_key",
    "latitude",
    "longitude",
    "text",
    "url",
    "email",
    "json",
    "blob",
    "constant",
    "unknown",
)


@dataclass
class TemporalInfo:
    kind: str  # one of TEMPORAL_KINDS
    match_rate: float  # share of present values that pass the value test
    min: str | None  # earliest value, as ISO 8601 text
    max: str | None  # latest value, as ISO 8601 text
    has_offset: bool  # any value carries "Z" or a UTC offset
    origin: str  # "detected" | "config" | "column_type"


@dataclass
class ColumnProfile:
    name: str
    position: int
    declared_type: str | None = None
    affinity: str | None = None
    # Counts by typeof(): integer, real, text, blob, null
    type_counts: dict = field(default_factory=dict)
    non_null: int = 0
    present: int = 0  # not null and not ''
    null_rate: float = 0.0
    # Distinct non-null values: {"count": n, "capped": bool}, or None if unknown
    distinct: dict | None = None
    unique: bool | None = None
    # True when uniqueness comes from the schema: a primary key or unique index
    unique_declared: bool = False
    # min, max, mean, stddev, and quantiles for measures
    numeric: dict | None = None
    # min_length, max_length, mean_length
    text: dict | None = None
    # [[value, count], ...] most common first, for low distinct counts
    top_values: list | None = None
    temporal: TemporalInfo | None = None
    # Matched on values but lacked the hint a numeric kind needs
    temporal_candidate: dict | None = None
    # Counts from the value tests, kept for debugging and evals
    tests: dict = field(default_factory=dict)
    column_type: str | None = None
    is_pk: bool = False
    # {"table":, "column":, "label_column":}
    foreign_key: dict | None = None
    role: str = "unknown"
    secondary_roles: list = field(default_factory=list)
    evidence: list = field(default_factory=list)

    def has_role(self, role: str) -> bool:
        return self.role == role or role in self.secondary_roles

    @property
    def distinct_count(self) -> int | None:
        "Distinct non-null values, or None when unknown or over the cap"
        if not self.distinct or self.distinct.get("capped"):
            return None
        return self.distinct["count"]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Profile:
    source: dict
    columns: list = field(default_factory=list)
    row_count: int | None = None
    row_count_exact: bool = False
    rows_profiled: int = 0
    basis: str = "full"  # "sample" | "full"
    # {"method": "rowid_probes" | "first_n", "rows": n} when basis is "sample"
    sample: dict | None = None
    primary_keys: list = field(default_factory=list)
    foreign_keys: list = field(default_factory=list)
    # Detected column pairs, e.g. {"kind": "latlon", "latitude":, "longitude":}
    pairs: list = field(default_factory=list)
    is_view: bool = False
    notes: list = field(default_factory=list)
    profiler_version: int = PROFILER_VERSION
    # Bookkeeping, left out of anything that has to be deterministic
    profiled_at: str | None = None
    duration_ms: int | None = None
    queries: int = 0

    def column(self, name: str) -> ColumnProfile | None:
        for column in self.columns:
            if column.name == name:
                return column
        return None

    @property
    def column_names(self) -> list:
        return [column.name for column in self.columns]

    def with_role(self, role: str, secondary: bool = True) -> list:
        return [
            column
            for column in self.columns
            if column.role == role or (secondary and role in column.secondary_roles)
        ]

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> dict:
        "The compact form sent with suggestions"
        return {
            "basis": self.basis,
            "status": "complete",
            "stale": False,
            "row_count": self.row_count,
            "row_count_exact": self.row_count_exact,
            "rows_profiled": self.rows_profiled,
            "sample": self.sample,
            "notes": list(self.notes),
        }
