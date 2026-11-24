"""Update TAIEX and turnover from official TWSE daily market statistics."""
import json
import math
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "data" / "taiex.json"
NOW = datetime.now(ZoneInfo("Asia/Taipei"))

def fetch_month(month):
    ymd = month.replace("-", "") + "01"
    urls = [
        "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?response=json&date=" + ymd,
        "https://www.twse.com.tw/exchangeReport/FMTQIK?response=json&date=" + ymd,
    ]
    errors = []
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Referer": "https://www.twse.com.tw/"})
            with urllib.request.urlopen(req, timeout=30) as response:
                payload = json.load(response)
            fields = [str(x).replace(" ", "") for x in payload.get("fields", [])]
            def col(term):
                return next(i for i, name in enumerate(fields) if term in name)
            di, vi, ci = col("日期"), col("成交金額"), col("發行量加權股價指數")
            rows = []
            for item in payload.get("data", []):
                try:
                    year, mm, dd = [int(x) for x in str(item[di]).strip().replace("-", "/").split("/")]
                    if year < 1911:
                        year += 1911
                    day = date(year, mm, dd).isoformat()
                    if day[:7] != month:
                        continue
                    close = float(str(item[ci]).replace(",", ""))
                    value = int(str(item[vi]).replace(",", ""))
                    if math.isfinite(close) and close > 0 and value > 0:
                        rows.append({"date": day, "close": close, "trade_value": value})
                except (ValueError, TypeError, IndexError):
                    continue
            if rows:
                return rows
            errors.append("No valid rows from " + url)
        except Exception as exc:
            errors.append(type(exc).__name__ + ": " + str(exc)[:120])
    raise RuntimeError(month + ": " + " | ".join(errors))

def main():
    end = NOW.date() if NOW.hour >= 16 else NOW.date() - timedelta(days=1)
    months = sorted({f"{year:04d}-{month:02d}" for year in range(2026, end.year + 1)
                     for month in range(1, 13) if date(year, month, 1) <= end and (year, month) >= (2026, 1)})
    official = {}
    failed_months = []
    for month in months:
        try:
            rows = fetch_month(month)
            official.update({r["date"]: r for r in rows if r["date"] <= end.isoformat()})
            print("Verified", month, len(rows))
        except RuntimeError as exc:
            print("WARNING:", exc)
            failed_months.append(month)
        time.sleep(0.3)
    if failed_months:
        raise RuntimeError("Incomplete official history; refusing to publish partial/mixed data: " + ", ".join(failed_months))
    if not official:
        raise RuntimeError("TWSE official data unavailable; no file overwritten")
    # Do not preserve unverified historical figures. A failed month remains explicitly missing.
    if min(official) > "2026-01-31":
        raise RuntimeError("January official data missing; refusing incomplete backfill")
    latest = max(official)
    if latest < end.isoformat():
        print("WARNING latest verified trading date:", latest, "eligible:", end)
    result = {"source": "TWSE official FMTQIK daily market statistics", "updated_at": NOW.isoformat(timespec="seconds"),
              "history_start": "2026-01-01", "verified": True, "quarter_start": "2026-10-01", "rows": sorted(official.values(), key=lambda x: x["date"])}
    if TARGET.exists():
        previous = json.loads(TARGET.read_text(encoding="utf-8"))
        if previous.get("verified"):
            old_rows = previous.get("rows", [])
            if {row["date"] for row in old_rows} - official.keys():
                raise RuntimeError("Official response omitted existing trading dates")
            if old_rows == result["rows"]:
                print("No new TWSE close")
                return
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Saved", len(result["rows"]), "official rows; latest", latest, "turnover", official[latest]["trade_value"])

if __name__ == "__main__":
    main()
