"""Refresh completed market quotes. A source failure never replaces a stored quote."""
import concurrent.futures
import datetime as dt
import json
import math
import os
from pathlib import Path
from urllib.parse import quote

import market_cycle
import update_night_futures as futures
import update_market_news

DATA = market_cycle.DATA
TAIPEI = market_cycle.TZ
SYMBOLS = [
    ("道瓊工業指數", "^DJI", "America/New_York", "16:00"),
    ("S&P 500", "^GSPC", "America/New_York", "16:00"),
    ("NASDAQ", "^IXIC", "America/New_York", "16:00"),
    ("費城半導體", "^SOX", "America/New_York", "16:00"),
    ("NVIDIA", "NVDA", "America/New_York", "16:00"),
    ("台積電 ADR", "TSM", "America/New_York", "16:00"),
    ("日經 225", "^N225", "Asia/Tokyo", "15:30"),
    ("韓國 KOSPI", "^KS11", "Asia/Seoul", "15:30"),
    ("香港恆生指數", "^HSI", "Asia/Hong_Kong", "16:00"),
]
STOCKS = {"2330": "台積電"}
TPEX = "https://www.tpex.org.tw/openapi/v1/tpex_index"


def completed_bars(report, symbol, timezone, close_at, now):
    market_now = now.astimezone(timezone)
    meta = report["meta"]
    if meta["symbol"] != symbol or meta["exchangeTimezoneName"] != timezone.key:
        raise ValueError("Unexpected symbol or exchange timezone")
    today = market_now.date()
    end = dt.datetime.combine(today, dt.time.fromisoformat(close_at), timezone)
    regular = meta.get("currentTradingPeriod", {}).get("regular", {})
    if isinstance(regular.get("end"), int):
        provider_end = dt.datetime.fromtimestamp(regular["end"], timezone)
        if provider_end.date() == today:
            end = provider_end
    points = {}
    for stamp, value in zip(report["timestamp"], report["indicators"]["quote"][0]["close"], strict=True):
        day = dt.datetime.fromtimestamp(stamp, timezone).date()
        if day > today or (day == today and market_now < end + dt.timedelta(minutes=5)):
            continue
        if isinstance(value, (float, int)) and math.isfinite(value) and value > 0:
            points[day] = value
    if len(points) < 2:
        raise ValueError("Not enough completed bars")
    return [(day, points[day]) for day in sorted(points)[-2:]]


def fetch_global(name, symbol, zone, close_at, now, previous):
    errors = []
    candidates = []
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}?range=10d&interval=1d"
        try:
            series = market_cycle.fetch(url)["chart"]["result"][0]
            timezone = market_cycle.ZoneInfo(zone)
            (_, prior), (day, close) = completed_bars(series, symbol, timezone, close_at, now)
            session_end = None
            regular_end = series["meta"].get("currentTradingPeriod", {}).get("regular", {}).get("end")
            if isinstance(regular_end, int):
                provider_end = dt.datetime.fromtimestamp(regular_end, timezone)
                if provider_end.date() == day:
                    session_end = provider_end.isoformat()
            item = {"name": name, "symbol": symbol, "date": day.isoformat(), "close": round(close, 4),
                    "change": round(close - prior, 4), "change_pct": round(100 * (close / prior - 1), 2), "exchange_timezone": zone,
                    "session_complete": True, "source": "Yahoo Finance 延遲日線", "source_url": url}
            if session_end:
                item["session_end"] = session_end
            candidates.append(item)
            previous_date = previous.get("date", "")
            market_day = now.astimezone(timezone).date()
            expected = market_day
            if now.astimezone(timezone).time() < dt.time.fromisoformat(close_at):
                expected -= dt.timedelta(days=1)
            while expected.weekday() >= 5:
                expected -= dt.timedelta(days=1)
            if item["date"] >= max(previous_date, expected.isoformat()):
                return item
        except (OSError, ValueError, TypeError, KeyError, IndexError) as error:
            errors.append(f"{host}: {error}")
    if candidates:
        item = max(candidates, key=lambda row: row["date"])
        if zone == "America/New_York":
            local = now.astimezone(market_cycle.ZoneInfo(zone))
            completed_before = local.date() + dt.timedelta(days=int(local.time() >= dt.time(16, 30)))
            expected_us = market_cycle.previous_us_session(completed_before)
            if item["date"] < expected_us:
                raise RuntimeError(f"US close {expected_us} has not reached either endpoint")
        return item
    raise RuntimeError(" | ".join(errors))


