"""Capture the morning outlook and verify it using official TWSE closes."""
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, time as clock, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from update_twse import fetch_month

DATA = Path(__file__).resolve().parents[1] / "data"
TZ = ZoneInfo("Asia/Taipei")
CLOSE_SOURCE = "https://openapi.twse.com.tw/v1/exchangeReport/FMTQIK"
DAILY_SOURCE = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
CALENDAR_SOURCE = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"
START = "2026-11-25"
LOCK = clock(8, 45)
FLAT_PCT = 0.30
ICONS = {"偏多": "🌤️", "偏空": "🌧️", "震盪": "☁️"}
US = ("^IXIC", "^SOX", "NVDA", "TSM")


def read(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def write(name, payload):
    path = DATA / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def fetch(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def roc_date(value):
    if len(value) != 7 or not value.isdecimal():
        raise ValueError("Unexpected TWSE date")
    return datetime(int(value[:3]) + 1911, int(value[3:5]), int(value[5:])).date().isoformat()


def is_session(day):
    if day.weekday() >= 5:
        return False
    if day.year == 2026 and day.isoformat() >= "2026-11-24":
        # TWSE 115 年市場開休市日期, remaining dates after deployment.
        return day.isoformat() != "2026-12-25"
    rows = fetch(CALENDAR_SOURCE)
    if not rows or {roc_date(row["Date"])[:4] for row in rows} != {str(day.year)}:
        raise RuntimeError("TWSE calendar unavailable for this year")
    return not any(roc_date(row["Date"]) == day.isoformat() and
                   not any(word in row["Name"] for word in ("開始交易", "最後交易")) for row in rows)


def forecast_inputs(day):
    history = read("taiex.json")
    closes = [r for r in history["rows"] if r["date"] < day.isoformat()]
    if not history["verified"] or len(closes) < 11:
        raise RuntimeError("TWSE history insufficient")
    previous = day - timedelta(days=1)
    while not is_session(previous):
        previous -= timedelta(days=1)
    if closes[-1]["date"] != previous.isoformat():
        raise RuntimeError("Previous TWSE session is missing")
    global_data = read("global.json")
    quotes = {r["symbol"]: r for r in global_data["rows"]
              if r.get("session_complete") and not r.get("stale") and math.isfinite(r["change_pct"])}
    us = [quotes[symbol] for symbol in US if symbol in quotes]
    if len(us) != len(US) or len({r["date"] for r in us}) != 1 or not (
        (day - timedelta(days=4)).isoformat() <= us[0]["date"] < day.isoformat()
    ):
        raise RuntimeError("Recent matching completed US sessions unavailable")
    night = read("night-futures.json")
    night_is_current = (night.get("verified") is True and night.get("trade_date") == day.isoformat()
                        and math.isfinite(night["change_pct"]))
    last = closes[-1]
    momentum = {n: (last["close"] / closes[-1 - n]["close"] - 1) * 100 for n in (5, 10)}
    us_signal = sum((r["change_pct"] > 0) - (r["change_pct"] < 0) for r in us) / len(us)
    technical = .6 * max(-1, min(1, momentum[5] / 5)) + .4 * max(-1, min(1, momentum[10] / 8))
    score = .65 * us_signal + .35 * technical
    if night_is_current:
        score = .85 * score + .15 * max(-1, min(1, night["change_pct"] / 2))
    direction = "偏多" if score > .18 else "偏空" if score < -.18 else "震盪"
    reason = "、".join(f"{r['name']} {r['change_pct']:+.2f}%" for r in us)
    reason += f"；台股近 5 日 {momentum[5]:+.2f}%、近 10 日 {momentum[10]:+.2f}%"
    reason += f"；臺指期夜盤 {night['change_pct']:+.2f}%" if night_is_current else "；當日夜盤日報尚缺"
    return {
        "direction": direction, "icon": ICONS[direction], "reason": reason,
        "risk": "海外收盤與台股開盤存在時間差，突發消息及開盤後資金流可能改變方向。",
        "model_version": "direction-1", "flat_pct": FLAT_PCT,
        "inputs": {"twse": {"source": history["source"], "rows": closes[-11:]},
                   "us": us, "night": night if night_is_current else None},
    }


def lock_forecast(now):
    day = now.date()
    cutoff = datetime.combine(day, LOCK, TZ)
    if not is_session(day):
        return
    book = read("predictions.json")
    if any(r["date"] == day.isoformat() for r in book["rows"]):
        print("Existing record retained")
        return
    if not clock(8, 40) <= now.time() < LOCK:
        raise RuntimeError("Morning run missed pre-08:45 capture window")
    source_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    forecast = forecast_inputs(day)
    captured = datetime.now(TZ)
    if not now <= captured < cutoff:
        raise RuntimeError("Inputs were not verified before 08:45")
    time.sleep(max(0, (cutoff - datetime.now(TZ)).total_seconds()))
    locked = datetime.now(TZ)
    if not cutoff <= locked < datetime.combine(day, clock(9), TZ):
        raise RuntimeError("Could not lock before market open")
    forecast.update(date=day.isoformat(), time="08:45", status="locked",
                    captured_at=captured.isoformat(), cutoff_at=cutoff.isoformat(),
                    locked_at=locked.isoformat(), on_time=locked < cutoff + timedelta(minutes=1),
                    run_id=os.getenv("GITHUB_RUN_ID"), source_commit=source_commit)
    book["rows"].append(forecast)
    write("predictions.json", book)
    print("Lock recorded", forecast["locked_at"], "from", forecast["captured_at"])


def verify(close, previous, prediction):
    change = (Decimal(str(close["close"])) - Decimal(str(previous["close"]))) * 100 / Decimal(str(previous["close"]))
    outcome = {"date": close["date"], "close": close["close"], "previous": previous,
               "change_pct": float(change), "source": close.get("source", CLOSE_SOURCE), "status": "unlocked"}
    if not prediction:
        return outcome
    captured, cutoff, locked = (datetime.fromisoformat(prediction[k])
                                for k in ("captured_at", "cutoff_at", "locked_at"))
    valid = (prediction["status"] == "locked" and
             cutoff.astimezone(TZ) == datetime.fromisoformat(close["date"] + "T08:45:00+08:00")
             and cutoff - timedelta(minutes=5) <= captured < cutoff <= locked < cutoff + timedelta(minutes=1))
    outcome.update(prediction_sha256=hashlib.sha256(
        json.dumps(prediction, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        direction=prediction["direction"], locked_at=prediction["locked_at"],
        flat_pct=prediction["flat_pct"])
    if not valid:
        outcome["status"] = "late"
        return outcome
    direction = prediction["direction"]
    correct = {"偏多": change > 0, "偏空": change < 0,
               "震盪": abs(change) <= Decimal(str(prediction["flat_pct"]))}[direction]
    outcome["status"] = "correct" if correct else "incorrect"
    return outcome


def stats(rows):
    valid = [r for r in rows if r["status"] in ("correct", "incorrect")]
    def accuracy(sample):
        return sum(r["status"] == "correct" for r in sample) / len(sample) if sample else None
    correct = sum(r["status"] == "correct" for r in valid)
    return {"valid": len(valid), "correct": correct, "incorrect": len(valid) - correct,
            "unlocked": sum(r["status"] == "unlocked" for r in rows),
            "late": sum(r["status"] == "late" for r in rows),
            "overall": accuracy(valid), "last5": accuracy(valid[-5:]), "last20": accuracy(valid[-20:])}


def daily_close(day):
    ymd = day.replace("-", "")
    url = f"{DAILY_SOURCE}?response=json&date={ymd}&type=IND"
    report = fetch(url)
    if report.get("stat") != "OK" or report.get("date") != ymd:
        raise ValueError("Daily TWSE report hasn't published the requested date")
    for table in report.get("tables", []):
        if not table.get("title", "").endswith("價格指數(臺灣證券交易所)"):
            continue
        fields = table["fields"]
        for row in table["data"]:
            if row[fields.index("指數")] == "發行量加權股價指數":
                close = float(row[fields.index("收盤指數")].replace(",", ""))
                if math.isfinite(close) and close > 0:
                    return {"date": day, "close": close, "source": url}
    raise ValueError("Daily TWSE report has no valid TAIEX close")


def publish_close(now):
    day = now.date().isoformat()
    if now.time() < clock(13, 30) or not is_session(now.date()):
        return
    history = read("taiex.json")
    official = {r["date"]: r for r in history["rows"]}
    current = official.get(day)
    if current is None:
        try:
            current = daily_close(day)
        except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
            print("MI_INDEX unavailable:", error)
    if current is None or current.get("trade_value") is None:
        try:
            monthly = next((r for r in fetch_month(day[:7]) if r["date"] == day), None)
        except (OSError, RuntimeError, ValueError) as error:
            print("FMTQIK unavailable:", error)
            monthly = None
        if monthly is not None:
            if current is not None and monthly["close"] != current["close"]:
                raise RuntimeError("TWSE daily and monthly closes disagree")
            current = {**monthly, **(current or {})}
    if current is None:
        raise RuntimeError("Today's official TWSE close is still pending")
    if official.get(day) != current:
        official[day] = current
        history.update(updated_at=datetime.now(TZ).isoformat(timespec="seconds"),
                       rows=[official[key] for key in sorted(official)])
        write("taiex.json", history)
    results = read("verification.json")
    predictions = {r["date"]: r for r in read("predictions.json")["rows"]}
    seen = {r["date"] for r in results["rows"]}
    rows = history["rows"]
    for prior, current in zip(rows, rows[1:]):
        if current["date"] == day and day >= START and day not in seen:
            try:
                outcome = verify(current, prior, predictions.get(day))
            except (KeyError, ValueError, TypeError):
                outcome = {"date": day, "close": current["close"], "previous": prior,
                           "status": "insufficient", "source": CLOSE_SOURCE}
            results["rows"].append(outcome)
    results["rows"].sort(key=lambda r: r["date"])
    results["stats"] = stats(results["rows"])
    write("verification.json", results)


if __name__ == "__main__":
    {"morning": lock_forecast, "close": publish_close}[sys.argv[1]](datetime.now(TZ))
