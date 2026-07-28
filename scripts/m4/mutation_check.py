#!/usr/bin/env python3
"""Prove the M4 guard tests are BEHAVIOURAL: delete/neuter each guard in the
source, run the suite, and require it to go red.

A previous audit of this project found a guard that could be removed entirely
with a green suite. This script is the standing check that it cannot happen
again. Every mutation is applied to a COPY of bot/ in a temp dir; the real tree
is never modified.

Usage:  python3 scripts/m4/mutation_check.py            # all mutations
        python3 scripts/m4/mutation_check.py M5 V8      # only labels matching
                                                        # any of these substrings
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

    # Anchor re-pinned in M5: `max_book_age_s=max_book_age_s` was appended to
    # this call, which moved the old anchor's closing paren.
    ("G1 filter not threaded into the live order path",
     "polybot/execution.py",
     "                                           max_level_shares=max_level_shares,\n                                           anomalous_mode=anomalous_mode,\n                                           max_book_age_s=max_book_age_s)",
     "                                           max_book_age_s=max_book_age_s)"),

    ("G1 engine never computes a level ceiling",
     "polybot/engine.py",
     "        mls = max_level_shares(self._adverse_cfg, self.depth, market.family)",
     "        mls = None"),

    ("G1/G2/G3 guards missing from status.json",
     "polybot/status_server.py",
     "            \"guards\": {",
     "            \"_guards\": {"),

    # -----------------------------------------------------------------------
    # The 8 mutations the independent verifier wrote that SURVIVED a green
    # 214-test suite (verifier §M4). Five were in Config.risk_cfg, two in
    # risk.py's shadowed breaker defaults, one in the warmup uptime threshold.
    # Each silently disables or loosens a shipped guard for exactly the
    # pre-M4 config.yaml that audit/M4_risk_guards.md §4 promises is safe.
    # -----------------------------------------------------------------------
    ("V1 risk_cfg default: daily loss limit OFF for a pre-M4 config",
     "polybot/config.py",
     "        daily.setdefault(\"enabled\", True)",
     "        daily.setdefault(\"enabled\", False)"),

    ("V2 risk_cfg default: consecutive-loss brake OFF for a pre-M4 config",
     "polybot/config.py",
     "        streak.setdefault(\"enabled\", True)",
     "        streak.setdefault(\"enabled\", False)"),

    ("V3 risk_cfg default: daily loss pct 10x looser",
     "polybot/config.py",
     "        daily.setdefault(\"max_daily_loss_pct\", 8.0)",
     "        daily.setdefault(\"max_daily_loss_pct\", 80.0)"),

    ("V4 risk_cfg default: consecutive-loss brake 10x looser",
     "polybot/config.py",
     "        streak.setdefault(\"max_consecutive_losses\", 4)",
     "        streak.setdefault(\"max_consecutive_losses\", 40)"),

    ("V5 risk_cfg default: bankroll 10x, so the daily $ limit is 10x",
     "polybot/config.py",
     "        daily.setdefault(\"bankroll_usd\", 1250)",
     "        daily.setdefault(\"bankroll_usd\", 12500)"),

    ("V6 CircuitBreaker constructor default: daily limit OFF",
     "polybot/risk.py",
     "        self.daily_enabled = bool(daily.get(\"enabled\", True))",
     "        self.daily_enabled = bool(daily.get(\"enabled\", False))"),

    ("V7 CircuitBreaker constructor default: streak brake OFF",
     "polybot/risk.py",
     "        self.streak_enabled = bool(streak.get(\"enabled\", True))",
     "        self.streak_enabled = bool(streak.get(\"enabled\", False))"),

    ("V8 warmup uptime threshold silently HALVED (120s -> 60s effective)",
     "polybot/risk.py",
     "        if uptime < self.min_uptime_secs:",
     "        if uptime < self.min_uptime_secs / 2.0:"),

    # Second-line breaker defaults added in M5 in response to V6/V7.
    ("V9 CircuitBreaker second-line bankroll default removed",
     "polybot/risk.py",
     "            daily[\"bankroll_usd\"] = DEFAULT_BANKROLL_USD",
     "            daily[\"bankroll_usd\"] = None"),

    ("V10 CircuitBreaker second-line streak default 10x looser",
     "polybot/risk.py",
     "                                          DEFAULT_MAX_CONSECUTIVE_LOSSES))",
     "                                          10 * DEFAULT_MAX_CONSECUTIVE_LOSSES))"),

    # -----------------------------------------------------------------------
    # M5 multi-coin guards.
    # -----------------------------------------------------------------------
    ("M5 coin allowlist deleted (any discovered coin may fill)",
     "polybot/engine.py",
     "        if not (may_fill or is_shadow):",
     "        if False:"),

    ("M5 coin allowlist widened to every coin",
     "polybot/config.py",
     "        coins = self.snipe_cfg.get(\"allowed_coins\")\n        if not coins:\n            return [\"bitcoin\"]",
     "        coins = self.snipe_cfg.get(\"allowed_coins\")\n        if True:\n            return [\"bitcoin\", \"ethereum\", \"solana\", \"xrp\", \"dogecoin\", \"bnb\", \"hype\"]"),

    ("M5 shadow gate removed (shadow coins can fill)",
     "polybot/engine.py",
     "        if not may_fill:\n            self.status.add_event(\"shadow_signal\",",
     "        if False:\n            self.status.add_event(\"shadow_signal\","),

    ("M5 shadow list ignored, so a shadow coin reads as fillable",
     "polybot/engine.py",
     "        if is_shadow:\n            may_fill = False",
     "        if False:\n            may_fill = False"),

    ("M5 oracle routing falls back to BTC for an unknown coin",
     "polybot/engine.py",
     "        if not coin:\n            return None\n        return self.coin_oracles.get(coin)",
     "        if not coin:\n            return self.binance\n        return self.coin_oracles.get(coin, self.binance)"),

    ("M5 _snipe_inputs ignores a missing oracle and uses self.binance",
     "polybot/engine.py",
     "            oracle = self._oracle_for(market)\n            if oracle is None:",
     "            oracle = self._oracle_for(market) or self.binance\n            if oracle is None:"),

    ("M5 HYPE silently rewired from futures to spot",
     "polybot/oracle.py",
     "    \"hype\":     (\"HYPEUSDT\", _USDM_FUTURES),",
     "    \"hype\":     (\"HYPEUSDT\", _SPOT),"),

    ("M5 every coin priced off BTCUSDT",
     "polybot/oracle.py",
     "    \"ethereum\": (\"ETHUSDT\",  _SPOT),",
     "    \"ethereum\": (\"BTCUSDT\",  _SPOT),"),

    ("M5 futures venue silently uses the spot base URL",
     "polybot/oracle.py",
     "        self._base = (config.binance_rest_base if venue == _SPOT\n                      else config.binance_futures_rest_base)",
     "        self._base = config.binance_rest_base"),

    ("M5 stale-book guard removed from the fill path",
     "polybot/fill_engine.py",
     "    if book_is_stale(book, max_book_age_s, fill_time):",
     "    if False:"),

    ("M5 stale-book guard removed from the LIVE order path",
     "polybot/execution.py",
     "        if book_is_stale(book, max_book_age_s):",
     "        if False:"),

    ("M5 stale-book predicate always says fresh",
     "polybot/fill_engine.py",
     "    return max(0.0, age) > float(max_book_age_s)",
     "    return False"),

    # NOT a mutation: `max(0.0, age) > L` and `age > L` are IDENTICAL for every
    # real `age` whenever L > 0, and the function returns early when L <= 0. The
    # clamp is therefore a no-op, and a mutation removing it can never be
    # killed — which the checker duly reported as a SURVIVOR on its first run.
    # The behaviour that actually protects anything is the skew WARNING, so that
    # is what gets mutated instead. Recorded here rather than deleted silently,
    # because "we removed the mutation" and "we tested the guard" are different
    # sentences.
    ("M5 clock-skew warning never fires (operator cannot see a bad clock)",
     "polybot/fill_engine.py",
     "    if age < -_SKEW_WARN_SECS:",
     "    if False:"),

    ("M5 stale-book guard removed from the decision point",
     "polybot/engine.py",
     "        stale_age = self._book_too_stale(book_at_signal)\n        if stale_age is not None:",
     "        stale_age = self._book_too_stale(book_at_signal)\n        if False:"),

    ("M5 config default: max_book_age_s ignored entirely",
     "polybot/config.py",
     "        v = self.snipe_cfg.get(\"max_book_age_s\")",
     "        v = None; self.snipe_cfg.get(\"max_book_age_s\")"),

    ("M5 batch fetch swallows missing tokens as empty books",
     "polybot/engine.py",
     "        for tid in token_ids:\n            if out.get(tid) is None:\n                out[tid] = self.clob.get_book(tid)",
     "        pass"),

    ("M5 per-coin cap ignored, every coin gets the BTC clip",
     "polybot/config.py",
     "        if coin in per_coin:\n            return float(per_coin[coin])",
     "        if False:\n            return float(per_coin[coin])"),

    ("M5 one dead feed aborts the whole oracle poll cycle",
     "polybot/engine.py",
     "            except Exception:  # noqa: BLE001 - one bad feed must not stop the others\n                log.exception(\"oracle poll failed for %s\", coin)\n                return coin, None",
     "            except Exception:  # noqa: BLE001\n                raise"),
]


def run_suite(bot_dir: Path):
    return subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "--no-header",
                           "-x", "--tb=no"],
                          cwd=str(bot_dir), capture_output=True, text=True)


def main() -> int:
    # Optional label filter. A partial run is clearly announced so its
    # "N/N killed" line can never be mistaken for a full pass.
    filters = [a for a in sys.argv[1:] if not a.startswith("-")]
    mutations = MUTATIONS
    if filters:
        mutations = [m for m in MUTATIONS if any(f in m[0] for f in filters)]
        print(f"PARTIAL RUN: {len(mutations)} of {len(MUTATIONS)} mutations match "
              f"{filters}\n")
        if not mutations:
            print("no mutations matched the filter")
            return 2

    base = run_suite(BOT)
    if base.returncode != 0:
        print("BASELINE SUITE IS ALREADY RED — fix that first")
        print(base.stdout[-3000:])
        return 2
    print(f"baseline: GREEN  ({base.stdout.strip().splitlines()[-1]})\n")

    survived, killed, stale = [], [], []
    for label, rel, old, new in mutations:
        with tempfile.TemporaryDirectory() as td:
            dst = Path(td) / "bot"
            shutil.copytree(BOT, dst, ignore=shutil.ignore_patterns(
                "data", ".pytest_cache", "__pycache__", "*.pyc"))
            target = dst / rel
            src = target.read_text()
            if old not in src:
                # A stale anchor is NOT a pass. It means this guard is silently
                # no longer being mutated at all — the exact rot this script
                # exists to prevent — so it is reported separately and fails
                # the run just as a survivor does.
                print(f"  !! STALE ANCHOR (mutation never applied): {label}  [{rel}]")
                stale.append(f"{label}  [{rel}]")
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

    print(f"\n{len(killed)}/{len(mutations)} mutations killed by the suite")
    rc = 0
    if stale:
        print(f"STALE ANCHORS ({len(stale)}) — these mutations did not run at all; "
              f"re-pin them against the current source:")
        for s in stale:
            print("  - " + s)
        rc = 1
    if survived:
        print(f"SURVIVORS ({len(survived)}) — each is an untested guard:")
        for s in survived:
            print("  - " + s)
        rc = 1
    if rc == 0:
        print(f"All {len(mutations)} guard mutations were applied and all were caught "
              f"by the test suite.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
