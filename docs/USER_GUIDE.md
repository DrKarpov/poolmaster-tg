# User guide

Use `/start` in a private chat to open the menu and choose Russian or English. The language choice applies to private screens, chart labels, and farming-history captions.

## Price

Choose **Price** or send `/price` for a MEXC Spot `JETTONUSDT` chart. The chart includes 1h or 12h candles, volume, a moving average, and UTC timestamps. In a group, `/price` sends the 1h chart without private controls. Inline mode provides a shareable text quote; it does not return a chart.

## Farming and history

Choose **Farming** or send `/farming` to view the four configured DeDust and STON.fi pools. In a private chat, open **History** for completed-week observations. In a group, `/farming` sends the history chart. Missing weeks are not interpolated, and the history chart is generated from saved observations when requested.

## Alerts

Open **Alerts** to configure price and farming alerts independently. Each alert has a threshold and direction. Alerts require usable, fresh source data. A personal pause is separate from the administrator-controlled global pause.

## Commands

- `/start` — private menu or first-use language choice.
- `/price` — price chart; a 1h chart in groups.
- `/farming` — farming screen in private chat; history chart in groups.
- `/admin` — administrator panel for configured administrators.

Values are informational and come from public data sources. See [Data sources](DATA_SOURCES.md) for validation and freshness behavior.
