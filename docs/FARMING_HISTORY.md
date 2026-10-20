# Farming history

POOLMASTER stores confirmed farming observations and generates a weekly chart locally as a PNG. No public image host is required.

## Collection and storage

- The tracked seed file `resources/farming_history_seed.csv` provides curated starting observations.
- Seed rows are imported without overwriting an existing record.
- The bot collects a fresh observation at startup and on a daily UTC schedule.
- Only a fresh, identity-verified, confirmed primary yield is recorded.
- The history is keyed by pool and UTC date. A later valid observation for the same pool and date can replace an earlier one.
- Source metric kind, observation time, source, and available supporting data are retained so APR and APY are not silently treated as equivalent.

## Chart and summaries

The chart shows complete Monday–Sunday UTC weeks. Each point averages observations available for that pool during the week; missing weeks remain gaps. The caption includes per-pool averages and, when all four pools have data, an equal-weight average of the four values. This is descriptive and is not a portfolio return or TVL-weighted result.

The current incomplete week is omitted. Charts are generated when requested; images already sent to Telegram do not update themselves.
