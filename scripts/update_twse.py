"""Retrieve TWSE official daily TAIEX historical closes. No invented fallback data."""
import json
import os
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / 'data' / 'taiex.json'
NOW = datetime.now(ZoneInfo('Asia/Taipei'))

def fetch_month(year, month):
    url = f'https://www.twse.com.tw/indicesReport/FMTQIK?response=json&date={year:04d}{month:02d}01'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; StockIntelligence/2.0)', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=25) as response:
        payload = json.load(response)
    if payload.get('stat') != 'OK' or not isinstance(payload.get('data'), list):
        raise ValueError(f'TWSE response not OK for {year}-{month}: {payload.get("stat")}')
    fields = payload.get('fields', [])
    def find(*keys):
        for i, field in enumerate(fields):
            if any(k in field for k in keys): return i
        raise ValueError(f'Missing TWSE field {keys}: {fields}')
    date_i = find('日期')
    close_i = find('發行量加權股價指數', '加權指數')
    value_i = next((i for i, f in enumerate(fields) if '成交金額' in f), None)
    out=[]
    for row in payload['data']:
        try:
            roc, m, d = [int(x) for x in row[date_i].strip().split('/')]
            date=f'{roc+1911:04d}-{m:02d}-{d:02d}'
            close=float(row[close_i].replace(',', ''))
            value=float(row[value_i].replace(',', '')) if value_i is not None and row[value_i] not in ('--','') else None
            out.append({'date':date,'close':close,'trade_value':value})
        except (ValueError, IndexError, TypeError):
            continue
    return out

def months_back(count):
    y,m=NOW.year,NOW.month
    for _ in range(count):
        yield y,m
        m-=1
        if m==0:y,m=y-1,12

def main():
    previous={}
    if TARGET.exists():
        try: previous={r['date']:r for r in json.loads(TARGET.read_text())['rows']}
        except (ValueError,KeyError,TypeError): pass
    failures=[];successes=0
    for y,m in months_back(3):
        try:
            rows=fetch_month(y,m)
            for r in rows: previous[r['date']]=r
            successes+=1
        except Exception as exc:
            failures.append(f'{y}-{m:02d}: {exc}')
        time.sleep(0.8)
    if successes==0:
        raise RuntimeError('No TWSE month could be fetched; refusing to publish fake/stale overwrite. '+ '; '.join(failures))
    rows=sorted(previous.values(),key=lambda r:r['date'])[-120:]
    if not rows: raise RuntimeError('No valid trading rows')
    data={'source':'TWSE official indicesReport/FMTQIK','updated_at':NOW.isoformat(timespec='seconds'),'rows':rows}
    TARGET.parent.mkdir(parents=True,exist_ok=True)
    TARGET.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    print(f'Saved {len(rows)} rows through {rows[-1]["date"]}. Month failures: {failures}')

if __name__=='__main__':main()
