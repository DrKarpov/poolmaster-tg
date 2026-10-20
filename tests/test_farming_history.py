import hashlib
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from farming import FARMING_POOLS, PoolMetrics, RateMetric, SourceStatus
from farming_history import FarmingHistoryStore


SEED_CSV = Path(__file__).parents[1] / "resources" / "farming_history_seed.csv"
SEED_SHA256 = "9b483b87198f4fdccec0fda372eefb292f18f4dd0aa96781806369078451175f"


def _metric(
    pool_index: int,
    value: str,
    fetched_at: datetime,
    *,
    kind: str = "APR",
    source_field: str = "apr_rewards",
    status: SourceStatus = SourceStatus.OK,
    identity_valid: bool = True,
    is_stale: bool = False,
    farm_rates: tuple[RateMetric, ...] | None = None,
) -> PoolMetrics:
    rate = RateMetric(Decimal(value), kind, source_field)
    return PoolMetrics(
        pool=FARMING_POOLS[pool_index],
        farm_rates=farm_rates if farm_rates is not None else (rate,),
        pool_rates=(),
        total_rate=rate,
        tvl_usd=Decimal("12345.67"),
        fetched_at=fetched_at,
        status=status,
        identity_valid=identity_valid,
        is_stale=is_stale,
    )


def _read_rows(path):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute("SELECT * FROM observations ORDER BY date_utc, series_key").fetchall()


def test_seed_import_is_idempotent_and_preserves_provenance(tmp_path):
    store = FarmingHistoryStore(tmp_path / "history.sqlite3")

    assert hashlib.sha256(SEED_CSV.read_bytes()).hexdigest() == SEED_SHA256
    assert store.seed_csv(SEED_CSV) == 140
    assert store.seed_csv(SEED_CSV) == 0

    rows = _read_rows(store.path)
    assert len(rows) == 140
    sample = next(
        row
        for row in rows
        if row["date_utc"] == "2026-07-24" and row["series_key"] == "dedust_jetton_ton"
    )
    assert sample["series_label"] == "DeDust JETTON/TON"
    assert sample["metric_kind"] == "basis not specified in historic POOLMASTER snapshot"
    assert sample["value_pct"] == "101.89"
    assert sample["cadence"] == "daily historic POOLMASTER snapshot"
    assert sample["source"] == "User-provided historic POOLMASTER snapshot; time and rate basis not supplied"
    assert sample["observed_at_utc"] is None
    assert sample["source_url"] is None
    assert sample["tvl_usd"] == "95376"
    assert sample["pool_url"] == FARMING_POOLS[0].url
    assert sample["source_note"] == "Updated user snapshot; earlier value was 101.76%."

    externally_sourced = next(
        row
        for row in rows
        if row["date_utc"] == "2026-05-25" and row["series_key"] == "dedust_jetton_ton"
    )
    assert externally_sourced["source_url"] == "https://yields.llama.fi/chart/d1ba7749-4a19-4ce1-836e-c950b9ce375c"
    assert externally_sourced["metric_field"] is None


def test_live_write_upserts_latest_pool_daily_row(tmp_path):
    store = FarmingHistoryStore(tmp_path / "history.sqlite3")
    first_time = datetime(2026, 10, 2, 21, 30, tzinfo=timezone.utc)
    second_time = datetime.fromisoformat("2026-10-03T01:00:00+03:00")

    assert store.record_confirmed([_metric(0, "12.3400", first_time)]) == 1
    assert store.record_confirmed([_metric(0, "13.5", second_time)]) == 1

    rows = _read_rows(store.path)
    assert len(rows) == 1
    assert rows[0]["date_utc"] == "2026-10-02"
    assert rows[0]["value_pct"] == "13.5"
    assert rows[0]["metric_kind"] == "APR"
    assert rows[0]["metric_field"] == "apr_rewards"
    assert rows[0]["observed_at_utc"] == "2026-10-02T22:00:00.000000Z"
    assert rows[0]["tvl_usd"] == "12345.67"
    assert store.record_confirmed([_metric(0, "11", first_time)]) == 0
    assert _read_rows(store.path)[0]["value_pct"] == "13.5"


