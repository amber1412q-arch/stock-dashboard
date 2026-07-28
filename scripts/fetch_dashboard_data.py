#!/usr/bin/env python3
"""
仪表盘数据抓取 v4 — 精修版（a-stock-data + hhxg-market 整合）
参考: a-stock-data V3.5.0 (simonlin1212) + hhxg-market API
"""
import json, os, sys, time, random, requests
from datetime import datetime, date, timedelta

# ====== 配置 ======
FUYAO_KEY = os.environ.get("FUYAO_KEY", "")  # 从 GitHub Secrets 注入
FUYAO_BASE = "https://fuyao.aicubes.cn"

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'dashboard')
OUTPUT_FILE = os.path.join(OUTPUT_DIR, 'data.json')
KLINE_DIR = os.path.join(OUTPUT_DIR, 'kline')
HISTORY_DIR = os.path.join(OUTPUT_DIR, 'history')
CACHE_DIR = os.path.join(OUTPUT_DIR, '.cache')

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
ZTB_UT = "7eea3edcaed734bea9cbfc24409ed989"

ETF_LIST = [
    ("510300", "沪深300ETF"), ("510500", "中证500ETF"), ("159915", "创业板ETF"),
    ("588000", "科创50ETF"), ("515080", "中证红利ETF"), ("512000", "券商ETF"),
    ("513180", "恒生科技ETF"),
]

DELIVERY_DATES = [
    "2026-01-16","2026-02-20","2026-03-20","2026-04-17","2026-05-15","2026-06-19",
    "2026-07-17","2026-08-21","2026-09-18","2026-10-16","2026-11-20","2026-12-18",
]

# ====== a-stock-data 东财防封 helpers ======
EM_SESSION = requests.Session()
EM_SESSION.headers.update({"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"})
# 绕过系统代理 + 禁用环境变量代理（Windows 常见问题）
EM_SESSION.proxies = {"http": None, "https": None}
EM_SESSION.trust_env = False
EM_MIN_INTERVAL = 0.6  # 稍微加快，网络本身有延迟
_em_last_call = [0.0]

def em_get(url, params=None, headers=None, timeout=15, **kwargs):
    """东财请求入口：自动节流 + 每次新建连接（避免东财session连接池识别）"""
    wait = EM_MIN_INTERVAL - (time.time() - _em_last_call[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.05, 0.3))
    try:
        merged = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}
        if headers:
            merged.update(headers)
        # 每次都直连，不复用 session（东财封 session 连接池）
        return requests.get(url, params=params, headers=merged,
                          timeout=timeout, proxies={"http": None, "https": None})
    finally:
        _em_last_call[0] = time.time()

def eastmoney_datacenter(report_name, columns="ALL", filter_str="",
                          page_size=50, sort_columns="", sort_types="-1"):
    """东财数据中心统一查询（来自 a-stock-data）"""
    params = {
        "reportName": report_name, "columns": columns,
        "filter": filter_str, "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    r = em_get(DATACENTER_URL, params=params, timeout=15)
    d = r.json()
    if d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return []

# ====== 工具函数 ======
def safe_get(url, params=None, timeout=15, retries=2):
    """非东财源的HTTP GET"""
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers={"User-Agent": UA},
                           timeout=timeout, proxies={"http": None, "https": None})
            r.raise_for_status()
            return r
        except Exception:
            if attempt == retries - 1: raise
            time.sleep(1)

def last_trading_day():
    today = date.today()
    if today.weekday() == 0: return today - timedelta(days=3)
    if today.weekday() == 6: return today - timedelta(days=2)
    if today.weekday() == 5: return today - timedelta(days=1)
    return today

def is_trading_day():
    return date.today().weekday() < 5

def get_date_key():
    return last_trading_day().strftime("%Y-%m-%d")

def save_history(data):
    os.makedirs(HISTORY_DIR, exist_ok=True)
    date_key = data.get('data_date', get_date_key())
    filepath = os.path.join(HISTORY_DIR, f'{date_key}.json')
    if not os.path.exists(filepath):
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

def load_history(days=10):
    os.makedirs(HISTORY_DIR, exist_ok=True)
    files = sorted([f for f in os.listdir(HISTORY_DIR) if f.endswith('.json')], reverse=True)
    results = []
    for f in files[:days]:
        try:
            with open(os.path.join(HISTORY_DIR, f), 'r', encoding='utf-8') as fh:
                results.append((f.replace('.json', ''), json.load(fh)))
        except Exception:
            pass
    return results


