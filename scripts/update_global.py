#!/usr/bin/env python3
"""Fetch verified delayed daily international closes; never synthesize quotes."""
import datetime as dt
import json
import math
import pathlib
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "global.json"
SYMBOLS = [
    ("NASDAQ", "^IXIC"),
    ("費城半導體", "^SOX"),
    ("NVIDIA", "NVDA"),
    ("台積電 ADR", "TSM"),
    ("日經 225", "^N225"),
]

def fetch(name, symbol):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol, safe="") + "?range=10d&interval=1d"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=18) as response:
        payload = json.load(response)
    result = payload["chart"]["result"][0]
    stamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    meta = result["meta"]
    if meta["symbol"] != symbol:
        raise ValueError("Unexpected quote symbol")
    tz = ZoneInfo(meta["exchangeTimezoneName"])
    now = dt.datetime.now(tz)
    regular = meta["currentTradingPeriod"]["regular"]
    current_session = dt.datetime.fromtimestamp(regular["start"], tz).date()
    session_end = dt.datetime.fromtimestamp(regular["end"], tz)
    points = {}
    for stamp, value in zip(stamps, closes, strict=True):
        day = dt.datetime.fromtimestamp(stamp, tz).date()
        if day > now.date():
            raise ValueError("Future-dated quote")
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            continue
        if day >= current_session and (day != current_session or now < session_end):
            continue
        if day == now.date() and day != current_session:
            continue
        points[day] = value
    days = sorted(points)
    if len(days) < 2:
        raise ValueError("Not enough completed sessions")
    day, prior_day = days[-2:][::-1]
    latest, previous = points[day], points[prior_day]
    return {"name": name, "symbol": symbol, "date": day.isoformat(), "close": round(latest, 4),
            "change_pct": round((latest / previous - 1) * 100, 2),
            "session_complete": True, "source": "Yahoo Finance 延遲日線"}

def main():
    previous = {}
    if OUT.exists():
        try:
            previous = {r["name"]: r for r in json.loads(OUT.read_text(encoding="utf-8")).get("rows", [])}
        except (ValueError, KeyError, TypeError):
            pass
    rows = []
    confirmed = 0
    for name, symbol in SYMBOLS:
        try:
            item = fetch(name, symbol)
            old = previous.get(name, {})
            if old.get("date", "") > item["date"]:
                raise ValueError("Provider response is older than the last completed session")
            rows.append(item)
            confirmed += 1
            print(f"OK {name}: {item['date']} {item['close']}")
        except Exception as error:
            print(f"UNAVAILABLE {name}: {error}")
            if name in previous:
                rows.append({**previous[name], "stale": True})
    if rows == list(previous.values()):
        if not confirmed:
            raise RuntimeError("No completed quotes verified")
        print("No new international closes")
        return
    if not rows:
        raise RuntimeError("No completed quotes verified")
    payload = {"updated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds"), "note": "Delayed daily close; individual quote dates shown. Unavailable symbols retain prior verified date.", "rows": rows}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {confirmed}/{len(SYMBOLS)} refreshed quotes; {len(rows) - confirmed} retained")

if __name__ == "__main__":
    main()
