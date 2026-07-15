# Polymarket BTC Up/Down — Strategy Research + Trading Bot

Systematic search for executable edges on Polymarket's BTC Up/Down markets (5m/15m/1h/4h),
from raw data to out-of-sample-validated strategies — plus a production paper-trading bot
implementing the surviving strategies with a gated live-order path.

- **The bot (paper-trading now, live-capable): [`bot/README.md`](bot/README.md)**
- Executability re-audit (which strategy is actually tradeable): [`docs/04_executability_audit.md`](docs/04_executability_audit.md)
- Live-verified CLOB API spec: [`docs/05_clob_api_spec.md`](docs/05_clob_api_spec.md)

- **Final report (ranked strategies, OOS numbers, risks): [`docs/03_final_report.md`](docs/03_final_report.md)**
- Data audit: [`docs/01_data_audit.md`](docs/01_data_audit.md)
- Hypothesis battery (30, written before testing): [`docs/02_hypotheses.md`](docs/02_hypotheses.md)
- Every test, pass or fail: [`results/results_table.csv`](results/results_table.csv)
- Per-trade OOS records: `results/bt_*_test.csv`, summary `results/oos_summary.csv`

## Headline findings
1. **Settlement sweep** — after a window closes, the outcome is publicly determined (Binance 1H
   candle for the hourly family; visible Chainlink print for 5m/15m/4h) while stranded GTC orders
   keep trading. Buying the known winner below 0.98: +17 to +36¢/share, ~99% win rate OOS.
2. **Close-snipe** — in the final 6 seconds, fair value from the correct visible feed vs stale
   quotes: +7 to +14¢/share OOS across 5m/15m/1h, robust to 2–3s latency.
3. **Cross-family dominance arb** and a validated, tuned version of the **pre-open maker fade**.

## Setup
```
pip install boto3 pandas pyarrow numpy scikit-learn scipy python-dotenv requests telonex
# .env in repo root (never committed): DS_* B2 credentials, TELONEX_API_KEY
python scripts/sync_data.py            # pull the 13GB vault
python scripts/fetch_binance.py 2025-10-10 2026-07-13
python scripts/build_windows_all.py
```