def test_live_ston_farming_rows_link_to_the_rate_source_endpoint(tmp_path):
    store = FarmingHistoryStore(tmp_path / "history.sqlite3")
    observed_at = datetime(2026, 10, 2, 21, 30, tzinfo=timezone.utc)

    store.record_confirmed([_metric(2, "42.5", observed_at, kind="APY", source_field="apy")])

    row = _read_rows(store.path)[0]
    assert row["source_url"] == f"https://api.ston.fi/v1/farms/by_pool/{FARMING_POOLS[2].address}"


def test_seed_import_does_not_overwrite_a_live_daily_observation(tmp_path):
    store = FarmingHistoryStore(tmp_path / "history.sqlite3")
    store.record_confirmed(
        [_metric(0, "120.5", datetime(2026, 7, 24, 18, tzinfo=timezone.utc))]
    )

    assert store.seed_csv(SEED_CSV) == 139

    rows = _read_rows(store.path)
    sample = next(
        row
        for row in rows
        if row["date_utc"] == "2026-07-24" and row["series_key"] == "dedust_jetton_ton"
    )
    assert sample["value_pct"] == "120.5"
    assert sample["source"] == "DeDust API"


def test_record_confirmed_accepts_primary_yield_from_partial_source_only(tmp_path):
    store = FarmingHistoryStore(tmp_path / "history.sqlite3")
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    valid = _metric(0, "12", now)
    ambiguous = _metric(
        3,
        "25",
        now,
        farm_rates=(
            RateMetric(Decimal("25"), "APY", "first_farm"),
            RateMetric(Decimal("30"), "APY", "second_farm"),
        ),
    )
    non_finite = _metric(1, "12", now, farm_rates=(RateMetric(Decimal("NaN"), "APR", "apr_rewards"),))
    naive_timestamp = _metric(2, "12", datetime(2026, 10, 2, 12))

    recorded = store.record_confirmed(
        [
            valid,
            replace(_metric(1, "20", now), is_stale=True),
            replace(_metric(2, "21", now), identity_valid=False),
            replace(
                _metric(3, "22", now),
                status=SourceStatus.PARTIAL,
                pool_rates=(),
                total_rate=None,
                tvl_usd=None,
            ),
            ambiguous,
            non_finite,
            naive_timestamp,
        ]
    )

    assert recorded == 2
    rows = _read_rows(store.path)
    assert [row["series_key"] for row in rows] == ["dedust_jetton_ton", "ston_jetton_usdt"]


def test_weekly_rollup_uses_monday_sunday_utc_and_excludes_partial_week(tmp_path):
    store = FarmingHistoryStore(tmp_path / "history.sqlite3")
    store.record_confirmed(
        [
            # Local Monday is still Sunday UTC, so this belongs to week 2026-09-28.
            _metric(0, "10", datetime.fromisoformat("2026-10-05T02:30:00+03:00")),
            _metric(0, "20", datetime(2026, 10, 5, 0, 30, tzinfo=timezone.utc)),
        ]
    )

    summary = store.get_summary(as_of=datetime(2026, 10, 6, 12, tzinfo=timezone.utc))

    points = summary.series[0].points
    assert [(point.week_start.isoformat(), point.value_pct) for point in points] == [
        ("2026-09-28", Decimal("10"))
    ]
    assert points[0].observation_count == 1
    assert summary.period_start.isoformat() == "2026-09-28"
    assert summary.period_end.isoformat() == "2026-10-04"


def test_weekly_history_rolls_over_at_monday_utc(tmp_path):
    store = FarmingHistoryStore(tmp_path / "history.sqlite3")
    store.record_confirmed(
        [_metric(0, "10", datetime(2026, 10, 4, 23, 59, tzinfo=timezone.utc))]
    )

    before_rollover = store.get_summary(as_of=datetime(2026, 10, 4, 23, 59, tzinfo=timezone.utc))
    after_rollover = store.get_summary(as_of=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc))

    assert before_rollover.series[0].points == ()
    assert [(point.week_start.isoformat(), point.value_pct) for point in after_rollover.series[0].points] == [
        ("2026-09-28", Decimal("10"))
    ]


