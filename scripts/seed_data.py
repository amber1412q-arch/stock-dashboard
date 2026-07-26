#!/usr/bin/env python3
"""一次性数据填充脚本——用上周五(2026-07-24)数据生成 data.json"""
import requests, json, time, os
from datetime import datetime, date

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'dashboard')
OUTPUT_FILE = os.path.join(OUTPUT_DIR, 'data.json')
KLINE_DIR = os.path.join(OUTPUT_DIR, 'kline')
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(KLINE_DIR, exist_ok=True)

s = requests.Session()

def slow_get(url, params=None, timeout=15):
    time.sleep(2)
    r = s.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r

# ===== 1. 北向资金 =====
print('[1/5] 北向资金...')
northbound = []
try:
    url = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
    r = slow_get(url, params={
        'reportName': 'RPT_MUTUAL_DAILY_STOCK_DETAIL',
        'columns': 'ALL', 'pageNumber': '1', 'pageSize': '10',
        'sortColumns': 'NET_BUY_AMT', 'sortTypes': '-1',
        'token': '894050c76af8597a853f5b408b759f5d',
        'tradeDate': '2026-07-24',
    })
    data = r.json()
    if data.get('success') and data.get('result'):
        for item in data['result']['data'][:5]:
            northbound.append({
                'code': item.get('STOCK_CODE',''),
                'name': item.get('STOCK_NAME',''),
                'price': item.get('CLOSE_PRICE', 0),
                'change_pct': item.get('CHANGE_PCT', 0),
                'net_flow': item.get('NET_BUY_AMT', 0) / 100000000,
            })
    print(f'  -> {len(northbound)} 条')
except Exception as e:
    print(f'  -> 失败: {e}')

# ===== 2. 前高附近强势股 =====
print('[2/5] 前高附近强势股...')
near_high = []
try:
    stocks = []
    for page in range(1, 4):
        r = slow_get('https://push2.eastmoney.com/api/qt/clist/get', params={
            'fid': 'f62', 'po': '1', 'pz': '100', 'pn': str(page),
            'np': '1', 'fltt': '2', 'invt': '2',
            'fs': 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23',
            'fields': 'f2,f3,f12,f14,f62'
        })
        data = r.json()
        if data.get('data') and data['data'].get('diff'):
            for s in data['data']['diff']:
                stocks.append({
                    'code': s.get('f12',''), 'name': s.get('f14',''),
                    'price': s.get('f2',0), 'change_pct': s.get('f3',0),
                    'turnover': s.get('f62',0),
                })
        print(f'  页{page}: +{len(data["data"]["diff"])} 只')
    print(f'  共 {len(stocks)} 只活跃股')

    check_n = min(100, len(stocks))
    for i, stock in enumerate(stocks[:check_n]):
        code = stock['code']
        secid = ('1.' if (code.startswith('6') or code.startswith('5')) else '0.') + code
        try:
            r = slow_get('https://push2his.eastmoney.com/api/qt/stock/kline/get', params={
                'secid': secid, 'fields1': 'f1,f2,f3,f4,f5,f6',
                'fields2': 'f51,f52,f53,f54,f55,f56,f57',
                'klt': '101', 'fqt': '1', 'lmt': '250', 'end': '20500101',
            })
            data = r.json()
            if data.get('data') and data['data'].get('klines'):
                klines = data['data']['klines']
                high_250 = max(float(line.split(',')[3]) for line in klines)
                pct = (stock['price'] - high_250) / high_250 * 100
                if -5 <= pct <= 5:
                    stock['high_250'] = round(high_250, 2)
                    stock['pct_from_high'] = round(pct, 2)
                    # Save kline
                    kline_json = []
                    for line in klines[-60:]:
                        parts = line.split(',')
                        kline_json.append({
                            'date': parts[0], 'open': float(parts[1]),
                            'close': float(parts[2]), 'high': float(parts[3]),
                            'low': float(parts[4]), 'volume': float(parts[5]),
                            'amount': float(parts[6]),
                        })
                    with open(os.path.join(KLINE_DIR, f'{code}.json'), 'w', encoding='utf-8') as f:
                        json.dump(kline_json, f, ensure_ascii=False)
                    near_high.append(stock)
        except Exception:
            pass
        if (i+1) % 20 == 0:
            print(f'  进度: {i+1}/{check_n}, 已找到 {len(near_high)}')

    near_high.sort(key=lambda x: x.get('turnover', 0), reverse=True)
    near_high = near_high[:20]
    for i, s in enumerate(near_high):
        s['rank'] = i + 1
    print(f'  -> 最终 {len(near_high)} 只')
except Exception as e:
    print(f'  -> 失败: {e}')