def load_previous_data():
    """加载上一次部署的 data.json 用于历史对比（MA5/前日）"""
    prev_file = os.path.join(OUTPUT_DIR, 'data_previous.json')
    if os.path.exists(prev_file):
        try:
            with open(prev_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def compute_turnover_comparison(prev_data, current_turnover_yi, today_str):
    """按交易日计算前日对比和MA5。
    关键：盘中每10分钟重跑，历史里会有当天早些时候的记录——必须按日期去重，
    并排除当天日期，否则 prev_turnover 会等于当日值（对比恒为0）。"""
    series = {}  # date_str -> turnover_yi

    # 来源1：history 目录（数据最全）
    for date_str, hd in load_history(10):
        t = hd.get('market_overview', {}).get('total_turnover')
        if t and t > 0:
            series[date_str] = t

    # 来源2：上次 data.json 内置的 turnover_history（[date, turnover]）
    if prev_data:
        for item in prev_data.get('turnover_history', []) or []:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                dt, t = item
                if t and t > 0 and dt not in series:
                    series[dt] = t

    # 排除当天（盘中重跑的自我污染）
    series.pop(today_str, None)

    dates = sorted(series.keys(), reverse=True)
    prev_turnover = series[dates[0]] if dates else None

    # MA5 = 当日 + 最近4个不同交易日
    vals = [current_turnover_yi] + [series[d] for d in dates[:4]]
    vals = [v for v in vals if v and v > 0]
    ma5 = round(sum(vals) / len(vals), 2) if vals else None
    # 真正的5日均量需要4个历史交易日
    ma5_ready = len(dates) >= 4
    return prev_turnover, ma5, ma5_ready

def lookup_name(code):
    try:
        r = requests.get(f"{FUYAO_BASE}/api/meta/tickers/search",
            params={"q": code}, headers={"X-api-key": FUYAO_KEY}, timeout=10)
        data = r.json()
        if data.get('code') == 0 and data['data'].get('item'):
            for item in data['data']['item']:
                if item.get('ticker') == code:
                    return item.get('name_zh', item.get('name', code))
    except Exception:
        pass
    return code


# ====== mootdx 客户端（a-stock-data §Prerequisites，规避 0.11.x BESTIP bug）======
_TDX_SERVERS = [
    ('119.97.185.59', 7709), ('124.70.133.119', 7709), ('116.205.183.150', 7709),
    ('123.60.73.44', 7709),  ('116.205.163.254', 7709), ('121.36.225.169', 7709),
    ('123.60.70.228', 7709), ('124.71.9.153', 7709),    ('110.41.147.114', 7709),
    ('124.71.187.122', 7709),
]

def tdx_client():
    """创建 mootdx 客户端：显式 server + 真实取数验活。海外IP（GitHub Actions）通常全部超时，
    会快速抛 RuntimeError — 调用方需捕获并走备用方案。"""
    import socket
    from mootdx.quotes import Quotes

    def _probe(ip, port, timeout=2.0):
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except Exception:
            return False

    def _validate(c):
        try:
            df = c.bars(symbol='000001', frequency=9, offset=1)
            return df is not None and not df.empty
        except Exception:
            return False

    for ip, port in _TDX_SERVERS:
        if not _probe(ip, port):
            continue
        try:
            c = Quotes.factory(market='std', server=(ip, port))
            if _validate(c):
                return c
        except Exception:
            continue
    for kwargs in ({'bestip': True}, {}):
        try:
            c = Quotes.factory(market='std', **kwargs)
            if _validate(c):
                return c
        except Exception:
            continue
    raise RuntimeError("所有 mootdx 服务器均不可用（海外网络通常全部超时）")


def _tencent_batch_info(codes):
    """腾讯批量行情：code -> {name, price, change_pct, mkt_cap_yi(总市值,44), turnover_rate(换手率,38)}
    字段索引按 a-stock-data §1.2 实测校准：1=名称 3=现价 32=涨跌幅 38=换手率 44=总市值(亿) 45=流通市值(亿)"""
    info = {}
    if not codes:
        return info
    def pfx(c):
        return ('sh' if c.startswith(('5', '6', '9')) else 'sz') + c
    for i in range(0, len(codes), 50):
        batch = codes[i:i+50]
        try:
            r = safe_get(f"https://qt.gtimg.cn/q={','.join(pfx(c) for c in batch)}", timeout=15)
            for line in r.text.strip().split('\n'):
                if '~' not in line or '="' not in line:
                    continue
                parts = line.split('~')
                if len(parts) < 46:
                    continue
                header = line.split('="')[0]
                sym = header.split('_')[-1] if '_' in header else ''
                for c in batch:
                    if pfx(c) == sym:
                        def f(idx):
                            try:
                                return float(parts[idx]) if parts[idx] not in ('', '0') else 0.0
                            except (ValueError, IndexError):
                                return 0.0
                        info[c] = {
                            "name": parts[1] if len(parts) > 1 else '',
                            "price": f(3),
                            "change_pct": round(f(32), 2),
                            "turnover_rate": f(38),
                            "mkt_cap_yi": f(44) or f(45),  # 总市值优先，流通市值兜底
                        }
                        break
        except Exception as e:
            print(f"  腾讯批次{i}失败: {e}")
        if i + 50 < len(codes):
            time.sleep(0.2)
    return info


# ====== 1. 市场总览 ======
def fetch_market_overview():
    print("[1/13] 市场总览...")
    total_turnover = 0
    advancing = declining = limit_up = limit_down = 0

    for offset in range(0, 10000, 100):
        data = {}
        for attempt in range(3):
            try:
                r = requests.get(f"{FUYAO_BASE}/api/a-share/prices/snapshot",
                    params={"limit": 100, "offset": offset},
                    headers={"X-api-key": FUYAO_KEY}, timeout=30)
                data = r.json()
                if data.get('code') == 0:
                    break
            except Exception:
                if attempt < 2:
                    time.sleep(1)
        if data.get('code') != 0:
            print(f"  页{offset}失败(重试3次)，已跳过")
            continue
        items = data['data']['item']
        for s in items:
            t = s.get('turnover', 0) or 0
            if t > 0: total_turnover += t
            chg = s.get('price_change_ratio_pct', 0) or 0
            if chg > 0: advancing += 1
            elif chg < 0: declining += 1
            if chg >= 9.9: limit_up += 1
            elif chg <= -9.9: limit_down += 1
        if len(items) < 100: break
        time.sleep(0.08)

    turnover_yi = round(total_turnover / 1e8, 2)

    # 从持久化数据计算前日对比和 MA5（按交易日去重，排除当天重跑污染）
    prev_data = load_previous_data()
    prev_turnover, ma5_turnover, ma5_ready = compute_turnover_comparison(
        prev_data, turnover_yi, get_date_key())

    print(f"  成交额={turnover_yi}亿 涨{advancing}跌{declining} 前日={prev_turnover} MA5={ma5_turnover} MA5就绪={ma5_ready}")
    return {
        "total_turnover": turnover_yi, "prev_turnover": prev_turnover,
        "ma5_turnover": ma5_turnover, "ma5_ready": ma5_ready,
        "advancing": advancing, "declining": declining,
        "limit_up": limit_up, "limit_down": limit_down,
    }


# ====== 2. 大宗商品（布油/上原油/纽约金/纽约银）======
def _sina_inner_futures_daily(symbol):
    """新浪内盘期货日K（取最后一根的高/低）— nf_ 报价无可靠高低价索引"""
    try:
        r = requests.get(
            f"https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/InnerFuturesNewService.getDailyKLine?symbol={symbol}",
            headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn"},
            timeout=10, proxies={"http": None, "https": None})
        import re as _re
        m = _re.search(r'\((\[.*\])\)', r.text, _re.DOTALL)
        if not m:
            return None
        bars = json.loads(m.group(1))
        if bars:
            last = bars[-1]
            return {"high": float(last.get("h", 0)), "low": float(last.get("l", 0)),
                    "date": last.get("d", "")}
    except Exception:
        pass
    return None


def fetch_commodities():
    print("[2/13] 大宗商品...")
    result = {"brent": None, "sc": None, "gold_comex": None, "silver_comex": None}
    # hf_ 外盘格式实测（2026-07-26）：0=最新 2=昨结 3=今开 4=最高 5=最低
    # nf_ 内盘格式：2=昨结 3=最新，无可靠高低价 → 走日K补充
    configs = [
        ("brent",       "hf_OIL",  0, 2, 4, 5, "布伦特原油"),
        ("sc",          "nf_SC0",  3, 2, None, None, "上海原油SC"),
        ("gold_comex",  "hf_GC",   0, 2, 4, 5, "纽约金(COMEX)"),
        ("silver_comex","hf_SI",   0, 2, 4, 5, "纽约银(COMEX)"),
    ]
    try:
        codes = ",".join(c[1] for c in configs)
        r = requests.get(f"https://hq.sinajs.cn/list={codes}",
            headers={"Referer": "https://finance.sina.com.cn"}, timeout=15,
            proxies={"http": None, "https": None})
        for key, code, pi, pci, hi, li, name in configs:
            for line in r.text.strip().split('\n'):
                if code in line and '="' in line:
                    parts = line.split('="')[1].rstrip('";').split(',')
                    if len(parts) > max(pi, pci):
                        try:
                            price = float(parts[pi]) if parts[pi] else 0
                            preclose = float(parts[pci]) if parts[pci] else 0
                            chg_pct = (price - preclose) / preclose * 100 if preclose else 0
                            high = low = 0
                            if hi is not None and li is not None:
                                high = round(float(parts[hi]), 2) if len(parts) > hi and parts[hi] else 0
                                low = round(float(parts[li]), 2) if len(parts) > li and parts[li] else 0
                            result[key] = {
                                "name": name, "price": round(price, 2),
                                "pre_close": round(preclose, 2),
                                "change_pct": round(chg_pct, 2),
                                "high": high, "low": low,
                            }
                        except (ValueError, IndexError): pass
    except Exception as e:
        print(f"  商品失败: {e}")

    # SC 内盘原油：从日K补高低价
    if result.get("sc") and (not result["sc"]["high"] or not result["sc"]["low"]):
        k = _sina_inner_futures_daily("SC0")
        if k:
            result["sc"]["high"] = round(k["high"], 2)
            result["sc"]["low"] = round(k["low"], 2)
            print(f"  SC高低价来自日K({k['date']}): 高{k['high']} 低{k['low']}")

    for k, v in result.items():
        print(f"  {k}: {'¥'+str(v['price']) if v else '无数据'}")
    return result


# ====== 3. ETF一级市场申赎（用总市值变化代替份额变化）======
def fetch_etf_subscription():
    """ETF 一级市场申赎：腾讯API总市值(亿)，历史对比算1日/5日市值变化"""
    print("[3/13] ETF一级市场申赎...")
    results = []
    cache_file = os.path.join(CACHE_DIR, 'etf_mktcap.json')
    os.makedirs(CACHE_DIR, exist_ok=True)

    # 加载历史总市值缓存  code -> [mkt_cap_t0, mkt_cap_t-1, ...]
    mktcap_history = {}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                mktcap_history = json.load(f)
            print(f"  cache: {len(mktcap_history)} ETFs")
        except Exception:
            pass
    if not mktcap_history:
        prev_data = load_previous_data()
        for e in prev_data.get('etfs', []):
            if e.get('mkt_cap_yi', 0) > 0:
                mktcap_history[e['code']] = [e['mkt_cap_yi']]

    # 批量获取腾讯行情
    prefixes = []
    for code, _ in ETF_LIST:
        pfx = 'sh' + code if (code.startswith('5') or code.startswith('6')) else 'sz' + code
        prefixes.append(pfx)

    qt_data = {}
    try:
        r = safe_get(f"https://qt.gtimg.cn/q={','.join(prefixes)}", timeout=10)
        for line in r.text.strip().split('\n'):
            if '~' not in line: continue
            parts = line.split('~')
            if len(parts) < 50: continue
            pfx = line.split('="')[0].split('_')[-1] if '_' in line.split('="')[0] else ''
            for code, p in zip([c for c,_ in ETF_LIST], prefixes):
                if p == pfx:
                    qt_data[code] = {
                        "price": round(float(parts[3]) if parts[3] else 0, 3),
                        "change_pct": round(float(parts[32]) if parts[32] else 0, 2),
                        "mkt_cap_yi": round(float(parts[44]) if parts[44] else 0, 2),  # 总市值(亿元)
                    }
                    break
    except Exception as e:
        print(f"  Tencent batch failed: {e}")

    for code, name in ETF_LIST:
        q = qt_data.get(code, {})
        price = q.get('price', 0)
        chg_pct = q.get('change_pct', 0)
        mkt_cap_yi = q.get('mkt_cap_yi', 0)

        if price <= 0:
            results.append({"code": code, "name": name, "price": 0, "change_pct": 0,
                "mkt_cap_yi": 0, "mkt_cap_change_1d": None, "mkt_cap_change_5d": None})
            continue

        # 更新历史
        hist = mktcap_history.get(code, [])
        if not hist or abs(hist[0] - mkt_cap_yi) > 0.01:
            hist.insert(0, mkt_cap_yi)
            hist = hist[:10]
            mktcap_history[code] = hist

        # 1日 / 5日 总市值变化（亿元）
        chg_1d = round(hist[0] - hist[1], 2) if len(hist) >= 2 else None
        chg_5d = round(hist[0] - hist[5], 2) if len(hist) >= 6 else (round(hist[0] - hist[-1], 2) if len(hist) >= 2 else None)

        results.append({
            "code": code, "name": name,
            "price": price,
            "change_pct": chg_pct,
            "mkt_cap_yi": mkt_cap_yi,
            "mkt_cap_change_1d": chg_1d,
            "mkt_cap_change_5d": chg_5d,
        })

    # 保存缓存
    clean = {c: h for c, h in mktcap_history.items() if h and h[0] > 0}
    if clean:
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(clean, f, ensure_ascii=False)
        print(f"  cache saved: {len(clean)} ETFs")

    valid = sum(1 for r in results if r.get('price', 0) > 0)
    print(f"  ETF {valid}/{len(results)} OK")
    for r in results:
        chg = r.get('mkt_cap_change_1d')
        chg_s = f"{chg:+.1f}亿" if chg is not None else "--"
        print(f"    {r['code']} {r['name']}: price={r['price']:.3f} mkt_cap={r.get('mkt_cap_yi',0):.0f}亿 1d={chg_s}")
    return results


# ====== 4. 主力资金净买入（市值>800亿）======
def fetch_northbound():
    """主力资金净买入Top5（市值>800亿）。
    注：北向资金实时净买入官方数据2024-08起交易所已停止披露，
    本卡片用东财主力资金流（f62=主力净流入额）作为诚实替代口径。"""
    print("[4/13] 主力资金净买入(市值>800亿)...")
    # 方案A：东财 push2 全A按主力净流入降序
    try:
        r = em_get("https://push2.eastmoney.com/api/qt/clist/get", params={
            "fid": "f62", "po": "1", "pz": "200", "pn": "1", "np": "1",
            "fltt": "2", "invt": "2", "fs": "m:0+t6,m:0+t13,m:1+t2,m:1+t23",
            "fields": "f2,f3,f12,f14,f20,f62"
        }, timeout=10)
        data = r.json()
        results = []
        if data.get('data') and data['data'].get('diff'):
            for item in data['data']['diff']:
                mkt_cap = (item.get("f20", 0) or 0)
                if mkt_cap < 800e8: continue
                net_buy = (item.get("f62", 0) or 0)
                if net_buy <= 0: continue
                results.append({
                    "code": item.get("f12", ""), "name": item.get("f14", ""),
                    "price": item.get("f2", 0), "change_pct": item.get("f3", 0),
                    "market_cap": round(mkt_cap / 1e8, 1),
                    "net_buy": round(net_buy / 1e8, 2),
                })
                if len(results) >= 5: break
        if results:
            print(f"  [东财] 主力净买入Top{len(results)}: {results[0]['name']}+{results[0]['net_buy']}亿")
            return results
    except Exception as e:
        print(f"  东财主力资金失败: {e}")

    # 方案B：新浪资金流（独立风控面，东财被封时兜底）
    print("  尝试新浪资金流 fallback...")
    try:
        results = _main_force_via_sina()
        if results:
            return results
    except Exception as e:
        print(f"  新浪资金流异常: {e}")

    print(f"  主力资金数据不可用（东财盘后更新，或网络受限）")
    return []


def _main_force_via_sina():
    """主力资金净买入Top5（非东财风控面）：
    fuyao全市场快照 → 腾讯批量市值筛>800亿 → 新浪逐股r0_net(主力净流入)排名。
    链条上每一环都实测不封IP，适合东财被风控时兜底。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # 1. fuyao 全市场快照（成交额>2000万；市值>800亿的股票成交额必然远超此线）
    codes = []
    for offset in range(0, 6000, 100):
        data = {}
        for attempt in range(3):
            try:
                r = requests.get(f"{FUYAO_BASE}/api/a-share/prices/snapshot",
                    params={"limit": 100, "offset": offset},
                    headers={"X-api-key": FUYAO_KEY}, timeout=30,
                    proxies={"http": None, "https": None})
                data = r.json()
                break
            except Exception:
                time.sleep(1)
        if data.get('code') != 0:
            print(f"  [新浪链] 快照第{offset}页失败，已得{len(codes)}只")
            break
        items = data['data']['item']
        for s in items:
            if (s.get('turnover') or 0) >= 2e7:
                codes.append(s['ticker'])
        if len(items) < 100:
            break
        time.sleep(0.08)
    print(f"  [新浪链] 快照池 {len(codes)} 只")
    if not codes:
        return []

    # 2. 腾讯批量补市值/名称/价格，筛>800亿
    info = _tencent_batch_info(codes)
    big = {c: v for c, v in info.items() if v.get('mkt_cap_yi', 0) >= 800}
    print(f"  [新浪链] 市值>800亿 {len(big)} 只，逐股查主力净流入...")
    if not big:
        return []

    # 3. 新浪逐股资金流：r0_net=主力净流入（元），取最近一个交易日
    def flow(code):
        pre = ('sh' if code.startswith(('6', '9')) else
               'bj' if code.startswith('8') else 'sz') + code
        try:
            r = requests.get(
                "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                f"MoneyFlow.ssl_qsfx_zjlrqs?page=1&num=1&sort=opendate&asc=0&daima={pre}",
                headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"},
                timeout=10, proxies={"http": None, "https": None})
            t = r.text
            arr = json.loads(t[t.index("["):t.rindex("]") + 1])
            if not arr:
                return None
            return float(arr[0].get('r0_net') or 0)
        except Exception:
            return None

    rows = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(flow, c): c for c in big}
        for fu in as_completed(futs):
            net = fu.result()
            if net is None or net <= 0:
                continue
            v = big[futs[fu]]
            rows.append({
                "code": futs[fu], "name": v.get('name') or futs[fu],
                "price": v.get('price', 0), "change_pct": v.get('change_pct', 0),
                "market_cap": round(v.get('mkt_cap_yi', 0), 1),
                "net_buy": round(net / 1e8, 2),
            })
    rows.sort(key=lambda x: x['net_buy'], reverse=True)
    if rows:
        print(f"  [新浪] 主力净买入Top5: {rows[0]['name']}+{rows[0]['net_buy']}亿")
    return rows[:5]


# ====== 5. 央行OMO（最近一次操作 — 逆回购到期+新投放）======
def fetch_omo():
    """央行公开市场操作：东财7x24快讯 → 财联社快讯 → 金十快讯 → 继承上次
    快讯源参考 a-stock-data §5.2/§5.3（req_trace / 本地签名均为实测可用）"""
    print("[5/13] 央行OMO...")
    import re
    result = {"matured": None, "new_issue": None, "net": None, "trade_date": None}
    reasons = []

    def parse_omo(text, date_str=""):
        """从快讯文本提取OMO数据。只统计逆回购（7天期/14天期/买断式），
        跳过预告（'将...开展'）和MLF等非逆回购操作。
        典型文案：
          央行今日开展4810亿元7天期逆回购操作，中标利率1.40%
          今日有4507亿元7天期逆回购到期，当日实现净投放303亿元
        """
        if '逆回购' not in text or '亿' not in text:
            return None
        # 预告类（"将在7月29日...开展"）不是当日操作，跳过
        if re.search(r'(将|将于|计划在|拟)[^。；]{0,20}?开展', text):
            return None
        new_issue = matured = net = None
        # 新投放：开展X亿...逆回购（限定逆回购，排除MLF等）
        m = re.search(r'开展\s*(\d+[\d,]*\.?\d*)\s*亿[^。；]{0,12}?逆回购', text)
        if m:
            new_issue = float(m.group(1).replace(',', ''))
        # 到期：X亿...逆回购...到期
        m = re.search(r'(\d+[\d,]*\.?\d*)\s*亿[^。；]{0,15}?逆回购[^。；]{0,4}?到期', text)
        if m:
            matured = float(m.group(1).replace(',', ''))
        # 净投放/净回笼（明示优先）
        m = re.search(r'净投放\s*(\d+[\d,]*\.?\d*)\s*亿', text)
        if m:
            net = float(m.group(1).replace(',', ''))
        else:
            m = re.search(r'净回笼\s*(\d+[\d,]*\.?\d*)\s*亿', text)
            if m:
                net = -float(m.group(1).replace(',', ''))
        if new_issue is None and matured is None and net is None:
            return None
        if net is None:
            net = round((new_issue or 0) - (matured or 0), 2)
        return {"new_issue": new_issue or 0, "matured": matured or 0,
                "net": round(net, 2), "trade_date": date_str}

    def scan_news(items, source_name):
        """items: [(text, date_str)] — 收集所有OMO快讯，取最近一个日期并合并当日数据
        （同日可能有7天期+买断式两条操作，合并为当日总投放/总到期）"""
        by_date = {}  # date -> [(new_issue, matured, net)]
        for text, dstr in items:
            r = parse_omo(text, dstr)
            if r and r['trade_date']:
                by_date.setdefault(r['trade_date'], [])
                pair = (r['new_issue'], r['matured'], r['net'])
                if pair not in by_date[r['trade_date']]:  # 去重（title+summary重复）
                    by_date[r['trade_date']].append(pair)
        if not by_date:
            return False
        latest = max(by_date.keys())
        pairs = by_date[latest]
        new_sum = sum(p[0] for p in pairs)
        matured_sum = sum(p[1] for p in pairs)
        # 有明示净额且只有一条时用明示，否则用投放-到期
        nets = [p[2] for p in pairs if p[2] is not None]
        if len(pairs) == 1 and nets:
            net = nets[0]
        else:
            net = round(new_sum - matured_sum, 2)
        result.update({"new_issue": round(new_sum, 2), "matured": round(matured_sum, 2),
                       "net": round(net, 2), "trade_date": latest})
        print(f"  [{source_name}] {latest} 投放={result['new_issue']}亿 到期={result['matured']}亿 净={result['net']}亿 (合并{len(pairs)}条)")
        return True

    # 三个快讯源汇总后统一扫描（东财的"开展"和财联社的"到期"按日期自动合并去重）
    all_items = []

    # 源1：东财7x24快讯（§5.3，必须带 req_trace；sortEnd 分页回溯，周末也能找到周五的操作）
    try:
        import uuid
        sort_end = ""
        for page in range(8):  # 最多8页×200条（快讯流速~15条/小时，≈覆盖4-5天）
            r = em_get("https://np-weblist.eastmoney.com/comm/web/getFastNewsList", params={
                "client": "web", "biz": "web_724", "fastColumn": "102",
                "sortEnd": sort_end, "pageSize": "200", "req_trace": str(uuid.uuid4()),
            }, headers={"Referer": "https://kuaixun.eastmoney.com/"}, timeout=10)
            payload = r.json().get('data') or {}
            news_list = payload.get('fastNewsList', []) or []
            if not news_list:
                break
            for it in news_list:
                all_items.append(((it.get('title', '') + '。' + it.get('summary', '')),
                                  (it.get('showTime', '') or '')[:10]))
            oldest = (news_list[-1].get('showTime', '') or '')[:10]
            if oldest and (date.today() - datetime.strptime(oldest, "%Y-%m-%d").date()).days > 7:
                break
            sort_end = payload.get('sortEnd', '')
            if not sort_end:
                break
        print(f"  东财7x24: {len(all_items)}条")
    except Exception as e:
        reasons.append(f"东财7x24异常:{str(e)[:40]}")

    # 源2：财联社快讯（§5.2，v1 API + 本地签名；rn>50 会返回空）
    try:
        import hashlib
        n0 = len(all_items)
        params = {"appName": "CailianpressWeb", "os": "web", "sv": "7.7.5",
                  "last_time": "", "refresh_type": "1", "rn": "50"}
        qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
        sign = hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()
        r = safe_get(f"https://www.cls.cn/v1/roll/get_roll_list?{qs}&sign={sign}",
                     timeout=10)
        data = r.json()
        for it in (data.get('data') or {}).get('roll_data', []) or []:
            text = (it.get('title', '') or '') + '。' + (it.get('content', '') or it.get('brief', '') or '')
            ts = it.get('ctime')
            dstr = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
            all_items.append((text, dstr))
        print(f"  财联社: {len(all_items)-n0}条")
    except Exception as e:
        reasons.append(f"财联社异常:{str(e)[:40]}")

    # 源3：金十数据快讯
    try:
        n0 = len(all_items)
        r = safe_get("https://www.jin10.com/flash_newest.js", timeout=10)
        json_match = re.search(r'var newest\s*=\s*(\[.*\]);?\s*$', r.text, re.DOTALL)
        flashes = []
        if json_match:
            try:
                flashes = json.loads(json_match.group(1))
            except Exception:
                flashes = []
        for flash in (flashes or [])[:300]:
            if not isinstance(flash, dict):
                continue
            inner = flash.get('data', flash)
            content = inner.get('content', '') if isinstance(inner, dict) else ''
            if not content:
                content = flash.get('title', '')
            all_items.append((content, (flash.get('time', '') or '')[:10]))
        print(f"  金十: {len(all_items)-n0}条")
    except Exception as e:
        reasons.append(f"金十异常:{str(e)[:40]}")

    if scan_news(all_items, "快讯合并"):
        return result
    reasons.append(f"快讯扫描{len(all_items)}条未匹配OMO")

    # 方案D：从上次运行数据继承（跨天持久化，OMO周末/节假日无操作时沿用）
    prev_data = load_previous_data()
    prev_omo = prev_data.get('omo', {}) if prev_data else {}
    if prev_omo.get('matured') is not None or prev_omo.get('new_issue') is not None:
        result['matured'] = prev_omo.get('matured')
        result['new_issue'] = prev_omo.get('new_issue')
        result['net'] = prev_omo.get('net')
        result['trade_date'] = prev_omo.get('trade_date', '')
        print(f"  [继承上次] {result.get('trade_date','?')} 投放={result['new_issue']}亿 到期={result['matured']}亿")
        return result

    print(f"  ⚠️ OMO所有源失败且无历史数据: {'; '.join(reasons)}")
    return result


# ====== 6. 两融余额（市场汇总）======
def fetch_margin_balance():
    """融资融券市场汇总 — hhxg 7日数据 + 东财 datacenter fallback"""
    print("[6/13] 两融余额...")
    result = {"balance": None, "balance_change": None,
              "long_balance": None, "short_balance": None, "trade_date": None}

    # 方案A：hhxg-market 两融专用API（daily_totals）
    try:
        r = safe_get("https://hhxg.top/static/data/assistant/recent_margin_7d.json", timeout=15)
        data = r.json()
        mkt = data.get('market', {})
        totals = mkt.get('daily_totals', [])
        if totals:
            latest = totals[-1]
            result['balance'] = latest.get('rzrqye_yi') or latest.get('rzye_yi', 0)
            result['long_balance'] = latest.get('rzye_yi', 0)
            result['short_balance'] = latest.get('rqye_yi', 0)
            result['trade_date'] = latest.get('date', '')
            # 7日变化
            delta_rz = mkt.get('delta_rzye_yi', 0)
            delta_rq = mkt.get('delta_rqye_yi', 0)
            result['balance_change'] = round((delta_rz or 0) + (delta_rq or 0), 2)
            print(f"  [hhxg] 两融={result['balance']}亿 融资={result['long_balance']}亿 融券={result['short_balance']}亿")
            return result
    except Exception as e:
        print(f"  hhxg两融失败: {e}")

    # 方案B：东财 datacenter
    try:
        data = eastmoney_datacenter(
            "RPTA_WEB_RZRQ_GGMX",
            columns="ALL", page_size=2,
            sort_columns="DATE", sort_types="-1",
        )
        if data:
            latest = data[0]
            result['balance'] = round((latest.get('RZRQYE', 0) or 0) / 1e8, 2)
            result['long_balance'] = round((latest.get('RZYE', 0) or 0) / 1e8, 2)
            result['trade_date'] = str(latest.get('DATE', ''))[:10]
            if len(data) >= 2:
                prev_bal = round((data[1].get('RZRQYE', 0) or 0) / 1e8, 2)
                result['balance_change'] = round(result['balance'] - prev_bal, 2)
            print(f"  [东财] 两融={result['balance']}亿")
            return result
    except Exception as e:
        print(f"  东财两融失败: {e}")

    print(f"  两融数据不可用")
    return result


# ====== 7. 板块表现（申万一级行业 领涨/领跌 Top5）======
def fetch_sector_flow():
    """申万一级行业指数实时行情（申万宏源官方API，akshare index_realtime_sw 同源）
    字段: l3=昨收 l4=今开 l5=成交额 l6=最高 l7=最低 l8=最新 l11=成交量
    失败时降级为东财行业资金流（前端按 source 字段区分展示）"""
    print("[7/13] 申万一级行业...")
    # 方案A：申万宏源官方（该站证书链不完整，akshare 同样 verify=False）
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        r = requests.get(
            "https://www.swsresearch.com/institute-sw/api/index_publish/current/",
            params={"page": "1", "page_size": "50", "indextype": "一级行业"},
            headers={"User-Agent": UA}, timeout=15, verify=False,
            proxies={"http": None, "https": None})
        data = r.json()
        rows = (data.get('data') or {}).get('results') or []
        sectors = []
        for row in rows:
            try:
                last = float(row.get('l8', 0) or 0)
                prev = float(row.get('l3', 0) or 0)
                if prev <= 0 or last <= 0:
                    continue
                sectors.append({
                    "code": row.get('swindexcode', ''),
                    "name": row.get('swindexname', ''),
                    "change_pct": round((last - prev) / prev * 100, 2),
                    "index_val": round(last, 2),
                })
            except (ValueError, TypeError):
                continue
        if sectors:
            sectors.sort(key=lambda x: x['change_pct'], reverse=True)
            up_top5 = sectors[:5]
            down_top5 = sorted(sectors[-5:], key=lambda x: x['change_pct'])
            print(f"  [申万] {len(sectors)}个一级行业, 领涨={up_top5[0]['name']}({up_top5[0]['change_pct']:+.2f}%) 领跌={down_top5[0]['name']}({down_top5[0]['change_pct']:+.2f}%)")
            return {"source": "sw", "inflow_top5": up_top5, "outflow_top5": down_top5}
        print("  申万API返回空，降级东财")
    except Exception as e:
        print(f"  申万API失败: {e}")

    # 方案B：东财行业资金流（降级）
    try:
        r = em_get("https://push2.eastmoney.com/api/qt/clist/get", params={
            "fid": "f128", "po": "1", "pz": "80", "pn": "1", "np": "1",
            "fltt": "2", "invt": "2", "fs": "m:90+t2",
            "fields": "f2,f3,f12,f14,f62,f128,f184"
        }, timeout=10)
        data = r.json()
        sectors = []
        if data.get('data') and data['data'].get('diff'):
            for item in data['data']['diff']:
                net_flow = (item.get("f128", 0) or 0)
                sectors.append({
                    "code": item.get("f12", ""), "name": item.get("f14", ""),
                    "change_pct": item.get("f3", 0),
                    "net_flow": round(net_flow / 1e8, 2),
                    "turnover": round((item.get("f62", 0) or 0) / 1e8, 2),
                })
        if sectors:
            sectors.sort(key=lambda x: x['net_flow'], reverse=True)
            inflow_top5 = [s for s in sectors[:5] if s['net_flow'] > 0]
            outflow_top5 = sorted([s for s in sectors[-5:] if s['net_flow'] < 0], key=lambda x: x['net_flow'])
            print(f"  [东财-降级] 板块 {len(sectors)} 个, 流入Top5={len(inflow_top5)} 流出Top5={len(outflow_top5)}")
            return {"source": "fund_flow", "inflow_top5": inflow_top5, "outflow_top5": outflow_top5}
    except Exception as e:
        print(f"  东财板块失败: {e}")

    # 方案C：hhxg-market sectors fallback
    try:
        r = safe_get("https://hhxg.top/static/data/assistant/skill_snapshot.json", timeout=15)
        data = r.json()
        sectors_data = data.get('sectors', [])
        inflow_top5 = []; outflow_top5 = []
        for sec_group in sectors_data:
            for s in sec_group.get('strong', []):
                inflow_top5.append({
                    "name": s.get('name', ''), "change_pct": s.get('bias_pct', 0),
                    "net_flow": -(s.get('net_yi', 0) or 0),
                    "leader": s.get('leader', ''),
                })
            for s in sec_group.get('weak', []):
                outflow_top5.append({
                    "name": s.get('name', ''), "change_pct": s.get('bias_pct', 0),
                    "net_flow": s.get('net_yi', 0) or 0,
                    "leader": s.get('leader', ''),
                })
        inflow_top5.sort(key=lambda x: x['net_flow'], reverse=True)
        outflow_top5.sort(key=lambda x: x['net_flow'])
        print(f"  [hhxg-降级] 板块流入{len(inflow_top5[:5])} 流出{len(outflow_top5[:5])}")
        return {"source": "fund_flow", "inflow_top5": inflow_top5[:5], "outflow_top5": outflow_top5[:5]}
    except Exception as e:
        print(f"  hhxg板块失败: {e}")

    return {"source": "sw", "inflow_top5": [], "outflow_top5": []}


# ====== 8. 突破压力区（250日前高 ±5%）— fuyao全市场扫描 ======
def fetch_breakout_stocks():
    """全市场扫描距250日高±5%的个股，fuyao历史K线（同十日涨幅方案B），
    防腾讯ifzq WAF限流 + 消除Top500采样偏差"""
    print("[8/13] 突破压力区(250日高)...")
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # 1. fuyao全市场快照池（成交额>0.5亿，重试防截断）
    all_stocks = []
    for offset in range(0, 6000, 100):
        data = {}
        for attempt in range(3):
            try:
                r = requests.get(f"{FUYAO_BASE}/api/a-share/prices/snapshot",
                    params={"limit": 100, "offset": offset},
                    headers={"X-api-key": FUYAO_KEY}, timeout=30,
                    proxies={"http": None, "https": None})
                data = r.json()
                break
            except Exception:
                time.sleep(1)
        if data.get('code') != 0:
            print(f"  快照第{offset}页失败，已得{len(all_stocks)}只")
            break
        for s in data['data']['item']:
            t = s.get('turnover', 0) or 0
            if t < 5e7 or s.get('last_price', 0) <= 0:
                continue
            all_stocks.append({
                "code": s['ticker'], "name": "",
                "price": s['last_price'],
                "change_pct": s.get('price_change_ratio_pct', 0) or 0,
                "turnover": t,
            })
        if len(data['data']['item']) < 100:
            break
        time.sleep(0.08)

    # 按成交额排序取Top1000（平衡覆盖面 vs K线API耗时 ~1000×0.12s≈120s）
    all_stocks.sort(key=lambda x: x['turnover'], reverse=True)
    pool = all_stocks[:1000]
    print(f"  全市场 {len(all_stocks)} 只(成交额>0.5亿), 取Top1000")

    # 2. fuyao历史K线并发查250日高
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 300 * 86400 * 1000  # ~300天覆盖250个交易日

    def suffix(code):
        if code.startswith(('6', '5', '9')): return 'SH'
        if code.startswith(('4', '8')): return 'BJ'
        return 'SZ'

    def check_250d(stock):
        for attempt in range(2):
            try:
                r = requests.get(f"{FUYAO_BASE}/api/a-share/prices/historical",
                    params={"thscode": f"{stock['code']}.{suffix(stock['code'])}",
                            "interval": "1d", "start": start_ms, "end": now_ms,
                            "adjust": "forward"},
                    headers={"X-api-key": FUYAO_KEY}, timeout=15,
                    proxies={"http": None, "https": None})
                d = r.json()
                if d.get('code') != 0:
                    if attempt == 0:
                        time.sleep(0.5)
                        continue
                    return None
                items = d['data']['item']
                if len(items) < 200:  # 次新股K线不足
                    return None
                recent = items[-250:]
                high_250 = max(k['high_price'] for k in recent)
                cp = stock['price']
                if high_250 <= 0 or cp <= 0:
                    return None
                pct = (cp - high_250) / high_250 * 100
                if -5 <= pct <= 5:
                    stock['high_d'] = round(high_250, 2)
                    stock['pct_from_high'] = round(pct, 2)
                    # 缓存K线（最近90日）
                    all_klines = items[-90:]
                    kline_json = [{"date": k['trade_date'], "open": k['open_price'],
                        "close": k['close_price'], "high": k['high_price'],
                        "low": k['low_price'], "volume": k['volume']} for k in all_klines]
                    os.makedirs(KLINE_DIR, exist_ok=True)
                    with open(os.path.join(KLINE_DIR, f'{stock["code"]}.json'), 'w', encoding='utf-8') as f:
                        json.dump(kline_json, f, ensure_ascii=False)
                    return stock
                return None
            except Exception:
                if attempt == 0:
                    time.sleep(0.5)
        return None

    breakout = []
    done = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(check_250d, s): s for s in pool}
        for fu in as_completed(futs):
            done += 1
            if done % 200 == 0:
                print(f"  突破进度:{done}/{len(pool)} 符合={len(breakout)}")
            try:
                r = fu.result()
                if r:
                    breakout.append(r)
            except Exception:
                pass

    breakout.sort(key=lambda x: x.get('turnover', 0), reverse=True)
    top = breakout[:20]

    # 补名称
    if top:
        tinfo = _tencent_batch_info([s['code'] for s in top])
        for j, s in enumerate(top):
            s['rank'] = j + 1
            s['name'] = tinfo.get(s['code'], {}).get('name') or lookup_name(s['code'])

    print(f"  突破榜: {len(top)} 只 (扫描{len(pool)}只 命中{len(breakout)}只)")
    return top


# ====== 9. 交割日倒计时 ======
def calc_countdown():
    today = date.today()
    future = [d for d in DELIVERY_DATES if d >= today.strftime("%Y-%m-%d")]
    next_date = future[0] if future else DELIVERY_DATES[-1]
    days_left = (datetime.strptime(next_date, "%Y-%m-%d").date() - today).days
    return {"next_date": next_date, "days_left": days_left,
            "all_dates": DELIVERY_DATES, "today": today.strftime("%Y-%m-%d")}


# ====== 10. 连板高度（来自 a-stock-data §8.1/8.3）======
def fetch_limit_up_height():
    """涨停池连板高度 — 使用 a-stock-data em_zt_pool 模式"""
    print("[9/13] 连板高度...")
    date_str = datetime.now().strftime("%Y%m%d")
    main_best = {"days": 0, "code": "", "name": "", "change_pct": 0}
    chinext_best = {"days": 0, "code": "", "name": "", "change_pct": 0}

    try:
        r = em_get("https://push2ex.eastmoney.com/getTopicZTPool", params={
            "ut": ZTB_UT, "dpt": "wz.ztzt", "Pageindex": 0,
            "pagesize": 10000, "sort": "fbt:asc", "date": date_str,
        }, headers={"Referer": "https://quote.eastmoney.com/"}, timeout=10)
        pool = (r.json().get("data") or {}).get("pool") or []

        for p in pool:
            code = p.get("c", "")
            name = p.get("n", "")
            days = p.get("lbc", 0) or 0  # 连板数
            chg = round(p.get("zdp", 0), 2)
            entry = {"days": int(days), "code": code, "name": name, "change_pct": chg}

            if code.startswith(("300", "301", "688")):
                if days > chinext_best['days']:
                    chinext_best = entry
            elif code.startswith(("0", "6")):
                if days > main_best['days']:
                    main_best = entry

        print(f"  涨停池 {len(pool)} 只")
    except Exception as e:
        print(f"  涨停池失败: {e}")

    # Fallback: hhxg-market
    if main_best['days'] == 0 and chinext_best['days'] == 0:
        try:
            r = safe_get("https://hhxg.top/static/data/assistant/skill_snapshot.json", timeout=15)
            data = r.json()
            ladder = data.get('ladder', {})
            ts = ladder.get('top_streak', {})
            if ts:
                main_best = {"days": ts.get('main_days', 0), "code": ts.get('main_code', ''),
                            "name": ts.get('main_name', ''), "change_pct": 0}
                chinext_best = {"days": ts.get('chinext_days', 0), "code": ts.get('chinext_code', ''),
                               "name": ts.get('chinext_name', ''), "change_pct": 0}
            print(f"  [hhxg] 连板: 主板{main_best['days']} 创科{chinext_best['days']}")
        except Exception as e:
            print(f"  hhxg连板失败: {e}")

    print(f"  主板: {main_best.get('name','')} {main_best['days']}板")
    print(f"  创科: {chinext_best.get('name','')} {chinext_best['days']}板")
    return {"main_board": main_best, "chinext_star": chinext_best}


# ====== 11. 十日涨幅Top10（全市场真实排名，三级方案）======
def _kline_ret_10d(code):
    """腾讯K线计算单只股票10日涨幅（用于校验/兜底）"""
    pfx = 'sh' if code.startswith(('6', '5')) else 'sz'
    try:
        r = requests.get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": f"{pfx}{code},day,,,20,qfq"},
            headers={"User-Agent": UA}, timeout=10, proxies={"http": None, "https": None})
        klines = r.json().get('data', {}).get(f'{pfx}{code}', {}).get('qfqday', [])
        valid = [k for k in klines if isinstance(k, list) and len(k) >= 3]
        if len(valid) < 11:
            return None
        c0, c1 = float(valid[-1][2]), float(valid[-11][2])
        if c1 <= 0:
            return None
        return round((c0 - c1) / c1 * 100, 2)
    except Exception:
        return None


def _gainers_via_push2():
    """方案A：东财 push2 f160=10日涨跌幅 全市场排序（东财风控较严，本地与Actions均可能被拦，
    被拦时自动落方案B fuyao扫描）；用腾讯K线校验第一名，防字段语义漂移"""
    r = em_get("https://push2.eastmoney.com/api/qt/clist/get", params={
        "fid": "f160", "po": "1", "pz": "40", "pn": "1", "np": "1",
        "fltt": "2", "invt": "2", "fs": "m:0+t6,m:0+t13,m:1+t2,m:1+t23",
        "fields": "f2,f3,f12,f14,f109,f160"
    }, timeout=10)
    items = (r.json().get('data') or {}).get('diff') or []
    rows = []
    for it in items:
        name = it.get('f14', '') or ''
        if name.startswith(('ST', '*ST', 'N', 'C')):
            continue
        ret = it.get('f160')
        if ret is None:
            continue
        rows.append({
            "code": it.get('f12', ''), "name": name,
            "price": it.get('f2', 0), "today_chg": it.get('f3', 0),
            "ret_10d": round(float(ret), 2),
        })
        if len(rows) >= 10:
            break
    if not rows:
        return []
    # 腾讯K线校验第一名（容差3pp，防f160语义变化）
    check = _kline_ret_10d(rows[0]['code'])
    if check is not None and abs(check - rows[0]['ret_10d']) > 3:
        print(f"  ⚠️ push2 f160校验失败: {rows[0]['code']} f160={rows[0]['ret_10d']} vs K线={check}，弃用")
        return []
    print(f"  [push2] 全市场10日涨幅Top10, 最高 {rows[0]['name']}+{rows[0]['ret_10d']}% (K线校验{'通过' if check is not None else '跳过'})")
    return rows


def _gainers_via_fuyao():
    """方案B：fuyao历史K线全市场扫描（同花顺API，国内/海外Actions均可用，实测~0.1s/只）
    这是 Actions 环境下的主力方案：push2被封/mootdx不可达时唯一能做的真·全市场排名"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # 1. 全市场快照池（成交额>2000万，防漏缩量启动股）
    stocks = []
    for offset in range(0, 6000, 100):
        data = {}
        for attempt in range(3):
            try:
                r = requests.get(f"{FUYAO_BASE}/api/a-share/prices/snapshot",
                    params={"limit": 100, "offset": offset},
                    headers={"X-api-key": FUYAO_KEY}, timeout=30,
                    proxies={"http": None, "https": None})
                data = r.json()
                break
            except Exception:
                time.sleep(1)
        if data.get('code') != 0:
            break
        for s in data['data']['item']:
            t = s.get('turnover', 0) or 0
            if t < 2e7 or s.get('last_price', 0) <= 0:
                continue
            stocks.append({
                "code": s['ticker'], "name": "",
                "price": s['last_price'],
                "today_chg": s.get('price_change_ratio_pct', 0) or 0,
            })
        if len(data['data']['item']) < 100:
            break
        time.sleep(0.08)
    print(f"  [fuyao] 扫描池 {len(stocks)} 只")
    if not stocks:
        return []

    # 2. 并发拉历史K线（前复权），算10交易日涨幅
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 40 * 86400 * 1000

    def suffix(code):
        if code.startswith(('6', '5', '9')):
            return 'SH'
        if code.startswith(('4', '8')):
            return 'BJ'
        return 'SZ'

    def calc(stock):
        try:
            r = requests.get(f"{FUYAO_BASE}/api/a-share/prices/historical",
                params={"thscode": f"{stock['code']}.{suffix(stock['code'])}",
                        "interval": "1d", "start": start_ms, "end": now_ms,
                        "adjust": "forward"},
                headers={"X-api-key": FUYAO_KEY}, timeout=15,
                proxies={"http": None, "https": None})
            d = r.json()
            if d.get('code') != 0:
                return None
            items = d['data']['item']
            if len(items) < 11:
                return None
            c0, c1 = items[-1]['close_price'], items[-11]['close_price']
            if not c1 or c1 <= 0:
                return None
            stock['ret_10d'] = round((c0 - c1) / c1 * 100, 2)
            return stock
        except Exception:
            return None

    gainers = []
    done = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(calc, s) for s in stocks]
        for fu in as_completed(futs):
            done += 1
            if done % 1000 == 0:
                print(f"  [fuyao] 进度 {done}/{len(stocks)} 有效={len(gainers)}")
            r = fu.result()
            if r:
                gainers.append(r)
    print(f"  [fuyao] K线完成 {len(gainers)}/{len(stocks)}")
    if not gainers:
        return []
    gainers.sort(key=lambda x: x['ret_10d'], reverse=True)
    # 3. 补名称 + 过滤ST/新股，取前10
    top40 = gainers[:40]
    tinfo = _tencent_batch_info([s['code'] for s in top40])
    rows = []
    for s in top40:
        name = tinfo.get(s['code'], {}).get('name') or s['code']
        if name.startswith(('ST', '*ST', 'N', 'C')):
            continue
        s['name'] = name if name != s['code'] else lookup_name(s['code'])
        rows.append(s)
        if len(rows) >= 10:
            break
    return rows


