from __future__ import annotations

import csv
import logging
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator, Sequence

from display_names import display_pair_name
from farming import (
    DEDUST_BASE_URL,
    FARMING_POOLS,
    STON_BASE_URL,
    PoolMetrics,
    is_primary_farming_usable,
)


SERIES_KEY_ORDER = (
    "dedust_jetton_ton",
    "dedust_jetton_usdt",
    "ston_jetton_ton",
    "ston_jetton_usdt",
)
SERIES_KEY_BY_POOL_ADDRESS = {
    pool.address: series_key
    for pool, series_key in zip(FARMING_POOLS, SERIES_KEY_ORDER, strict=True)
}
POOL_BY_SERIES_KEY = {
    series_key: pool for pool, series_key in zip(FARMING_POOLS, SERIES_KEY_ORDER, strict=True)
}

_SEED_FIELDS = {
    "date_utc",
    "series_key",
    "series_label",
    "metric",
    "value_pct",
    "cadence",
    "observed_at_utc",
    "source",
    "source_url",
    "tvl_usd",
    "pool_url",
    "note",
}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    date_utc TEXT NOT NULL,
    series_key TEXT NOT NULL,
    series_label TEXT NOT NULL,
    metric_kind TEXT NOT NULL,
    metric_field TEXT,
    value_pct TEXT NOT NULL,
    cadence TEXT NOT NULL,
    observed_at_utc TEXT,
    source TEXT NOT NULL,
    source_url TEXT,
    pool_url TEXT NOT NULL,
    tvl_usd TEXT,
    source_note TEXT,
    PRIMARY KEY (date_utc, series_key)
)
"""
_UPSERT_SQL = """
INSERT INTO observations (
    date_utc, series_key, series_label, metric_kind, metric_field, value_pct,
    cadence, observed_at_utc, source, source_url, pool_url, tvl_usd, source_note
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(date_utc, series_key) DO UPDATE SET
    series_label = excluded.series_label,
    metric_kind = excluded.metric_kind,
    metric_field = excluded.metric_field,
    value_pct = excluded.value_pct,
    cadence = excluded.cadence,
    observed_at_utc = excluded.observed_at_utc,
    source = excluded.source,
    source_url = excluded.source_url,
    pool_url = excluded.pool_url,
    tvl_usd = excluded.tvl_usd,
    source_note = excluded.source_note
WHERE observations.observed_at_utc IS NULL
   OR excluded.observed_at_utc >= observations.observed_at_utc
"""


@dataclass(frozen=True)
class HistoryObservation:
    observed_date: date
    value_pct: Decimal
    metric_kind: str
    source_method: str
    source_url: str | None
    observed_at_utc: str | None
    cadence: str
    source_note: str | None


@dataclass(frozen=True)
class WeeklyPoint:
    week_start: date
    value_pct: Decimal
    observation_count: int
    metric_kinds: tuple[str, ...]
    observations: tuple[HistoryObservation, ...] = ()


@dataclass(frozen=True)
class WeeklyPoolSeries:
    series_key: str
    label: str
    points: tuple[WeeklyPoint, ...]
    mean_pct: Decimal | None
    observed_weeks: int


@dataclass(frozen=True)
class FarmingHistorySummary:
    series: tuple[WeeklyPoolSeries, ...]
    period_start: date | None
    period_end: date | None
    overall_mean_pct: Decimal | None


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observation timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _timestamp_text(value: datetime) -> str:
    return _utc_datetime(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _seed_timestamp_text(value: str) -> str | None:
    if not value.strip():
        return None
    parsed_text = value.strip()
    if parsed_text.endswith("Z"):
        parsed_text = parsed_text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(parsed_text)
    except ValueError as exc:
        raise ValueError(f"invalid seed observation timestamp: {value!r}") from exc
    return _timestamp_text(parsed)


def _seed_decimal(value: str, *, field: str, nullable: bool = False) -> str | None:
    if nullable and not value.strip():
        return None
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"invalid decimal in seed field {field!r}: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"non-finite decimal in seed field {field!r}: {value!r}")
    return value.strip()


class FarmingHistoryStore:
    """Persist one provenance-rich farming observation per pool and UTC day."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute(_SCHEMA)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10.0)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def seed_csv(self, path: Path) -> int:
        """Import curated source rows once without replacing existing observations."""
        rows: list[tuple[object, ...]] = []
        with Path(path).open("r", newline="", encoding="utf-8") as source_file:
            reader = csv.DictReader(source_file)
            if reader.fieldnames is None or not _SEED_FIELDS.issubset(reader.fieldnames):
                missing = sorted(_SEED_FIELDS - set(reader.fieldnames or ()))
                raise ValueError(f"farming history seed is missing columns: {', '.join(missing)}")

            for line_number, row in enumerate(reader, start=2):
                if row["series_key"] not in POOL_BY_SERIES_KEY:
                    raise ValueError(f"unknown series key on seed line {line_number}: {row['series_key']!r}")
                pool = POOL_BY_SERIES_KEY[row["series_key"]]
                if row["series_label"] != f"{pool.dex} {pool.name}" or row["pool_url"] != pool.url:
                    raise ValueError(f"pool label or URL mismatch on seed line {line_number}")
                try:
                    parsed_date = date.fromisoformat(row["date_utc"])
                except ValueError as exc:
                    raise ValueError(f"invalid date on seed line {line_number}: {row['date_utc']!r}") from exc
                value_pct = _seed_decimal(row["value_pct"], field="value_pct")
                tvl_usd = _seed_decimal(row["tvl_usd"], field="tvl_usd", nullable=True)
                rows.append(
                    (
                        parsed_date.isoformat(),
                        row["series_key"],
                        row["series_label"],
                        row["metric"],
                        None,
                        value_pct,
                        row["cadence"],
                        _seed_timestamp_text(row["observed_at_utc"]),
                        row["source"],
                        row["source_url"].strip() or None,
                        row["pool_url"],
                        tvl_usd,
                        row["note"].strip() or None,
                    )
                )

        with self._connection() as connection:
            before = connection.total_changes
            connection.executemany(
                """
                INSERT OR IGNORE INTO observations (
                    date_utc, series_key, series_label, metric_kind, metric_field, value_pct,
                    cadence, observed_at_utc, source, source_url, pool_url, tvl_usd, source_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            return connection.total_changes - before

    def record_confirmed(self, metrics: Sequence[PoolMetrics]) -> int:
        """Upsert eligible fresh pool values, rejecting fallback or ambiguous data."""
        records: list[tuple[object, ...]] = []
        for metrics_item in metrics:
            if (
                not is_primary_farming_usable(metrics_item)
            ):
                continue
            series_key = SERIES_KEY_BY_POOL_ADDRESS.get(metrics_item.pool.address)
            rate = metrics_item.primary_farm_yield
            if series_key is None or rate is None or not isinstance(rate.value_percent, Decimal):
                continue
            if not rate.value_percent.is_finite():
                continue
            try:
                observed_at = _utc_datetime(metrics_item.fetched_at)
            except (AttributeError, TypeError, ValueError):
                continue

            pool = POOL_BY_SERIES_KEY[series_key]
            if metrics_item.pool.url != pool.url:
                continue
            source_url = (
                f"{DEDUST_BASE_URL}/{pool.address}"
                if pool.dex == "DeDust"
                else f"{STON_BASE_URL}/farms/by_pool/{pool.address}"
            )
            records.append(
                (
                    observed_at.date().isoformat(),
                    series_key,
                    f"{pool.dex} {pool.name}",
                    rate.kind,
                    rate.source_field,
                    _decimal_text(rate.value_percent),
                    "daily confirmed farming refresh",
                    _timestamp_text(observed_at),
                    f"{pool.dex} API",
                    source_url,
                    pool.url,
                    _decimal_text(metrics_item.tvl_usd) if metrics_item.tvl_usd is not None else None,
                    None,
                )
            )

        if not records:
            return 0
        try:
            with self._connection() as connection:
                cursor = connection.executemany(_UPSERT_SQL, records)
                return cursor.rowcount
        except sqlite3.Error:
            logging.exception("Failed to persist confirmed farming history")
            raise

    def get_summary(self, as_of: datetime | None = None) -> FarmingHistorySummary:
        """Return completed UTC Monday–Sunday week means, preserving missing weeks."""
        reference = _utc_datetime(as_of or datetime.now(timezone.utc))
        current_week_start = reference.date() - timedelta(days=reference.date().weekday())
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT date_utc, series_key, metric_kind, value_pct, source, source_url,
                       observed_at_utc, cadence, source_note
                FROM observations
                WHERE date_utc < ?
                ORDER BY date_utc, series_key
                """,
                (current_week_start.isoformat(),),
            ).fetchall()

        grouped: dict[tuple[str, date], list[HistoryObservation]] = defaultdict(list)
        for (
            date_text,
            series_key,
            metric_kind,
            value_text,
            source,
            source_url,
            observed_at_utc,
            cadence,
            source_note,
        ) in rows:
            observed_date = date.fromisoformat(date_text)
            week_start = observed_date - timedelta(days=observed_date.weekday())
            grouped[(series_key, week_start)].append(
                HistoryObservation(
                    observed_date=observed_date,
                    value_pct=Decimal(value_text),
                    metric_kind=metric_kind,
                    source_method=source,
                    source_url=source_url,
                    observed_at_utc=observed_at_utc,
                    cadence=cadence,
                    source_note=source_note,
                )
            )

        series_result: list[WeeklyPoolSeries] = []
        all_week_starts: list[date] = []
        for pool in FARMING_POOLS:
            series_key = SERIES_KEY_BY_POOL_ADDRESS[pool.address]
            week_starts = sorted(week for key, week in grouped if key == series_key)
            points: list[WeeklyPoint] = []
            for week_start in week_starts:
                observations = grouped[(series_key, week_start)]
                value = sum((item.value_pct for item in observations), Decimal(0)) / Decimal(len(observations))
                kinds = tuple(sorted({item.metric_kind for item in observations}))
                points.append(
                    WeeklyPoint(
                        week_start=week_start,
                        value_pct=value,
                        observation_count=len(observations),
                        metric_kinds=kinds,
                        observations=tuple(observations),
                    )
                )
            mean = (
                sum((point.value_pct for point in points), Decimal(0)) / Decimal(len(points))
                if points
                else None
            )
            series_result.append(
                WeeklyPoolSeries(
                    series_key=series_key,
                    label=f"{pool.dex} {display_pair_name(pool)}",
                    points=tuple(points),
                    mean_pct=mean,
                    observed_weeks=len(points),
                )
            )
            all_week_starts.extend(point.week_start for point in points)

        nonempty_means = [series.mean_pct for series in series_result if series.mean_pct is not None]
        overall_mean = (
            sum(nonempty_means, Decimal(0)) / Decimal(4)
            if len(nonempty_means) == len(FARMING_POOLS)
            else None
        )
        return FarmingHistorySummary(
            series=tuple(series_result),
            period_start=min(all_week_starts) if all_week_starts else None,
            period_end=max(all_week_starts) + timedelta(days=6) if all_week_starts else None,
            overall_mean_pct=overall_mean,
        )
