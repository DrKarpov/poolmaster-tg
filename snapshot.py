from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Awaitable, Callable

from farming import (
    FARMING_POOLS,
    FarmingPool,
    PoolMetrics,
    RateMetric,
    SourceStatus,
    is_primary_farming_usable,
    unavailable_metrics,
)
from farming_cache import FarmingStaleCache
from farming_history import FarmingHistoryStore, POOL_BY_SERIES_KEY, SERIES_KEY_BY_POOL_ADDRESS
from price import PriceMetrics, fetch_price_metrics
from state_backup import secure_backup_file


FarmingFetcher = Callable[[], Awaitable[list[PoolMetrics]]]
PriceFetcher = Callable[[], Awaitable[PriceMetrics]]
PriceIdentityVerifier = Callable[[], Awaitable[bool | None]]


@dataclass(frozen=True)
class DataSnapshot:
    fetched_at: datetime
    farming: dict[str, PoolMetrics]
    price: PriceMetrics
    source_statuses: dict[str, str]
    anomalies: dict[str, str]


class SnapshotManager:
    def __init__(
        self,
        fetch_farming: FarmingFetcher,
        fetch_price: PriceFetcher = fetch_price_metrics,
        *,
        snapshot_path: Path | None = None,
        farming_cache: FarmingStaleCache | None = None,
        farming_history_store: FarmingHistoryStore | None = None,
        farming_stale_max_age_seconds: int = 1800,
        price_stale_max_age_seconds: int = 900,
        freshness_seconds: int = 60,
        farming_freshness_seconds: int | None = None,
        price_freshness_seconds: int | None = None,
        apr_anomaly_confirm_threshold: Decimal | float | None = None,
        price_anomaly_confirm_threshold: Decimal | float | None = None,
        anomaly_confirm_delay_seconds: float = 2,
        confirmation_tolerance_percent: Decimal | float = Decimal("1"),
        price_identity_valid: bool | None = True,
        verify_price_identity: PriceIdentityVerifier | None = None,
    ) -> None:
        self.fetch_farming = fetch_farming
        self.fetch_price = fetch_price
        self.snapshot_path = Path(snapshot_path) if snapshot_path is not None else None
        self.farming_cache = farming_cache or FarmingStaleCache(farming_stale_max_age_seconds)
        self.farming_history_store = farming_history_store
        self.last_history_write_error: Exception | None = None
        self._pending_farming_history_metrics: tuple[PoolMetrics, ...] | None = None
        self.farming_stale_max_age_seconds = farming_stale_max_age_seconds
        self.price_stale_max_age_seconds = price_stale_max_age_seconds
        self.freshness_seconds = freshness_seconds
        self.farming_freshness_seconds = farming_freshness_seconds or freshness_seconds
        self.price_freshness_seconds = price_freshness_seconds or freshness_seconds
        self.apr_anomaly_confirm_threshold = self._as_decimal(apr_anomaly_confirm_threshold)
        self.price_anomaly_confirm_threshold = self._as_decimal(price_anomaly_confirm_threshold)
        self.anomaly_confirm_delay_seconds = anomaly_confirm_delay_seconds
        self.confirmation_tolerance_percent = self._as_decimal(confirmation_tolerance_percent) or Decimal(1)
        self.price_identity_valid = price_identity_valid
        self.verify_price_identity = verify_price_identity
        self.current_snapshot, self.previous_snapshot = self._load()
        self._last_farming_refresh_at: datetime | None = None
        self._last_price_refresh_at: datetime | None = None
        self._refresh_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[DataSnapshot] | None = None
        self._refresh_task_sources: tuple[bool, bool] | None = None
        self._refresh_required = False

    def retry_pending_farming_history_write(self) -> bool:
        """Retry persistence of the most recent confirmed metrics without another API fetch."""
        if self.farming_history_store is None:
            self.last_history_write_error = None
            self._pending_farming_history_metrics = None
            return True
        if self._pending_farming_history_metrics is None:
            return self.last_history_write_error is None
        try:
            self.farming_history_store.record_confirmed(self._pending_farming_history_metrics)
        except Exception as exc:
            self.last_history_write_error = exc
            logging.exception("Retrying confirmed farming history persistence failed")
            return False
        self.last_history_write_error = None
        self._pending_farming_history_metrics = None
        return True

    def _record_confirmed_farming_history(self, metrics: list[PoolMetrics]) -> None:
        if self.farming_history_store is None:
            self.last_history_write_error = None
            self._pending_farming_history_metrics = None
            return
        metrics_to_record = self._merge_confirmed_history_metrics(
            self._pending_farming_history_metrics or (),
            metrics,
        )
        try:
            self.farming_history_store.record_confirmed(metrics_to_record)
        except Exception as exc:
            self.last_history_write_error = exc
            self._pending_farming_history_metrics = metrics_to_record
            logging.exception("Farming history write failed; keeping the live snapshot refresh")
        else:
            self.last_history_write_error = None
            self._pending_farming_history_metrics = None

    @staticmethod
    def _merge_confirmed_history_metrics(*groups: tuple[PoolMetrics, ...] | list[PoolMetrics]) -> tuple[PoolMetrics, ...]:
        latest: dict[tuple[str, str], tuple[datetime, PoolMetrics]] = {}
        for metrics in groups:
            for item in metrics:
                series_key = SERIES_KEY_BY_POOL_ADDRESS.get(item.pool.address)
                pool = POOL_BY_SERIES_KEY.get(series_key) if series_key is not None else None
                rate = item.primary_farm_yield
                if (
                    not is_primary_farming_usable(item)
                    or pool is None
                    or item.pool.url != pool.url
                    or rate is None
                    or not isinstance(rate.value_percent, Decimal)
                    or not rate.value_percent.is_finite()
                    or item.fetched_at.tzinfo is None
                    or item.fetched_at.utcoffset() is None
                ):
                    continue
                observed_at = item.fetched_at.astimezone(timezone.utc)
                key = (series_key, observed_at.date().isoformat())
                previous = latest.get(key)
                if previous is None or observed_at >= previous[0]:
                    latest[key] = (observed_at, item)
        return tuple(item for _observed_at, item in latest.values())

    @staticmethod
    def _as_decimal(value: Decimal | float | None) -> Decimal | None:
        if value is None:
            return None
        parsed = Decimal(str(value))
        if not parsed.is_finite() or parsed < 0:
            raise ValueError("threshold must be finite and non-negative")
        return parsed

    async def get_snapshot(
        self,
        *,
        now: datetime | None = None,
    ) -> DataSnapshot:
        timestamp = now or datetime.now(timezone.utc)
        if self.current_snapshot is None or self._refresh_required:
            return await self._request_refresh(timestamp, refresh_farming=True, refresh_price=True)
        refresh_farming = self._source_due(
            timestamp, self._last_farming_refresh_at, self.farming_freshness_seconds
        )
        refresh_price = self._source_due(
            timestamp, self._last_price_refresh_at, self.price_freshness_seconds
        )
        if not refresh_farming and not refresh_price:
            return self.current_snapshot
        return await self._request_refresh(
            timestamp,
            refresh_farming=refresh_farming,
            refresh_price=refresh_price,
        )

    @staticmethod
    def _source_due(now: datetime, last_refresh_at: datetime | None, interval: int) -> bool:
        if last_refresh_at is None or now.tzinfo is None or last_refresh_at.tzinfo is None:
            return True
        elapsed = (now.astimezone(timezone.utc) - last_refresh_at.astimezone(timezone.utc)).total_seconds()
        return elapsed < 0 or elapsed >= interval

    async def refresh(self, *, now: datetime | None = None) -> DataSnapshot:
        timestamp = now or datetime.now(timezone.utc)
        return await self._request_refresh(timestamp, refresh_farming=True, refresh_price=True)

    async def refresh_farming(self, *, now: datetime | None = None) -> DataSnapshot:
        timestamp = now or datetime.now(timezone.utc)
        if self.current_snapshot is None or self._refresh_required:
            return await self.get_snapshot(now=timestamp)
        return await self._request_refresh(timestamp, refresh_farming=True, refresh_price=False)

    async def refresh_price(self, *, now: datetime | None = None) -> DataSnapshot:
        timestamp = now or datetime.now(timezone.utc)
        if self.current_snapshot is None or self._refresh_required:
            return await self.get_snapshot(now=timestamp)
        return await self._request_refresh(timestamp, refresh_farming=False, refresh_price=True)

    async def _request_refresh(
        self,
        timestamp: datetime,
        *,
        refresh_farming: bool,
        refresh_price: bool,
    ) -> DataSnapshot:
        async with self._refresh_lock:
            if self._refresh_task is None:
                self._refresh_task_sources = (refresh_farming, refresh_price)
                self._refresh_task = asyncio.create_task(
                    self._refresh_once(timestamp, refresh_farming=refresh_farming, refresh_price=refresh_price)
                )
            task = self._refresh_task
            active_farming, active_price = self._refresh_task_sources or (False, False)
            missing_farming = refresh_farming and not active_farming
            missing_price = refresh_price and not active_price
        try:
            result = await asyncio.shield(task)
        finally:
            if task.done():
                async with self._refresh_lock:
                    if self._refresh_task is task:
                        self._refresh_task = None
                        self._refresh_task_sources = None
        if missing_farming or missing_price:
            return await self._request_refresh(
                timestamp,
                refresh_farming=missing_farming,
                refresh_price=missing_price,
            )
        return result

    async def _fetch_farming(self) -> list[PoolMetrics]:
        try:
            return list(await self.fetch_farming())
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Farming source refresh failed")
            return [
                unavailable_metrics(pool, SourceStatus.UNAVAILABLE, "farming source request failed")
                for pool in FARMING_POOLS
            ]

    async def _fetch_price(self) -> PriceMetrics:
        if self.price_identity_valid is None and self.verify_price_identity is not None:
            try:
                self.price_identity_valid = await self.verify_price_identity()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Price market identity recheck failed")
        if self.price_identity_valid is not True:
            status = SourceStatus.INVALID_RESPONSE if self.price_identity_valid is False else SourceStatus.UNAVAILABLE
            detail = "MEXC market identity mismatch" if self.price_identity_valid is False else "MEXC market identity unverified"
            return PriceMetrics(
                price_usdt=None,
                bid_usdt=None,
                ask_usdt=None,
                quote_volume_24h=None,
                symbol="JETTONUSDT",
                quote_currency="USDT",
                source="MEXC spot",
                fetched_at=datetime.now(timezone.utc),
                source_timestamp=None,
                status=status,
                status_detail=detail,
            )
        try:
            return await self.fetch_price()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Price source refresh failed")
            return PriceMetrics(
                price_usdt=None,
                bid_usdt=None,
                ask_usdt=None,
                quote_volume_24h=None,
                symbol="JETTONUSDT",
                quote_currency="USDT",
                source="MEXC spot",
                fetched_at=datetime.now(timezone.utc),
                source_timestamp=None,
                status=SourceStatus.UNAVAILABLE,
                status_detail="price source request failed",
            )

    async def _refresh_once(
        self,
        now: datetime,
        *,
        refresh_farming: bool,
        refresh_price: bool,
    ) -> DataSnapshot:
        prior = self.current_snapshot
        if refresh_farming and refresh_price:
            farming_results, price_result = await asyncio.gather(self._fetch_farming(), self._fetch_price())
        elif refresh_farming:
            farming_results = await self._fetch_farming()
            price_result = prior.price
        elif refresh_price:
            farming_results = list(prior.farming.values()) if prior else []
            price_result = await self._fetch_price()
        else:
            return prior

        anomalies: dict[str, str] = {}
        if prior is not None:
            if not refresh_farming:
                anomalies.update(
                    {key: value for key, value in prior.anomalies.items() if key.startswith("farming:")}
                )
            if not refresh_price and "price" in prior.anomalies:
                anomalies["price"] = prior.anomalies["price"]

        if refresh_farming:
            farming_results = await self._confirm_farming_anomalies(farming_results, prior, anomalies)
            self._record_confirmed_farming_history(farming_results)
            farming_results = self.farming_cache.apply(farming_results, now=now)
        if refresh_price:
            price_result = await self._confirm_price_anomaly(price_result, prior, anomalies)
        if refresh_price:
            price_result = self._apply_price_stale_fallback(price_result, prior, now)

        farming_by_id = {metrics.pool.address: metrics for metrics in farming_results}

        source_statuses: dict[str, str] = {
            "farming": self._aggregate_farming_status(farming_results).value,
            "price": price_result.status.value,
        }
        source_statuses.update(
            {f"farming:{pool_id}": metrics.status.value for pool_id, metrics in farming_by_id.items()}
        )
        snapshot = DataSnapshot(
            fetched_at=now,
            farming=farming_by_id,
            price=price_result,
            source_statuses=source_statuses,
            anomalies=anomalies,
        )
        self.previous_snapshot = prior
        self.current_snapshot = snapshot
        self._refresh_required = False
        if refresh_farming:
            self._last_farming_refresh_at = now
        if refresh_price:
            self._last_price_refresh_at = now
        self._save()
        return snapshot

    async def _confirm_price_anomaly(
        self,
        candidate: PriceMetrics,
        prior: DataSnapshot | None,
        anomalies: dict[str, str],
    ) -> PriceMetrics:
        old_price = prior.price if prior else None
        threshold = self.price_anomaly_confirm_threshold
        if (
            threshold is None
            or old_price is None
            or old_price.price_usdt is None
            or candidate.status != SourceStatus.OK
            or candidate.price_usdt is None
        ):
            return candidate
        change = self._relative_change(candidate.price_usdt, old_price.price_usdt)
        if change is None or change < threshold:
            return candidate

        confirmed = await self._fetch_confirmation_price()
        if (
            confirmed.status == SourceStatus.OK
            and confirmed.price_usdt is not None
            and self._within_tolerance(candidate.price_usdt, confirmed.price_usdt)
        ):
            return confirmed

        anomalies["price"] = "unconfirmed"
        logging.warning("Unconfirmed price anomaly; retaining only a recent confirmed price")
        if self._within_stale_age(old_price.source_timestamp or old_price.fetched_at, candidate.fetched_at, self.price_stale_max_age_seconds):
            return replace(
                old_price,
                status=SourceStatus.PARTIAL,
                status_detail="large price change was not confirmed",
                is_stale=True,
            )
        return replace(
            candidate,
            price_usdt=None,
            bid_usdt=None,
            ask_usdt=None,
            quote_volume_24h=None,
            price_change_24h_percent=None,
            high_price_24h=None,
            low_price_24h=None,
            status=SourceStatus.PARTIAL,
            status_detail="large price change was not confirmed; no recent confirmed value is available",
            is_stale=True,
        )

    async def _fetch_confirmation_price(self) -> PriceMetrics:
        if self.anomaly_confirm_delay_seconds > 0:
            await asyncio.sleep(self.anomaly_confirm_delay_seconds)
        return await self._fetch_price()

    async def _confirm_farming_anomalies(
        self,
        candidates: list[PoolMetrics],
        prior: DataSnapshot | None,
        anomalies: dict[str, str],
    ) -> list[PoolMetrics]:
        threshold = self.apr_anomaly_confirm_threshold
        if threshold is None or prior is None:
            return candidates

        candidate_by_id = {metrics.pool.address: metrics for metrics in candidates}
        anomaly_key_by_pool: dict[str, tuple[str, str, str | None, str | None]] = {}
        for pool_id, candidate in candidate_by_id.items():
            old_metrics = prior.farming.get(pool_id)
            if (
                old_metrics is None
                or candidate.status not in {SourceStatus.OK, SourceStatus.PARTIAL}
                or not candidate.identity_valid
                or not old_metrics.identity_valid
            ):
                continue
            old_rate = old_metrics.primary_farm_yield
            candidate_rate = candidate.primary_farm_yield
            if old_rate is None or candidate_rate is None:
                continue
            identity = (
                candidate_rate.source_field,
                candidate_rate.kind,
                candidate_rate.period,
                candidate_rate.farm_address,
            )
            old_identity = (
                old_rate.source_field,
                old_rate.kind,
                old_rate.period,
                old_rate.farm_address,
            )
            old_value = old_rate.value_percent
            candidate_value = candidate_rate.value_percent
            if (
                identity != old_identity
                or not isinstance(old_value, Decimal)
                or not isinstance(candidate_value, Decimal)
                or not old_value.is_finite()
                or not candidate_value.is_finite()
                or old_value < 0
                or candidate_value < 0
            ):
                continue
            change = self._relative_change(candidate_value, old_value)
            if (old_value == 0 and candidate_value != 0) or (change is not None and change >= threshold):
                anomaly_key_by_pool[pool_id] = identity

        if not anomaly_key_by_pool:
            return candidates

        if self.anomaly_confirm_delay_seconds > 0:
            await asyncio.sleep(self.anomaly_confirm_delay_seconds)
        try:
            retry_metrics = list(await self.fetch_farming())
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Farming anomaly confirmation request failed")
            retry_metrics = []
        retry_by_id = {metrics.pool.address: metrics for metrics in retry_metrics}

        for pool_id, identity in anomaly_key_by_pool.items():
            initial = candidate_by_id[pool_id]
            retry = retry_by_id.get(pool_id)
            initial_rate = initial.primary_farm_yield
            retry_rate = retry.primary_farm_yield if retry is not None else None
            retry_identity = (
                (
                    retry_rate.source_field,
                    retry_rate.kind,
                    retry_rate.period,
                    retry_rate.farm_address,
                )
                if retry_rate is not None
                else None
            )
            confirmed = bool(
                retry
                and retry.status in {SourceStatus.OK, SourceStatus.PARTIAL}
                and retry.identity_valid
                and not retry.is_stale
                and initial_rate is not None
                and retry_rate is not None
                and retry_identity == identity
                and self._within_tolerance(initial_rate.value_percent, retry_rate.value_percent)
            )
            if confirmed and retry is not None:
                candidate_by_id[pool_id] = retry
                continue

            anomalies[f"farming:{pool_id}"] = "unconfirmed"
            logging.warning(
                "Unconfirmed farming anomaly pool=%s; retaining only recent confirmed rates",
                pool_id,
            )
            old_metrics = prior.farming.get(pool_id)
            if old_metrics is not None and self._within_stale_age(
                old_metrics.fetched_at,
                initial.fetched_at,
                self.farming_stale_max_age_seconds,
            ):
                candidate_by_id[pool_id] = replace(
                    old_metrics,
                    status=SourceStatus.PARTIAL,
                    status_detail="large APR change was not confirmed",
                    is_stale=True,
                )
            else:
                candidate_by_id[pool_id] = replace(
                    initial,
                    farm_rates=(),
                    pool_rates=(),
                    total_rate=None,
                    tvl_usd=None,
                    status=SourceStatus.PARTIAL,
                    status_detail="large farming-rate change was not confirmed; no recent confirmed value is available",
                    is_stale=True,
                )
        return list(candidate_by_id.values())

    def _apply_price_stale_fallback(
        self,
        current_price: PriceMetrics,
        prior: DataSnapshot | None,
        now: datetime,
    ) -> PriceMetrics:
        if current_price.status not in {SourceStatus.UNAVAILABLE, SourceStatus.TIMEOUT} or prior is None:
            return current_price
        confirmed = prior.price
        if confirmed.price_usdt is None or not self._within_stale_age(
            confirmed.source_timestamp or confirmed.fetched_at,
            now,
            self.price_stale_max_age_seconds,
        ):
            return current_price
        return replace(
            confirmed,
            status=current_price.status,
            status_detail="last confirmed price shown; source is temporarily unavailable",
            is_stale=True,
        )

    @staticmethod
    def _relative_change(new: Decimal, old: Decimal) -> Decimal | None:
        if old == 0:
            return None
        return abs(new - old) / abs(old) * Decimal(100)

    def _within_tolerance(self, first: Decimal | None, second: Decimal | None) -> bool:
        if first is None or second is None:
            return False
        if first == second:
            return True
        if first == 0:
            return False
        return abs(first - second) / abs(first) * Decimal(100) <= self.confirmation_tolerance_percent

    @staticmethod
    def _within_stale_age(fetched_at: datetime, now: datetime, max_age_seconds: int) -> bool:
        age = (now - fetched_at).total_seconds()
        return 0 <= age <= max_age_seconds

    @staticmethod
    def _aggregate_farming_status(metrics: list[PoolMetrics]) -> SourceStatus:
        if metrics and all(metric.status == SourceStatus.OK for metric in metrics):
            return SourceStatus.OK
        if any(metric.status in {SourceStatus.OK, SourceStatus.PARTIAL} for metric in metrics):
            return SourceStatus.PARTIAL
        if any(metric.status == SourceStatus.TIMEOUT for metric in metrics):
            return SourceStatus.TIMEOUT
        if any(metric.status == SourceStatus.INVALID_RESPONSE for metric in metrics):
            return SourceStatus.INVALID_RESPONSE
        return SourceStatus.UNAVAILABLE

    def set_price_identity(self, valid: bool | None) -> None:
        self.price_identity_valid = valid
        if valid is False:
            self.current_snapshot = self._invalidate_price_in_snapshot(self.current_snapshot)
            self.previous_snapshot = self._invalidate_price_in_snapshot(self.previous_snapshot)
            self._save()

    def set_farming_identity(self, pool_id: str, valid: bool | None) -> None:
        if valid is not False:
            return
        self.current_snapshot = self._invalidate_pool_in_snapshot(self.current_snapshot, pool_id)
        self.previous_snapshot = self._invalidate_pool_in_snapshot(self.previous_snapshot, pool_id)
        self._save()

    def invalidate_freshness(self) -> None:
        """Require refresh before serving a snapshot loaded before startup checks."""
        self._refresh_required = True

    @classmethod
    def _invalidate_pool_in_snapshot(cls, snapshot: DataSnapshot | None, pool_id: str) -> DataSnapshot | None:
        if snapshot is None or pool_id not in snapshot.farming:
            return snapshot
        farming = dict(snapshot.farming)
        farming[pool_id] = replace(
            farming[pool_id],
            farm_rates=(),
            pool_rates=(),
            total_rate=None,
            tvl_usd=None,
            status=SourceStatus.INVALID_RESPONSE,
            identity_valid=False,
            status_detail="startup pool identity check failed",
            is_stale=False,
        )
        statuses = dict(snapshot.source_statuses)
        statuses[f"farming:{pool_id}"] = SourceStatus.INVALID_RESPONSE.value
        statuses["farming"] = cls._aggregate_farming_status(list(farming.values())).value
        return replace(snapshot, farming=farming, source_statuses=statuses)

    @staticmethod
    def _invalidate_price_in_snapshot(snapshot: DataSnapshot | None) -> DataSnapshot | None:
        if snapshot is None:
            return None
        invalid_price = replace(
            snapshot.price,
            price_usdt=None,
            bid_usdt=None,
            ask_usdt=None,
            quote_volume_24h=None,
            price_change_24h_percent=None,
            high_price_24h=None,
            low_price_24h=None,
            source_timestamp=None,
            status=SourceStatus.INVALID_RESPONSE,
            status_detail="MEXC market identity mismatch",
            is_stale=False,
        )
        statuses = dict(snapshot.source_statuses)
        statuses["price"] = SourceStatus.INVALID_RESPONSE.value
        return replace(snapshot, price=invalid_price, source_statuses=statuses)

    def _load(self) -> tuple[DataSnapshot | None, DataSnapshot | None]:
        if self.snapshot_path is None or not self.snapshot_path.exists():
            return None, None
        try:
            payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("snapshot root is not an object")
            current = self._decode_snapshot(payload.get("current"))
            previous = self._decode_snapshot(payload.get("previous"))
            return current, previous
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError, IndexError):
            self._backup_corrupt_snapshot()
            logging.exception("Snapshot cache is unreadable; starting without persisted snapshots")
            return None, None

    def _save(self) -> None:
        if self.snapshot_path is None:
            return
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "current": self._encode_snapshot(self.current_snapshot),
            "previous": self._encode_snapshot(self.previous_snapshot),
        }
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        descriptor, temporary_name = tempfile.mkstemp(dir=self.snapshot_path.parent, prefix=f".{self.snapshot_path.name}.")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
                temporary_file.write(encoded)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            Path(temporary_name).replace(self.snapshot_path)
        except OSError:
            logging.exception("Could not persist accepted snapshot")
        finally:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                logging.warning("Could not remove temporary snapshot file")

    def _backup_corrupt_snapshot(self) -> None:
        if self.snapshot_path is None or not self.snapshot_path.exists():
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup = self.snapshot_path.with_name(f"{self.snapshot_path.name}.corrupt-{timestamp}")
        try:
            secure_backup_file(self.snapshot_path, backup)
        except OSError:
            logging.exception("Could not preserve unreadable snapshot cache")
            raise

    @classmethod
    def _encode_snapshot(cls, snapshot: DataSnapshot | None) -> dict[str, Any] | None:
        if snapshot is None:
            return None
        return {
            "fetched_at": snapshot.fetched_at.isoformat(),
            "farming": {pool_id: cls._encode_pool(metrics) for pool_id, metrics in snapshot.farming.items()},
            "price": cls._encode_price(snapshot.price),
            "source_statuses": snapshot.source_statuses,
            "anomalies": snapshot.anomalies,
        }

    @staticmethod
    def _encode_rate(rate: RateMetric | None) -> dict[str, Any] | None:
        if rate is None:
            return None
        return {
            "value_percent": str(rate.value_percent),
            "kind": rate.kind,
            "source_field": rate.source_field,
            "period": rate.period,
            "farm_address": rate.farm_address,
            "reward_assets": list(rate.reward_assets),
        }

    @classmethod
    def _encode_pool(cls, metrics: PoolMetrics) -> dict[str, Any]:
        return {
            "pool": {
                "name": metrics.pool.name,
                "dex": metrics.pool.dex,
                "address": metrics.pool.address,
                "url": metrics.pool.url,
            },
            "farm_rates": [cls._encode_rate(rate) for rate in metrics.farm_rates],
            "pool_rates": [cls._encode_rate(rate) for rate in metrics.pool_rates],
            "total_rate": cls._encode_rate(metrics.total_rate),
            "tvl_usd": str(metrics.tvl_usd) if metrics.tvl_usd is not None else None,
            "fetched_at": metrics.fetched_at.isoformat(),
            "status": metrics.status.value,
            "identity_valid": metrics.identity_valid,
            "status_detail": metrics.status_detail,
            "is_stale": metrics.is_stale,
        }

    @staticmethod
    def _encode_price(metrics: PriceMetrics) -> dict[str, Any]:
        return {
            "price_usdt": str(metrics.price_usdt) if metrics.price_usdt is not None else None,
            "bid_usdt": str(metrics.bid_usdt) if metrics.bid_usdt is not None else None,
            "ask_usdt": str(metrics.ask_usdt) if metrics.ask_usdt is not None else None,
            "quote_volume_24h": str(metrics.quote_volume_24h) if metrics.quote_volume_24h is not None else None,
            "price_change_24h_percent": (
                str(metrics.price_change_24h_percent) if metrics.price_change_24h_percent is not None else None
            ),
            "high_price_24h": str(metrics.high_price_24h) if metrics.high_price_24h is not None else None,
            "low_price_24h": str(metrics.low_price_24h) if metrics.low_price_24h is not None else None,
            "symbol": metrics.symbol,
            "quote_currency": metrics.quote_currency,
            "source": metrics.source,
            "fetched_at": metrics.fetched_at.isoformat(),
            "source_timestamp": metrics.source_timestamp.isoformat() if metrics.source_timestamp else None,
            "status": metrics.status.value,
            "status_detail": metrics.status_detail,
            "is_stale": metrics.is_stale,
        }

    @staticmethod
    def _decode_decimal(
        value: Any,
        field: str,
        *,
        allow_zero: bool = True,
        allow_negative: bool = False,
    ) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"snapshot {field} is not a valid decimal") from exc
        if not parsed.is_finite() or (parsed < 0 and not allow_negative) or (not allow_zero and parsed == 0):
            raise ValueError(f"snapshot {field} is outside the accepted numeric domain")
        return parsed

    @staticmethod
    def _decode_timestamp(value: Any, field: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"snapshot {field} is not a valid timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"snapshot {field} timestamp must include a timezone")
        return parsed

    @staticmethod
    def _decode_rate(record: dict[str, Any] | None) -> RateMetric | None:
        if record is None:
            return None
        return RateMetric(
            value_percent=SnapshotManager._decode_decimal(record["value_percent"], "rate"),
            kind=str(record["kind"]),
            source_field=str(record["source_field"]),
            period=str(record["period"]) if record.get("period") is not None else None,
            farm_address=str(record["farm_address"]) if record.get("farm_address") is not None else None,
            reward_assets=tuple(str(asset) for asset in record.get("reward_assets", [])),
        )

    @classmethod
    def _decode_pool(cls, record: dict[str, Any]) -> PoolMetrics:
        pool_record = record["pool"]
        identity_valid = record["identity_valid"]
        if type(identity_valid) is not bool:
            raise ValueError("snapshot pool identity_valid must be a boolean")
        pool = FarmingPool(
            name=str(pool_record["name"]),
            dex=str(pool_record["dex"]),
            address=str(pool_record["address"]),
            url=str(pool_record["url"]),
        )
        farm_rates = tuple(cls._decode_rate(rate) for rate in record.get("farm_rates", []))
        pool_rates = tuple(cls._decode_rate(rate) for rate in record.get("pool_rates", []))
        return PoolMetrics(
            pool=pool,
            farm_rates=tuple(rate for rate in farm_rates if rate is not None),
            pool_rates=tuple(rate for rate in pool_rates if rate is not None),
            total_rate=cls._decode_rate(record.get("total_rate")),
            tvl_usd=(
                cls._decode_decimal(record["tvl_usd"], "TVL")
                if record.get("tvl_usd") is not None
                else None
            ),
            fetched_at=SnapshotManager._decode_timestamp(record["fetched_at"], "pool fetched_at"),
            status=SourceStatus(str(record["status"])),
            identity_valid=identity_valid,
            status_detail=str(record["status_detail"]) if record.get("status_detail") is not None else None,
            is_stale=bool(record.get("is_stale", False)),
        )

    @staticmethod
    def _decode_price(record: dict[str, Any]) -> PriceMetrics:
        def decimal_or_none(
            key: str,
            *,
            allow_zero: bool = True,
            allow_negative: bool = False,
        ) -> Decimal | None:
            if record.get(key) is None:
                return None
            return SnapshotManager._decode_decimal(
                record[key], key, allow_zero=allow_zero, allow_negative=allow_negative
            )

        return PriceMetrics(
            price_usdt=decimal_or_none("price_usdt", allow_zero=False),
            bid_usdt=decimal_or_none("bid_usdt", allow_zero=False),
            ask_usdt=decimal_or_none("ask_usdt", allow_zero=False),
            quote_volume_24h=decimal_or_none("quote_volume_24h"),
            price_change_24h_percent=decimal_or_none("price_change_24h_percent", allow_negative=True),
            high_price_24h=decimal_or_none("high_price_24h", allow_zero=False),
            low_price_24h=decimal_or_none("low_price_24h", allow_zero=False),
            symbol=str(record["symbol"]),
            quote_currency=str(record["quote_currency"]),
            source=str(record["source"]),
            fetched_at=SnapshotManager._decode_timestamp(record["fetched_at"], "price fetched_at"),
            source_timestamp=(
                SnapshotManager._decode_timestamp(record["source_timestamp"], "price source_timestamp")
                if record.get("source_timestamp") is not None
                else None
            ),
            status=SourceStatus(str(record["status"])),
            status_detail=str(record["status_detail"]) if record.get("status_detail") is not None else None,
            is_stale=bool(record.get("is_stale", False)),
        )

    @classmethod
    def _decode_snapshot(cls, record: dict[str, Any] | None) -> DataSnapshot | None:
        if record is None:
            return None
        if not isinstance(record, dict) or not isinstance(record.get("farming"), dict):
            raise ValueError("snapshot record is invalid")
        price_record = record.get("price")
        if not isinstance(price_record, dict):
            raise ValueError("snapshot price record is invalid")
        statuses = record.get("source_statuses", {})
        anomalies = record.get("anomalies", {})
        if not isinstance(statuses, dict) or not isinstance(anomalies, dict):
            raise ValueError("snapshot metadata is invalid")
        return DataSnapshot(
            fetched_at=cls._decode_timestamp(record["fetched_at"], "snapshot fetched_at"),
            farming={pool_id: cls._decode_pool(metric) for pool_id, metric in record["farming"].items()},
            price=cls._decode_price(price_record),
            source_statuses={str(key): str(value) for key, value in statuses.items()},
            anomalies={str(key): str(value) for key, value in anomalies.items()},
        )