def _gainers_via_mootdx():
    """方案C：mootdx 全市场扫描（国内网络可用，海外GitHub Actions会快速失败）"""
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # 全市场列表（fuyao快照，成交额>2000万防止漏掉缩量启动股）
    stocks = []
    for offset in range(0, 6000, 100):
        for attempt in range(3):
            try:
                r = requests.get(f"{FUYAO_BASE}/api/a-share/prices/snapshot",
                    params={"limit": 100, "offset": offset},
                    headers={"X-api-key": FUYAO_KEY}, timeout=30,
                    proxies={"http": None, "https": None})
                data = r.json()
                break
            except Exception:
                if attempt == 2:
                    data = {}
                time.sleep(1)
        if data.get('code') != 0:
            break
        for s in data['data']['item']:
            t = s.get('turnover', 0) or 0
            if t < 2e7 or s.get('last_price', 0) <= 0:
                continue
            stocks.append({
                "code": s['ticker'], "name": "",
                "price": s['last_price'],
                "today_chg": s.get('price_change_ratio_pct', 0) or 0,
            })
        if len(data['data']['item']) < 100:
            break
        time.sleep(0.08)
    print(f"  [mootdx] 扫描池 {len(stocks)} 只")
    if not stocks:
        return []

    # 先探活一次：通达信服务器全不可达（海外/被运营商拦）就立刻放弃，
    # 否则每只股票的calc都会重做一轮服务器探测（~30s×5000只，近乎死循环）
    try:
        tdx_client()
    except Exception as e:
        print(f"  [mootdx] 服务器不可达，放弃方案B: {str(e)[:50]}")
        return []

    tls = threading.local()
    def get_client():
        if getattr(tls, 'dead', False):
            return None
        if not hasattr(tls, 'client'):
            try:
                tls.client = tdx_client()
            except Exception:
                tls.dead = True  # 本线程不再重试服务器探测
                return None
        return tls.client

    def calc(stock):
        try:
            client = get_client()
            if client is None:
                return None
            bars = client.bars(symbol=stock['code'], frequency=9, offset=11)
            if bars is None or len(bars) < 11:
                return None
            c0 = float(bars['close'].iloc[-1])
            c1 = float(bars['close'].iloc[0])
            if c1 <= 0:
                return None
            stock['ret_10d'] = round((c0 - c1) / c1 * 100, 2)
            return stock
        except Exception:
            return None

    gainers = []
    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(calc, s) for s in stocks]
        for fu in as_completed(futs):
            done += 1
            if done % 500 == 0:
                print(f"  [mootdx] 进度 {done}/{len(stocks)} 有效={len(gainers)}")
            r = fu.result()
            if r:
                gainers.append(r)
    print(f"  [mootdx] K线完成 {len(gainers)}/{len(stocks)}")
    if not gainers:
        return []
    gainers.sort(key=lambda x: x['ret_10d'], reverse=True)
    # 补名称 + 过滤ST，取前10
    top40 = gainers[:40]
    tinfo = _tencent_batch_info([s['code'] for s in top40])
    rows = []
    for s in top40:
        name = tinfo.get(s['code'], {}).get('name') or s['code']
        if name.startswith(('ST', '*ST', 'N', 'C')):
            continue
        s['name'] = name if name != s['code'] else lookup_name(s['code'])
        rows.append(s)
        if len(rows) >= 10:
            break
    return rows


