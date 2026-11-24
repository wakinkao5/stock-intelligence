"""Publish the official completed TX night report after settlement reconciliation."""
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, time, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "https://www.taifex.com.tw/cht/3/futDailyMarketExcel"
URL = BASE + "?marketCode=1"
TARGET = Path(__file__).resolve().parents[1] / "data/night-futures.json"
TAIPEI = ZoneInfo("Asia/Taipei")
DATE = r"(20\d{2}/\d{1,2}/\d{1,2})"


class TableReader(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        if tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        self.text.append(data)
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(re.sub(r"\s+", "", "".join(self.cell)))
            self.cell = None
        if tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read().decode("utf-8")


def parse_report(html, night):
    parser = TableReader()
    parser.feed(html)
    text = re.sub(r"\s+", " ", " ".join(parser.text))
    hours = r"15:00\s*[~～]\s*次日\s*05:00\s*盤後交易時段行情表" if night else r"08:45\s*[~～]\s*13:45\s*一般交易時段行情表"
    header = re.search(r"日期\s*[:：]\s*" + DATE + r".*?臺股期貨\s*\(\s*TX\s*\)\s*行情表\s*" + DATE + r"\s*" + hours, text)
    if not header:
        raise ValueError("TAIFEX report type, product, or session dates could not be verified")
    trade_day, session_day = [datetime.strptime(value, "%Y/%m/%d").date() for value in header.groups()]
    columns = ("契約", "到期月份(週別)", "最高價", "最低價", "最後成交價", "漲跌價", "漲跌%", "結算價")
    if night:
        columns += ("*成交量",)
    else:
        columns += ("*一般交易時段成交量",)
    rows = {}
    indices = None
    for row in parser.rows:
        if all(column in row for column in columns):
            indices = {column: row.index(column) for column in columns}
            continue
        if indices is None:
            continue
        if len(row) <= max(indices.values()) or row[indices["契約"]] != "TX":
            break
        fields = {key: row[index] for key, index in indices.items()}
        contract = fields["到期月份(週別)"]
        if re.fullmatch(r"20\d{2}(0[1-9]|1[0-2])", contract):
            if contract in rows:
                raise ValueError("Duplicate TX contract")
            rows[contract] = fields
    if not rows:
        raise ValueError("Missing TX monthly contracts under verified column headings")
    return trade_day, session_day, rows


def number(value):
    if value.startswith(("▼", "▽")):
        value = "-" + value[1:]
    elif value.startswith(("▲", "△")):
        value = value[1:]
    result = Decimal(value.rstrip("%").replace(",", ""))
    if not result.is_finite():
        raise ValueError("Non-finite TAIFEX number")
    return result


def build_report(night_html, fetch_regular, now):
    trade_day, session_day, rows = parse_report(night_html, night=True)
    session_end = datetime.combine(session_day + timedelta(days=1), time(5), TAIPEI)
    if not (session_end <= now and session_day < trade_day <= now.date() and trade_day.weekday() < 5
            and session_day.weekday() < 5 and now.date() - trade_day <= timedelta(days=14)):
        raise ValueError("Invalid TAIFEX night session dates")
    month = trade_day.strftime("%Y%m")
    contract = min(key for key in rows if key >= month)
    row = rows[contract]
    regular_url = BASE + "?" + urllib.parse.urlencode({
        "marketCode": "0", "commodity_id": "TX", "queryDate": session_day.strftime("%Y/%m/%d")
    })
    regular_date, regular_session, regular_rows = parse_report(fetch_regular(regular_url), night=False)
    if regular_date != session_day or regular_session != session_day:
        raise ValueError("Settlement report date does not match the night-session start")
    settlement = number(regular_rows[contract]["結算價"])
    last = number(row["最後成交價"])
    change = number(row["漲跌價"])
    pct = number(row["漲跌%"])
    if not (settlement > 0 and 0 < number(row["最低價"]) <= last <= number(row["最高價"])
            and number(row["*成交量"]) > 0 and last - settlement == change
            and abs(pct - 100 * change / settlement) <= Decimal("0.005")
            and abs(change) <= settlement * Decimal("0.10")):
        raise ValueError("TX trade or published change does not reconcile to regular settlement")
    return {
        "symbol": "TX", "contract": contract, "trade_date": trade_day.isoformat(),
        "session_start": datetime.combine(session_day, time(15), TAIPEI).isoformat(),
        "session_end": session_end.isoformat(),
        "last": int(last), "change": int(change), "change_pct": float(pct),
        "reference_settlement": int(settlement), "verified": True,
        "source": "TAIFEX", "quote_type": "official_after_hours_daily_report",
        "source_url": URL, "settlement_source_url": regular_url,
    }


def main():
    now = datetime.now(TAIPEI)
    result = build_report(fetch(URL), fetch, now)
    if TARGET.exists():
        previous = json.loads(TARGET.read_text(encoding="utf-8"))
        if previous.get("verified") and previous["session_end"] > result["session_end"]:
            raise ValueError("Existing verified night report is newer")
        if all(previous.get(key) == value for key, value in result.items()):
            print("Official TX report unchanged")
            return
    result["updated_at"] = datetime.now(TAIPEI).isoformat(timespec="seconds")
    TARGET.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Verified official TX report:", result)


if __name__ == "__main__":
    main()
