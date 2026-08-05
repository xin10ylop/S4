-- Per-trade forensics for every resolved close_snipe fill.
--
-- Answers the one question the aggregate `pnl` command cannot: was each trade
-- inside the |z| band that carries the edge, or in the saturated tail that
-- runs below break-even?
--
--   |z| = |ln(S_t / S_open)| / (sigma_1s * sqrt(tau))
--
-- Measured on 66 backtest bitcoin fills (docs/10_realmoney_audit.md §4):
--   |z| <= 5 : 96.15% win, +20.38c/share
--   |z| >  5 : 57.14% win, -12.92c/share, against a ~76.6% break-even
--
-- Run:  cd /root/S4/bot && venv/bin/python -c "
--         import sqlite3,sys; c=sqlite3.connect('data/polybot.db')
--         [print(r) for r in c.execute(open('scripts/per_trade_z.sql').read())]"
--
-- or simply:  sqlite3 -header -column data/polybot.db < scripts/per_trade_z.sql
SELECT
    f.market_slug,
    f.side,
    ROUND(s.fair, 4)                                        AS fair,
    CASE WHEN s.fair >= 0.9799 THEN 'PINNED' ELSE '' END    AS saturated,
    ROUND(f.avg_price, 4)                                   AS avg_px,
    ROUND(f.shares, 2)                                      AS shares,
    ROUND(f.cost_usd, 2)                                    AS cost,
    ROUND(ABS(LOG(json_extract(s.meta_json, '$.s_t')
                  / json_extract(s.meta_json, '$.s_open')))
          / (json_extract(s.meta_json, '$.sigma_1s')
             * SQRT(json_extract(s.meta_json, '$.tau_secs'))), 2)  AS abs_z,
    CASE WHEN ABS(LOG(json_extract(s.meta_json, '$.s_t')
                      / json_extract(s.meta_json, '$.s_open')))
              / (json_extract(s.meta_json, '$.sigma_1s')
                 * SQRT(json_extract(s.meta_json, '$.tau_secs'))) > 5.0
         THEN '>5  WOULD BE VETOED' ELSE 'in band' END      AS z_gate,
    r.resolved_winner,
    CASE WHEN r.resolved_winner = f.side THEN 'WIN' ELSE 'LOSS' END AS result,
    ROUND(CASE WHEN r.resolved_winner = f.side
               THEN f.shares - f.cost_usd - f.fees_usd
               ELSE -(f.cost_usd + f.fees_usd) END, 2)      AS pnl
FROM fills f
JOIN resolutions r ON r.market_slug = f.market_slug
LEFT JOIN signals s ON s.id = f.signal_id
WHERE f.outcome = 'filled' AND f.strategy = 'close_snipe'
ORDER BY f.ts;