def fetch_top_10day_gainers():
    """十日涨幅Top10：push2 f160 → fuyao历史K线全市场扫描 → mootdx → 50只采样兜底"""
    print("[10/13] 十日涨幅Top10...")

    # 方案A：东财 push2 f160（国内可达则最省事）
    try:
        rows = _gainers_via_push2()
        if rows:
            return rows
    except Exception as e:
        print(f"  push2涨幅榜失败: {str(e)[:50]}")

    # 方案B：fuyao 历史K线全市场扫描（国内/海外均可用，Actions主力方案）
    try:
        rows = _gainers_via_fuyao()
        if rows:
            print(f"  [fuyao] Top10, 最高 {rows[0]['name']}+{rows[0]['ret_10d']}%")
            return rows
    except Exception as e:
        print(f"  fuyao涨幅榜失败: {str(e)[:60]}")

    # 方案C：mootdx 全市场扫描（国内网络）
    try:
        rows = _gainers_via_mootdx()
        if rows:
            print(f"  [mootdx] Top10, 最高 {rows[0]['name']}+{rows[0]['ret_10d']}%")
            return rows
    except Exception as e:
        print(f"  mootdx涨幅榜失败: {str(e)[:60]}")

    # 方案D：50只采样兜底（有偏，仅保底）
    print("  ⚠️ 降级为50只采样（结果可能漏涨）")
    return _gainers_via_sampling()


