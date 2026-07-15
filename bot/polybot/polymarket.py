"""Polymarket market discovery (gamma) + order book access (CLOB REST)."""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from .config import Config
from .logging_setup import get_logger
from .net import get_session

log = get_logger("polymarket")

ET = ZoneInfo("America/New_York")

# Slug patterns per family. Hourly requires an explicit hour+am/pm suffix so we
# don't accidentally match unrelated "bitcoin-up-or-down-on-<date>" daily markets.
_HOURLY_RE = re.compile(r"^bitcoin-up-or-down-[a-z]+-\d{1,2}-\d{4}-\d{1,2}(am|pm)-et$")
_SHORT_RE = re.compile(r"^btc-updown-(5m|15m|4h)-(\d+)$")

_SHORT_DURATION = {"5m": 300, "15m": 900, "4h": 14400}
_DURATION_SECS = {"1h": 3600, **_SHORT_DURATION}


@dataclass
class Market:
    slug: str
    family: str                # "1h", "15m", "5m", "4h"
    question: str
    condition_id: str
    up_token_id: str
    down_token_id: str
    start_date: dt.datetime
    end_date: dt.datetime       # window close = price-measurement time
    accepting_orders: bool
    closed: bool
    active: bool
    enable_order_book: bool
    order_min_size: float
    tick_size: float
    raw: dict

    @property
    def window_start_ts(self) -> int:
        return int(self.start_date.timestamp())

    @property
    def close_ts(self) -> int:
        return int(self.end_date.timestamp())


def _parse_market(m: dict) -> Optional[Market]:
    slug = m.get("slug", "")
    if _HOURLY_RE.match(slug):
        family = "1h"
    else:
        sm = _SHORT_RE.match(slug)
        if sm:
            family = sm.group(1)
        else:
            return None
    try:
        token_ids = json.loads(m["clobTokenIds"])
        end_date = dt.datetime.fromisoformat(m["endDate"].replace("Z", "+00:00"))
        start_date = dt.datetime.fromisoformat(m["startDate"].replace("Z", "+00:00"))
    except Exception as exc:  # noqa: BLE001
        log.warning("failed to parse market %s: %s", slug, exc)
        return None
    if len(token_ids) != 2:
        return None
    # IMPORTANT: gamma's `startDate` field is NOT the pricing-window open —
    # for hourly markets it is ~2 days before `endDate` (market listing
    # time). The true window start for every family is always
    # `endDate - duration`; for short families that also matches the unix
    # timestamp embedded in the slug (cross-checked below as a sanity check).
    duration = _DURATION_SECS[family]
    derived_start = end_date - dt.timedelta(seconds=duration)
    if family in _SHORT_DURATION:
        sm = _SHORT_RE.match(slug)
        window_start_unix = int(sm.group(2))
        slug_start = dt.datetime.fromtimestamp(window_start_unix, tz=dt.timezone.utc)
        if abs((slug_start - derived_start).total_seconds()) > 1:
            log.warning("slug window_start %s != endDate-duration %s for %s", slug_start,
                        derived_start, slug)
        start_date = slug_start
    else:
        start_date = derived_start
    return Market(
        slug=slug,
        family=family,
        question=m.get("question", ""),
        condition_id=m.get("conditionId", ""),
        up_token_id=token_ids[0],
        down_token_id=token_ids[1],
        start_date=start_date,
        end_date=end_date,
        accepting_orders=bool(m.get("acceptingOrders", False)),
        closed=bool(m.get("closed", False)),
        active=bool(m.get("active", False)),
        enable_order_book=bool(m.get("enableOrderBook", False)),
        order_min_size=float(m.get("orderMinSize", 5)),
        tick_size=float(m.get("orderPriceMinTickSize", 0.01)),
        raw=m,
    )


def _hourly_slug(et_dt: dt.datetime) -> str:
    hour12 = et_dt.hour % 12
    if hour12 == 0:
        hour12 = 12
    ampm = "am" if et_dt.hour < 12 else "pm"
    month = et_dt.strftime("%B").lower()
    return f"bitcoin-up-or-down-{month}-{et_dt.day}-{et_dt.year}-{hour12}{ampm}-et"


