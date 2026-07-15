#!/bin/bash
# OOS validation wave — frozen parameters from train
set -e
cd /home/user/S4
python3 scripts/backtest_1h.py --from-date 2026-05-01 --to-date 2026-07-12 --tag test
python3 scripts/backtest_close_snipe.py 5m --from-date 2026-04-16 --to-date 2026-07-07 --tag test --cl-signal
python3 scripts/backtest_close_snipe.py 15m --from-date 2026-04-01 --to-date 2026-07-07 --tag test
python3 scripts/backtest_close_snipe.py 4h --from-date 2026-04-01 --to-date 2026-07-07 --tag test
echo ALL_OOS_DONE
