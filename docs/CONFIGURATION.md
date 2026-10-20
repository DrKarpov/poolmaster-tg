# Configuration

Local development may read variables from `.env`. Start from [`.env.example`](../.env.example), use a dedicated test bot, and keep `.env` private. Do not use production credentials for local development.

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Required Bot API credential. |
| `TELEGRAM_BOT_USERNAME` | Bot username without `@`, used for addressed commands and links. |
| `ADMIN_USER_IDS` | Comma-separated numeric administrator IDs; empty means no administrator access. |
| `GLOBAL_AUTO_PUSH_PAUSED` | Initial global automatic user-alert pause when admin state is first created. |
| `USER_STATE_PATH` | Private user registry and preference state. |
| `AUTO_PUSH_STATE_PATH` | Admin pause and publication state. |
| `MANAGED_DESTINATIONS_PATH` | Managed channel/group configuration and schedule state. |
| `FARMING_HISTORY_PATH` | Farming-history SQLite database. |
| `FARMING_CACHE_PATH` / `SNAPSHOT_CACHE_PATH` | Cached source data. |
| `PRICE_*`, `FARMING_*`, `APR_ANOMALY_CONFIRM_THRESHOLD` | Polling, freshness, cooldown, and anomaly thresholds. |
| `CHART_CACHE_DIR` / `CHART_STALE_MAX_AGE_SECONDS` | Chart cache location and stale-chart limit. |

The default paths are relative to the working directory and use the `data/` directory. Do not place persistent state in Git. Boolean values accept `true` / `false`, `yes` / `no`, `on` / `off`, and `1` / `0` (case-insensitive). Invalid configuration is rejected rather than silently replaced.
