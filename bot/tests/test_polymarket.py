import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polybot.polymarket import _HOURLY_RE, _SHORT_RE, _parse_market


def _raw_market(slug, end_date, start_date, up="UPID", down="DOWNID"):
    return {
        "slug": slug,
        "question": "Bitcoin Up or Down",
        "conditionId": "0xabc",
        "clobTokenIds": json.dumps([up, down]),
        "outcomes": '["Up","Down"]',
        "endDate": end_date,
        "startDate": start_date,
        "acceptingOrders": True,
        "closed": False,
        "active": True,
        "enableOrderBook": True,
        "orderMinSize": 5,
        "orderPriceMinTickSize": 0.01,
    }


class TestSlugPatterns(unittest.TestCase):
    def test_hourly_matches(self):
        self.assertTrue(_HOURLY_RE.match("bitcoin-up-or-down-july-15-2026-3pm-et"))
        self.assertTrue(_HOURLY_RE.match("bitcoin-up-or-down-january-1-2027-12am-et"))

    def test_hourly_does_not_match_daily_variant(self):
        # this slug pattern was observed live on gamma and must NOT be treated as hourly
        self.assertIsNone(_HOURLY_RE.match("bitcoin-up-or-down-on-july-17-2026"))

    def test_short_family_matches(self):
        self.assertTrue(_SHORT_RE.match("btc-updown-5m-1784137500"))
        self.assertTrue(_SHORT_RE.match("btc-updown-15m-1784137500"))
        self.assertTrue(_SHORT_RE.match("btc-updown-4h-1784131200"))

    def test_short_family_rejects_other_symbols(self):
        # bnb/eth markets use the same scheme with a different symbol prefix elsewhere;
        # our pattern is anchored to "btc-updown-" specifically.
        self.assertIsNone(_SHORT_RE.match("bnb-updown-5m-1784137500"))


class TestParseMarket(unittest.TestCase):
    def test_hourly_window_start_derived_from_end_minus_duration_not_startDate(self):
        # LIVE-VERIFIED QUIRK: gamma's startDate for hourly markets is ~2 days before
        # endDate (market listing time), not the candle open. window_start must be
        # end_date - 3600s, ignoring the raw startDate field.
        raw = _raw_market(
            "bitcoin-up-or-down-july-15-2026-3pm-et",
            end_date="2026-07-15T20:00:00Z",
            start_date="2026-07-13T19:00:12Z",  # ~2 days earlier, as observed live
        )
        m = _parse_market(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.family, "1h")
        self.assertEqual(m.window_start_ts, m.close_ts - 3600)
        self.assertEqual(m.start_date.isoformat(), "2026-07-15T19:00:00+00:00")

    def test_short_family_window_start_from_slug(self):
        # slug embeds window_start_ts=1784137500 = 2026-07-15T17:45:00Z; a 5m window
        # closes 300s later at 17:50:00Z (real values, cross-checked against the live API).
        raw = _raw_market("btc-updown-5m-1784137500", end_date="2026-07-15T17:50:00Z",
                           start_date="2026-07-15T17:45:00Z")
        m = _parse_market(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.family, "5m")
        self.assertEqual(m.window_start_ts, 1784137500)
        self.assertEqual(m.close_ts - m.window_start_ts, 300)

    def test_token_mapping_up_is_index_0(self):
        raw = _raw_market("btc-updown-5m-1784137500", "2026-07-15T17:50:00Z",
                           "2026-07-15T17:45:00Z", up="UPTOKEN", down="DOWNTOKEN")
        m = _parse_market(raw)
        self.assertEqual(m.up_token_id, "UPTOKEN")
        self.assertEqual(m.down_token_id, "DOWNTOKEN")

    def test_non_matching_slug_returns_none(self):
        raw = _raw_market("some-other-market-slug", "2026-07-15T17:55:00Z",
                           "2026-07-15T17:50:00Z")
        self.assertIsNone(_parse_market(raw))

    def test_malformed_clob_token_ids_returns_none(self):
        raw = _raw_market("btc-updown-5m-1784137500", "2026-07-15T17:50:00Z",
                           "2026-07-15T17:45:00Z")
        raw["clobTokenIds"] = "not json"
        self.assertIsNone(_parse_market(raw))


if __name__ == "__main__":
    unittest.main()
