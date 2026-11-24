"""Q4 TAIEX backfill, preserving verified closes; requires FINMIND_TOKEN GitHub secret."""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / 'data' / 'taiex.json'
NOW = datetime.now(ZoneInfo('Asia/Taipei'))
START = date(2026, 10, 1)
API = 'https://api.finmindtrade.com/api/v4/data'


def fetch_day(day, token):
    params = urllib.parse.urlencode({'dataset':'TaiwanVariousIndicators5Seconds', 'start_date':day, 'end_date':day})
    req = urllib.request.Request(API+'?'+params, headers={'Authorization':'Bearer '+token,'Accept':'application/json','User-Agent':'StockIntelligence/3.3'})
    with urllib.request.urlopen(req, timeout=60) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or payload.get('status') != 200:
        raise ValueError('FinMind: '+str(payload.get('msg','unknown'))[:160])
    valid=[]
    for item in payload.get('data',[]):
        stamp=str(item.get('date',''))
        if not stamp.startswith(day+' '):continue
        try: close=float(item['TAIEX'])
        except (KeyError,TypeError,ValueError):continue
        if close>0 and stamp[11:16]=='13:30':valid.append((stamp,close))
    if not valid:return None
    stamp,close=max(valid)
    return {'date':day,'close':close,'trade_value':None}



def enrich_twse_turnover(previous, end):
    """Backfill actual TWSE daily traded value (TWD); never fabricate turnover."""
    months = sorted({key[:7] for key in previous if key <= end.isoformat()})
    total_filled = 0
    for month in months:
        month_date = month.replace('-', '') + '01'
        urls = [
            'https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?response=json&date=' + month_date,
            'https://www.twse.com.tw/exchangeReport/FMTQIK?response=json&date=' + month_date,
        ]
        month_filled = 0
        failures = []
        for url in urls:
            try:
                req = urllib.request.Request(url, headers={
                    'User-Agent': 'Mozilla/5.0 (compatible; StockIntelligence/3.6)',
                    'Accept': 'application/json',
                    'Referer': 'https://www.twse.com.tw/',
                })
                with urllib.request.urlopen(req, timeout=30) as response:
                    payload = json.load(response)
                if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
                    raise ValueError('Missing TWSE data array: ' + str(payload.get('stat', 'unknown')))
                fields = [str(x).replace(' ', '') for x in payload.get('fields', [])]
                date_idx = next((i for i, field in enumerate(fields) if '日期' in field or 'Date' in field), None)
                amount_idx = next((i for i, field in enumerate(fields) if '成交金額' in field or 'TradeValue' in field), None)
                if date_idx is None or amount_idx is None:
                    raise ValueError('Unrecognized TWSE columns: ' + str(fields))
                for record in payload['data']:
                    try:
                        raw = str(record[date_idx]).strip().replace('-', '/')
                        year, mm, dd = [int(x) for x in raw.split('/')]
                        if year < 1911:
                            year += 1911
                        key = f'{year:04d}-{mm:02d}-{dd:02d}'
                        amount = int(str(record[amount_idx]).replace(',', '').strip())
                    except (ValueError, TypeError, IndexError):
                        continue
                    if key in previous and amount > 0:
                        previous[key]['trade_value'] = amount
                        month_filled += 1
                if month_filled:
                    print('TWSE official turnover:', month, month_filled, 'rows from', url)
                    break
                failures.append('No matching dated turnover rows')
            except (urllib.error.URLError, ValueError, TimeoutError, OSError) as exc:
                failures.append(str(exc)[:180])
        total_filled += month_filled
        if not month_filled:
            print('WARNING TWSE turnover unavailable:', month, ' | '.join(failures))
        time.sleep(0.3)
    print('TWSE turnover verified:', total_filled, 'rows')
    latest = max(previous)
    if previous[latest].get('trade_value') is None:
        print('WARNING: latest trading day has no verified official turnover:', latest)
    return total_filled

