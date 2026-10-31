#!/usr/bin/env python3
"""The exchange layer every tool in this pack shares. Standard library only.

    python3 markets.py list                           # assets and where each one trades
    python3 markets.py quote GOLD --venue okx-perp    # price, spread, volume, funding
    python3 markets.py compare BTC                    # the same asset across every exchange
    python3 markets.py spec SOL --venue binance       # price step and quantity step

Public market data only: no account, no API key and no order endpoints. Every
request goes to a host in ALLOWED_HOSTS. An exchange that refuses your location
is reported and skipped; this pack never tries to get around a refusal.

Sessions. Crypto and commodity perpetuals trade around the clock, so a "day" is
24 hours from an anchor, 00:00 UTC unless you choose another (13:30 is the US
open, 08:00 is London). Commodity perpetuals track COMEX, NYMEX and ICE futures
but are not those contracts; underlying_status() says when the futures are shut.
"""

import argparse
import datetime
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

ALLOWED_HOSTS = {
    "data-api.binance.vision", "www.okx.com", "api.kraken.com", "futures.kraken.com",
    "api.exchange.coinbase.com", "api.kucoin.com", "api.gateio.ws", "api.hyperliquid.xyz",
}
USER_AGENT = "day-trader-skill-crypto-commodities/1.0"
PAUSE = 0.12          # seconds between pages: be polite to free endpoints
BACKOFF = 3.0         # seconds before the one retry after a 429
MAX_PAGES = 120
SLEEP = time.sleep    # tests swap this out
DAY = 86400

INTERVALS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}

SPOT_VENUES = ("binance", "okx", "coinbase", "kraken", "kucoin", "gate")
PERP_VENUES = ("okx-perp", "hyperliquid", "kraken-perp")

COMMODITIES = {
    "GOLD": {"okx-perp": "XAU-USDT-SWAP", "hyperliquid": "xyz:GOLD", "kraken-perp": "PF_XAUUSD"},
    "SILVER": {"okx-perp": "XAG-USDT-SWAP", "hyperliquid": "xyz:SILVER", "kraken-perp": "PF_XAGUSD"},
    "WTI": {"okx-perp": "CL-USDT-SWAP", "hyperliquid": "xyz:CL", "kraken-perp": "PF_WTIOILUSD"},
    "BRENT": {"okx-perp": "BZ-USDT-SWAP", "hyperliquid": "xyz:BRENTOIL", "kraken-perp": "PF_BRENTOILUSD"},
    "NATGAS": {"okx-perp": "NG-USDT-SWAP", "hyperliquid": "xyz:NATGAS"},
    "COPPER": {"okx-perp": "XCU-USDT-SWAP", "hyperliquid": "xyz:COPPER"},
    "PLATINUM": {"okx-perp": "XPT-USDT-SWAP", "hyperliquid": "xyz:PLATINUM"},
    "PALLADIUM": {"okx-perp": "XPD-USDT-SWAP", "hyperliquid": "xyz:PALLADIUM"},
}
ALIASES = {"XAU": "GOLD", "XAG": "SILVER", "CL": "WTI", "OIL": "WTI", "USOIL": "WTI", "CRUDE": "WTI",
           "BZ": "BRENT", "UKOIL": "BRENT", "NG": "NATGAS", "GAS": "NATGAS", "XCU": "COPPER", "HG": "COPPER",
           "XPT": "PLATINUM", "XPD": "PALLADIUM", "XBT": "BTC"}
STABLECOINS = {"USDT", "USDC", "FDUSD", "TUSD", "DAI", "USDP", "BUSD", "USD1", "USDE", "RLUSD", "PYUSD", "EUR",
               "EURI", "AEUR", "XUSD", "USDD", "GUSD", "BFUSD"}
TOKENIZED_METALS = {"PAXG", "XAUT"}

# Taker fee per side in percent, a typical entry-tier estimate. Fee tiers change, so set "fee_pct"
# in trader/config.json to what your account actually pays.
FEES = {"binance": 0.10, "okx": 0.10, "okx-perp": 0.05, "coinbase": 0.60, "kraken": 0.40, "kraken-perp": 0.05,
        "kucoin": 0.10, "gate": 0.10, "hyperliquid": 0.045}


class MarketError(Exception):
    """A plain-words failure the tools print as one line."""


class VenueRefused(MarketError):
    """The exchange refused this location (HTTP 403 or 451)."""


# ---------------------------------------------------------------- HTTP

