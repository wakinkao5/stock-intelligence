"""Publish delayed Fugle candles only when explicitly licensed for public reuse."""
import json
import math
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import urlencode

import market_cycle as market

SYMBOLS = {"TAIEX": "IX0001", "TPEX": "IX0043", "2330": "2330"}


def delayed_series(payload, symbol, day, cutoff, *, include_ohlc=False):
    if payload.get("symbol") != symbol or payload.get("date") != day:
        raise ValueError("Unexpected Fugle symbol or trading date")
    series = {}
    for row in payload["data"]:
        stamp = datetime.fromisoformat(row["date"])
        end = stamp.astimezone(market.TZ) + timedelta(minutes=1)
        value = row["close"]
        if (stamp.tzinfo is None or stamp.astimezone(market.TZ).date().isoformat() != day
                or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0):
            raise ValueError("Invalid Fugle candle")
        if end <= cutoff and end.strftime("%H:%M") >= "09:01" and end.strftime("%H:%M") <= "13:31":
            if include_ohlc:
                prices = [row.get(name) for name in ("open", "high", "low")]
                if (not all(isinstance(price, (int, float)) and math.isfinite(price) and price > 0 for price in prices)
                        or not prices[2] <= min(prices[0], value) <= max(prices[0], value) <= prices[1]):
                    raise ValueError("Invalid Fugle OHLC")
                series[end] = {"at": stamp.astimezone(market.TZ).isoformat(), "published_at": end.isoformat(),
                               "value": value, "open": prices[0], "high": prices[1], "low": prices[2]}
            else:
                series[end] = value
    return sorted(series.items())


def candle_report(code, token):
    url = f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/candles/{code}?{urlencode({'timeframe': 1})}"
    req = urllib.request.Request(url, headers={"X-API-KEY": token, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=12) as response:
        return json.load(response)


def refresh_final_chart(now):
    token = os.getenv("FUGLE_API_KEY")
    if not token or os.getenv("FUGLE_PUBLIC_DISPLAY_AUTHORIZED") != "true":
        return
    delay = max(20, int(os.getenv("FUGLE_PUBLIC_DELAY_MINUTES") or "20"))
    cutoff = now - timedelta(minutes=delay)
    day = now.date().isoformat()
    if cutoff.strftime("%H:%M") < "13:31":
        return
    path = market.DATA / "intraday.json"
    existing = market.read(path.name) if path.exists() else {}
    if existing.get("date") == day and existing.get("chart_complete"):
        if not (market.DATA / "intraday-complete.json").exists():
            market.write("intraday-complete.json", existing)
        return
    series = delayed_series(candle_report(SYMBOLS["TAIEX"], token), SYMBOLS["TAIEX"], day, cutoff, include_ohlc=True)
    if len(series) != 271 or series[0][1]["at"][11:16] != "09:00" or series[-1][1]["at"][11:16] != "13:30":
        raise ValueError("富果當日大盤完整分時尚未發布")
    next_book = existing if existing.get("date") == day else {"date": day, "quotes": []}
    next_book.update(points=[value for _, value in series], chart_complete=True,
                     delay_minutes=delay, retrieved_at=datetime.now(market.TZ).isoformat(),
                     source="富果 Fugle 延遲分 K")
    market.write(path.name, next_book)
    market.write("intraday-complete.json", next_book)


def refresh(now, previous, global_data):
    token = os.getenv("FUGLE_API_KEY")
    if not token or os.getenv("FUGLE_PUBLIC_DISPLAY_AUTHORIZED") != "true":
        raise ValueError("富果盤中行情未配置公開展示授權或憑證")
    delay = int(os.getenv("FUGLE_PUBLIC_DELAY_MINUTES") or "20")
    if delay < 20:
        raise ValueError("Public quote delay is below the configured minimum")
    cutoff = now - timedelta(minutes=delay)
    day = now.date().isoformat()
    previous_session = market.adjacent_session(now.date(), -1).isoformat()
    if previous["date"] != previous_session:
        raise ValueError("缺少加權指數前一交易日正式收盤")
    prior = {"TAIEX": previous["close"]}
    for row in global_data["rows"]:
        if row["symbol"] in ("TPEX", "2330") and row["date"] == previous_session:
            prior[row["symbol"]] = row["close"]
    def fetch_symbol(key, code):
        previous_close = prior.get(key)
        if not previous_close or not math.isfinite(previous_close):
            return None, [], None
        try:
            payload = candle_report(code, token)
            candles = delayed_series(payload, code, day, cutoff, include_ohlc=True)
        except (OSError, KeyError, TypeError, ValueError):
            return None, [], None
        if not candles:
            return None, [], None
        stamp, last = candles[-1]
        value = last["value"]
        quote = {"symbol": key, "value": value, "quote_at": stamp.isoformat(),
                 "change": round(value - previous_close, 2),
                 "change_pct": round((value / previous_close - 1) * 100, 2)}
        points = [point for _, point in candles] if key == "TAIEX" else []
        amount = None
        if points:
            eligible = {point["at"] for point in points}
            volumes = [row.get("volume") for row in payload["data"]
                       if datetime.fromisoformat(row["date"]).astimezone(market.TZ).isoformat() in eligible]
            if (len(volumes) == len(points) and points[0]["at"][11:16] == "09:00"
                    and stamp - candles[0][0] == timedelta(minutes=len(points) - 1)
                    and all(isinstance(volume, (int, float)) and math.isfinite(volume) and volume >= 0
                            for volume in volumes)):
                amount = sum(volumes)
        return quote, points, amount

    with ThreadPoolExecutor(max_workers=len(SYMBOLS)) as pool:
        tasks = [pool.submit(fetch_symbol, key, code) for key, code in SYMBOLS.items()]
        results = [task.result() for task in tasks]
    quotes = [quote for quote, _, _ in results if quote]
    if not quotes:
        raise ValueError("富果未取得可驗證的當日延遲行情")
    _, points, amount = results[0]
    result = {"date": day, "retrieved_at": datetime.now(market.TZ).isoformat(),
              "source": "富果 Fugle 延遲分 K", "delay_minutes": delay, "quotes": quotes, "points": points}
    if amount is not None:
        result["trade_value"] = amount
    return result


def save_intraday(series):
    name = "intraday.json"
    old = market.read(name) if (market.DATA / name).exists() else {}
    if old.get("date", "") > series["date"]:
        return
    if old.get("date") == series["date"]:
        quotes = {row["symbol"]: row for row in old["quotes"]}
        for row in series["quotes"]:
            symbol = row["symbol"]
            if row["quote_at"] >= quotes.get(symbol, {}).get("quote_at", ""):
                quotes[symbol] = row
        series["quotes"] = list(quotes.values())
        old_points, new_points = old["points"], series["points"]
        if old_points and (old.get("chart_complete") or not new_points or old_points[-1]["at"] > new_points[-1]["at"]):
            series["points"] = old_points
            if "trade_value" in old:
                series["trade_value"] = old["trade_value"]
        if old.get("chart_complete"):
            series["chart_complete"] = True
    market.write(name, series)
