# Configuration

These technical details are for separately authorized maintainers; they do not grant operational use under the [license](../LICENSE.md). Local configuration may read `.env`. Start from [`.env.example`](../.env.example), use a dedicated test identity and never commit credentials or production state.

| Variable | Purpose / default |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot API credential; sample contains only a placeholder. |
| `TELEGRAM_BOT_USERNAME` | Bot username without @. |
| `ADMIN_USER_IDS` | Numeric admin allowlist; empty by default. |
| `GLOBAL_AUTO_PUSH_PAUSED` | Initial automatic user-alert pause; independent of destination schedules. |
| `USER_STATE_PATH` | Private preferences/registry, default `data/user_state.json`. |
| `AUTO_PUSH_STATE_PATH` | Admin pause/publication state, default `data/admin_state.json`. |
| `MANAGED_DESTINATIONS_PATH` | Private channels/groups and scheduling state. |
| `FARMING_HISTORY_PATH` | Confirmed farming-history SQLite database. |
| `FARMING_CACHE_PATH` / `SNAPSHOT_CACHE_PATH` | Bounded cached source data. |
| `PRICE_*`, `FARMING_*`, `APR_ANOMALY_CONFIRM_THRESHOLD` | Source polling, freshness, cooldown and anomaly thresholds. |
| `CHART_CACHE_DIR` / `CHART_STALE_MAX_AGE_SECONDS` | Legacy chart cache and stale limit. |
| `UPDATE_CONCURRENCY` | Active user/widget handlers; default 16, integer >=1. |
| `PRICE_WIDGETS_ENABLED` | Private price widget; default false. |
| `PRICE_WIDGET_QUERIES_ENABLED` | In-widget query controls; default false. |
| `PRICE_WIDGET_BETA_UPDATES` | Existing poller uses beta update route; default false. |
| `FARMING_WIDGETS_ENABLED` | Private weekly farming widget/query path; default false. |
| `COIN_WIDGETS_ENABLED` | Coin home menu; default false. |

Price and farming sessions use `price_widget_sessions.sqlite3` and `farming_widget_sessions.sqlite3` beside `USER_STATE_PATH`. They are runtime state, not distributed fixtures. Keep the beta route available while existing interactive widgets need responses.

Concurrency bounds executing handlers, not total users. Waiting users/messages retain independent queues; Telegram HTTP pool size is at least 32 and concurrency+8. Larger values require measuring memory, latency and API throttling. Offline burst checks are not a guarantee of live throughput or unlimited users.

Boolean values accept true/false, yes/no, on/off and 1/0, case-insensitively. Invalid settings are rejected. Relative defaults resolve from the working directory. State, caches, backups and `.env` belong outside Git.

[Widget behavior](TELEGRAM_WIDGETS.md) · [Security](../SECURITY.md)
