"""Build a public snapshot, verify Pages, and send one private Telegram message."""
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import market_cycle as market
import update_intraday

PAGES = "https://wakinkao5.github.io/stock-intelligence/"
SLOTS = {"40 0 * * 1-5": "08:40", "0 2 * * 1-5": "10:00",
         "0 5 * * 1-5": "13:00", "0 8 * * 1-5": "16:00"}
NAMES = {"TAIEX": "加權指數", "TPEX": "上櫃指數", "2330": "台積電"}
VERIFICATION = {"correct": "方向正確", "incorrect": "方向錯誤", "unlocked": "未鎖定，排除統計",
                "late": "延遲鎖定，排除統計", "insufficient": "資料不足，排除統計"}


def stamp(now):
    return now.isoformat(timespec="seconds")


def snapshot_path(day, slot):
    return market.DATA / "official-reports" / f"{day}-{slot.replace(':', '')}.json"


def optional(name):
    return market.read(name) if (market.DATA / name).is_file() else {}


def complete_quotes(day, history, global_data):
    rows = history["rows"]
    if not history.get("verified") or len(rows) < 2 or rows[-1]["date"] != day:
        raise ValueError("證交所正式收盤尚未發布")
    close, prior = rows[-1]["close"], rows[-2]["close"]
    if not all(math.isfinite(n) and n > 0 for n in (close, prior)):
        raise ValueError("加權指數正式收盤數值異常")
    quote_at = day + "T13:30:00+08:00"
    result = [{"symbol": "TAIEX", "value": close, "change_pct": round((close / prior - 1) * 100, 2),
               "quote_at": quote_at, "source": "臺灣證券交易所正式收盤"}]
    for symbol in ("TPEX", "2330"):
        row = next((item for item in global_data["rows"] if item["symbol"] == symbol), None)
        if (not row or row["date"] != day or row.get("session_complete") is not True
                or not math.isfinite(row["close"]) or row["close"] <= 0
                or not math.isfinite(row["change_pct"]) or not row.get("source")):
            raise ValueError(f"{NAMES[symbol]}正式收盤尚未驗證")
        result.append({"symbol": symbol, "value": row["close"], "change_pct": row["change_pct"],
                       "quote_at": quote_at, "source": row["source"]})
    return result


