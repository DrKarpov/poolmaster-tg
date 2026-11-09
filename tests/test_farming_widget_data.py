import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from farming import FARMING_POOLS
from farming_history import FarmingHistoryStore
from farming_widget_data import load_weekly_view
from test_farming_history import _metric, SEED_CSV

NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


class Snapshots:
    def __init__(self):
        self.metrics = [_metric(i, str((i+1)*10), NOW) for i in range(4)]
        self.metrics = [replace(m, tvl_usd=Decimal((i+1)*100)) for i,m in enumerate(self.metrics)]
        self.refreshed = 0
        self.last_history_write_error = None
        self.farming_stale_max_age_seconds = 1800
    async def get_snapshot(self):
        return SimpleNamespace(farming={m.pool_id:m for m in self.metrics}, anomalies={})
    async def refresh_farming(self):
        self.refreshed += 1
        self.metrics = [replace(m, farm_rates=(replace(m.primary_farm_yield, value_percent=Decimal(50)),),
                                tvl_usd=Decimal(500)) for m in self.metrics]
        return await self.get_snapshot()


@pytest.fixture
def sources(tmp_path):
    history = FarmingHistoryStore(tmp_path/'history.sqlite3')
    history.seed_csv(SEED_CSV)
    for stamp in (datetime(2026,10,19,tzinfo=timezone.utc),datetime(2026,10,26,tzinfo=timezone.utc),NOW):
        history.record_confirmed([_metric(i,"70",stamp) for i in range(4)])
    return history, Snapshots()


def test_weekly_view_matches_legacy_summary(sources):
    history,snapshots = sources
    view = asyncio.run(load_weekly_view(history,snapshots,as_of=NOW))
    summary = history.get_summary(as_of=NOW)
    assert len(view.points) == 23
    for i,series in enumerate(summary.series):
        assert [(p.start,p.values[i]) for p in view.points if p.values[i] is not None] == [(p.week_start,p.value_pct) for p in series.points]
    assert view.points[0].start.isoformat() == '2026-05-25'
    assert view.points[-1].end.isoformat() == '2026-11-01'


def test_current_mean_uses_latest_four_rates_not_history_mean(sources):
    history,snapshots = sources
    view = asyncio.run(load_weekly_view(history,snapshots,as_of=NOW))
    assert view.current_mean_pct == Decimal(25)
    assert view.total_tvl_usd == Decimal(1000)
    assert view.status == 'ok' and view.observed_at == NOW


def test_force_refresh_changes_kpis_without_inventing_weekly_points(sources):
    history,snapshots = sources
    before = asyncio.run(load_weekly_view(history,snapshots,as_of=NOW))
    after = asyncio.run(load_weekly_view(history,snapshots,force=True,as_of=NOW))
    assert before.points == after.points
    assert after.current_mean_pct == 50 and after.total_tvl_usd == 2000


@pytest.mark.parametrize('change', [dict(is_stale=True),dict(identity_valid=False),dict(tvl_usd=None),
                                   dict(fetched_at=datetime(2026,11,1,tzinfo=timezone.utc))])
def test_incomplete_snapshot_does_not_claim_full_totals(sources,change):
    history,snapshots = sources
    snapshots.metrics[0] = replace(snapshots.metrics[0],**change)
    view = asyncio.run(load_weekly_view(history,snapshots,as_of=NOW))
    assert view.total_tvl_usd is None and view.status == 'unavailable'
    if 'tvl_usd' not in change:
        assert view.current_mean_pct is None
