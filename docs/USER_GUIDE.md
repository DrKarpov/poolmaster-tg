# User guide

Use `/start` in a private chat to choose Russian/English and open the menu. When enabled and supported, the menu contains a gold coin: tap it to toss; no wager or statistics are involved. The public source uses TST on one face and a blank reverse.

## Price

Price or `/price` opens a private chart when widgets are enabled. Choose 30m/1h/4h/1D/1W/1M in the widget. Drag the plot to browse history and the time band to change horizontal scale. Tap for candle time/OHLC. Refresh updates the current quote and 24-hour caption; navigation does not turn an old close into the current price. Back returns to the menu.

With widgets disabled or explicitly unsupported, the legacy chart flow remains. Group `/price` keeps the 1h PNG chart; inline price is a shareable text quote, not a widget.

## Farming

With the weekly widget enabled, Farming or `/farming` opens four pool series and current mean/TVL. Tap a weekly point to see its values. Tap the same point or detail card to close. Refresh updates the sources in the same message; Back returns to the menu. There is no pan/zoom or daily/three-day control.

Current mean and historical-period mean are different metrics. Unknown observations are not zero, and an observed zero is not a gap. The legacy private farming/history flow remains as fallback. Group `/farming` still sends the completed-week PNG chart; inline farming remains text.

## Alerts and commands

Alerts configure personal price/farming thresholds and direction independently. Fresh usable data is required. Personal settings are separate from the administrator's global pause and destination schedules. Language controls remain in the menu.

- `/start`: private menu / first-use language selection.
- `/price`: private price flow; legacy 1h chart in groups.
- `/farming`: private farming flow; history chart in groups.
- `/admin`: configured administrators only.

Open a new widget after an interface update: old messages retain their original HTML. Support depends on Telegram client/API capability. [Widget details](TELEGRAM_WIDGETS.md), [data validation](DATA_SOURCES.md) and [disclaimer](../DISCLAIMER.md).