def taiwan_stocks(day):
    ymd = day.replace("-", "")
    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?response=json&date={ymd}&type=24"
    report = market_cycle.fetch(url)
    if report.get("stat") != "OK" or report.get("date") != ymd:
        raise ValueError("TWSE stock report date unavailable")
    for table in report["tables"]:
        fields = table.get("fields", [])
        if "證券代號" not in fields or "收盤價" not in fields:
            continue
        index = {field: fields.index(field) for field in ("證券代號", "收盤價", "漲跌(+/-)", "漲跌價差")}
        for row in table["data"]:
            symbol = row[index["證券代號"]]
            if symbol not in STOCKS:
                continue
            close = futures.number(row[index["收盤價"]])
            change = abs(futures.number(row[index["漲跌價差"]]))
            if ">-<" in row[index["漲跌(+/-)"]]:
                change = -change
            if close <= 0 or close - change <= 0:
                raise ValueError("Invalid TWSE stock close")
            yield {"name": STOCKS[symbol], "symbol": symbol, "date": day, "close": float(close),
                   "change": float(change), "change_pct": float(round(100 * change / (close - change), 2)),
                   "session_complete": True, "exchange_timezone": TAIPEI.key,
                   "source": "證交所正式收盤", "source_url": url}


def otc(now):
    rows = [(dt.date.fromisoformat(f"{r['Date'][:4]}-{r['Date'][4:6]}-{r['Date'][6:]}"), r)
            for r in market_cycle.fetch(TPEX)]
    allowed = [(day, row) for day, row in rows if day < now.date() or
               (day == now.date() and now.time() >= dt.time(13, 30))]
    day, row = max(allowed, key=lambda pair: pair[0])
    close, change = futures.number(row["Close"]), futures.number(row["Change"])
    if close <= 0 or close - change <= 0:
        raise ValueError("Invalid OTC index close")
    return {"name": "上櫃指數", "symbol": "TPEX", "date": day.isoformat(), "close": float(close),
            "change": float(change), "change_pct": float(round(100 * change / (close - change), 2)),
            "session_complete": True, "exchange_timezone": TAIPEI.key,
            "source": "櫃買中心正式收盤", "source_url": TPEX}


def store_quotes(book, updates, now):
    previous = {row["symbol"]: row for row in book["rows"]}
    results = {}
    for symbol, item in updates.items():
        old = previous.get(symbol)
        if isinstance(item, Exception):
            results[symbol] = {"status": "error", "detail": str(item), "retained": (old or {}).get("date")}
        elif old and item["date"] < old["date"]:
            results[symbol] = {"status": "retained" if old.get("session_complete") is True else "error", "detail": "older source data rejected",
                               "received": item["date"], "retained": old["date"]}
        else:
            if old and item["date"] == old["date"]:
                item = {**old, **item}
            if not old or {key: value for key, value in item.items() if key != "updated_at"} != {
                    key: value for key, value in old.items() if key != "updated_at"} or not old.get("updated_at"):
                item["updated_at"] = now.isoformat(timespec="seconds")
            previous[symbol] = item
            results[symbol] = {"status": "unchanged" if old == item else "updated", "quote_date": item["date"]}
    rows = list(previous.values())
    errors = {}
    for symbol, result in results.items():
        if result["status"] not in ("error", "retained"):
            continue
        detail = result["detail"]
        if result.get("received"):
            errors[symbol] = f"來源回傳較舊的 {result['received']}，保留 {result['retained']} 行情"
        elif detail.startswith("US close ") and detail.endswith("has not reached either endpoint"):
            errors[symbol] = f"兩個來源都尚未提供 {detail.split()[2]} 美股正式收盤"
        elif "timed out" in detail.lower():
            errors[symbol] = "行情來源連線逾時"
        else:
            errors[symbol] = "行情來源未回傳有效報價"
    if book["rows"] != rows or book.get("source_errors", {}) != errors:
        market_cycle.write("global.json", {**book, "updated_at": now.isoformat(timespec="seconds"),
                                           "rows": rows, "source_errors": errors})
    return results


