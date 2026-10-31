#!/usr/bin/env python3
"""Writes the files the website reads for news, earnings and Congress trades.

Browsers are not allowed to read these sources directly, so a scheduled job fetches them
and saves plain JSON next to the site. Standard library only.

    python3 build_data.py --out data                     # all three
    python3 build_data.py --out data --only news
    python3 build_data.py --out data --offline-sec DIR   # earnings from saved SEC files

Earnings need SEC_USER_AGENT ("Your Name you@example.com"), which the SEC requires of anyone
reading its data. Without it, earnings.json says so and the site shows that message.
Congress rows need `pdftotext`; without it each filing is listed with a link to its PDF.

facts.py, ptr.py and news.py come from day-trader-skill by Jake Schincariol (MIT).
"""

import argparse
import datetime
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import facts  # noqa: E402
import markets  # noqa: E402
import news  # noqa: E402
import ptr  # noqa: E402

NEWS_ASSETS = ["BTC", "ETH", "SOL", "XRP", "GOLD", "SILVER", "WTI", "BRENT", "NATGAS", "COPPER"]
NEWS_QUERIES = {"TSLA": "Tesla stock", "NVDA": "Nvidia stock", "AAPL": "Apple stock", "MSFT": "Microsoft stock",
                "AMZN": "Amazon stock", "META": "Meta stock", "GOOGL": "Alphabet stock", "AMD": "AMD stock"}
EARNINGS_TICKERS = ["TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "AMD"]
FILINGS_LISTED = 40
FILINGS_READ = 12
KEEP_NEWS = 300


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_news():
    errors = []
    news.QUERIES.update(NEWS_QUERIES)
    # kind_of() treats a stock ticker as crypto, so stock headlines are collected from their search feed alone.
    items = news.collect(NEWS_ASSETS, errors)
    for ticker, query in NEWS_QUERIES.items():
        try:
            rows = news.feed(news.GOOGLE % news.urllib.parse.quote_plus(query))
            items += [dict(r, asset=ticker, tags=news.tags_for(r["title"])) for r in rows]
        except markets.MarketError as e:
            errors.append("%s: %s" % (ticker, e))
    now = markets.now_ts()
    items = [i for i in news.dedupe(items) if i.get("time") and now - 2 * markets.DAY <= i["time"] <= now + 3600]
    return {"generated_at": stamp(), "items": items[:KEEP_NEWS], "errors": errors}


def build_earnings(offline=None):
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua and not offline:
        return {"generated_at": stamp(), "status": "needs_contact", "companies": [],
                "message": "Earnings are not set up yet: the SEC asks for a contact name and email before serving its data."}
    fetcher = facts.Fetcher(ua, offline=offline)
    ciks = facts.ticker_ciks(fetcher)
    companies, errors = [], []
    for ticker in EARNINGS_TICKERS:
        cik, name = ciks.get(ticker, (None, None))
        if not cik:
            errors.append("%s: no SEC filer" % ticker)
            continue
        try:
            result = facts.analyse(fetcher.json(facts.FACTS_URL % cik, "companyfacts-CIK%s.json" % cik), quarters=6, name=name)
            companies.append({"ticker": ticker, "name": result.get("entity"), "quarters": result.get("quarters") or result.get("rows") or []})
        except (facts.FactsError, ValueError, KeyError) as e:
            errors.append("%s: %s" % (ticker, e))
    return {"generated_at": stamp(), "status": "ok", "companies": companies, "errors": errors,
            "note": "Numbers as filed with the SEC. A fourth quarter is the full year minus the first three."}


def build_congress():
    year = datetime.datetime.now(datetime.timezone.utc).year
    rows = ptr.select(ptr.load_index(year), kind="P")[:FILINGS_LISTED]
    can_read = shutil.which("pdftotext") is not None
    filings, errors = [], []
    with tempfile.TemporaryDirectory() as tmp:
        for i, row in enumerate(rows):
            entry = {"member": row["name"], "district": row["district"], "filed": row["filed"], "doc_id": row["doc_id"], "url": row["url"], "trades": []}
            if can_read and i < FILINGS_READ:
                try:
                    _, report = ptr._fetch_and_read(row["doc_id"], row["year"], tmp, filed=row["filed"])
                    for t in (report or {}).get("rows", []):
                        entry["trades"].append({k: t.get(k) for k in ("ticker", "asset", "type_name", "owner_name", "date", "amount", "days_to_disclose", "late")})
                except (ptr.PtrError, OSError, ValueError) as e:
                    errors.append("%s: %s" % (row["doc_id"], e))
            filings.append(entry)
    return {"generated_at": stamp(), "filings": filings, "rows_read": can_read, "errors": errors,
            "note": "Public filings under the STOCK Act. They arrive days or weeks after the trade, and amounts are ranges. House only."}


def main(argv=None):
    p = argparse.ArgumentParser(description="Build the data files the website reads.")
    p.add_argument("--out", default="data")
    p.add_argument("--only", choices=["news", "earnings", "congress"])
    p.add_argument("--offline-sec", help="read SEC data from a folder of saved files")
    a = p.parse_args(argv)
    builders = {"news": build_news, "earnings": lambda: build_earnings(a.offline_sec), "congress": build_congress}
    failed = 0
    for name, build in builders.items():
        if a.only and a.only != name:
            continue
        path = os.path.join(a.out, name + ".json")
        try:
            doc = build()
        except (markets.MarketError, ptr.PtrError, facts.FactsError, OSError, ValueError) as e:
            # Keep the last good file: stale data with its timestamp beats an empty page.
            print("%s failed: %s" % (name, e), file=sys.stderr)
            failed += 1
            continue
        markets.write_json(path, doc)
        print("%s: wrote %s" % (name, path))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
