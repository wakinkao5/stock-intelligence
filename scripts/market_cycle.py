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


def refresh_calendar():
    calendar = read("twse-calendar.json")
    closed = calendar["closed"]
    rows = fetch(CALENDAR_SOURCE)
    years = {roc_date(row["Date"])[:4] for row in rows}
    if len(years) != 1:
        raise RuntimeError("Unexpected TWSE calendar years")
    year = years.pop()
    dates = sorted({roc_date(row["Date"]) for row in rows if
                    not any(word in row["Name"] for word in ("開始交易", "最後交易"))})
    if closed.get(year) != dates:
        closed[year] = dates
        calendar["updated_at"] = datetime.now(TZ).isoformat(timespec="seconds")
        write("twse-calendar.json", calendar)


def is_session(day):
    if day.weekday() >= 5:
        return False
    year = str(day.year)
    closed = read("twse-calendar.json")["closed"]
    if year not in closed:
        raise RuntimeError("TWSE calendar unavailable for this year")
    return day.isoformat() not in closed[year]


def adjacent_session(day, step):
    while True:
        day += timedelta(days=step)
        if is_session(day):
            return day


def forecast_day(now):
    if is_session(now.date()) and now.time() < clock(13, 30):
        return now.date()
    return adjacent_session(now.date(), 1)


# Exchange holidays for the supported calendar years.
US_CLOSED = {
    2026: {"01-01", "01-19", "02-16", "04-03", "05-25", "06-19", "07-03", "09-07", "11-26", "12-25"},
    2027: {"01-01", "01-18", "02-15", "03-26", "05-31", "06-18", "07-05", "09-06", "11-25", "12-24"},
    2028: {"01-17", "02-21", "04-14", "05-29", "06-19", "07-04", "09-04", "11-23", "12-25"},
}


def previous_us_session(day):
    previous = day - timedelta(days=1)
    while previous.weekday() >= 5 or previous.isoformat()[5:] in US_CLOSED.get(previous.year, set()):
        previous -= timedelta(days=1)
    return previous.isoformat()


def forecast_inputs(day, strict=True, as_of=None):
    history = read("taiex.json")
    closes = [r for r in history["rows"] if r["date"] < day.isoformat()]
    if not history["verified"] or len(closes) < 11:
        raise RuntimeError("缺少足夠的證交所收盤歷史（至少 11 筆）")
    previous = adjacent_session(day, -1)
    if closes[-1]["date"] != previous.isoformat():
        raise RuntimeError(f"缺少 {previous} 證交所正式收盤")
    global_data = read("global.json")
    quotes = {r["symbol"]: r for r in global_data["rows"]
              if r.get("session_complete") and not r.get("stale") and math.isfinite(r["change_pct"])}
    expected_us = previous_us_session((as_of.date() if as_of else day))
    us = [quotes[symbol] for symbol in US if symbol in quotes and quotes[symbol]["date"] == expected_us]
    if strict and len(us) != len(US):
        raise RuntimeError(f"Required completed US session {expected_us} unavailable for all four inputs")
    regular = read("day-futures.json") if (DATA / "day-futures.json").exists() else {}
    day_is_current = (regular.get("verified") is True and regular.get("trade_date") == previous.isoformat()
                      and math.isfinite(regular["change_pct"]))
    night = read("night-futures.json")
    night_is_current = (night.get("verified") is True and night.get("trade_date") == day.isoformat()
                        and math.isfinite(night["change_pct"]))
    last = closes[-1]
    momentum = {n: (last["close"] / closes[-1 - n]["close"] - 1) * 100 for n in (5, 10)}
    technical = .6 * max(-1, min(1, momentum[5] / 5)) + .4 * max(-1, min(1, momentum[10] / 8))
    score = technical
    if us:
        us_signal = sum((r["change_pct"] > 0) - (r["change_pct"] < 0) for r in us) / len(us)
        weight = .65 * len(us) / len(US)
        score = weight * us_signal + (1 - weight) * technical
    if day_is_current:
        score = .90 * score + .10 * max(-1, min(1, regular["change_pct"] / 2))
    if night_is_current:
        score = .85 * score + .15 * max(-1, min(1, night["change_pct"] / 2))
    window = closes[-11:]
    typical_pct = sum(abs(current["close"] / prior["close"] - 1) * 100
                      for prior, current in zip(window, window[1:])) / 10
    estimate = round(last["close"] * (1 + score * typical_pct / 100), 2)
    spread = last["close"] * typical_pct / 100
    change = round(estimate - last["close"], 2)
    change_pct = round(100 * change / last["close"], 2)
    direction = "偏多" if change_pct > FLAT_PCT else "偏空" if change_pct < -FLAT_PCT else "震盪"
    reason = "、".join(f"{r['name']} {r['change_pct']:+.2f}%" for r in us)
    reason += f"；上一交易日收盤 {last['close']:,.2f}；台股近 5 日 {momentum[5]:+.2f}%、近 10 日 {momentum[10]:+.2f}%"
    reason += f"；臺指期日盤 {regular['change_pct']:+.2f}%" if day_is_current else "；前一交易日日盤報表尚缺"
    reason += f"；臺指期夜盤 {night['change_pct']:+.2f}%" if night_is_current else "；當日夜盤日報尚缺"
    return {
        "direction": direction, "icon": ICONS[direction], "reason": reason,
        "risk": "海外收盤與台股開盤存在時間差，突發消息及開盤後資金流可能改變方向。",
        "model_version": "projection-3.1", "flat_pct": FLAT_PCT,
        "base_date": last["date"], "base_close": last["close"], "estimated_close": estimate,
        "estimated_range": {"low": round(max(0, estimate - spread), 2), "high": round(estimate + spread, 2)},
        "estimated_change": change, "estimated_change_pct": change_pct,
        "completeness": {"twse_sessions": len(window), "us_count": len(us),
                         "us_target": len(US), "day_futures_available": day_is_current,
                         "overnight_available": night_is_current},
        "inputs": {"twse": {"source": history["source"], "rows": closes[-11:]},
                   "us": us, "day_futures": regular if day_is_current else None,
                   "night": night if night_is_current else None},
    }