class GammaClient:
    def __init__(self, config: Config):
        self.config = config
        self.session = get_session()
        self.base = config.gamma_base

    def get_market_by_slug(self, slug: str, closed: Optional[bool] = None) -> Optional[dict]:
        """Look up a market by slug.

        IMPORTANT (live-verified quirk): when the `closed` query param is
        omitted, gamma behaves as if `closed=false` were passed — a market
        that has already resolved returns an EMPTY list unless you pass
        `closed=true` explicitly. Discovery (open markets) should leave
        `closed=None` (omitted); resolution polling (ledger.py) must pass
        `closed=True` once the window's close time has passed.
        """
        params = {"slug": slug}
        if closed is not None:
            params["closed"] = str(closed).lower()
        try:
            r = self.session.get(f"{self.base}/markets", params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("gamma slug lookup failed for %s: %s", slug, exc)
            return None
        return data[0] if data else None

    def get_markets_page(self, closed: bool, limit: int, offset: int, order: str,
                          ascending: bool) -> List[dict]:
        try:
            r = self.session.get(
                f"{self.base}/markets",
                params={
                    "closed": str(closed).lower(),
                    "limit": limit,
                    "offset": offset,
                    "order": order,
                    "ascending": str(ascending).lower(),
                },
                timeout=15,
            )
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("gamma markets page failed (offset=%s): %s", offset, exc)
            return []

    def get_events_page(self, closed: bool, limit: int, offset: int, order: str,
                         ascending: bool) -> List[dict]:
        try:
            r = self.session.get(
                f"{self.base}/events",
                params={
                    "closed": str(closed).lower(),
                    "limit": limit,
                    "offset": offset,
                    "order": order,
                    "ascending": str(ascending).lower(),
                },
                timeout=15,
            )
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("gamma events page failed (offset=%s): %s", offset, exc)
            return []

    def market_by_condition_or_slug(self, slug: str) -> Optional[dict]:
        return self.get_market_by_slug(slug)


def discover_markets(gamma: GammaClient, config: Config) -> Dict[str, Market]:
    """Discover currently-relevant BTC Up/Down markets across all enabled families.

    Two complementary strategies, merged and deduplicated by slug:
      1. Direct slug construction (authoritative, low-latency): compute the
         current + next few windows for every family and look them up
         individually. This is the primary source — deterministic and fast.
      2. Broad scan of gamma /events (closed=false, order=createdAt desc),
         pattern filtered. Catches anything the deterministic construction
         missed (clock skew, family edge cases) at the cost of more requests.

    NOTE on `/markets?closed=false&order=endDate&ascending=false` (also
    named in the original brief as a discovery source): live-tested and
    NOT used here — it surfaces long-dated markets (observed: 2028/2029
    yearly prediction markets) that dominate that sort order, returning
    zero BTC Up/Down matches even after scanning 500 offset. `GammaClient
    .get_markets_page` is kept available for ad hoc use, but discovery
    relies on slug construction + the /events scan above, which were
    verified live to reliably surface all current BTC markets.
    """
    families = config.families()
    disc_cfg = config.discovery_cfg
    found: Dict[str, Market] = {}

    # --- 1. deterministic slug construction ---------------------------------
    now_utc = dt.datetime.now(dt.timezone.utc)

    if families.get("1h") and families["1h"].enabled:
        lookahead = int(disc_cfg.get("hourly_lookahead_hours", 3))
        et_now = now_utc.astimezone(ET)
        et_hour = et_now.replace(minute=0, second=0, microsecond=0)
        for i in range(-1, lookahead + 1):
            slug = _hourly_slug(et_hour + dt.timedelta(hours=i))
            raw = gamma.get_market_by_slug(slug)
            if raw:
                mkt = _parse_market(raw)
                if mkt:
                    found[mkt.slug] = mkt

    for fam_name, dur in _SHORT_DURATION.items():
        fc = families.get(fam_name)
        if not fc or not fc.enabled:
            continue
        now_ts = int(now_utc.timestamp())
        cur_window_start = (now_ts // dur) * dur
        for i in range(-1, 3):
            ws = cur_window_start + i * dur
            slug = f"btc-updown-{fam_name}-{ws}"
            raw = gamma.get_market_by_slug(slug)
            if raw:
                mkt = _parse_market(raw)
                if mkt:
                    found[mkt.slug] = mkt

    # --- 2. broad scan fallback ----------------------------------------------
    events_limit = int(disc_cfg.get("events_limit", 200))
    for offset in range(0, events_limit, 100):
        page = gamma.get_events_page(closed=False, limit=100, offset=offset,
                                      order="createdAt", ascending=False)
        if not page:
            break
        for ev in page:
            for m in ev.get("markets", []) or []:
                # event-embedded markets sometimes omit fields present on the
                # full /markets record; merge what's needed.
                slug = m.get("slug") or ev.get("slug", "")
                if not (_HOURLY_RE.match(slug) or _SHORT_RE.match(slug)):
                    continue
                if slug in found:
                    continue
                m.setdefault("slug", slug)
                mkt = _parse_market(m)
                if mkt:
                    found[mkt.slug] = mkt
        if len(page) < 100:
            break

    return found


class ClobClientREST:
    """Minimal REST client for order book / midpoint (no auth needed for reads)."""

    def __init__(self, config: Config):
        self.session = get_session()
        self.base = config.clob_base

    def get_book(self, token_id: str) -> Optional["OrderBook"]:
        try:
            r = self.session.get(f"{self.base}/book", params={"token_id": token_id}, timeout=5)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("book fetch failed for %s: %s", token_id, exc)
            return None
        return OrderBook.from_raw(token_id, data)

    def get_midpoint(self, token_id: str) -> Optional[float]:
        try:
            r = self.session.get(f"{self.base}/midpoint", params={"token_id": token_id}, timeout=5)
            r.raise_for_status()
            return float(r.json()["mid"])
        except Exception as exc:  # noqa: BLE001
            log.warning("midpoint fetch failed for %s: %s", token_id, exc)
            return None


@dataclass
class BookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: List[BookLevel]  # sorted descending by price (best bid first)
    asks: List[BookLevel]  # sorted ascending by price (best ask first)
    fetched_at: float

    @classmethod
    def from_raw(cls, token_id: str, data: dict) -> "OrderBook":
        import time
        bids = sorted(
            (BookLevel(float(l["price"]), float(l["size"])) for l in data.get("bids", [])),
            key=lambda l: l.price, reverse=True,
        )
        asks = sorted(
            (BookLevel(float(l["price"]), float(l["size"])) for l in data.get("asks", [])),
            key=lambda l: l.price,
        )
        return cls(token_id=token_id, bids=bids, asks=asks, fetched_at=time.time())

    @property
    def best_ask(self) -> Optional[BookLevel]:
        return self.asks[0] if self.asks else None

    @property
    def best_bid(self) -> Optional[BookLevel]:
        return self.bids[0] if self.bids else None