# ===== 3. ETF 资金流向 =====
print('[3/5] ETF资金流向...')
etfs = []
ETFS = [
    ('510300','沪深300ETF'), ('588000','科创50ETF'), ('159915','创业板ETF'),
    ('588200','科创芯片ETF'), ('515880','通信ETF'), ('588080','科创板ETF'), ('510330','华夏沪深300ETF'),
]
for code, name in ETFS:
    try:
        secid = ('1.' if (code.startswith('6') or code.startswith('5')) else '0.') + code
        r = slow_get('https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get', params={
            'secid': secid, 'fields1': 'f1,f3',
            'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63',
            'klt': '101', 'lmt': '5',
        })
        data = r.json()
        price, change_pct, daily_flow, flow_5d = 0, 0, None, 0
        if data.get('data') and data['data'].get('klines'):
            klines = data['data']['klines']
            for i, line in enumerate(klines):
                parts = line.split(',')
                nf = float(parts[1]) if len(parts) > 1 else 0
                flow_5d += nf
                if i == len(klines) - 1:
                    daily_flow = nf
                    price = float(parts[11]) if len(parts) > 11 else 0
                    change_pct = float(parts[12]) if len(parts) > 12 else 0
        etfs.append({'code': code, 'name': name, 'price': price,
            'change_pct': change_pct, 'daily_flow': daily_flow, 'flow_5d': flow_5d})
        print(f'  {code} {name}: price={price}, flow={daily_flow}')
    except Exception as e:
        print(f'  {code} {name}: 失败({e})')
        etfs.append({'code': code, 'name': name, 'price': 0,
            'change_pct': 0, 'daily_flow': None, 'flow_5d': None})

# ===== 4. 原油 =====
print('[4/5] 原油期货...')
crude_oil = {'sc': None, 'brent': None}
try:
    r = slow_get('https://hq.sinajs.cn/list=nf_SC0', timeout=10)
    r.encoding = 'gb2312'
    text = r.text
    start, end = text.find('"')+1, text.rfind('"')
    parts = text[start:end].split(',')
    if len(parts) >= 8:
        price = float(parts[3]) if parts[3] else 0
        pre = float(parts[2]) if parts[2] else 0
        crude_oil['sc'] = {
            'name': '国内原油SC (INE)', 'price': price, 'pre_close': pre,
            'high': float(parts[4]) if parts[4] else 0,
            'low': float(parts[5]) if parts[5] else 0,
            'change_pct': round((price-pre)/pre*100,2) if pre > 0 else 0,
            'chart_data': [],
        }
    print(f'  SC: {crude_oil["sc"]["price"] if crude_oil["sc"] else "N/A"}')
except Exception as e:
    print(f'  SC失败: {e}')

try:
    r = slow_get('https://hq.sinajs.cn/list=hf_OIL', timeout=10)
    r.encoding = 'gb2312'
    text = r.text
    start, end = text.find('"')+1, text.rfind('"')
    parts = text[start:end].split(',')
    if len(parts) >= 8:
        price = float(parts[3]) if parts[3] else 0
        pre = float(parts[2]) if parts[2] else 0
        crude_oil['brent'] = {
            'name': '布伦特原油 (Brent)', 'price': price, 'pre_close': pre,
            'high': float(parts[4]) if parts[4] else 0,
            'low': float(parts[5]) if parts[5] else 0,
            'change_pct': round((price-pre)/pre*100,2) if pre > 0 else 0,
            'chart_data': [],
        }
    print(f'  Brent: {crude_oil["brent"]["price"] if crude_oil["brent"] else "N/A"}')
except Exception as e:
    print(f'  Brent失败: {e}')

# ===== 5. 逆回购 + 交割日倒计时 =====
print('[5/5] 逆回购 & 交割日倒计时...')
repo = {'maturity_today': None, 'new_release': None, 'trade_date': '2026-07-24'}
DELIVERY_DATES_2026 = [
    '2026-01-16','2026-02-20','2026-03-20','2026-04-17','2026-05-15','2026-06-19',
    '2026-07-17','2026-08-21','2026-09-18','2026-10-16','2026-11-20','2026-12-18',
]
today = date.today()
next_date, days_left = None, None
for ds in DELIVERY_DATES_2026:
    d = date.fromisoformat(ds)
    if d >= today:
        next_date, days_left = ds, (d - today).days
        break
countdown = {'next_date': next_date, 'days_left': days_left,
    'all_dates': DELIVERY_DATES_2026, 'today': today.isoformat()}
print(f'  交割日: {days_left}天后 ({next_date})')

# ===== 保存 =====
dashboard_data = {
    'updated_at': datetime.now().strftime('%Y-%m-%dT%H:%M:%S+08:00'),
    'data_date': '2026-07-24',
    'is_trading_day': False,
    'northbound': northbound,
    'near_high_stocks': near_high,
    'etfs': etfs,
    'crude_oil': crude_oil,
    'repo': repo,
    'delivery_countdown': countdown,
}
with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
    json.dump(dashboard_data, f, ensure_ascii=False, indent=2)

# Also save caches
CACHE_DIR = os.path.join(OUTPUT_DIR, '.cache')
os.makedirs(CACHE_DIR, exist_ok=True)
if near_high:
    with open(os.path.join(CACHE_DIR, 'near_high.json'), 'w', encoding='utf-8') as f:
        json.dump({'date': '2026-07-24', 'data': near_high}, f, ensure_ascii=False)
has_etf = any(e.get('price') and e['price'] > 0 for e in etfs)
if has_etf:
    with open(os.path.join(CACHE_DIR, 'etf.json'), 'w', encoding='utf-8') as f:
        json.dump({'date': '2026-07-24', 'data': etfs}, f, ensure_ascii=False)

print()
print('=' * 50)
print(f'DONE: {OUTPUT_FILE}')
print(f'  北向资金: {len(northbound)} 条')
print(f'  前高附近: {len(near_high)} 条')
print(f'  ETF: {len(etfs)} 条')
print(f'  SC原油: {"YES" if crude_oil["sc"] else "NO"}')
print(f'  布伦特: {"YES" if crude_oil["brent"] else "NO"}')
print(f'  K线文件: {len(os.listdir(KLINE_DIR))} 个')
print(f'  缓存已保存')
print('=' * 50)
