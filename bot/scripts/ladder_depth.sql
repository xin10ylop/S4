-- How deep did each live fill actually walk?
--
-- This is the question the M3 backtest CANNOT answer. That tape is
-- "Telonex hourly quotes: TOP OF BOOK" (scripts/multicoin/replay_hourly.py:227)
-- and every one of its 66 bitcoin fills walked exactly 1 level. So every
-- capacity number derived from it — the $250 cap ceiling, the $285 max
-- fillable, "above $400 earns zero" — is a TOP-OF-BOOK figure and therefore a
-- FLOOR, not a ceiling.
--
-- The live bot walks the full CLOB ladder. `levels_json` records what it
-- actually took. If the deep fills are multi-level, live capacity is
-- materially higher than the backtest says and the max-bet answer moves.
--
-- Run:  cd /root/S4/bot && sqlite3 -header -column data/polybot.db < scripts/ladder_depth.sql
SELECT
    f.market_slug,
    ROUND(f.shares, 2)                                   AS shares,
    ROUND(f.cost_usd, 2)                                 AS cost,
    ROUND(f.avg_price, 4)                                AS avg_px,
    json_array_length(f.levels_json)                     AS n_levels,
    ROUND(json_extract(f.levels_json, '$[0].price'), 4)  AS best_ask,
    ROUND(json_extract(f.levels_json, '$[0].shares'), 2) AS shares_at_best,
    ROUND(f.avg_price - json_extract(f.levels_json, '$[0].price'), 4) AS walked_above_best,
    f.levels_json
FROM fills f
WHERE f.outcome = 'filled' AND f.strategy = 'close_snipe'
ORDER BY f.cost_usd DESC;