def main():
    token=os.getenv('FINMIND_TOKEN','').strip()
    if not token:raise RuntimeError('FINMIND_TOKEN missing')
    previous={}
    if TARGET.exists():
        old=json.loads(TARGET.read_text(encoding='utf-8'))
        previous={r['date']:r for r in old.get('rows',[]) if isinstance(r,dict) and r.get('date') and isinstance(r.get('close'),(int,float))}
    end=NOW.date() if NOW.hour>=14 else NOW.date()-timedelta(days=1)
    print(f'Execution time (Asia/Taipei): {NOW.isoformat(timespec="seconds")}')
    print(f'Latest date eligible for 13:30 close: {end.isoformat()}')
    # 2026-09-30 provides a baseline for Q4 return; Q4 reporting starts 10/01.
    start=date(2026,9,30)
    day=start; added=0; errors=[]
    while day<=end:
        key=day.isoformat()
        if day.weekday()<5 and key not in previous:
            try:
                result=fetch_day(key,token)
                if result:previous[key]=result;added+=1;print('Added',key,result['close'])
                else:print('No 13:30 close (holiday/unavailable):',key)
            except (urllib.error.URLError, ValueError, TimeoutError) as exc:
                errors.append(f'{key}: {exc}')
                print('ERROR',key,exc)
            time.sleep(0.25)
        day+=timedelta(days=1)
    if errors:raise RuntimeError('Backfill incomplete, refusing partial commit: '+'; '.join(errors[:8]))
    # The legacy FinMind 5-second series is NOT accepted as verified TAIEX.
    # Replace its closing prices only with official TWSE FMTQIK values.
    official_closes = {}
    for month in sorted({key[:7] for key in previous}):
        url = 'https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?response=json&date=' + month.replace('-', '') + '01'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as response:
                payload = json.load(response)
            fields = [str(v).replace(' ', '') for v in payload.get('fields', [])]
            date_col = next(i for i, v in enumerate(fields) if '日期' in v)
            index_col = next(i for i, v in enumerate(fields) if '發行量加權股價指數' in v)
            for row in payload.get('data', []):
                try:
                    yy, mm, dd = map(int, str(row[date_col]).split('/'))
                    key = f'{yy + 1911 if yy < 1911 else yy:04d}-{mm:02d}-{dd:02d}'
                    close = float(str(row[index_col]).replace(',', ''))
                    if close > 0:
                        official_closes[key] = close
                except (ValueError, IndexError, TypeError):
                    continue
        except (urllib.error.URLError, ValueError, StopIteration, TimeoutError) as exc:
            print('WARNING official index fetch:', month, str(exc))
    if not official_closes:
        raise RuntimeError('No official TWSE index closes; refusing to publish unverified FinMind index')
    # Drop old unverified rows instead of keeping incorrect historical chart values.
    previous = {key: {'date': key, 'close': close, 'trade_value': None}
                for key, close in official_closes.items() if key <= end.isoformat()}
    if not previous:
        raise RuntimeError('No official TWSE rows in eligible date range')
    if not previous:raise RuntimeError('No verified rows')
    enriched = enrich_twse_turnover(previous, end)
    if not enriched and not any(row.get('trade_value') is not None for row in previous.values()):
        raise RuntimeError('TWSE official turnover unavailable from both endpoints; refusing to publish an apparently refreshed but incomplete dataset')
    latest=max(previous)
    print(f'Latest verified close in dataset: {latest}')
    if latest < end.isoformat():
        print(f'WARNING: latest verified close ({latest}) is older than eligible date ({end}); possible holiday, provider delay, or missing close.')
    else:
        print('Market data freshness: latest eligible date is present.')
    result={'source':'TWSE FMTQIK official weighted index and trading value', 'updated_at':NOW.isoformat(timespec='seconds'),'quarter_start':'2026-10-01','rows':sorted(previous.values(),key=lambda r:r['date'])[-130:]}
    TARGET.parent.mkdir(parents=True,exist_ok=True)
    TARGET.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Saved',len(result['rows']),'verified trading days; added',added)

if __name__=='__main__':main()