def _urlopen(url, body=None):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, application/xml, text/xml, */*"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


TRANSPORT = _urlopen  # tests swap this out


def fetch(url, body=None, extra_hosts=()):
    """Bytes from an allowed host. One retry after a 429; a refusal is never retried."""
    host = urllib.parse.urlparse(url).hostname
    if host not in ALLOWED_HOSTS and host not in extra_hosts:
        raise MarketError("%s is not on this pack's list of allowed hosts" % host)
    for attempt in (0, 1):
        try:
            return TRANSPORT(url, body)
        except urllib.error.HTTPError as e:
            if e.code in (403, 451):
                raise VenueRefused("%s refused this location (HTTP %d)" % (host, e.code))
            if e.code == 429 and attempt == 0:
                SLEEP(BACKOFF)
                continue
            raise MarketError("%s answered HTTP %d" % (host, e.code))
        except (urllib.error.URLError, OSError) as e:
            raise MarketError("could not reach %s: %s" % (host, getattr(e, "reason", e)))
    raise MarketError("%s kept answering HTTP 429" % host)


def fetch_json(url, body=None):
    raw = fetch(url, body)
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise MarketError("%s did not send JSON" % urllib.parse.urlparse(url).hostname)


# ---------------------------------------------------------------- instruments

def canonical(asset):
    a = str(asset or "").strip().upper()
    for suffix in ("USDT", "USD"):
        if a.endswith(suffix) and len(a) > len(suffix):
            a = a[: -len(suffix)]
    a = a.replace("-", "").replace("/", "")
    a = ALIASES.get(a, a)
    if not a or not a.replace("1000", "").isalnum():
        raise MarketError("%r does not look like an asset; try BTC, SOL, GOLD or WTI" % asset)
    return a


def kind_of(asset):
    return "commodity" if canonical(asset) in COMMODITIES else "crypto"


def symbol_on(asset, venue):
    """The exchange's own symbol for an asset, or None if this pack does not map it there."""
    a = canonical(asset)
    if a in COMMODITIES:
        return COMMODITIES[a].get(venue)
    k = "XBT" if a == "BTC" else a
    return {
        "binance": a + "USDT", "okx": a + "-USDT", "okx-perp": a + "-USDT-SWAP", "coinbase": a + "-USD",
        "kraken": k + "USD", "kraken-perp": "PF_%sUSD" % k, "kucoin": a + "-USDT", "gate": a + "_USDT",
        "hyperliquid": a,
    }.get(venue)


def venues_for(asset):
    a = canonical(asset)
    if a in COMMODITIES:
        return [v for v in PERP_VENUES if v in COMMODITIES[a]]
    return list(SPOT_VENUES) + list(PERP_VENUES)


def instrument(asset, venue=None):
    """{asset, kind, venue, symbol, market, quote}. Without a venue: the asset's first venue."""
    a = canonical(asset)
    chosen = venue or venues_for(a)[0]
    if chosen not in SPOT_VENUES + PERP_VENUES:
        raise MarketError("unknown exchange %r; use one of: %s" % (chosen, ", ".join(SPOT_VENUES + PERP_VENUES)))
    sym = symbol_on(a, chosen)
    if not sym:
        raise MarketError("%s is not mapped on %s; use one of: %s" % (a, chosen, ", ".join(venues_for(a))))
    quote = "USD" if chosen in ("coinbase", "kraken", "kraken-perp", "hyperliquid") else "USDT"
    return {"asset": a, "kind": kind_of(a), "venue": chosen, "symbol": sym,
            "market": "perp" if chosen in PERP_VENUES else "spot", "quote": quote}


def first_working(asset, venue, attempt):
    """Runs attempt(instrument) on the given venue, or on the asset's venues in turn until one works."""
    if venue:
        return attempt(instrument(asset, venue))
    errors = []
    for v in venues_for(asset):
        try:
            return attempt(instrument(asset, v))
        except MarketError as e:
            errors.append("%s: %s" % (v, e))
    raise MarketError("no exchange had %s (%s)" % (canonical(asset), "; ".join(errors)))


# ---------------------------------------------------------------- candles

def _bar(t, o, h, l, c, v):
    return {"t": int(t), "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": float(v or 0)}


def _binance(symbol, iv, start, end):
    out, cursor = [], start
    for _ in range(MAX_PAGES):
        q = urllib.parse.urlencode({"symbol": symbol, "interval": iv, "limit": 1000,
                                    "startTime": cursor * 1000, "endTime": end * 1000})
        rows = fetch_json("https://data-api.binance.vision/api/v3/klines?" + q)
        out += [_bar(r[0] // 1000, r[1], r[2], r[3], r[4], r[5]) for r in rows]
        if len(rows) < 1000:
            break
        cursor = rows[-1][0] // 1000 + INTERVALS[iv]
        SLEEP(PAUSE)
    return out


OKX_BAR = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "4h": "4H", "1d": "1Dutc"}


def _okx(symbol, iv, start, end):
    def page(path, after, limit):
        q = {"instId": symbol, "bar": OKX_BAR[iv], "limit": limit}
        if after:
            q["after"] = after
        doc = fetch_json("https://www.okx.com/api/v5/market/%s?%s" % (path, urllib.parse.urlencode(q)))
        if doc.get("code") != "0":
            raise MarketError("OKX: %s" % (doc.get("msg") or "code %s" % doc.get("code")))
        return doc["data"]

    out, after = [], (end + INTERVALS[iv]) * 1000
    rows = page("candles", after, 300)
    for _ in range(MAX_PAGES):
        if not rows:
            break
        out += [_bar(int(r[0]) // 1000, r[1], r[2], r[3], r[4], r[5]) for r in rows]
        after = int(rows[-1][0])
        if after // 1000 <= start:
            break
        SLEEP(PAUSE)
        rows = page("history-candles", after, 100)
    return out


def _kraken(symbol, iv, start, end):
    q = urllib.parse.urlencode({"pair": symbol, "interval": INTERVALS[iv] // 60, "since": start - 1})
    doc = fetch_json("https://api.kraken.com/0/public/OHLC?" + q)
    if doc.get("error"):
        raise MarketError("Kraken: %s" % "; ".join(doc["error"]))
    res = doc["result"]
    rows = next((res[k] for k in res if k != "last"), [])
    return [_bar(r[0], r[1], r[2], r[3], r[4], r[6]) for r in rows]


def _coinbase(symbol, iv, start, end):
    step = INTERVALS[iv]
    if step not in (60, 300, 900, 3600, 21600, 86400):
        raise MarketError("Coinbase has no %s candles; use 1m, 5m, 15m, 1h or 1d" % iv)
    out, hi = [], end
    for _ in range(MAX_PAGES):
        lo = max(start, hi - 300 * step)
        q = urllib.parse.urlencode({"granularity": step, "start": _iso(lo), "end": _iso(hi)})
        rows = fetch_json("https://api.exchange.coinbase.com/products/%s/candles?%s" % (symbol, q))
        out += [_bar(r[0], r[3], r[2], r[1], r[4], r[5]) for r in rows]
        if lo <= start:
            break
        hi = lo - step
        SLEEP(PAUSE)
    return out


KUCOIN_TYPE = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1hour", "4h": "4hour", "1d": "1day"}


def _kucoin(symbol, iv, start, end):
    out, hi = [], end
    step = INTERVALS[iv]
    for _ in range(MAX_PAGES):
        lo = max(start, hi - 1400 * step)
        q = urllib.parse.urlencode({"type": KUCOIN_TYPE[iv], "symbol": symbol, "startAt": lo, "endAt": hi})
        doc = fetch_json("https://api.kucoin.com/api/v1/market/candles?" + q)
        if doc.get("code") != "200000":
            raise MarketError("KuCoin: %s" % (doc.get("msg") or doc.get("code")))
        out += [_bar(r[0], r[1], r[3], r[4], r[2], r[5]) for r in doc["data"]]
        if lo <= start or not doc["data"]:
            break
        hi = lo - step
        SLEEP(PAUSE)
    return out


def _gate(symbol, iv, start, end):
    out, hi = [], end
    step = INTERVALS[iv]
    for _ in range(MAX_PAGES):
        lo = max(start, hi - 990 * step)
        q = urllib.parse.urlencode({"currency_pair": symbol, "interval": iv, "from": lo, "to": hi})
        rows = fetch_json("https://api.gateio.ws/api/v4/spot/candlesticks?" + q)
        out += [_bar(r[0], r[5], r[3], r[4], r[2], r[6]) for r in rows]
        if lo <= start or not rows:
            break
        hi = lo - step
        SLEEP(PAUSE)
    return out


def _hyperliquid(symbol, iv, start, end):
    body = {"type": "candleSnapshot", "req": {"coin": symbol, "interval": iv, "startTime": start * 1000,
                                              "endTime": (end + INTERVALS[iv]) * 1000}}
    rows = fetch_json("https://api.hyperliquid.xyz/info", body)
    return [_bar(r["t"] // 1000, r["o"], r["h"], r["l"], r["c"], r["v"]) for r in rows]


def _kraken_perp(symbol, iv, start, end):
    out, cursor = [], start
    for _ in range(MAX_PAGES):
        q = urllib.parse.urlencode({"from": cursor, "to": end + INTERVALS[iv]})
        doc = fetch_json("https://futures.kraken.com/api/charts/v1/trade/%s/%s?%s" % (symbol, iv, q))
        rows = doc.get("candles") or []
        out += [_bar(r["time"] // 1000, r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows]
        if not doc.get("more_candles") or not rows:
            break
        cursor = rows[-1]["time"] // 1000 + INTERVALS[iv]
        SLEEP(PAUSE)
    return out


_CANDLES = {"binance": _binance, "okx": _okx, "okx-perp": _okx, "kraken": _kraken, "kraken-perp": _kraken_perp,
            "coinbase": _coinbase, "kucoin": _kucoin, "gate": _gate, "hyperliquid": _hyperliquid}


def candles(inst, interval, start, end=None, closed_only=True, now=None):
    """Bars oldest first, one per open time, from `start` (unix seconds). closed_only drops the forming bar."""
    if interval not in INTERVALS:
        raise MarketError("unknown interval %r; use %s" % (interval, ", ".join(INTERVALS)))
    now = int(now if now is not None else time.time())
    end = int(end if end is not None else now)
    step = INTERVALS[interval]
    rows = _CANDLES[inst["venue"]](inst["symbol"], interval, int(start), end)
    by_t = {b["t"]: b for b in rows if start <= b["t"] <= end and b["t"] % step == 0}
    out = [by_t[t] for t in sorted(by_t)]
    if closed_only:
        out = [b for b in out if b["t"] + step <= now]
    return out


# ---------------------------------------------------------------- quotes

def _hl_context(symbol):
    dex = symbol.split(":")[0] if ":" in symbol else None
    body = {"type": "metaAndAssetCtxs"}
    if dex:
        body["dex"] = dex
    meta, ctxs = fetch_json("https://api.hyperliquid.xyz/info", body)
    for spec_row, ctx in zip(meta["universe"], ctxs):
        if spec_row["name"] == symbol:
            return spec_row, ctx
    raise MarketError("Hyperliquid has no market %s" % symbol)


def quote(inst):
    """{last, bid, ask, volume_24h_usd, open_24h, funding_8h_pct}. Funding is None on spot."""
    venue, sym = inst["venue"], inst["symbol"]
    q = {"venue": venue, "symbol": sym, "asset": inst["asset"], "quote": inst["quote"], "funding_8h_pct": None,
         "open_24h": None}
    if venue == "binance":
        d = fetch_json("https://data-api.binance.vision/api/v3/ticker/24hr?symbol=" + sym)
        q.update(last=float(d["lastPrice"]), bid=float(d["bidPrice"]), ask=float(d["askPrice"]),
                 volume_24h_usd=float(d["quoteVolume"]), open_24h=float(d["openPrice"]))
    elif venue in ("okx", "okx-perp"):
        doc = fetch_json("https://www.okx.com/api/v5/market/ticker?instId=" + sym)
        if doc.get("code") != "0" or not doc.get("data"):
            raise MarketError("OKX has no market %s" % sym)
        d = doc["data"][0]
        vol = float(d["volCcy24h"]) * (float(d["last"]) if venue == "okx-perp" else 1.0)
        q.update(last=float(d["last"]), bid=float(d["bidPx"]), ask=float(d["askPx"]), volume_24h_usd=vol,
                 open_24h=float(d["open24h"]))
        if venue == "okx-perp":
            f = fetch_json("https://www.okx.com/api/v5/public/funding-rate?instId=" + sym)["data"][0]
            hours = (int(f["nextFundingTime"]) - int(f["fundingTime"])) / 3600000.0 or 8.0
            q["funding_8h_pct"] = float(f["fundingRate"]) * 100 * 8 / hours
    elif venue == "kraken":
        doc = fetch_json("https://api.kraken.com/0/public/Ticker?pair=" + sym)
        if doc.get("error"):
            raise MarketError("Kraken: %s" % "; ".join(doc["error"]))
        d = next(iter(doc["result"].values()))
        q.update(last=float(d["c"][0]), bid=float(d["b"][0]), ask=float(d["a"][0]),
                 volume_24h_usd=float(d["v"][1]) * float(d["p"][1]), open_24h=float(d["o"]))
    elif venue == "kraken-perp":
        doc = fetch_json("https://futures.kraken.com/derivatives/api/v3/tickers")
        d = next((t for t in doc.get("tickers", []) if t.get("symbol") == sym), None)
        if d is None:
            raise MarketError("Kraken Futures has no market %s" % sym)
        q.update(last=float(d["last"]), bid=float(d["bid"]), ask=float(d["ask"]),
                 volume_24h_usd=float(d["volumeQuote"]), open_24h=float(d["open24h"]))
        if d.get("relativeFundingRate") is not None:   # hourly, as a fraction of the price
            q["funding_8h_pct"] = float(d["relativeFundingRate"]) * 100 * 8
    elif venue == "coinbase":
        t = fetch_json("https://api.exchange.coinbase.com/products/%s/ticker" % sym)
        s = fetch_json("https://api.exchange.coinbase.com/products/%s/stats" % sym)
        q.update(last=float(t["price"]), bid=float(t["bid"]), ask=float(t["ask"]),
                 volume_24h_usd=float(s["volume"]) * float(t["price"]), open_24h=float(s["open"]))
    elif venue == "kucoin":
        doc = fetch_json("https://api.kucoin.com/api/v1/market/stats?symbol=" + sym)
        d = doc.get("data") or {}
        if not d.get("last"):
            raise MarketError("KuCoin has no market %s" % sym)
        change = float(d.get("changePrice") or 0)
        q.update(last=float(d["last"]), bid=float(d["buy"]), ask=float(d["sell"]),
                 volume_24h_usd=float(d["volValue"]), open_24h=float(d["last"]) - change)
    elif venue == "gate":
        d = fetch_json("https://api.gateio.ws/api/v4/spot/tickers?currency_pair=" + sym)[0]
        last, pct = float(d["last"]), float(d.get("change_percentage") or 0)
        q.update(last=last, bid=float(d["highest_bid"]), ask=float(d["lowest_ask"]),
                 volume_24h_usd=float(d["quote_volume"]), open_24h=last / (1 + pct / 100.0))
    elif venue == "hyperliquid":
        _, c = _hl_context(sym)
        book = fetch_json("https://api.hyperliquid.xyz/info", {"type": "l2Book", "coin": sym})
        bids, asks = book["levels"][0], book["levels"][1]
        last = float(c.get("midPx") or c["markPx"])
        q.update(last=last, bid=float(bids[0]["px"]) if bids else last, ask=float(asks[0]["px"]) if asks else last,
                 volume_24h_usd=float(c.get("dayNtlVlm") or 0), open_24h=float(c.get("prevDayPx") or 0) or None,
                 funding_8h_pct=float(c.get("funding") or 0) * 100 * 8)
    else:
        raise MarketError("unknown exchange %s" % venue)
    q["time"] = int(time.time())
    if q["last"] <= 0:
        raise MarketError("%s has no price for %s" % (venue, sym))
    return q


def spec(inst):
    """{tick, step, contract}: the smallest price move, the smallest quantity in the asset's own
    units, and what one contract is worth where the exchange trades contracts."""
    venue, sym = inst["venue"], inst["symbol"]
    if venue == "binance":
        s = fetch_json("https://data-api.binance.vision/api/v3/exchangeInfo?symbol=" + sym)["symbols"][0]
        f = {x["filterType"]: x for x in s["filters"]}
        return {"tick": float(f["PRICE_FILTER"]["tickSize"]), "step": float(f["LOT_SIZE"]["stepSize"]), "contract": None}
    if venue in ("okx", "okx-perp"):
        kind = "SWAP" if venue == "okx-perp" else "SPOT"
        doc = fetch_json("https://www.okx.com/api/v5/public/instruments?instType=%s&instId=%s" % (kind, sym))
        if doc.get("code") != "0" or not doc.get("data"):
            raise MarketError("OKX has no market %s" % sym)
        d = doc["data"][0]
        if venue == "okx":
            return {"tick": float(d["tickSz"]), "step": float(d["lotSz"]), "contract": None}
        contract = float(d["ctVal"])
        return {"tick": float(d["tickSz"]), "step": contract * float(d["lotSz"]), "contract": contract}
    if venue == "hyperliquid":
        row, c = _hl_context(sym)
        decimals = int(row["szDecimals"])
        return {"tick": _significant_tick(float(c["markPx"]), 6 - decimals), "step": 10.0 ** -decimals, "contract": None}
    if venue == "kraken-perp":
        rows = fetch_json("https://futures.kraken.com/derivatives/api/v3/instruments")["instruments"]
        d = next((r for r in rows if str(r.get("symbol", "")).upper() == sym.upper()), None)
        if d is None:
            raise MarketError("Kraken Futures has no market %s" % sym)
        return {"tick": float(d["tickSize"]), "step": 10.0 ** -int(d.get("contractValueTradePrecision", 4)), "contract": None}
    if venue == "kraken":
        res = fetch_json("https://api.kraken.com/0/public/AssetPairs?pair=" + sym).get("result") or {}
        if not res:
            raise MarketError("Kraken has no market %s" % sym)
        d = next(iter(res.values()))
        return {"tick": 10.0 ** -int(d["pair_decimals"]), "step": 10.0 ** -int(d["lot_decimals"]), "contract": None}
    if venue == "coinbase":
        d = fetch_json("https://api.exchange.coinbase.com/products/" + sym)
        return {"tick": float(d["quote_increment"]), "step": float(d["base_increment"]), "contract": None}
    if venue == "kucoin":
        rows = fetch_json("https://api.kucoin.com/api/v2/symbols").get("data") or []
        d = next((r for r in rows if r.get("symbol") == sym), None)
        if d is None:
            raise MarketError("KuCoin has no market %s" % sym)
        return {"tick": float(d["priceIncrement"]), "step": float(d["baseIncrement"]), "contract": None}
    if venue == "gate":
        d = fetch_json("https://api.gateio.ws/api/v4/spot/currency_pairs/" + sym)
        return {"tick": 10.0 ** -int(d["precision"]), "step": 10.0 ** -int(d["amount_precision"]), "contract": None}
    raise MarketError("unknown exchange %s" % venue)


def _significant_tick(price, max_decimals):
    """Hyperliquid prices carry at most 5 significant figures and max_decimals decimals."""
    if price <= 0:
        return 10.0 ** -max(max_decimals, 0)
    digits = len(str(int(price))) if price >= 1 else 0
    decimals = max(0, 5 - digits) if price >= 1 else max_decimals
    return 10.0 ** -min(decimals, max(max_decimals, 0))


def default_fee(venue):
    return FEES.get(venue, 0.1)


def universe(group="all", top=30):
    """Asset names to scan: every mapped commodity and the most traded crypto on a working exchange."""
    out = []
    if group in ("all", "crypto"):
        out += top_crypto(top)
    if group in ("all", "commodity"):
        out += sorted(COMMODITIES)
    if group not in ("all", "crypto", "commodity"):
        raise MarketError("--group is all, crypto or commodity")
    return out


def top_crypto(top=30):
    def keep(base):
        return base not in STABLECOINS and base not in TOKENIZED_METALS and not base.endswith(("UP", "DOWN", "BULL", "BEAR"))

    try:
        rows = fetch_json("https://data-api.binance.vision/api/v3/ticker/24hr")
        pairs = [(float(r["quoteVolume"]), r["symbol"][:-4]) for r in rows
                 if r["symbol"].endswith("USDT") and r.get("count", 1) and keep(r["symbol"][:-4])]
    except MarketError:
        doc = fetch_json("https://www.okx.com/api/v5/market/tickers?instType=SPOT")
        pairs = [(float(r["volCcy24h"]), r["instId"][:-5]) for r in doc["data"]
                 if r["instId"].endswith("-USDT") and keep(r["instId"][:-5])]
    pairs.sort(reverse=True)
    return [p[1] for p in pairs[:top]]


# ---------------------------------------------------------------- price and quantity rounding

def _dec(x):
    return Decimal(repr(float(x)))


def round_price(x, tick, direction="nearest"):
    """x on the exchange's price grid. direction: nearest, down or up."""
    if not tick or tick <= 0:
        return float(x)
    t = _dec(tick)
    n = _dec(x) / t
    if direction == "down":
        n = n.to_integral_value(rounding=ROUND_FLOOR)
    elif direction == "up":
        n = n.to_integral_value(rounding=ROUND_CEILING)
    else:
        n = n.to_integral_value()
    return float(n * t)


def floor_step(qty, step):
    if not step or step <= 0:
        return float(qty)
    s = _dec(step)
    return float((_dec(qty) / s).to_integral_value(rounding=ROUND_FLOOR) * s)


def fmt(x):
    """A price for people: enough digits for BTC at 77,000 and a token at 0.0000012."""
    if x is None:
        return "n/a"
    x = float(x)
    a = abs(x)
    if a >= 1000:
        return "%.2f" % x
    if a >= 1:
        return "%.4f" % x
    if a == 0:
        return "0"
    return "%.4g" % x


def fmt_usd(x):
    if x is None:
        return "n/a"
    x = float(x)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= limit:
            return "%.1f%s" % (x / limit, suffix)
    return "%.0f" % x


# ---------------------------------------------------------------- the clock and sessions

def now_ts(text=None):
    """Unix seconds. text pins the clock: 'YYYY-MM-DD HH:MM', in UTC."""
    if not text:
        return int(time.time())
    for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            d = datetime.datetime.strptime(str(text), pattern)
            return int(d.replace(tzinfo=datetime.timezone.utc).timestamp())
        except ValueError:
            continue
    raise MarketError("--now wants UTC like 2026-10-31 10:15, not %r" % text)


def utc(ts):
    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc)


def _iso(ts):
    return utc(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(ts):
    return utc(ts).strftime("%Y-%m-%d %H:%M UTC")


def clock(ts):
    return utc(ts).strftime("%H:%M")


def parse_anchor(text):
    """'13:30' -> seconds after 00:00 UTC. Empty means 00:00."""
    if text in (None, ""):
        return 0
    try:
        hh, mm = str(text).split(":")
        h, m = int(hh), int(mm)
    except ValueError:
        raise MarketError("the session start wants HH:MM in UTC, not %r" % text)
    if not (0 <= h < 24 and 0 <= m < 60) or m % 5:
        raise MarketError("the session start wants HH:MM in UTC on the 5-minute grid, not %r" % text)
    return h * 3600 + m * 60


def session_start(ts, anchor=0):
    return (int(ts) - anchor) // DAY * DAY + anchor


def session_date(ts, anchor=0):
    return utc(session_start(ts, anchor)).strftime("%Y-%m-%d")


def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7 + 7 * (n - 1))
    return d


def eastern_offset(ts):
    """US Eastern offset from UTC in seconds, for the futures hours below."""
    u = utc(ts).replace(tzinfo=None)
    begin = datetime.datetime.combine(_nth_sunday(u.year, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(u.year, 11, 1), datetime.time(6, 0))
    return -4 * 3600 if begin <= u < end else -5 * 3600


def underlying_status(asset, ts=None):
    """For a commodity: are the futures it tracks in their regular electronic hours? Globex-style hours,
    Sunday 18:00 to Friday 17:00 New York with a daily break from 17:00 to 18:00. Holidays are not
    included, and ICE Brent differs slightly. None for crypto."""
    if kind_of(asset) != "commodity":
        return None
    ts = int(ts if ts is not None else time.time())
    local = utc(ts + eastern_offset(ts))
    wd, hour = local.weekday(), local.hour
    if wd == 5 or (wd == 6 and hour < 18) or (wd == 4 and hour >= 17):
        return {"open": False, "why": "the futures it tracks are shut for the weekend"}
    if hour == 17:
        return {"open": False, "why": "the futures it tracks are in their daily break (17:00 to 18:00 New York)"}
    return {"open": True, "why": ""}


# ---------------------------------------------------------------- files

def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except ValueError as e:
        raise MarketError("%s is not valid JSON: %s" % (path, e))


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


CONFIG_DEFAULTS = {
    "budget": None,                  # your trading balance, in the quote currency (USD or USDT)
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order": 4000,
    "min_order": 5,
    "max_loss_per_trade": 50,
    "max_daily_loss": 150,
    "max_trades_per_day": 3,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "max_leverage": 1.0,             # 1 means no borrowing, even on a perpetual
    "allow_short": False,
    "fee_pct": None,                 # per side; None uses the pack's estimate for the exchange
    "max_spread_share_of_risk": 0.10,
    "min_volume_24h": 1000000,
    "max_funding_8h_pct": 0.10,
    "max_entry_drift_share_of_risk": 0.5,
    "block_when_underlying_closed": False,
    "hold_hours": 8,
    "session_start": "00:00",
    "blocked": [],
}


def load_config(path="trader/config.json"):
    cfg = dict(CONFIG_DEFAULTS)
    doc = read_json(path, {})
    if not isinstance(doc, dict):
        raise MarketError("%s must be a JSON object" % path)
    cfg.update(doc)
    return cfg


# ---------------------------------------------------------------- cross-exchange comparison

def compare(asset, fetcher=None):
    """Every exchange's quote for one asset. Refusals and missing markets are listed, not hidden."""
    fetcher = fetcher or quote
    rows, missing = [], []
    for v in venues_for(asset):
        try:
            rows.append(fetcher(instrument(asset, v)))
        except VenueRefused as e:
            missing.append({"venue": v, "why": "refused: %s" % e})
        except (MarketError, KeyError, IndexError, ValueError, TypeError) as e:
            missing.append({"venue": v, "why": str(e) or "no market"})
    if rows:
        mid = statistics.median(r["last"] for r in rows)
        for r in rows:
            r["vs_median_bps"] = round((r["last"] / mid - 1) * 10000, 1)
            m = (r["bid"] + r["ask"]) / 2.0
            r["spread_bps"] = round((r["ask"] - r["bid"]) / m * 10000, 2) if m > 0 else None
            r["fee_pct"] = default_fee(r["venue"])
    rows.sort(key=lambda r: -(r.get("volume_24h_usd") or 0))
    notes = []
    if rows:
        out = [r for r in rows if abs(r["vs_median_bps"]) >= 100]
        if out:
            notes.append("%s more than 1%% from the median: check it is the same contract and unit before comparing."
                         % ", ".join(r["venue"] for r in out))
        if len({r["quote"] for r in rows}) > 1:
            notes.append("Some markets quote in USD and some in USDT. They are close, not identical.")
        u = underlying_status(asset)
        if u and not u["open"]:
            notes.append("%s: %s, so these prices can drift from the next futures open and books can be thin." % (
                canonical(asset), u["why"]))
    return {"asset": canonical(asset), "rows": rows, "missing": missing, "notes": notes,
            "note": "A price gap is not free money: fees, withdrawal time and funding usually take it. Not financial advice."}


def report_compare(doc):
    lines = ["%s across exchanges" % doc["asset"]]
    lines.append("  %-12s %-5s %14s %9s %9s %11s %12s" % ("exchange", "ccy", "last", "vs med", "spread", "24h volume", "funding/8h"))
    for r in doc["rows"]:
        funding = "%+.4f%%" % r["funding_8h_pct"] if r.get("funding_8h_pct") is not None else "spot"
        lines.append("  %-12s %-5s %14s %+7.1fbp %7.2fbp %11s %12s" % (
            r["venue"], r["quote"], fmt(r["last"]), r["vs_median_bps"], r["spread_bps"] or 0,
            fmt_usd(r["volume_24h_usd"]), funding))
    for m in doc["missing"]:
        lines.append("  %-12s not available: %s" % (m["venue"], m["why"]))
    lines += doc["notes"]
    lines.append(doc["note"])
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description="The exchange layer: list assets, quote and compare them.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("quote", "spec"):
        s = sub.add_parser(name)
        s.add_argument("asset")
        s.add_argument("--venue")
        s.add_argument("--json", action="store_true")
    c = sub.add_parser("compare")
    c.add_argument("asset")
    c.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    try:
        if a.cmd == "list":
            print("Commodities (perpetual contracts that track the futures price):")
            for name, venues in sorted(COMMODITIES.items()):
                print("  %-10s %s" % (name, ", ".join(venues)))
            print("Crypto: any coin the exchange lists. Spot: %s. Perpetuals: %s." % (
                ", ".join(SPOT_VENUES), ", ".join(PERP_VENUES)))
        elif a.cmd == "quote":
            doc = first_working(a.asset, a.venue, quote)
            print(json.dumps(doc, indent=2) if a.json else "%s on %s: %s %s, bid %s, ask %s, 24h volume %s%s" % (
                doc["asset"], doc["venue"], fmt(doc["last"]), doc["quote"], fmt(doc["bid"]), fmt(doc["ask"]),
                fmt_usd(doc["volume_24h_usd"]),
                ", funding %+.4f%% per 8h" % doc["funding_8h_pct"] if doc["funding_8h_pct"] is not None else ""))
        elif a.cmd == "spec":
            inst = instrument(a.asset, a.venue)
            doc = dict(spec(inst), asset=inst["asset"], venue=inst["venue"], symbol=inst["symbol"])
            print(json.dumps(doc, indent=2))
        else:
            doc = compare(a.asset)
            print(json.dumps(doc, indent=2) if a.json else report_compare(doc))
            if not doc["rows"]:
                return 1
    except MarketError as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
