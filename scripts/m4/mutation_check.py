#!/usr/bin/env python3
"""Prove the M4 guard tests are BEHAVIOURAL: delete/neuter each guard in the
source, run the suite, and require it to go red.

A previous audit of this project found a guard that could be removed entirely
with a green suite. This script is the standing check that it cannot happen
again. Every mutation is applied to a COPY of bot/ in a temp dir; the real tree
is never modified.

Usage:  python3 scripts/m4/mutation_check.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOT = ROOT / "bot"

# (label, relative file, old snippet, new snippet)
MUTATIONS = [
    ("G2 delete warmup gate from _maybe_snipe",
     "polybot/engine.py",
     "        if not wu.ready:\n            return\n",
     "        if False:\n            return\n"),

    ("G2 WarmupGate.check always ready",
     "polybot/risk.py",
     "        if samples < self.min_samples:",
     "        if False:"),

    ("G2 warmup uptime condition removed",
     "polybot/risk.py",
     "        if uptime < self.min_uptime_secs:",
     "        if False:"),

    ("G2 missing-n_samples oracle treated as warm",
     "polybot/risk.py",
     "            samples = int(getter(window_secs)) if callable(getter) else 0",
     "            samples = int(getter(window_secs)) if callable(getter) else 10**9"),

    ("G2 n_samples anchored on newest point instead of the clock",
     "polybot/oracle.py",
     "        now = time.time()\n        return sum(1 for p in self._series if now - p.ts <= window_secs)",
     "        now = self._series[-1].ts\n        return sum(1 for p in self._series if now - p.ts <= window_secs)"),

    ("G2 config.py fallback default weaker than the shipped config",
     "polybot/config.py",
     "        cfg.setdefault(\"min_uptime_secs\", 120)",
     "        cfg.setdefault(\"min_uptime_secs\", 1)"),

    ("G2 WarmupGate constructor default weaker than the shipped config",
     "polybot/risk.py",
     "        self.min_uptime_secs = float(cfg.get(\"min_uptime_secs\", 120))",
     "        self.min_uptime_secs = float(cfg.get(\"min_uptime_secs\", 0))"),

    ("G3 delete the pre-dispatch breaker re-check (resolution lands mid-window)",
     "polybot/engine.py",
     "        br = self.breaker.allow_new_position()\n        if br.tripped:\n            log.error(\"skipping fill for %s: circuit breaker tripped (%s)\",",
     "        br = self.breaker.allow_new_position()\n        if False:\n            log.error(\"skipping fill for %s: circuit breaker tripped (%s)\","),

    ("G3 delete breaker check from _maybe_snipe (pre-book)",
     "polybot/engine.py",
     "        br = self.breaker.allow_new_position()\n        if br.tripped:\n            if market.slug not in self._breaker_blocked_slugs:",
     "        br = self.breaker.allow_new_position()\n        if False:\n            if market.slug not in self._breaker_blocked_slugs:"),

    ("G3 delete breaker check from _maybe_settle",
     "polybot/engine.py",
     "        br = self.breaker.allow_new_position()\n        if br.tripped:\n            if market.slug not in self._breaker_blocked_slugs:\n                self._breaker_blocked_slugs.add(market.slug)\n                self.status.add_event(\"risk_block\", f\"settle_sweep {market.slug} blocked: \"",
     "        br = self.breaker.allow_new_position()\n        if False:\n            if market.slug not in self._breaker_blocked_slugs:\n                self._breaker_blocked_slugs.add(market.slug)\n                self.status.add_event(\"risk_block\", f\"settle_sweep {market.slug} blocked: \""),

    ("G3 daily loss limit never trips",
     "polybot/risk.py",
     "            if pnl_today <= floor:",
     "            if False:"),

    ("G3 consecutive-loss brake never trips",
     "polybot/risk.py",
     "            if eff_streak >= self.max_streak:",
     "            if False:"),

    ("G3 combine pct/usd limits the LOOSE way",
     "polybot/risk.py",
     "    return min(cands) if cands else None",
     "    return max(cands) if cands else None"),

    ("G3 override is not scoped to a UTC day",
     "polybot/risk.py",
     "        if rec.get(\"utc_day\") != utc_day_str(now):\n            return None",
     "        if False:\n            return None"),

    ("G3 corrupt override resumes trading",
     "polybot/risk.py",
     "            log.warning(\"risk override at %s is unreadable (%s) — ignoring it\",\n                        self.override_path, exc)\n            return None",
     "            log.warning(\"risk override at %s is unreadable (%s)\",\n                        self.override_path, exc)\n            return {\"utc_day\": utc_day_str(now), \"pnl_at_override\": 0.0}"),

    ("G3 streak does not reset at UTC midnight",
     "polybot/risk.py",
     "        streak = int(self.ledger.consecutive_losses(since_ts=utc_midnight(now)))",
     "        streak = int(self.ledger.consecutive_losses(since_ts=None))"),

    ("G3 pnl_today ignores the UTC-midnight boundary",
     "polybot/ledger.py",
     "        midnight = time.time() - (time.time() % 86400)",
     "        midnight = 0.0"),

    ("G3 consecutive_losses ignores since_ts",
     "polybot/ledger.py",
     "        if since_ts is not None:\n            sql += \" AND resolved_ts >= ?\"\n            params.append(float(since_ts))",
     "        if False:\n            sql += \" AND resolved_ts >= ?\"\n            params.append(float(since_ts))"),

    ("G3 a winning trade does not reset the streak",
     "polybot/ledger.py",
     "            if pnl < 0:\n                n += 1\n            else:\n                break",
     "            if pnl < 0:\n                n += 1"),

    ("G1 adverse-size filter removed from walk_asks",
     "polybot/fill_engine.py",
     "        if filter_on and lvl.size > max_level_shares:",
     "        if False:"),

    ("G1 skip mode re-baselines the walk bound",
     "polybot/fill_engine.py",
     "        if best_price is None:\n            best_price = lvl.price\n        elif max_above_best is not None",
     "        if best_price is None and not (filter_on and lvl.size > max_level_shares):\n            best_price = lvl.price\n        elif best_price is not None and max_above_best is not None"),

    ("G1 depth reference pools out-of-band levels",
     "polybot/depth.py",
     "        vals = [float(lv.size) for lv in levels\n                if lv is not None and lv.size and lv.size > 0\n                and price_min < float(lv.price) < price_max]",
     "        vals = [float(lv.size) for lv in levels\n                if lv is not None and lv.size and lv.size > 0]"),

    ("G1 reference returned before min_samples",
     "polybot/depth.py",
     "            if not dq or len(dq) < self.min_samples:\n                return None",
     "            if not dq:\n                return None"),

    ("G1 filter not threaded into the live order path",
     "polybot/execution.py",
     "                                           max_level_shares=max_level_shares,\n                                           anomalous_mode=anomalous_mode)",
     "                                           )"),

    ("G1 engine never computes a level ceiling",
     "polybot/engine.py",
     "        mls = max_level_shares(self._adverse_cfg, self.depth, market.family)",
     "        mls = None"),

    ("G1/G2/G3 guards missing from status.json",
     "polybot/status_server.py",
     "            \"guards\": {",
     "            \"_guards\": {"),
]


def run_suite(bot_dir: Path):
    return subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "--no-header",
                           "-x", "--tb=no"],
                          cwd=str(bot_dir), capture_output=True, text=True)


def main() -> int:
    base = run_suite(BOT)
    if base.returncode != 0:
        print("BASELINE SUITE IS ALREADY RED — fix that first")
        print(base.stdout[-3000:])
        return 2
    print(f"baseline: GREEN  ({base.stdout.strip().splitlines()[-1]})\n")

    survived, killed = [], []
    for label, rel, old, new in MUTATIONS:
        with tempfile.TemporaryDirectory() as td:
            dst = Path(td) / "bot"
            shutil.copytree(BOT, dst, ignore=shutil.ignore_patterns(
                "data", ".pytest_cache", "__pycache__", "*.pyc"))
            target = dst / rel
            src = target.read_text()
            if old not in src:
                print(f"  !! SKIP (anchor not found): {label}  [{rel}]")
                survived.append(label + "  (anchor missing)")
                continue
            target.write_text(src.replace(old, new, 1))
            res = run_suite(dst)
            tail = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else "?"
            if res.returncode == 0:
                print(f"  SURVIVED  {label}   <-- guard is NOT covered")
                survived.append(label)
            else:
                first = [ln for ln in res.stdout.splitlines() if ln.startswith("FAILED")]
                print(f"  killed    {label}   ({first[0] if first else tail})")
                killed.append(label)

    print(f"\n{len(killed)}/{len(MUTATIONS)} mutations killed by the suite")
    if survived:
        print("SURVIVORS (each is an untested guard):")
        for s in survived:
            print("  - " + s)
        return 1
    print("All guard mutations are caught by the test suite.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