def _gainers_via_sampling():
    """旧方案：fuyao高流动性候选50只 + 腾讯K线（有采样偏差，仅兜底）"""
    candidates = []
    try:
        for offset in range(0, 300, 100):
            r = requests.get(f"{FUYAO_BASE}/api/a-share/prices/snapshot",
                params={"limit": 100, "offset": offset},
                headers={"X-api-key": FUYAO_KEY}, timeout=30)
            data = r.json()
            if data.get('code') != 0: break
            for s in data['data']['item']:
                t = s.get('turnover', 0) or 0
                if t < 1e8: continue
                candidates.append({
                    "code": s['ticker'], "name": "",
                    "price": s.get('last_price', 0),
                    "today_chg": s.get('price_change_ratio_pct', 0) or 0,
                })
            time.sleep(0.1)
        candidates.sort(key=lambda x: abs(x.get('today_chg', 0)), reverse=True)
        candidates = candidates[:50]
        tinfo = _tencent_batch_info([c['code'] for c in candidates])
        for c in candidates:
            ti = tinfo.get(c['code'], {})
            if ti.get('name'):
                c['name'] = ti['name']
        candidates = [c for c in candidates if not c['name'].startswith(('ST', '*ST', 'N', 'C'))]
        print(f"  候选: {len(candidates)} 只")
    except Exception as e:
        print(f"  获取候选失败: {e}")

    if not candidates:
        return []

    from concurrent.futures import ThreadPoolExecutor, as_completed
    def calc(stock):
        ret = _kline_ret_10d(stock['code'])
        if ret is None:
            return None
        stock['ret_10d'] = ret
        return stock

    gainers = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(calc, s): s for s in candidates}
        for future in as_completed(futures):
            try:
                r = future.result()
                if r: gainers.append(r)
            except Exception:
                pass

    gainers.sort(key=lambda x: x['ret_10d'], reverse=True)
    top10 = gainers[:10]
    for s in top10:
        if not s.get('name') or s['name'] == s['code']:
            s['name'] = lookup_name(s['code'])
    if top10:
        print(f"  [采样兜底] {len(top10)} 只, 最高+{top10[0]['ret_10d']:.2f}%")
    return top10


