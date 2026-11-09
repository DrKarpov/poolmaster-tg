"""Farming widget view using the legacy weekly aggregation and accepted snapshots."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from farming import FARMING_POOLS, is_primary_farming_usable
from farming_history import SERIES_KEY_ORDER

SUPPORTED_PERIODS = ('7D',)
RESERVED_PERIODS = ('3D', '1D')


@dataclass(frozen=True)
class FarmingWidgetPoint:
    start: date
    end: date
    values: tuple[Decimal | None, ...]


@dataclass(frozen=True)
class FarmingWidgetView:
    period: str
    points: tuple[FarmingWidgetPoint, ...]
    current_mean_pct: Decimal | None
    total_tvl_usd: Decimal | None
    observed_at: datetime
    status: str


async def load_weekly_view(history, snapshots, *, force=False, as_of=None):
    snapshot = await (snapshots.refresh_farming() if force else snapshots.get_snapshot())
    now = as_of or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('aware timestamp required')
    summary = history.get_summary(as_of=now)
    series = {s.series_key: {p.week_start:p.value_pct for p in s.points} for s in summary.series}
    weeks = sorted({w for s in series.values() for w in s})
    points = tuple(FarmingWidgetPoint(w,w+timedelta(days=6),
                   tuple(series.get(k,{}).get(w) for k in SERIES_KEY_ORDER)) for w in weeks)
    rates, tvls, stamps = [], [], []
    for pool in FARMING_POOLS:
        metric = snapshot.farming.get(pool.address)
        if metric is None:
            continue
        age = (now-metric.fetched_at).total_seconds()
        if not (0 <= age <= snapshots.farming_stale_max_age_seconds):
            continue
        if not is_primary_farming_usable(metric, anomaly='farming:'+pool.address in snapshot.anomalies):
            continue
        rates.append(metric.primary_farm_yield.value_percent)
        stamps.append(metric.fetched_at)
        tvl = metric.tvl_usd
        if isinstance(tvl,Decimal) and tvl.is_finite() and tvl >= 0:
            tvls.append(tvl)
    mean = sum(rates)/4 if len(rates)==4 else None
    total = sum(tvls) if len(tvls)==4 else None
    status = 'ok' if mean is not None and total is not None and snapshots.last_history_write_error is None else 'unavailable'
    return FarmingWidgetView('7D',points,mean,total,
                             min(stamps) if stamps else datetime.fromtimestamp(0,timezone.utc),status)