def run_if_needed(action, files):
    before = {filename: (DATA / filename).read_bytes() for filename in files}
    action()
    return {"status": "updated" if any((DATA / name).read_bytes() != content for name, content in before.items()) else "unchanged"}


def taiwan_close(now):
    result = run_if_needed(lambda: market_cycle.publish_close(now), ("taiex.json", "verification.json"))
    result["quote_date"] = market_cycle.read("taiex.json")["rows"][-1]["date"]
    return result


def keep_futures_status(results):
    labels = {"day": "TX-DAY", "night": "TX-NIGHT"}
    status = {session: result.get("detail") if result.get("status") == "error" else None
              for session, key in labels.items() if (result := results.get(key))}
    file = DATA / "futures-status.json"
    if not file.exists() or market_cycle.read(file.name) != status:
        market_cycle.write(file.name, status)


def outcome(task):
    try:
        return task.result()
    except Exception as error:
        return error


def main():
    now = dt.datetime.now(TAIPEI)
    book = market_cycle.read("global.json")
    previous = {row["symbol"]: row for row in book["rows"]}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        jobs = {symbol: pool.submit(fetch_global, name, symbol, zone, close_at, now, previous.get(symbol, {}))
                for name, symbol, zone, close_at in SYMBOLS}
        jobs["TPEX"] = pool.submit(otc, now)
        news_job = pool.submit(update_market_news.refresh)
        regular_job = pool.submit(futures.refresh_report, DATA / "day-futures.json", False)
        close_job = pool.submit(taiwan_close, now)
        night_job = pool.submit(futures.refresh_report, DATA / "night-futures.json", True)
        result_close = outcome(close_job)
        published_day = market_cycle.read("taiex.json")["rows"][-1]["date"]
        for symbol in STOCKS:
            if previous.get(symbol, {}).get("date") != published_day or "change" not in previous.get(symbol, {}):
                jobs[symbol] = pool.submit(lambda symbol=symbol: next(r for r in taiwan_stocks(published_day)
                                                                        if r["symbol"] == symbol))
        results = store_quotes(book, {symbol: outcome(task) for symbol, task in jobs.items()}, now)
        for symbol in STOCKS.keys() - jobs.keys():
            results[symbol] = {"status": "unchanged", "quote_date": published_day}
        result_night = outcome(night_job)
        result_regular = outcome(regular_job)
        result_news = outcome(news_job)
        if isinstance(result_news, Exception):
            print("Federal Reserve news refresh unavailable:", result_news)
    for symbol, result in (("TAIEX", result_close), ("TX-DAY", result_regular), ("TX-NIGHT", result_night)):
        results[symbol] = {"status": "error", "detail": str(result)} if isinstance(result, Exception) else result
    keep_futures_status(results)
    for label, update in (("TWSE-CALENDAR", market_cycle.refresh_calendar),
                          ("TWSE-OUTLOOK", lambda: market_cycle.publish_outlook(dt.datetime.now(TAIPEI)))):
        try:
            update()
            results[label] = {"status": "checked"}
        except (OSError, RuntimeError, ValueError, KeyError) as error:
            results[label] = {"status": "error", "detail": str(error)}
    report = {"checked_at": now.isoformat(timespec="seconds"), "finished_at": dt.datetime.now(TAIPEI).isoformat(timespec="seconds"),
              "event": os.environ.get("GITHUB_EVENT_NAME"), "schedule": os.environ.get("SCHEDULE"), "markets": results}
    content = json.dumps(report, ensure_ascii=False, indent=2)
    Path(os.environ.get("MARKET_REPORT", "/tmp/market-report.json")).write_text(content + "\n", encoding="utf-8")
    print(content)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as file:
            file.write("### Market refresh\n\n```json\n" + content + "\n```\n")
    return int(any(item["status"] in ("error", "pending") for item in results.values()))


if __name__ == "__main__":
    raise SystemExit(main())
