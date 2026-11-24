"""Fetch the latest published TAIFEX TX after-hours daily report (not live quotes)."""
import json, re, urllib.request
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

URL = "https://www.taifex.com.tw/cht/3/futDailyMarketExcel?marketCode=1"
TARGET = Path(__file__).resolve().parents[1] / "data/night-futures.json"
class TableReader(HTMLParser):
    def __init__(self):
        super().__init__(); self.rows=[]; self.row=None; self.cell=None; self.all_text=[]
    def handle_starttag(self,tag,attrs):
        if tag=="tr": self.row=[]
        if tag in ("td","th") and self.row is not None: self.cell=[]
    def handle_data(self,data):
        self.all_text.append(data)
        if self.cell is not None: self.cell.append(data)
    def handle_endtag(self,tag):
        if tag in ("td","th") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()));self.cell=None
        if tag=="tr" and self.row is not None:
            if self.row:self.rows.append(self.row)
            self.row=None
def main():
    req=urllib.request.Request(URL,headers={"User-Agent":"Mozilla/5.0","Accept":"text/html","Referer":"https://www.taifex.com.tw/"})
    with urllib.request.urlopen(req,timeout=30) as r:
        raw=r.read()
    html=raw.decode("utf-8",errors="replace")
    parser=TableReader();parser.feed(html)
    text=" ".join(parser.all_text)
    # A trading date on the report is the next regular-session date.
    match=re.search(r"(20\\d{2})[/-](\\d{1,2})[/-](\\d{1,2})\\s*15:00\\s*[~～至-]+\\s*(?:次日)?\\s*05:00",text)
    if not match: raise RuntimeError("Cannot verify after-hours session and trading date; refusing to publish")
    trade_date="-".join([match.group(1),match.group(2).zfill(2),match.group(3).zfill(2)])
    today=datetime.now(ZoneInfo("Asia/Taipei")).date()
    d=datetime.strptime(trade_date,"%Y-%m-%d").date()
    if d>today+timedelta(days=4) or d<today-timedelta(days=7):
        raise RuntimeError("Report trading date out of freshness range")
    contracts=[]
    for cells in parser.rows:
        if len(cells)<8 or cells[0]!="TX" or not re.fullmatch(r"20\\d{4}",cells[1]):continue
        try:
            last=float(cells[5].replace(",",""))
            change=float(re.sub(r"[^0-9.+-]","",cells[6].replace(",","")))
            pct=float(re.sub(r"[^0-9.+-]","",cells[7].replace(",","").replace("%","")))
            if "▼" in cells[6] or "▽" in cells[6]:change=-abs(change)
            if "▼" in cells[7] or "▽" in cells[7]:pct=-abs(pct)
            if not (1000<last<100000 and abs(pct)<=15):continue
            contracts.append((cells[1],last,change,pct))
        except ValueError:continue
    if not contracts:raise RuntimeError("No valid TX futures rows found")
    contract,last,change,pct=sorted(contracts)[0]
    data={"symbol":"TX","contract":contract,"trade_date":trade_date,"last":last,"change":change,"change_pct":pct,"source":"TAIFEX","quote_type":"official_after_hours_daily_report","source_url":URL,"updated_at":datetime.now(ZoneInfo("Asia/Taipei")).isoformat()}
    # Do not overwrite a newer report with an older one.
    if TARGET.exists():
        old=json.loads(TARGET.read_text(encoding="utf-8"))
        if old.get("trade_date","")>trade_date:raise RuntimeError("Existing report is newer")
    TARGET.parent.mkdir(exist_ok=True,parents=True)
    TARGET.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\\n",encoding="utf-8")
    print("Official TX after-hours report:",data)
if __name__=="__main__":main()
