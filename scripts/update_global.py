#!/usr/bin/env python3
"""Fetch verified delayed daily international closes; never synthesize quotes."""
import datetime as dt
import json
import pathlib
import urllib.parse
import urllib.request

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
    tz = dt.timezone(dt.timedelta(seconds=result["meta"].get("gmtoffset", 0)))
    points = [(dt.datetime.fromtimestamp(ts, tz).date().isoformat(), float(value)) for ts, value in zip(stamps, closes) if isinstance(value, (int, float)) and value > 0]
    if len(points) < 2:
        raise ValueError("Not enough verified closes")
    latest, previous = points[-1], points[-2]
    if latest[0] > dt.datetime.now(tz).date().isoformat():
        raise ValueError("Future-dated quote")
    return {"name": name, "symbol": symbol, "date": latest[0], "close": round(latest[1], 4), "change_pct": round((latest[1] / previous[1] - 1) * 100, 2), "source": "Yahoo Finance 延遲日線"}

def main():
    previous = {}
    if OUT.exists():
        try:
            previous = {r["name"]: r for r in json.loads(OUT.read_text(encoding="utf-8")).get("rows", [])}
        except (ValueError, KeyError, TypeError):
            pass
    rows = []
    for name, symbol in SYMBOLS:
        try:
            item = fetch(name, symbol)
            rows.append(item)
            print(f"OK {name}: {item['date']} {item['close']}")
        except Exception as error:
            print(f"UNAVAILABLE {name}: {error}")
            if name in previous:
                rows.append(previous[name])
    if not rows:
        raise RuntimeError("No confirmed quotes; leaving existing file unchanged")
    payload = {"updated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds"), "note": "Delayed daily close; individual quote dates shown. Unavailable symbols retain prior verified date.", "rows": rows}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(rows)}/5 verified entries")

if __name__ == "__main__":
    main()