# ====== 12. 底部筹码集中度（股东户数降≥5% + 市值>200亿）======
def fetch_chip_concentration():
    """筹码集中 = 股东户数环比降幅≥5%（主力吸筹信号），市值>200亿
    降级：低换手率(<3%)+大市值作为筹码锁定信号（换手率来自腾讯字段38）"""
    print("[11/13] 筹码集中度...")
    results = []
    start_time = time.time()

    try:
        # Step 1: 从fuyao获取候选股（不限市值，fuyao快照无市值字段）
        candidates = []
        for offset in range(0, 300, 100):
            r = requests.get(f"{FUYAO_BASE}/api/a-share/prices/snapshot",
                params={"limit": 100, "offset": offset},
                headers={"X-api-key": FUYAO_KEY}, timeout=30)
            data = r.json()
            if data.get('code') != 0: break
            for s in data['data']['item']:
                t = s.get('turnover', 0) or 0
                if t < 1e8: continue  # 成交额>1亿
                candidates.append({
                    "code": s['ticker'], "name": "",
                    "price": s.get('last_price', 0),
                    "change_pct": s.get('price_change_ratio_pct', 0) or 0,
                    "market_cap": 0,
                    "turnover_rate": 0,
                    "turnover": round(t / 1e8, 2),
                })
            if len(data['data']['item']) < 100: break
            time.sleep(0.08)
        candidates.sort(key=lambda x: x['turnover'], reverse=True)
        candidates = candidates[:200]
        print(f"  候选: {len(candidates)} 只（fuyao, 成交额>1亿）")

        if not candidates:
            print(f"  ⚠️ 无候选股")
            return []

        # Step 2: 腾讯批量行情（名称 + 总市值[44] + 换手率[38]，一次50只）
        tinfo = _tencent_batch_info([c['code'] for c in candidates])
        for s in candidates:
            ti = tinfo.get(s['code'], {})
            s['name'] = ti.get('name') or s['code']
            s['market_cap'] = round(ti.get('mkt_cap_yi', 0), 1)
            s['turnover_rate'] = ti.get('turnover_rate', 0)
        # 名称补齐后过滤 ST/新股
        candidates = [c for c in candidates if not c['name'].startswith(('ST', '*ST', 'N', 'C'))]
        print(f"  腾讯行情映射: {len(tinfo)} 只, 过滤ST后: {len(candidates)} 只")

        # Step 2b: push2 fallback（腾讯市值大面积缺失时）
        if sum(1 for s in candidates if s['market_cap'] > 0) < 10:
            print(f"  尝试push2 fallback...")
            try:
                r = em_get("https://push2.eastmoney.com/api/qt/clist/get", params={
                    "fid": "f20", "po": "1", "pz": "200", "pn": "1", "np": "1",
                    "fltt": "2", "invt": "2", "fs": "m:0+t6,m:0+t13",
                    "fields": "f12,f20"
                }, timeout=10)
                data = r.json()
                if data.get('data') and data['data'].get('diff'):
                    cap_map = {it.get('f12', ''): round((it.get('f20', 0) or 0) / 1e8, 1)
                               for it in data['data']['diff']}
                    for s in candidates:
                        if not s['market_cap'] and cap_map.get(s['code']):
                            s['market_cap'] = cap_map[s['code']]
                print(f"  [push2] 市值补齐后: {sum(1 for s in candidates if s['market_cap'] > 0)} 只")
            except Exception as e:
                print(f"  push2 fallback失败: {e}")

        # Step 3: 筛选市值>200亿
        qualified = [s for s in candidates if s['market_cap'] >= 200]
        if qualified:
            qualified.sort(key=lambda x: x['market_cap'], reverse=True)
            print(f"  市值>200亿: {len(qualified)} 只")
        else:
            qualified = sorted(candidates, key=lambda x: x['turnover'], reverse=True)[:40]
            print(f"  无市值数据，用成交额Top40: {len(qualified)} 只")

        # Step 4: 股东户数 — in 过滤一次查全部候选（§4.3 同款报表）
        try:
            codes = qualified[:60]  # 控制filter长度
            code_list = ','.join(f'"{c["code"]}"' for c in codes)
            hdata = eastmoney_datacenter(
                "RPT_HOLDERNUMLATEST",
                columns="SECURITY_CODE,SECURITY_NAME_ABBR,HOLDER_NUM_RATIO,END_DATE",
                filter_str=f"(SECURITY_CODE in ({code_list}))",
                page_size=100, sort_columns="END_DATE", sort_types="-1",
            )
            if hdata:
                holder_map = {}
                for h in hdata:
                    code = h.get('SECURITY_CODE', '')
                    if code and code not in holder_map:
                        holder_map[code] = {
                            'ratio': h.get('HOLDER_NUM_RATIO', 0) or 0,
                            'date': str(h.get('END_DATE', ''))[:10],
                        }
                print(f"  股东户数映射: {len(holder_map)}/{len(codes)} 条")
                for stock in qualified:
                    h = holder_map.get(stock['code'])
                    if h and h['ratio'] <= -5:
                        stock['concentration_90'] = abs(round(h['ratio'], 2))
                        stock['holder_date'] = h['date']
                        results.append(stock)
                print(f"  户数降≥5%: {len(results)} 只")
            else:
                print(f"  [诊断] 股东户数API返回空")
        except Exception as e:
            print(f"  股东户数查询异常: {e}")

        # Step 5: 降级方案 — 真实换手率<3% + 大市值（筹码锁定信号）
        if not results:
            print(f"  降级方案: 低换手率筛选...")
            for stock in qualified:
                tr = stock.get('turnover_rate', 0)
                if 0 < tr < 3.0 and stock.get('market_cap', 0) >= 200:
                    stock['concentration_90'] = round((3.0 - tr) * 3 + (stock['market_cap'] / 500), 1)
                    results.append(stock)
            results.sort(key=lambda x: x.get('concentration_90', 0), reverse=True)
            print(f"  降级命中: {len(results)} 只")

        results.sort(key=lambda x: x.get('market_cap', 0), reverse=True)
        top = results[:20]
        elapsed = time.time() - start_time
        print(f"  筹码集中: {len(top)} 只 (耗时{elapsed:.0f}s)")
        return top
    except Exception as e:
        print(f"  筹码集中失败: {e}")
        import traceback; traceback.print_exc()
        return []