def test_history_summary_uses_public_gram_labels_without_changing_pool_identity(tmp_path):
    store = FarmingHistoryStore(tmp_path / "history.sqlite3")
    store.record_confirmed(
        [
            _metric(index, "10", datetime(2026, 10, 12, 12, tzinfo=timezone.utc))
            for index in range(len(FARMING_POOLS))
        ]
    )

    summary = store.get_summary(as_of=datetime(2026, 10, 20, 12, tzinfo=timezone.utc))

    assert [series.label for series in summary.series] == [
        "DeDust JETTON/GRAM",
        "DeDust JETTON/USDT",
        "STON.fi JETTON/GRAM",
        "STON.fi JETTON/USDT",
    ]
    assert [pool.name for pool in FARMING_POOLS if pool.name == "JETTON/TON"] == ["JETTON/TON", "JETTON/TON"]


def test_weekly_points_retain_contributing_observation_provenance(tmp_path):
    store = FarmingHistoryStore(tmp_path / "history.sqlite3")
    store.record_confirmed(
        [
            _metric(0, "10", datetime(2026, 10, 5, 1, tzinfo=timezone.utc)),
            _metric(0, "20", datetime(2026, 10, 6, 1, tzinfo=timezone.utc)),
        ]
    )

    point = store.get_summary(as_of=datetime(2026, 10, 13, 1, tzinfo=timezone.utc)).series[0].points[0]

    assert point.observation_count == 2
    assert [
        (
            observation.observed_date.isoformat(),
            observation.value_pct,
            observation.metric_kind,
            observation.source_method,
            observation.source_url,
        )
        for observation in point.observations
    ] == [
        (
            "2026-10-05",
            Decimal("10"),
            "APR",
            "DeDust API",
            f"https://mainnet.api.dedust.io/v4/api/pools/{FARMING_POOLS[0].address}",
        ),
        (
            "2026-10-06",
            Decimal("20"),
            "APR",
            "DeDust API",
            f"https://mainnet.api.dedust.io/v4/api/pools/{FARMING_POOLS[0].address}",
        ),
    ]


def test_missing_week_stays_missing_and_pool_means_weight_weeks_equally(tmp_path):
    store = FarmingHistoryStore(tmp_path / "history.sqlite3")
    store.record_confirmed(
        [
            _metric(0, "10", datetime(2026, 10, 5, 1, tzinfo=timezone.utc)),
            _metric(0, "20", datetime(2026, 10, 6, 1, tzinfo=timezone.utc)),
            _metric(0, "30", datetime(2026, 10, 19, 1, tzinfo=timezone.utc)),
            _metric(1, "40", datetime(2026, 10, 5, 1, tzinfo=timezone.utc), kind="APY", source_field="active_farm"),
            _metric(2, "50", datetime(2026, 10, 5, 1, tzinfo=timezone.utc)),
            _metric(3, "70", datetime(2026, 10, 5, 1, tzinfo=timezone.utc)),
            _metric(3, "90", datetime(2026, 10, 19, 1, tzinfo=timezone.utc)),
        ]
    )

    summary = store.get_summary(as_of=datetime(2026, 10, 27, 12, tzinfo=timezone.utc))

    first_pool = summary.series[0]
    assert [point.week_start.isoformat() for point in first_pool.points] == ["2026-10-05", "2026-10-19"]
    assert [point.value_pct for point in first_pool.points] == [Decimal("15"), Decimal("30")]
    assert first_pool.points[0].observation_count == 2
    assert first_pool.mean_pct == Decimal("22.5")
    assert first_pool.observed_weeks == 2
    assert summary.series[1].points[0].metric_kinds == ("APY",)


def test_overall_mean_is_mean_of_four_displayed_pool_means(tmp_path):
    store = FarmingHistoryStore(tmp_path / "history.sqlite3")
    store.record_confirmed(
        [
            _metric(0, "10", datetime(2026, 10, 5, 1, tzinfo=timezone.utc)),
            _metric(0, "30", datetime(2026, 10, 12, 1, tzinfo=timezone.utc)),
            _metric(1, "25", datetime(2026, 10, 5, 1, tzinfo=timezone.utc)),
            _metric(2, "40", datetime(2026, 10, 5, 1, tzinfo=timezone.utc)),
            _metric(3, "55", datetime(2026, 10, 5, 1, tzinfo=timezone.utc)),
        ]
    )

    summary = store.get_summary(as_of=datetime(2026, 10, 20, 12, tzinfo=timezone.utc))

    assert [series.mean_pct for series in summary.series] == [
        Decimal("20"),
        Decimal("25"),
        Decimal("40"),
        Decimal("55"),
    ]
    assert summary.overall_mean_pct == Decimal("35")
