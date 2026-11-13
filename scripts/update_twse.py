"""Fetch verified TAIEX closes from FinMind; never invent market data."""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "data" / "taiex.json"
NOW = datetime.now(ZoneInfo("Asia/Taipei"))
API = "https://api.finmindtrade.com/api/v4/data"


def fetch_day(day, token):
    params = urllib.parse.urlencode({
        "dataset": "TaiwanVariousIndicators5Seconds",
        "start_date": day,
    })
    request = urllib.request.Request(
        API + "?" + params,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        if "json" not in response.headers.get("Content-Type", "").lower():
            raise ValueError("FinMind returned non-JSON content")
        payload = json.load(response)
    if not isinstance(payload, dict) or payload.get("status") != 200:
        raise ValueError("FinMind API error: " + str(payload.get("msg", "unknown"))[:200])
    records = payload.get("data")
    if not isinstance(records, list):
        raise ValueError("FinMind response has no data list")
    valid = []
    for item in records:
        if not isinstance(item, dict):
            continue
        stamp = str(item.get("date", ""))
        if not stamp.startswith(day + " "):
            continue
        try:
            close = float(item["TAIEX"])
        except (KeyError, ValueError, TypeError):
            continue
        if close > 0:
            valid.append((stamp, close))
    if not valid:
        return None  # Market holiday or data not yet available
    stamp, close = max(valid, key=lambda x: x[0])
    # The final 13:30 index reading is the official closing reading.
    # Never treat an intraday reading as a completed daily close.
    if stamp[11:16] != "13:30":
        raise ValueError(f"No 13:30 closing reading for {day}; latest={stamp}")
    return {"date": day, "close": close, "trade_value": None}


def main():
    token = os.environ.get("FINMIND_TOKEN", "").strip()
    if not token:
        raise RuntimeError("FINMIND_TOKEN is missing from environment")

    previous = {}
    if TARGET.exists():
        try:
            old = json.loads(TARGET.read_text(encoding="utf-8"))
            for item in old.get("rows", []):
                if isinstance(item, dict) and item.get("date") and item.get("close") is not None:
                    previous[item["date"]] = item
        except (OSError, ValueError, TypeError):
            pass

    # Start with a small, safe test window. Later runs accumulate history.
    failures = []
    fetched = 0
    for offset in range(14, -1, -1):
        day = (NOW.date() - timedelta(days=offset))
        if day.weekday() >= 5:
            continue
        # Do not publish today's index before the market has closed.
        if day == NOW.date() and (NOW.hour, NOW.minute) < (14, 0):
            continue
        day_text = day.isoformat()
        try:
            row = fetch_day(day_text, token)
            if row:
                previous[day_text] = row
                fetched += 1
                print(f"OK {day_text}: {row['close']}")
            else:
                print(f"No data {day_text} (holiday or unavailable)")
        except (urllib.error.URLError, ValueError, KeyError) as exc:
            failures.append(f"{day_text}: {exc}")
        time.sleep(0.3)

    if fetched == 0:
        raise RuntimeError("No verified FinMind closing data; refusing to overwrite. " + "; ".join(failures))

    rows = sorted(previous.values(), key=lambda r: r["date"])[-120:]
    output = {
        "source": "FinMind TaiwanVariousIndicators5Seconds (13:30 TAIEX)",
        "updated_at": NOW.isoformat(timespec="seconds"),
        "rows": rows,
    }
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(rows)} rows; new verified closes: {fetched}; failures: {failures}")


if __name__ == "__main__":
    main()