# ====== 13. 游资龙虎榜（hhxg-market + 东财datacenter fallback）======
def fetch_hotmoney():
    print("[12/13] 游资龙虎榜...")
    # 方案A：hhxg-market（主力，数据格式化好）
    try:
        r = safe_get("https://hhxg.top/static/data/assistant/skill_snapshot.json", timeout=15)
        data = r.json()
        hm = data.get('hotmoney', {})
        top_buy = hm.get('top_net_buy', [])
        seats = hm.get('seats', [])
        total_net = hm.get('total_net_yi', 0)

        entries = []
        for item in top_buy[:10]:
            stock_seats = []
            for seat in seats:
                for s in seat.get('stocks', []):
                    if s.get('name') == item.get('name'):
                        stock_seats.append(seat.get('name', ''))
            entries.append({
                "name": item.get('name', ''),
                "code": "",
                "net_buy": item.get('net_yi', 0),
                "ratio_pct": item.get('ratio_pct', 0),
                "seats": stock_seats[:3],
            })

        seat_list = []
        for seat in seats[:10]:
            seat_stocks = [{"name": s.get('name', ''), "net_yi": s.get('net_yi', 0)}
                          for s in seat.get('stocks', [])[:5]]
            seat_list.append({"name": seat.get('name', ''), "stocks": seat_stocks})

        result = {"date": hm.get('date', data.get('date', '')),
                  "total_net_yi": total_net, "entries": entries, "seats": seat_list}
        if entries:
            print(f"  [hhxg] 龙虎榜 {len(entries)} 只, 总净买{total_net}亿")
            return result
    except Exception as e:
        print(f"  hhxg龙虎榜失败: {e}")

    # 方案B：东财datacenter龙虎榜（a-stock-data §7 报表）
    try:
        print(f"  尝试东财龙虎榜...")
        data = eastmoney_datacenter(
            "RPT_DAILYBILLBOARD_DETAILSNEW",
            columns="SECURITY_CODE,SECURITY_NAME_ABBR,CLOSE_PRICE,CHANGE_RATE,NET_BUY_AMT,TOTAL_BUY_AMT,TOTAL_SELL_AMT,TRADE_DATE",
            page_size=20, sort_columns="NET_BUY_AMT", sort_types="-1",
        )
        if data:
            entries = []
            for row in data[:10]:
                net_buy = (row.get('NET_BUY_AMT', 0) or 0) / 1e8
                entries.append({
                    "name": row.get('SECURITY_NAME_ABBR', ''),
                    "code": row.get('SECURITY_CODE', ''),
                    "net_buy": round(net_buy, 2),
                    "ratio_pct": row.get('CHANGE_RATE', 0) or 0,
                    "seats": [],
                })
            total_net = round(sum(e['net_buy'] for e in entries), 2)
            result = {"date": str(data[0].get('TRADE_DATE', ''))[:10],
                      "total_net_yi": total_net, "entries": entries, "seats": []}
            print(f"  [东财] 龙虎榜 {len(entries)} 只, 总净买{total_net}亿")
            return result
        print(f"  东财龙虎榜返回空")
    except Exception as e:
        print(f"  东财龙虎榜失败: {e}")

    print(f"  龙虎榜数据不可用")
    return {"date": "", "total_net_yi": 0, "entries": [], "seats": []}