def publish_outlook(now):
    day = forecast_day(now)
    official = read("predictions.json")["rows"]
    if any(row["date"] == day.isoformat() for row in official):
        return
    try:
        outlook = forecast_inputs(day, strict=False, as_of=now)
    except RuntimeError as error:
        outlook = {"missing": str(error)}
    outlook.update(date=day.isoformat(), status="provisional")
    path = DATA / "outlook.json"
    if path.exists():
        old = read("outlook.json")
        if outlook == {key: value for key, value in old.items() if key != "generated_at"}:
            return
    outlook["generated_at"] = datetime.now(TZ).isoformat(timespec="seconds")
    write("outlook.json", outlook)


def capture_forecast(now):
    day = now.date()
    cutoff = datetime.combine(day, LOCK, TZ)
    if not is_session(day):
        return
    if not clock(8, 40) <= now.time() < LOCK:
        raise RuntimeError("Morning run missed pre-08:45 capture window")
    if any(row["date"] == day.isoformat() for row in read("predictions.json")["rows"]):
        return
    source_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    draft = forecast_inputs(day)
    captured = datetime.now(TZ)
    if not now <= captured < cutoff:
        raise RuntimeError("Inputs were not verified before 08:45")
    write("forecast-snapshot.json", {**draft, "date": day.isoformat(), "captured_at": captured.isoformat(),
                                     "cutoff_at": cutoff.isoformat(), "source_commit": source_commit,
                                     "capture_run_id": os.getenv("GITHUB_RUN_ID")})
    print("Forecast inputs captured", captured.isoformat())


def lock_forecast(now):
    day = now.date()
    cutoff = datetime.combine(day, LOCK, TZ)
    if not is_session(day):
        return
    book = read("predictions.json")
    if any(r["date"] == day.isoformat() for r in book["rows"]):
        print("Existing record retained")
        return
    deadline = cutoff + timedelta(minutes=1)
    if not cutoff <= now < deadline:
        raise RuntimeError(f"08:45 lock window missed; actual execution {now.isoformat()}")
    if not (DATA / "forecast-snapshot.json").exists():
        raise RuntimeError("No saved pre-08:45 forecast snapshot")
    draft = read("forecast-snapshot.json")
    captured = datetime.fromisoformat(draft["captured_at"])
    if (draft["date"] != day.isoformat() or datetime.fromisoformat(draft["cutoff_at"]) != cutoff
            or not cutoff - timedelta(minutes=5) <= captured < cutoff):
        raise RuntimeError("Saved forecast snapshot is outside today's pre-cutoff window")
    locked = datetime.now(TZ)
    if not cutoff <= locked < deadline:
        raise RuntimeError(f"08:45 lock window missed; actual execution {locked.isoformat()}")
    forecast = {**draft, "time": "08:45", "status": "locked", "locked_at": locked.isoformat(),
                "on_time": True, "run_id": os.getenv("GITHUB_RUN_ID")}
    book["rows"].append(forecast)
    write("predictions.json", book)
    print("Lock recorded", forecast["locked_at"], "from", draft["captured_at"])


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
    if prediction.get("estimated_close") is not None:
        error = Decimal(str(close["close"])) - Decimal(str(prediction["estimated_close"]))
        outcome["index_error_points"] = float(round(error, 2))
    if prediction.get("estimated_change_pct") is not None:
        error = change - Decimal(str(prediction["estimated_change_pct"]))
        outcome["change_error_pp"] = float(round(error, 4))
    interval = prediction.get("estimated_range")
    if interval is not None:
        outcome["range_hit"] = interval["low"] <= close["close"] <= interval["high"]
    return outcome


def stats(rows):
    valid = [r for r in rows if r["status"] in ("correct", "incorrect")]
    def accuracy(sample):
        return sum(r["status"] == "correct" for r in sample) / len(sample) if sample else None
    correct = sum(r["status"] == "correct" for r in valid)
    interval_samples = [r["range_hit"] for r in valid if isinstance(r.get("range_hit"), bool)]
    return {"range_valid": len(interval_samples),
            "range_accuracy": sum(interval_samples) / len(interval_samples) if interval_samples else None,
            "valid": len(valid), "correct": correct, "incorrect": len(valid) - correct,
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


def wait_until(now, local_time):
    target = datetime.combine(now.date(), local_time, TZ)
    if now < target:
        time.sleep((target - now).total_seconds())


if __name__ == "__main__":
    mode = sys.argv[1]
    now = datetime.now(TZ)
    if mode in ("wait-capture", "wait-lock"):
        wait_until(now, clock(8, 40) if mode == "wait-capture" else LOCK)
    else:
        {"capture": capture_forecast, "lock": lock_forecast, "close": publish_close,
         "outlook": publish_outlook}[mode](now)
