#!/usr/bin/env bash
# M1 step 3: keep sampling live CLOB books across consecutive hourly closes.
# Sleeps until LEAD seconds before the next hourly close, then samples all 7
# coins through the close.  One long-running loop covers many closes.
set -u
OUT=${OUT:-/home/user/S4/data/multicoin/books}
PASSES=${PASSES:-8}
LEAD=${LEAD:-420}          # start sampling this many seconds before the close
for i in $(seq 1 "$PASSES"); do
  now=$(date -u +%s)
  nxt=$(( (now/3600 + 1) * 3600 ))
  wait=$(( nxt - LEAD - now ))
  if [ "$wait" -gt 0 ]; then
    echo "=== pass $i/$PASSES: sleeping ${wait}s; close $(date -u -d @$nxt +%FT%TZ) ==="
    sleep "$wait"
  fi
  echo "=== pass $i/$PASSES sampling $(date -u +%FT%TZ) ==="
  python3 /home/user/S4/scripts/multicoin/sample_books.py \
      --minutes 10 --horizon $((LEAD + 120)) --fast-window 150 --fast 1 --slow 20 \
      --post-close 15 --out "$OUT" || echo "pass $i failed"
done