# ====== 主流程 ======
def main():
    import argparse

    parser = argparse.ArgumentParser(description="仪表盘数据抓取")
    parser.add_argument("--skip-daily", action="store_true",
                        help="盘中模式：跳过十日涨幅和突破榜的全市场扫描（收盘后运行再补）")
    args = parser.parse_args()

    print("=" * 60)
    print(f"📊 仪表盘数据抓取 v4 — {datetime.now().isoformat()}")
    trading = is_trading_day()
    print(f"   交易日: {'是' if trading else '否'}")
    if args.skip_daily:
        print(f"   模式: 盘中（跳过十日涨幅/突破榜全市场扫描）")
    print("=" * 60)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(KLINE_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    # 非交易日：直接用上次交易日的数据，只更新时间戳和交割日倒计时
    if not trading:
        prev = load_previous_data()
        if prev and prev.get('market_overview', {}).get('total_turnover', 0) > 0:
            print("⏸️ 非交易日，沿用上次交易日数据")
            prev['updated_at'] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00")
            prev['is_trading_day'] = False
            prev['delivery_countdown'] = calc_countdown()
            # 空卡片补抓：以下板块代码近期有优化（候选池扩大等），
            # 非交易日补抓一次确保最新逻辑生效——行情类数据源返回的仍是最近交易日口径
            if not prev.get('northbound'):
                print("   主力净买入为空，补抓...")
                prev['northbound'] = fetch_northbound()
            if not prev.get('top_10day_gainers'):
                print("   十日涨幅为空，补抓...")
                prev['top_10day_gainers'] = fetch_top_10day_gainers()
            # 筹码集中始终补抓（候选池100→200，需要覆盖旧数据）
            print("   筹码集中补抓...")
            prev['chip_concentration'] = fetch_chip_concentration()
            # 更新ETF缓存（市值数据在非交易日不变，保持缓存连续性）
            with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
                json.dump(prev, f, ensure_ascii=False, indent=2)
            print(f"✅ 沿用上次数据: {prev.get('data_date','?')}")
            print(f"   市场: 成交额={prev.get('market_overview',{}).get('total_turnover','?')}亿")
            print(f"   主力净买入: {len(prev.get('northbound',[]))}只")
            print(f"   十日涨幅: {len(prev.get('top_10day_gainers',[]))}只")
            print(f"   突破榜: {len(prev.get('breakout_stocks',[]))}只")
            print(f"   筹码集中: {len(prev.get('chip_concentration',[]))}只")
            print(f"   交割日: {prev.get('delivery_countdown',{}).get('days_left','?')}天")
            print("=" * 60)
            return

    # 交易日：正常抓取所有数据
    market = fetch_market_overview()       # 1
    commodities = fetch_commodities()      # 2
    etf_flow = fetch_etf_subscription()    # 3
    northbound = fetch_northbound()        # 4
    omo = fetch_omo()                      # 5
    margin = fetch_margin_balance()        # 6
    sector_flow = fetch_sector_flow()      # 7

    # 盘中模式：跳过全市场扫描步骤，沿用上次数据
    prev_data = load_previous_data()
    if args.skip_daily:
        print("⏩ 盘中模式：跳过突破榜和十日涨幅全市场扫描")
        breakout = (prev_data or {}).get('breakout_stocks', [])
        top_gainers = (prev_data or {}).get('top_10day_gainers', [])
        if not breakout:
            print("   上次突破榜为空，补抓...")
            breakout = fetch_breakout_stocks()
        if not top_gainers:
            print("   上次十日涨幅为空，补抓...")
            top_gainers = fetch_top_10day_gainers()
        print(f"   沿用: 突破榜{len(breakout)}只, 十日涨幅{len(top_gainers)}只")
    else:
        breakout = fetch_breakout_stocks()     # 8
        top_gainers = fetch_top_10day_gainers() # 11

    countdown = calc_countdown()           # 9
    limit_up = fetch_limit_up_height()     # 10
    chip_conc = fetch_chip_concentration()  # 12
    hotmoney = fetch_hotmoney()            # 13 游资龙虎榜

    # 构建 turnover_history（持久化最近5天成交额，用于下次MA5计算）
    turnover_history = prev_data.get('turnover_history', []) if prev_data else []
    # 把当前日期和成交额插入最前面
    today_turnover = market['total_turnover']
    if today_turnover and today_turnover > 0:
        today_str = get_date_key()
        # 避免同一天重复插入
        if not turnover_history or turnover_history[0][0] != today_str:
            turnover_history.insert(0, [today_str, today_turnover])
        turnover_history = turnover_history[:10]  # 保留最近10天

    # 组装
    dashboard = {
        "updated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "data_date": get_date_key(),
        "is_trading_day": trading,
        "market_overview": market,
        "turnover_history": turnover_history,
        "commodities": commodities,
        "etfs": etf_flow,
        "northbound": northbound,
        "omo": omo,
        "margin": margin,
        "sector_flow": sector_flow,
        "breakout_stocks": breakout,
        "delivery_countdown": countdown,
        "limit_up_height": limit_up,
        "top_10day_gainers": top_gainers,
        "chip_concentration": chip_conc,
        "hotmoney": hotmoney,
    }

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(dashboard, f, ensure_ascii=False, indent=2)
    save_history(dashboard)

    print("=" * 60)
    print(f"✅ 数据已保存")
    print(f"   市场: 成交额={market['total_turnover']}亿 (MA5={market.get('ma5_turnover','?')}亿)")
    print(f"   商品: {sum(1 for v in commodities.values() if v)}项")
    print(f"   ETF: {len(etf_flow)}只")
    print(f"   主力净买入: {len(northbound)}只(市值>800亿)")
    print(f"   OMO: 到期={omo.get('matured','?')}亿 新放={omo.get('new_issue','?')}亿")
    print(f"   两融: {margin.get('balance','?')}亿")
    print(f"   板块: 流入{len(sector_flow.get('inflow_top5',[]))} 流出{len(sector_flow.get('outflow_top5',[]))}")
    print(f"   突破榜: {len(breakout)}只(250日)")
    print(f"   连板: 主板{limit_up.get('main_board',{}).get('days',0)} 创科{limit_up.get('chinext_star',{}).get('days',0)}")
    print(f"   10日涨幅: {len(top_gainers)}只")
    print(f"   筹码集中: {len(chip_conc)}只")
    print(f"   龙虎榜: {len(hotmoney.get('entries',[]))}条")
    print(f"   交割日: {countdown['days_left']}天")
    print("=" * 60)


if __name__ == "__main__":
    main()
