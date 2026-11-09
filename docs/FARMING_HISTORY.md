# Farming history

Confirmed farming observations are stored locally and summarized into completed Monday–Sunday UTC weeks. The private widget and legacy PNG renderer consume saved observations; no public image host is required.

## Collection and provenance

- `resources/farming_history_seed.csv` provides curated starting observations and does not overwrite existing rows during import.
- Each record is keyed by pool and UTC date. A later confirmed observation for the same date may replace an earlier one.
- Startup/daily collection verifies that fresh observations for all four pools actually reached SQLite for the current UTC date. Missing coverage, source/coverage-read failures trigger retry after 300 seconds.
- Failed writes keep confirmed observations for persistence retry. After recovery the current date is checked again, including after midnight.
- Only fresh, identity-verified confirmed primary yields enter live history. Stale caches and historic seed records do not satisfy the daily live-coverage check.
- A genuine confirmed 0% is a valid observation. Missing data is not manufactured or silently converted into zero.
- Source metric kind, observation time, source and available supporting data distinguish APR/APY and avoid silently equating them.

## Weekly view and current metrics

Each weekly point averages that pool's available confirmed daily observations in the completed week. The incomplete current week is omitted; missing weeks remain gaps. Tap a week in the widget for four pool values. All weekly points fit without pan/zoom. Legacy PNG history and its descriptive caption remain available for groups/fallback.

The widget's top mean is the current equal-weight mean of four confirmed pool rates, not a TVL-weighted portfolio return or the mean across the full historical period. Complete combined TVL is shown separately. Refresh updates sources, observations and the same message; already sent messages do not poll new quotes autonomously.

Only the weekly 7D view is enabled. Daily/3D controls are absent; complete uninterrupted daily coverage over the entire history is not claimed. Real manually recovered observations are not treated as invented data, but they do not prove every date is present.

[Data sources](DATA_SOURCES.md) · [Widgets](TELEGRAM_WIDGETS.md)