def prepare(now, slot, report):
    day = now.date().isoformat()
    if not market.is_session(now.date()):
        return {}
    previous_status = optional("refresh-status.json")
    manual = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"
    status = {"checked_at": stamp(now), "slot": slot or ("手動" if manual else "背景"),
              "state": "failed", "last_success_at": previous_status.get("last_success_at")}
    try:
        history, global_data = market.read("taiex.json"), market.read("global.json")
        if slot == "08:40":
            try:
                market.publish_morning_slot(now)
            except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                print("08:40 forecast inputs unavailable before cutoff")
        intraday = slot in ("10:00", "13:00") or (not slot and "09:00" <= now.strftime("%H:%M") < "13:30")
        if intraday:
            if not "09:00" <= now.strftime("%H:%M") < "13:30":
                raise ValueError("盤中時段已結束，無法還原原定時段的行情快照")
            series = update_intraday.refresh(now, history["rows"][-1], global_data)
            update_intraday.save_intraday(series)
            quotes = [{**row, "source": series["source"]} for row in series["quotes"]]
            kind = f"盤中延遲至少 {series['delay_minutes']} 分鐘"
            if slot and any(datetime.fromisoformat(quote["quote_at"]) >
                            datetime.fromisoformat(f"{day}T{slot}:00+08:00") for quote in quotes):
                raise ValueError("執行延遲，最新報價晚於原定時段")
        else:
            if now.strftime("%H:%M") >= "13:50":
                try:
                    update_intraday.refresh_final_chart(now)
                except (OSError, ValueError, KeyError, TypeError):
                    print("已授權富果完整分時圖尚未取得")
            quote_day = day if now.strftime("%H:%M") >= "13:30" else market.adjacent_session(now.date(), -1).isoformat()
            if slot == "08:40" and now.strftime("%H:%M") >= "09:00":
                raise ValueError("開盤後不得補作盤前速報")
            quotes = complete_quotes(quote_day, history, global_data)
            kind = "正式收盤" if quote_day == day else "前一交易日正式收盤"
        if not slot and any(row.get("status") == "error" for row in report.get("markets", {}).values()):
            raise ValueError("部分後台來源擷取失敗，已保留已驗證資料")
        international = [dict(name=row["name"], date=row["date"], change_pct=row["change_pct"])
                         for row in global_data["rows"]
                         if row["symbol"] in market.US and row.get("session_complete") is True
                         and row["date"] == market.previous_us_session(now.date())
                         and row["symbol"] not in global_data.get("source_errors", {})]
        locked = next((row for row in market.read("predictions.json")["rows"]
                       if row["date"] == day and row.get("on_time") is True), None)
        provisional = optional("forecast-snapshot.json")
        forecast = locked or (provisional if slot == "08:40" and provisional.get("date") == day else {})
        if slot == "08:40" and not forecast and now.strftime("%H:%M") < "08:45":
            # A report can race the separate snapshot commit. Only estimate during the pre-open input window.
            forecast = market.forecast_inputs(now.date(), strict=False, as_of=now)
        forecast_status = "已鎖定" if locked else "盤前暫定" if forecast else "未鎖定"
        verification = next((x for x in market.read("verification.json")["rows"] if x["date"] == day), {})
        if slot == "16:00" and not verification:
            raise ValueError("今日正式收盤驗證紀錄尚未寫入")
        observation = None
        if slot in ("10:00", "13:00"):
            index = next((quote for quote in quotes if quote["symbol"] == "TAIEX"), None)
            if index and locked:
                change = index["change_pct"]
                direction = "偏多" if change > locked["flat_pct"] else "偏空" if change < -locked["flat_pct"] else "震盪"
                observation = f"盤中觀察：加權指數 {change:+.2f}%，{direction}。盤前原判 {locked['direction']}。"
            elif index:
                observation = f"盤中觀察：加權指數 {index['change_pct']:+.2f}%。盤前原始預測未鎖定。"
        record = {"date": day, "slot": slot, "executed_at": stamp(now), "kind": kind, "quotes": quotes,
                  "international": international, "observation": observation,
                  "forecast": {"direction": forecast.get("direction"), "estimated_close": forecast.get("estimated_close"),
                               "estimated_range": forecast.get("estimated_range"),
                               "estimated_change_pct": forecast.get("estimated_change_pct"), "status": forecast_status},
                  "verification": VERIFICATION.get(verification.get("status"), "等待正式收盤")}
        if slot:
            path = snapshot_path(day, slot)
            path.parent.mkdir(exist_ok=True)
            # Once published, the same slot can be re-delivered without replacing its original data.
            if not path.is_file():
                market.write(str(path.relative_to(market.DATA)), record)
            status["report"] = str(path.relative_to(market.DATA))
        status.update(state="success", last_success_at=stamp(now))
    except (ValueError, RuntimeError) as error:
        status["reason"] = str(error)
    except (TypeError, KeyError, OSError):
        status["reason"] = "行情資料格式、來源或存取驗證失敗"
    if (slot or manual or status["state"] == "success" or
            (status["state"], status.get("reason")) != (previous_status.get("state"), previous_status.get("reason"))):
        market.write("refresh-status.json", status)
    return status


def message(record):
    direction = lambda value: "🔴" if value > 0 else "🟢" if value < 0 else "⚪"
    lines = [f"台股速報｜{record['date']} {record['slot']}", f"實際執行 {record['executed_at']}｜{record['kind']}"]
    for quote in record["quotes"]:
        pct = quote["change_pct"]
        lines.append(f"{direction(pct)} {NAMES[quote['symbol']]} {quote['value']:,.2f}（{pct:+.2f}%）"
                     f"｜{quote['quote_at'][11:16]} {quote['source']}")
    overseas = record["international"]
    if overseas:
        lines.append("國際｜" + "、".join(f"{x['name']} {x['date'][5:]} {x['change_pct']:+.2f}%" for x in overseas))
    else:
        lines.append("國際｜尚無可核對的最新收盤")
    forecast = record["forecast"]
    estimate = f"｜估 {forecast['estimated_close']:,.2f}" if forecast["estimated_close"] is not None else ""
    lines.append(f"預判｜{forecast['status']} {forecast['direction'] or ''}{estimate}".rstrip())
    lines.extend([f"驗證｜{record['verification']}", PAGES])
    return "\n".join(lines)


