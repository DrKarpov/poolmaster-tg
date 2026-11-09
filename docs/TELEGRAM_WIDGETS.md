# Telegram message widgets

The private-chat interface has price, weekly farming and coin-menu widgets. The public source uses TST branding; runtime compatibility differences are listed in [Provenance](../PROVENANCE.md). Portfolio images show the original interface with selected branding blurred.

## Price

- Timeframes: 30m, 1h, 4h, 1D, 1W and 1M (calendar month), with real OHLCV, USDT volume and MA7. The last successfully selected timeframe and per-frame horizontal scale are retained.
- Dragging the plot browses history. Dragging the highlighted time band changes horizontal scale and anchors recent candles to the right. Vertical movement belongs to chat scrolling; no vertical scale or +/- controls are added.
- UTC axis labels adapt to the visible period. Tap a candle for UTC time and OHLC.
- A bounded reserve loads older candles through read-only query responses. Rendering is coalesced by animation frame; reserve data is applied after gesture release. Background history loading does not flash the manual refresh indicator or replace the current quote.
- Choosing a timeframe opens its latest candles. Only explicit refresh accepts a new market quote and updates the independent 24-hour caption below the widget.
- The status is a compact dot beside USDT. Back stays below the message and is restored with edits where the beta API clears keyboards. A closed session cannot be revived by a late source response.

## Farming

Four series show completed Monday–Sunday UTC weeks. The entire available weekly series fits without pan/zoom. Tap a week for four pool values; tap the same week or popup to dismiss it.

The top mean is the current equal-weight mean of four confirmed pool rates; it is neither a TVL-weighted return nor the mean of all historical weeks. Total TVL is displayed only when complete. A missing rate remains unknown, while a confirmed 0% remains valid. Refresh fetches sources, persists confirmed observations and updates the same message. No caption is added; Back stays below it.

Only 7D is active, without period buttons. 1D/3D are reserved for later data work. Complete continuous daily coverage is not claimed.

## Coin menu

The gold coin has a TST face and a blank reverse in this public source. Each toss chooses one random bit using `crypto.getRandomValues`, giving 50/50. Reflection, varying rotation speed and whole-coin landing bounce remain. Optional light/medium haptics are sent at toss/first landing; missing haptic support does not interrupt the animation. Animation stops after the toss. No wager, stats or per-toss server session exists.

The ordinary Price/Farming/Alerts/Language menu remains below the widget. Existing navigation retains the previous screen until delivery is confirmed.

## API, configuration and availability

This implementation uses Telegram Message Widgets beta API/profile 1. Widget updates share the existing poller; keep exactly one poller per token. The source flags default to false, independently of the accepted live installation.

| Flag | Role |
|---|---|
| `PRICE_WIDGETS_ENABLED` | Private price chart |
| `PRICE_WIDGET_QUERIES_ENABLED` | In-widget query controls |
| `PRICE_WIDGET_BETA_UPDATES` | Beta updates endpoint for the existing poller |
| `FARMING_WIDGETS_ENABLED` | Weekly farming, including its query/update route |
| `COIN_WIDGETS_ENABLED` | Client-only coin home menu |

Keep the beta update route while previously delivered query-capable widgets still need answers. An omitted capability flag is unknown, not an explicit refusal. Explicit unsupported delivery can select legacy fallback; ambiguous delivery is not blindly retried to create duplicate messages. Forwarded copies do not obtain the owner's private session.

Groups retain legacy `/price` and `/farming` charts and text inline results. Group inline widgets are not enabled. Supported-device checks covered the owner's phone and Windows Desktop 7.4.1; other client/version combinations are not certified.

## Assets and development

Editable price/farming sources are `assets/widgets/*/widget.source.html`; generated templates are checked in beside them. `assets/widgets/numeric-compat.js` has its own generated minified file. Three Python builder scripts use pinned Terser 5.51.2; Node/Terser are development-only.

Loaded HTML is checked against the 32768-byte raw and 4096-byte compressed limits; public/chat payloads have separate byte limits. Check both languages and query/copy variants after changing assets. The compact TST outline avoids a font dependency. Previously sent messages retain their old HTML; open a fresh widget to inspect a changed version.

[Testing](TESTING.md) · [Configuration](CONFIGURATION.md) · [User guide](USER_GUIDE.md)
