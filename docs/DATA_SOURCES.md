# Data sources

POOLMASTER reads public HTTPS APIs. It does not authenticate to exchanges, hold wallet keys, place orders, or scrape arbitrary webpages.

| Data | Source | Values used |
|---|---|---|
| JETTON spot market and candles | MEXC Spot REST API | Price, 24-hour market metrics, OHLC candles, and volume for `JETTONUSDT` |
| DeDust farming pools | DeDust API | Primary reward APR and supporting pool metrics for configured JETTON pools |
| STON.fi pools and farms | STON.fi API | Pool metrics and eligible active farm APY for configured JETTON pools |

The configured pool identities are application constants. Responses are checked against the expected market or pool identity and schema before the values are accepted.

## Freshness and fallback behavior

The application distinguishes usable, partial, unavailable, timed-out, and invalid responses. A fresh primary farming yield may remain usable when an auxiliary value such as TVL is unavailable. Stale fallback data is bounded by configured maximum age and labeled stale; it is not used for alerts or confirmed farming-history observations.

Large price and farming changes are checked against a second sample before replacing the accepted value. Failed or inconsistent confirmation does not silently promote the new value. Source APIs can change schema or availability, and reported yields are not promises of future returns.