def verify_pages(path, expected):
    for attempt in range(8):
        try:
            request = urllib.request.Request(PAGES + "data/" + path + "?run=" +
                                             os.environ.get("GITHUB_RUN_ID", "local") + f"-{attempt}",
                                             headers={"Cache-Control": "no-cache"})
            with urllib.request.urlopen(request, timeout=6) as response:
                valid = json.load(response) == expected
            if valid:
                with urllib.request.urlopen(PAGES + "?verify=" + str(attempt), timeout=6) as response:
                    if 'id="market-reports"' in response.read().decode("utf-8"):
                        return True
        except (OSError, ValueError):
            pass
        if attempt < 7:
            time.sleep(7)
    return False


def telegram_send(text):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat or not chat.isdecimal():
        raise ValueError("Telegram Bot Token 或私人 Chat ID 尚未配置")
    payload = json.dumps({"chat_id": chat, "text": text, "link_preview_options": {"is_disabled": True}}).encode()
    try:
        request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=15) as response:
            if not json.load(response).get("ok"):
                raise ValueError()
    except (OSError, ValueError):
        raise ValueError("Telegram API 發送未確認，請查核手機") from None


def reserve(key):
    # Serialize with the existing workflow. A reservation survives ambiguous delivery and retries.
    book = optional("notification-state.json")
    if key in book:
        return False
    now = datetime.now(market.TZ)
    book = {k: v for k, v in book.items()
            if (now.date() - datetime.fromisoformat(v).date()).days <= 30}
    book[key] = stamp(now)
    market.write("notification-state.json", book)
    subprocess.run(["git", "add", "data/notification-state.json"], check=True)
    subprocess.run(["git", "commit", "-m", "Reserve stock notification"], check=True)
    if subprocess.run(["git", "push", "origin", "HEAD:main"]).returncode:
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=True)
        subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)
    return True


def deliver(status):
    if not status:
        return
    current = datetime.now(market.TZ)
    reason = status.get("reason")
    if status["state"] == "success" and not status.get("report"):
        if status["slot"] != "手動" or verify_pages("refresh-status.json", status):
            return
        reason = "正式網站未同步本次手動更新狀態"
    if status.get("report"):
        record = market.read(status["report"])
        if verify_pages(status["report"], record):
            text, key = message(record), status["report"]
        else:
            reason = "正式網站未同步本次資料"
    if reason:
        text = (f"⚠️ 台股更新異常｜{status['checked_at']}\n"
                f"原定時段：{status['slot']}\n原因：{reason}\n"
                f"最後成功更新：{status.get('last_success_at') or '尚無'}\n{PAGES}")
        fingerprint = hashlib.sha256(reason.encode()).hexdigest()[:12]
        key = f"error {current.date()} {current.hour // 2} {fingerprint}"
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID", "").isdecimal():
        raise ValueError("Telegram 通知憑證未完整設定")
    if reserve(key):
        telegram_send(text)
        print("Telegram delivered")
    else:
        print("Duplicate notification suppressed")
    if reason:
        raise ValueError("行情更新或 Pages 同步未通過")


def main():
    path = Path(os.environ["RUNNER_TEMP"]) / "stock-notification.json"
    try:
        if sys.argv[1] == "prepare":
            # The run creation date is immutable across retries. Do not roll a late job
            # into a new trading day.
            started = datetime.fromisoformat(os.environ["SLOT_RUN_CREATED"]).astimezone(market.TZ)
            now = datetime.now(market.TZ)
            slot = SLOTS.get(os.getenv("SCHEDULE"))
            if slot and (started.date() != now.date() or now.strftime("%H:%M") < slot):
                raise ValueError("排程日期或時段不符")
            report_path = Path(os.environ["MARKET_REPORT"])
            report = json.loads(report_path.read_text()) if report_path.is_file() else {}
            path.write_text(json.dumps(prepare(now, slot, report), ensure_ascii=False))
        else:
            deliver(json.loads(path.read_text()))
    except (ValueError, RuntimeError, TypeError, KeyError, OSError, subprocess.SubprocessError) as error:
        # Network/token errors inside the Telegram and Fugle clients are sanitized.
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
