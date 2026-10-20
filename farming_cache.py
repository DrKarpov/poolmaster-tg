from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from farming import FarmingPool, PoolMetrics, RateMetric, SourceStatus, is_primary_farming_usable
from state_backup import secure_backup_file


class FarmingStaleCache:
    """Persist the latest identity-checked result with a usable primary yield."""

    def __init__(self, max_age_seconds: int, path: Path | None = None) -> None:
        if max_age_seconds < 1:
            raise ValueError("max_age_seconds must be greater than zero")
        self.max_age_seconds = max_age_seconds
        self.path = Path(path) if path is not None else None
        self._confirmed = self._load()

    def apply(
        self,
        current: list[PoolMetrics],
        *,
        now: datetime | None = None,
    ) -> list[PoolMetrics]:
        current_time = now or datetime.now(timezone.utc)
        updated = False
        results: list[PoolMetrics] = []

        for metrics in current:
            if is_primary_farming_usable(metrics):
                self._confirmed[metrics.pool.address] = replace(metrics, is_stale=False)
                results.append(replace(metrics, is_stale=False))
                updated = True
                continue

            cached = self._confirmed.get(metrics.pool.address)
            cached_rate = cached.primary_farm_yield if cached is not None else None
            if (
                metrics.pool.dex == "STON.fi"
                and metrics.status == SourceStatus.PARTIAL
                and metrics.identity_valid is True
                and metrics.stale_farm_yield_fallback_allowed
                and cached is not None
                and is_primary_farming_usable(cached)
                and cached_rate is not None
            ):
                age = (current_time - cached.fetched_at).total_seconds()
                if 0 <= age <= self.max_age_seconds:
                    results.append(
                        replace(
                            metrics,
                            farm_rates=(cached_rate,),
                            fetched_at=cached.fetched_at,
                            status_detail="last confirmed farm yield shown; STON.fi farm endpoint is temporarily unavailable",
                            is_stale=True,
                            stale_farm_yield_fallback_allowed=False,
                        )
                    )
                    continue

            if cached and metrics.status in {SourceStatus.UNAVAILABLE, SourceStatus.TIMEOUT}:
                age = (current_time - cached.fetched_at).total_seconds()
                if 0 <= age <= self.max_age_seconds:
                    results.append(
                        replace(
                            cached,
                            status=metrics.status,
                            status_detail="last confirmed value shown; source is temporarily unavailable",
                            is_stale=True,
                        )
                    )
                    continue

            results.append(metrics)

        if updated:
            self._save()
        return results

    def _load(self) -> dict[str, PoolMetrics]:
        if self.path is None or not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("cache root is not an object")
            records = payload.get("confirmed", {})
            if not isinstance(records, dict):
                raise ValueError("confirmed cache is not an object")
            return {
                pool_id: self._decode_metrics(record)
                for pool_id, record in records.items()
                if isinstance(record, dict)
            }
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError, IndexError):
            self._backup_corrupt_cache()
            logging.exception("Farming cache is unreadable; ignoring stored values")
            return {}

    def _backup_corrupt_cache(self) -> None:
        if self.path is None or not self.path.exists():
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup = self.path.with_name(f"{self.path.name}.corrupt-{timestamp}")
        try:
            secure_backup_file(self.path, backup)
        except OSError:
            logging.exception("Could not preserve unreadable farming cache")
            raise

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "confirmed": {
                pool_id: self._encode_metrics(metrics)
                for pool_id, metrics in self._confirmed.items()
            }
        }
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        descriptor, temporary_name = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
                temporary_file.write(encoded)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            Path(temporary_name).replace(self.path)
        finally:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                logging.warning("Could not remove temporary farming cache file")

    @staticmethod
    def _encode_metrics(metrics: PoolMetrics) -> dict[str, object]:
        return {
            "pool": {
                "name": metrics.pool.name,
                "dex": metrics.pool.dex,
                "address": metrics.pool.address,
                "url": metrics.pool.url,
            },
            "farm_rates": [FarmingStaleCache._encode_rate(rate) for rate in metrics.farm_rates],
            "pool_rates": [FarmingStaleCache._encode_rate(rate) for rate in metrics.pool_rates],
            "total_rate": FarmingStaleCache._encode_rate(metrics.total_rate) if metrics.total_rate else None,
            "tvl_usd": str(metrics.tvl_usd) if metrics.tvl_usd is not None else None,
            "fetched_at": metrics.fetched_at.isoformat(),
            "status": metrics.status.value,
            "identity_valid": metrics.identity_valid,
        }

    @staticmethod
    def _encode_rate(rate: RateMetric | None) -> dict[str, object] | None:
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

    @staticmethod
    def _decode_decimal(value: object, field: str) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"farming cache {field} is not a valid decimal") from exc
        if not parsed.is_finite() or parsed < 0:
            raise ValueError(f"farming cache {field} is outside the accepted numeric domain")
        return parsed

    @staticmethod
    def _decode_timestamp(value: object) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError("farming cache timestamp is invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("farming cache timestamp must include a timezone")
        return parsed

    @staticmethod
    def _decode_rate(record: dict[str, object] | None) -> RateMetric | None:
        if record is None:
            return None
        return RateMetric(
            value_percent=FarmingStaleCache._decode_decimal(record["value_percent"], "rate"),
            kind=str(record["kind"]),
            source_field=str(record["source_field"]),
            period=str(record["period"]) if record.get("period") is not None else None,
            farm_address=str(record["farm_address"]) if record.get("farm_address") is not None else None,
            reward_assets=tuple(str(asset) for asset in record.get("reward_assets", [])),
        )

    @staticmethod
    def _decode_metrics(record: dict[str, object]) -> PoolMetrics:
        pool_record = record["pool"]
        if not isinstance(pool_record, dict):
            raise ValueError("pool is not an object")
        identity_valid = record["identity_valid"]
        if type(identity_valid) is not bool:
            raise ValueError("farming cache identity_valid must be a boolean")
        pool = FarmingPool(
            name=str(pool_record["name"]),
            dex=str(pool_record["dex"]),
            address=str(pool_record["address"]),
            url=str(pool_record["url"]),
        )
        farm_rates = tuple(
            FarmingStaleCache._decode_rate(rate)
            for rate in record.get("farm_rates", [])
        )
        pool_rates = tuple(
            FarmingStaleCache._decode_rate(rate)
            for rate in record.get("pool_rates", [])
        )
        return PoolMetrics(
            pool=pool,
            farm_rates=tuple(rate for rate in farm_rates if rate is not None),
            pool_rates=tuple(rate for rate in pool_rates if rate is not None),
            total_rate=FarmingStaleCache._decode_rate(record.get("total_rate")),
            tvl_usd=(
                FarmingStaleCache._decode_decimal(record["tvl_usd"], "TVL")
                if record.get("tvl_usd") is not None
                else None
            ),
            fetched_at=FarmingStaleCache._decode_timestamp(record["fetched_at"]),
            status=SourceStatus(str(record["status"])),
            identity_valid=identity_valid,
        )
