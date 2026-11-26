"""Publish delayed Fugle candles only when explicitly licensed for public reuse."""
import json
import math
import os
import urllib.request
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
        return
    series = delayed_series(candle_report(SYMBOLS["TAIEX"], token), SYMBOLS["TAIEX"], day, cutoff, include_ohlc=True)
    if len(series) != 271 or series[0][1]["at"][11:16] != "09:00" or series[-1][1]["at"][11:16] != "13:30":
        raise ValueError("富果當日大盤完整分時尚未發布")
    next_book = existing if existing.get("date") == day else {"date": day, "quotes": []}
    next_book.update(points=[value for _, value in series], chart_complete=True,
                     delay_minutes=delay, retrieved_at=datetime.now(market.TZ).isoformat(),
                     source="富果 Fugle 延遲分 K")
    market.write(path.name, next_book)


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
    quotes, points = [], []
    for key, code in SYMBOLS.items():
        if key not in prior or not math.isfinite(prior[key]) or prior[key] <= 0:
            raise ValueError(f"{key} 缺少前一交易日正式收盤")
        try:
            payload = candle_report(code, token)
            series = delayed_series(payload, code, day, cutoff)
            if key == "TAIEX":
                points = [row for _, row in delayed_series(payload, code, day, cutoff, include_ohlc=True)]
        except (OSError, KeyError, TypeError, ValueError) as error:
            status = getattr(error, "code", None)
            raise RuntimeError(f"{key} 富果行情查詢失敗" + (f"（HTTP {status}）" if status else "")) from None
        if not series or cutoff - series[-1][0] > timedelta(minutes=10):
            raise ValueError(f"{key} 未取得合規延遲窗口內的當日行情")
        stamp, value = series[-1]
        quotes.append({"symbol": key, "value": value, "quote_at": stamp.isoformat(),
                       "change_pct": round((value / prior[key] - 1) * 100, 2)})
    return {"date": day, "retrieved_at": datetime.now(market.TZ).isoformat(),
            "source": "富果 Fugle 延遲分 K", "delay_minutes": delay, "quotes": quotes, "points": points}
