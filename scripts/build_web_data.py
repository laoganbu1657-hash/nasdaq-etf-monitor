#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import subprocess
import time
from urllib.parse import urlencode
from pathlib import Path

import akshare as ak
import pandas as pd
import requests
from market_extras import enrich_payload


ROOT = Path(__file__).resolve().parents[1]
DATA_CSV = ROOT / "data" / "nasdaq_qdii_live_premium.csv"
FEE_CSV = ROOT / "data" / "nasdaq_etf_fee.csv"
MARGIN_CSV = ROOT / "data" / "margin_history.csv"
OUT = ROOT / "docs" / "data" / "funds.json"
OUT_JS = ROOT / "docs" / "data" / "funds.js"
OUT_PREMIUM_HISTORY = ROOT / "docs" / "data" / "premium_history.json"
OUT_PREMIUM_HISTORY_JS = ROOT / "docs" / "data" / "premium_history.js"

# 融资融券明细：交易所每个交易日收盘后披露（一般在当晚，深市更早）。
# 取最近 15 个交易日即可覆盖「近10日净买入」= 余额较10个交易日前变化的窗口，
# 多留几天余量防止某天抓取失败造成断档。缓存为增量 CSV，每次只抓缺的日期。
MARGIN_WINDOW_DAYS = 15
# 只缓存监控列表里可能是融资标的的代码（LOF 161128/161130 通常不是标的，剔除）。
# 覆盖性检查用「该市场至少出现一只监控代码」判定，个别代码不是标的不会导致重复抓取。
MARGIN_EXPECTED_SH = {"513100", "513110", "513300", "513390", "513500", "513650", "513850", "513870"}
MARGIN_EXPECTED_SZ = {"159501", "159509", "159513", "159632", "159659", "159660", "159696", "159941", "159612", "159655", "159577"}

DISPLAY_NAMES = {
    "159501": "嘉实纳指100",
    "159509": "景顺长城纳指科技",
    "159513": "大成纳指100",
    "159632": "华安纳指100",
    "159659": "招商纳指100",
    "159660": "汇添富纳指100",
    "159696": "易方达纳指100",
    "159941": "广发纳指100",
    "513100": "国泰纳指100",
    "513110": "华泰柏瑞纳指100",
    "513300": "华夏纳指100",
    "513390": "博时纳指100",
    "513870": "富国纳指100",
    "513500": "博时标普500",
    "159612": "国泰标普500",
    "513650": "南方标普500",
    "159655": "华夏标普500",
    "161128": "易方达标普信息科技",
    "161130": "易方达纳指100LOF",
    "513850": "易方达美国50",
    "159577": "汇添富美国50",
}

FUND_INDEX_SYMBOLS = {
    # 易方达标普信息科技 LOF：跟踪标普500信息科技指数（雪球 .SP500-45）。
    # Yahoo 只有最新点位、新浪无该代码，用窗口内真实指数比 XLK 代理准 4 倍
    # （10 月 16 个交易日：锚误差 MAE 6.7bp vs XLK×0.95 的 28.2bp）。
    "161128": ".SP500-45",
    # 景顺长城纳指科技 ETF：跟踪纳斯达克科技市值加权指数 (NDXTMC)。
    # 与纳指100 (NDX) 不同——成分只取 NDX 中的科技股，波动更大。
    # Yahoo 的 ^NDXT 是另一版本（2026-11-23 +8.49%），与官方/集思录的 +8.18%
    # 不符；改用纳斯达克官网 HistoryData 接口的官方点位。
    "159509": "NDXTMC",
}

XUEQIU_INDEX_SYMBOLS = {".SP500-45"}
NASDAQ_INDEX_SYMBOLS = {"NDXTMC"}

# 该指数没有对应期货，盘中 15:00 一段用相近期货模拟。
FUND_FUTURE_PROXY = {
    "161128": "nasdaq100",
    # MSCI 美国50 无期货合约，用标普500期货（ES）代理；^OEX 对 ES 日收益
    # beta 0.998（40 日）/1.010（60 日），基本 1:1，故不另设 beta。
    "513850": "sp500",
    "159577": "sp500",
}

# 期货代理的 beta：代理期货涨跌 × beta ≈ 该基金跟踪指数的当日涨跌。
# 161128 跟踪的 S5INFT 无对应期货，只能用纳指期货代理，但 NQ 与 S5INFT
# 不是 1:1——直接用 NQ 涨跌会把盘中净值预估放大或缩小。近 20 个交易日
# （2026-10-01~10-30）日收益回归：beta=1.1366、corr=0.982、残差 MAE 0.267pct；
# 15 日窗口 1.1480/0.983、25 日窗口 1.1299/0.977，系数稳定，取 20 日值。
FUND_FUTURE_BETA = {
    "161128": 1.1366,
}

# 指数敞口：默认 1.0（全额跟踪）；单只基金确需打折时才在此登记。
FUND_INDEX_EXPOSURES: dict[str, float] = {}

GROUPS = {
    "nasdaq100": {
        "name": "纳指100",
        "index_symbol": ".NDX",
        "yahoo_index_symbol": "^NDX",
        "benchmark_symbol": "QQQ",
        "future_secid": "103.NQ00Y",
        "tencent_future_symbol": "",
        "sina_future_symbol": "hf_NQ",
        "etfcom_index_codes": ["NDX"],
        "default_holding": "159660",
    },
    "sp500": {
        "name": "标普500",
        "index_symbol": ".INX",
        "yahoo_index_symbol": "^GSPC",
        "benchmark_symbol": "SPY",
        "future_secid": "103.ES00Y",
        "tencent_future_symbol": "hf_ES",
        "sina_future_symbol": "hf_ES",
        "etfcom_index_codes": ["SPX", "SPTR500N"],
        "default_holding": "513500",
    },
    "usa50": {
        # MSCI 美国50（W00659）没有免费指数源：用标普100（^OEX）做锚，
        # 实测对两只基金净值的拟合 MAE 0.94bp（SPY 0.87bp、NDX 1.43bp，差异在噪声内），
        # 而 S&P100 与 MSCI 美国50 同属「美国最头部大市值」集合，语义上也最贴近。
        "name": "美国50",
        "index_symbol": "",
        "yahoo_index_symbol": "^OEX",
        "benchmark_symbol": "SPY",
        "future_secid": "103.ES00Y",
        "tencent_future_symbol": "hf_ES",
        "sina_future_symbol": "hf_ES",
        "etfcom_index_codes": [],
        "default_holding": "513850",
    },
}


def max_drawdown(series: pd.Series) -> float:
    running_max = series.cummax()
    drawdown = series / running_max - 1
    return float(drawdown.min() * 100)


def pct_return(start: float, end: float) -> float:
    return float((end / start - 1) * 100)


def clean_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def clean_json_value(value):
    if isinstance(value, dict):
        return {key: clean_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [clean_json_value(item) for item in value]
    if value is pd.NA:
        return None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    return value


def retry_fetch(label: str, fetcher, attempts: int = 4):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return fetcher()
        except Exception as error:
            last_error = error
            print(f"{label} 第 {attempt} 次获取失败：{error}")
            time.sleep(attempt)
    raise last_error


def fetch_etfcom_list(index_code: str) -> dict:
    url = "https://www.etf.com.cn/api/etf-api-service/pc/etf-funds/list"
    payload = {
        "page": 1,
        "pageSize": 100,
        "sortField": "changeRate",
        "sortOrder": "desc",
        "filters": {"indexCode": [index_code]},
        "listed": True,
    }
    response = requests.post(
        url,
        json=payload,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://www.etf.com.cn",
            "Referer": "https://www.etf.com.cn/searchETF.html",
            "Content-Type": "application/json;charset=utf-8",
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise RuntimeError(f"ETF.com.cn 返回异常：{data.get('message') or data.get('code')}")
    return data.get("data") or {}


def load_etfcom_premium_map() -> dict[str, dict]:
    result: dict[str, dict] = {}
    for group_key, meta in GROUPS.items():
        for index_code in meta.get("etfcom_index_codes", []):
            try:
                data = retry_fetch(
                    f"ETF.com.cn {meta['name']}实时溢折率 {index_code}",
                    lambda code=index_code: fetch_etfcom_list(code),
                    attempts=2,
                )
            except Exception as error:
                print(f"ETF.com.cn {meta['name']}实时溢折率获取失败：{error}")
                continue
            server_time = data.get("serverTime")
            for item in data.get("data") or []:
                code = str(item.get("trdCode") or "").zfill(6)
                if not code:
                    continue
                result[code] = {
                    "etfComServerTime": server_time,
                    "etfComIndexCode": item.get("indexCode"),
                    "etfComName": item.get("extdSecuSht") or item.get("fundName"),
                    "etfComPremiumRate": clean_number(item.get("premiumRate")),
                    "etfComLastPrice": clean_number(item.get("lastPrice")),
                    "etfComChangeRate": clean_number(item.get("changeRate")),
                    "etfComDealBalance": clean_number(item.get("dealBalance")),
                    "etfComUnitNav": clean_number(item.get("unitNav")),
                    "etfComNavDate": str(item.get("pubDt") or ""),
                }
    return result


def fetch_sina_future_quote(symbol: str) -> dict | None:
    if not symbol:
        return None
    response = requests.get(
        "https://hq.sinajs.cn/list=" + symbol,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"},
        timeout=20,
    )
    response.raise_for_status()
    text = response.text
    marker = '="'
    if marker not in text:
        return None
    raw = text.split(marker, 1)[1].split('";', 1)[0]
    if not raw:
        return None
    fields = raw.split(",")
    price = clean_number(fields[0] if len(fields) > 0 else None)
    prev = clean_number(fields[7] if len(fields) > 7 else None)
    time_text = fields[6] if len(fields) > 6 else ""
    date_text = fields[12] if len(fields) > 12 else ""
    if price is None:
        return None
    change_pct = (price / prev - 1) * 100 if prev not in (None, 0) else None
    return {
        "fallbackFuturePrice": price,
        "fallbackFutureChangePct": clean_number(change_pct),
        "fallbackFutureTime": f"{date_text} {time_text}".strip(),
        "fallbackFutureSource": "新浪期货",
    }


def load_future_fallbacks() -> dict[str, dict]:
    result = {}
    for group_key, meta in GROUPS.items():
        try:
            quote = retry_fetch(
                f"{meta['name']}新浪期货备用行情",
                lambda symbol=meta.get("sina_future_symbol", ""): fetch_sina_future_quote(symbol),
                attempts=2,
            )
            if quote:
                result[group_key] = quote
        except Exception as error:
            print(f"{meta['name']}新浪期货备用行情获取失败：{error}")
    return result


def fetch_us_index_history(symbol: str) -> pd.DataFrame:
    df = ak.index_us_stock_sina(symbol).copy()
    df["date"] = pd.to_datetime(df["date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna(subset=["date", "close"]).sort_values("date").set_index("date")


def fetch_group_index_history(meta: dict) -> pd.DataFrame:
    sources = []
    if meta.get("index_symbol"):
        sources.append((f"{meta['name']}新浪指数 {meta['index_symbol']}", lambda: fetch_us_index_history(meta["index_symbol"])))
    if meta.get("yahoo_index_symbol"):
        sources.append(
            (f"{meta['name']}Yahoo指数 {meta['yahoo_index_symbol']}", lambda: fetch_yahoo_daily(meta["yahoo_index_symbol"], adjusted=False))
        )
    if meta.get("benchmark_symbol"):
        sources.append(
            (f"{meta['name']}Yahoo ETF {meta['benchmark_symbol']}", lambda: fetch_yahoo_daily(meta["benchmark_symbol"], adjusted=False))
        )
    errors = []
    for label, fetcher in sources:
        try:
            df = retry_fetch(label, fetcher, attempts=2).copy()
            if df.empty:
                raise RuntimeError("返回空数据")
            latest_date = df.index.max()
            if latest_date < pd.Timestamp.now().normalize() - pd.Timedelta(days=7):
                raise RuntimeError(f"最新日期过旧：{latest_date.date()}")
            print(f"{meta['name']}估算净值采用：{label}，最新日期 {latest_date.date()}")
            return df
        except Exception as error:
            errors.append(f"{label}: {error}")
            print(f"{label} 不可用，尝试下一个数据源：{error}")
    raise RuntimeError("；".join(errors))


def fetch_xueqiu_index(symbol: str, count: int = 400) -> pd.DataFrame:
    """雪球美股指数日线（返回 date 索引 + close）。

    标普500信息科技指数（.SP500-45）在 Yahoo 只剩最新一个点位、新浪无此代码，
    雪球是唯一能取到完整历史的免费源。需要先访问首页拿 xq_a_token。
    """
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        }
    )
    session.get("https://xueqiu.com/", timeout=20)
    response = session.get(
        "https://stock.xueqiu.com/v5/stock/chart/kline.json",
        params={
            "symbol": symbol,
            "begin": int(time.time() * 1000),
            "period": "day",
            "type": "before",
            "count": -count,
            "indicator": "kline",
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = (response.json() or {}).get("data") or {}
    columns = payload.get("column") or []
    items = payload.get("item") or []
    if not columns or not items:
        raise RuntimeError(f"雪球 {symbol} 返回空数据")
    df = pd.DataFrame(items, columns=columns)
    df["date"] = (
        pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        .dt.tz_convert("America/New_York")
        .dt.normalize()
        .dt.tz_localize(None)
    )
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna(subset=["date", "close"]).sort_values("date").set_index("date")[["close"]]


_NASDAQ_INDEX_URL = "https://beta.indexes.nasdaq.com/Index/HistoryData"


def fetch_nasdaq_index_history(symbol: str, months: int = 6) -> pd.DataFrame:
    """纳斯达克官网指数日线（date 索引 + close）。

    159509 跟踪的「纳斯达克科技市值加权指数」(NDXTMC) 在 Yahoo 只有 ^NDXT
    （另一个版本，日涨跌与官方不符），东财/雪球都没有对应代码。纳斯达克官网
    beta 站的 /Index/HistoryData 接口能给出官方逐日点位（Value），与集思录
    展示的指数涨幅完全一致（如 2026-11-23 +8.18%）。
    """
    end = pd.Timestamp.today().normalize()
    start = end - pd.DateOffset(months=months)
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Referer": f"https://beta.indexes.nasdaq.com/Index/History/{symbol}",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    response = session.post(
        _NASDAQ_INDEX_URL,
        data={
            "id": symbol,
            "startDate": start.strftime("%Y-%m-%dT00:00:00"),
            "endDate": end.strftime("%Y-%m-%dT00:00:00"),
        },
        timeout=25,
    )
    response.raise_for_status()
    rows = (response.json() or {}).get("aaData") or []
    if not rows:
        raise RuntimeError(f"纳斯达克 {symbol} 返回空数据")
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["TimeStamp"], errors="coerce").dt.normalize()
    df["close"] = pd.to_numeric(df["Value"], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    df = df[df["close"] > 0]
    if df.empty:
        raise RuntimeError(f"纳斯达克 {symbol} 无有效点位")
    return df.sort_values("date").set_index("date")[["close"]]


def load_estimation_series() -> tuple[dict[str, pd.DataFrame], pd.DataFrame | None]:
    try:
        index_map = {}
        for group_key, meta in GROUPS.items():
            index_map[group_key] = fetch_group_index_history(meta)
        for code, symbol in FUND_INDEX_SYMBOLS.items():
            if symbol in XUEQIU_INDEX_SYMBOLS:
                fetcher = lambda symbol=symbol: fetch_xueqiu_index(symbol)
            elif symbol in NASDAQ_INDEX_SYMBOLS:
                fetcher = lambda symbol=symbol: fetch_nasdaq_index_history(symbol)
            elif symbol.startswith("."):
                fetcher = lambda symbol=symbol: fetch_us_index_history(symbol)
            else:
                fetcher = lambda symbol=symbol: fetch_yahoo_daily(symbol, adjusted=False)
            index_map[code] = retry_fetch(f"{code}历史行情 {symbol}", fetcher)

        try:
            fx = retry_fetch("新浪在岸美元人民币日线", fetch_sina_fx_daily).copy()
        except Exception as error:
            print(f"新浪在岸美元人民币日线获取失败，改用东财 USDCNYC：{error}")
            try:
                fx = retry_fetch("美元人民币中间价历史行情", fetch_usdcnyc_history).copy()
            except Exception as fallback_error:
                print(f"美元人民币中间价获取失败，改用Yahoo CNY=X：{fallback_error}")
                fx = retry_fetch("Yahoo美元人民币历史行情", lambda: fetch_yahoo_daily("CNY=X", adjusted=False).reset_index()).copy()
        fx = fx.dropna(subset=["date", "close"]).sort_values("date").set_index("date")
        return index_map, fx
    except Exception as error:
        print(f"估算净值数据源获取失败：{error}")
        return {}, None


def fetch_usdcnyc_history() -> pd.DataFrame:
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": "120.USDCNYC",
        "klt": "101",
        "fqt": "1",
        "lmt": "50000",
        "end": "20500000",
        "iscca": "1",
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64",
        "ut": "f057cbcbce2a86e2866ab8877db1d059",
        "forcect": 1,
    }
    try:
        response = requests.get(
            url,
            params=params,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()["data"]["klines"]
    except Exception:
        curl_url = f"{url}?{urlencode(params)}"
        completed = subprocess.run(
            ["curl", "-L", "-s", curl_url],
            check=True,
            capture_output=True,
            text=True,
            timeout=25,
        )
        data = json.loads(completed.stdout)["data"]["klines"]
    rows = [item.split(",") for item in data]
    df = pd.DataFrame(rows)
    return pd.DataFrame(
        {
            "date": pd.to_datetime(df[0], errors="coerce"),
            "close": pd.to_numeric(df[2], errors="coerce"),
        }
    )


SINA_FX_SYMBOL = "fx_susdcny"
_SINA_FX_MIN_CACHE: dict[int, pd.DataFrame] = {}


def _sina_forex_request(service: str, extra: str) -> str:
    url = (
        "https://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/var%20t=/"
        f"NewForexService.{service}?symbol={SINA_FX_SYMBOL}{extra}"
    )
    response = requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.text


def fetch_sina_fx_daily() -> pd.DataFrame:
    """在岸 USD/CNY 日线（收盘为当日 23:55 北京时间的最后一根）。

    在岸人民币 09:30-23:30 交易、隔夜不连续交易，所以「前一日收盘」与「美股收盘那一刻」
    本质是同一个价位。Yahoo 的 CNY=X 日线混了离岸/隔夜报价，分钟线还会在两个相差
    约 0.15% 的价位群间跳动，会把 15:00 的汇率分量放大约 6 倍（10-30 实际 -0.05%，
    Yahoo 算出 -0.29%），因此优先用新浪。
    """
    raw = _sina_forex_request("getDayKLine", "")
    payload = raw.split("var t=(", 1)[1].rsplit(")", 1)[0].strip().strip('"')
    dates, closes = [], []
    for item in payload.split("|"):
        parts = item.split(",")
        if len(parts) < 5:
            continue
        dates.append(parts[0])
        closes.append(parts[4])
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(dates, errors="coerce"),
            "close": pd.to_numeric(closes, errors="coerce"),
        }
    )
    return df.dropna(subset=["date", "close"])


def fetch_pbc_fixing_daily(days: int = 30) -> pd.DataFrame | None:
    """人民币兑美元中间价日线（中国货币网，每日 09:15 公布）。

    QDII 基金估值用的汇率口径是中间价，而不是在岸即期。两者每天会随机出现
    ±5bp 的基差漂移（例如 2026-10-29 即期贬 0.072%、中间价只贬 0.027%），
    用即期推净值锚会带来 ~0.05% 的系统性误差；改用中间价后锚误差收敛到
    ±0.6bp 以内（用 159501/513100/513870 近三个交易日官方净值验证）。
    接口对脚本访问偶尔限流（返回空 records），失败时由调用方回退在岸即期。
    """
    end = pd.Timestamp.now().normalize()
    start = end - pd.Timedelta(days=days)
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://www.chinamoney.com.cn/chinese/bkccpr/",
    }
    for attempt in range(3):
        try:
            response = requests.get(
                "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-ccpr/CcprHisNew",
                params={
                    "startDate": start.strftime("%Y-%m-%d"),
                    "endDate": end.strftime("%Y-%m-%d"),
                    "currency": "USD/CNY",
                    "pageNum": 1,
                    "pageSize": days,
                },
                headers=headers,
                timeout=20,
            )
            response.raise_for_status()
            records = ((response.json().get("data") or {}).get("records")) or []
            if not records:
                raise RuntimeError("中间价接口返回空 records")
            df = pd.DataFrame(
                {
                    "date": pd.to_datetime([item["date"] for item in records], errors="coerce"),
                    "close": pd.to_numeric([item["values"][0] for item in records], errors="coerce"),
                }
            )
            df = df.dropna(subset=["date", "close"]).sort_values("date")
            if df.empty:
                raise RuntimeError("中间价数据为空")
            return df
        except Exception as error:
            print(f"中间价获取失败（第 {attempt + 1} 次）：{error}")
            time.sleep(2)
    # 中国货币网接口近期限流（返回空 records），改用东财 120.USDCNYC ——
    # 该合约即 CFETS 人民币兑美元中间价日线，口径与货币网一致
    try:
        df = fetch_usdcnyc_history()
        if df is not None and not df.empty:
            print("货币网中间价不可用，改用东财 USDCNYC 中间价日线")
            return df
    except Exception as error:
        print(f"东财 USDCNYC 中间价兜底失败：{error}")
    return None


def fetch_sina_fx_minutes(scale: int = 5, datalen: int = 1200) -> pd.DataFrame:
    """在岸 USD/CNY 分钟线（时间戳为北京时间，naive）。"""
    if scale in _SINA_FX_MIN_CACHE:
        return _SINA_FX_MIN_CACHE[scale]
    raw = _sina_forex_request("getMinKline", f"&scale={scale}&datalen={datalen}")
    payload = raw.split("var t=(", 1)[1].rsplit(")", 1)[0].strip()
    rows = json.loads(payload)
    df = pd.DataFrame(
        {
            "date": pd.to_datetime([row.get("d") for row in rows], errors="coerce"),
            "close": pd.to_numeric([row.get("c") for row in rows], errors="coerce"),
        }
    ).dropna(subset=["date", "close"])
    _SINA_FX_MIN_CACHE[scale] = df
    return df


_YAHOO_DAILY_CACHE: dict[tuple[str, bool], pd.DataFrame] = {}


def fetch_yahoo_daily(symbol: str, adjusted: bool = True) -> pd.DataFrame:
    # 同一次构建内会反复取同一符号（期货 15:00 逐日回溯、基准指数等），缓存避免重复请求
    cache_key = (symbol, adjusted)
    if cache_key in _YAHOO_DAILY_CACHE:
        return _YAHOO_DAILY_CACHE[cache_key]
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {
        "period1": int(pd.Timestamp("2024-12-20", tz="UTC").timestamp()),
        "period2": int((pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=3)).timestamp()),
        "interval": "1d",
        "events": "history",
    }
    response = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    response.raise_for_status()
    result = response.json()["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    adjclose = result.get("indicators", {}).get("adjclose", [{}])[0].get("adjclose")
    close = adjclose if adjusted and adjclose is not None else quote["close"]
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(result["timestamp"], unit="s", utc=True).tz_convert(None).normalize(),
            "close": pd.to_numeric(close, errors="coerce"),
        }
    )
    df = df.dropna(subset=["date", "close"]).sort_values("date").set_index("date")
    _YAHOO_DAILY_CACHE[cache_key] = df
    return df


def fetch_yahoo_price_at_china_close(
    symbol: str, market_date: pd.Timestamp, interval: str = "1m"
) -> float | None:
    """Return the 15:00 Asia/Shanghai price from a Yahoo bar at 07:00 UTC.

    Uses the bar OPEN at 07:00 UTC (=15:00 Beijing), which is identical across
    1m/5m/15m/1h intervals for the same instant, so switching granularity keeps
    one consistent definition of "15:00 price".
    """
    target = pd.Timestamp(market_date).normalize() + pd.Timedelta(hours=7)
    span = {"1m": 15, "5m": 40, "15m": 90, "1h": 150}[interval]
    start = int((target - pd.Timedelta(minutes=span)).tz_localize("UTC").timestamp())
    end = int((target + pd.Timedelta(minutes=span)).tz_localize("UTC").timestamp())
    response = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"period1": start, "period2": end, "interval": interval, "includePrePost": "true"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    response.raise_for_status()
    result = response.json().get("chart", {}).get("result") or []
    if not result:
        return None
    payload = result[0]
    timestamps = payload.get("timestamp") or []
    quote = ((payload.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    closes = quote.get("close") or []
    for i, timestamp in enumerate(timestamps):
        point = pd.Timestamp(timestamp, unit="s", tz="UTC").tz_convert(None)
        if point == target:
            if i < len(opens) and opens[i] is not None:
                return clean_number(opens[i])
            if i < len(closes):
                return clean_number(closes[i])
    return None


def fetch_yahoo_minute_close_at_china_close(symbol: str, market_date: pd.Timestamp) -> float | None:
    """Return the 15:00 Asia/Shanghai price, falling back across bar intervals.

    Yahoo 1m bars only reach back ~30 days; 5m ~45 days, 15m ~60 days, 1h ~1 year.
    Trying them in order lets the real-premium history extend to 60 trading days.
    Because the 07:00-UTC OPEN is identical across intervals, the fallback does
    not change the price definition.
    """
    for interval in ("1m", "5m", "15m", "1h"):
        try:
            value = fetch_yahoo_price_at_china_close(symbol, market_date, interval)
            if value not in (None, 0):
                return value
        except Exception:
            continue
    return None


GROUP_FUTURE_YAHOO_SYMBOLS = {
    "nasdaq100": "NQ=F",
    "sp500": "ES=F",
}


def china_close_future_change(market_date: pd.Timestamp, yahoo_symbol: str = "NQ=F") -> float | None:
    """Compare the 15:00 Asia/Shanghai futures quote with the same contract's prior daily close.

    This is the futures' own daily change at 15:00 (open-to-15:00 is NOT used),
    measured against the previous settlement of the same contract and the same
    data source.
    """
    try:
        futures_history = fetch_yahoo_daily(yahoo_symbol, adjusted=False)
        prior_index_close = value_on_or_before(futures_history, pd.Timestamp(market_date).normalize() - pd.Timedelta(days=1))
        if prior_index_close in (None, 0):
            return None
        future_price = fetch_yahoo_minute_close_at_china_close(yahoo_symbol.replace("=", "%3D"), market_date)
    except Exception as error:
        print(f"北京时间15:00{yahoo_symbol}获取失败：{error}")
        return None
    if future_price in (None, 0):
        return None
    return (future_price / prior_index_close - 1) * 100


def fetch_yahoo_minute_median_at_china_close(symbol: str, market_date: pd.Timestamp, window_minutes: int = 5) -> float | None:
    """Median of 1-minute closes in the few minutes ending at 15:00 Asia/Shanghai.

    FX 1-minute bars regularly contain single-bar spikes that revert on the next
    bar (for example USD/CNY printing 6.6672 between 6.6780 and 6.6781). Taking
    the exact 15:00 bar would move the baseline premium by ~0.15pct at random,
    so a median is used instead.
    """
    target = pd.Timestamp(market_date).normalize() + pd.Timedelta(hours=7)
    start = int((target - pd.Timedelta(minutes=window_minutes)).tz_localize("UTC").timestamp())
    end = int((target + pd.Timedelta(minutes=1)).tz_localize("UTC").timestamp())
    try:
        response = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"period1": start, "period2": end, "interval": "1m", "includePrePost": "true"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        response.raise_for_status()
        result = response.json().get("chart", {}).get("result") or []
        if result:
            closes = ((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
            values = [clean_number(value) for value in closes]
            values = [value for value in values if value not in (None, 0)]
            if values:
                return clean_number(pd.Series(values).median())
    except Exception:
        pass
    # 1m 只覆盖约 30 天；更早日期用 15m/1h 的 07:00-UTC 开盘价兜底
    for interval in ("15m", "1h"):
        try:
            value = fetch_yahoo_price_at_china_close(symbol, market_date, interval)
            if value not in (None, 0):
                return value
        except Exception:
            continue
    return None


def china_close_fx_change(market_date: pd.Timestamp) -> float | None:
    """USD/CNY 在 15:00（北京时间）相对前一交易日收盘的涨幅。

    主力口径用新浪在岸分钟线：两端取价同源，且新浪是在岸价、隔夜不交易，
    前一交易日收盘（23:55）本身就是美股收盘那一刻的汇率，与锚的衔接一致。
    """
    market_date = pd.Timestamp(market_date).normalize()
    try:
        minutes = fetch_sina_fx_minutes()
        day = minutes[minutes["date"].dt.normalize() == market_date]
        clock = day["date"].dt.strftime("%H:%M")
        window = day[(clock > "14:50") & (clock <= "15:00")]["close"]
        if window.empty:
            window = day["close"]
        prior = minutes[minutes["date"].dt.normalize() < market_date]
        if window.empty or prior.empty:
            raise RuntimeError("新浪汇率分钟线未覆盖该交易日")
        fx_price = clean_number(window.median())
        prior_close = clean_number(prior.iloc[-1]["close"])
        if fx_price in (None, 0) or prior_close in (None, 0):
            return None
        return (fx_price / prior_close - 1) * 100
    except Exception as error:
        print(f"新浪 USD/CNY 15:00 取数失败，回退 Yahoo：{error}")

    try:
        fx_history = fetch_yahoo_daily("CNY=X", adjusted=False)
        prior_close = value_on_or_before(fx_history, pd.Timestamp(market_date).normalize() - pd.Timedelta(days=1))
        if prior_close in (None, 0):
            return None
        fx_price = fetch_yahoo_minute_median_at_china_close("CNY%3DX", market_date)
    except Exception as error:
        print(f"北京时间15:00美元人民币获取失败：{error}")
        return None
    if fx_price in (None, 0):
        return None
    return (fx_price / prior_close - 1) * 100


def estimate_nav(
    official_nav: float,
    official_nav_date: str,
    operating_fee: float | None,
    index_series: pd.DataFrame | None,
    fx: pd.DataFrame | None,
    index_exposure: float = 1.0,
    until: pd.Timestamp | None = None,
) -> dict:
    result = {
        "navForPremium": official_nav,
        "navForReturn": official_nav,
        "navForPremiumDate": official_nav_date,
        "navForPremiumSource": "official",
        "estimateNdxReturn": None,
        "estimateFxReturn": None,
    }
    if index_series is None:
        return result

    nav_date = pd.to_datetime(official_nav_date)

    def value_at_or_before(series: pd.DataFrame, date: pd.Timestamp) -> float | None:
        values = series.loc[series.index <= date]
        if values.empty:
            return None
        return clean_number(values.iloc[-1]["close"])

    target_dates = [d for d in index_series.index if d > nav_date]
    if until is not None:
        target_dates = [d for d in target_dates if d <= pd.Timestamp(until)]
    index_start = value_at_or_before(index_series, nav_date)
    if not target_dates or index_start is None:
        return result

    target_date = target_dates[-1]
    index_end = value_at_or_before(index_series, target_date)
    if index_end is None:
        return result
    index_ret = float(index_end / index_start - 1) * index_exposure
    fee_days = max((target_date - nav_date).days, 1)
    fee_drag = ((operating_fee or 0) / 100) / 365 * fee_days
    estimated_nav_for_premium = official_nav * (1 + index_ret) * (1 - fee_drag)
    fx_ret = None
    estimated_nav_for_return = estimated_nav_for_premium
    if fx is not None:
        fx_start = value_at_or_before(fx, nav_date)
        fx_end = value_at_or_before(fx, target_date)
        if fx_start is not None and fx_end is not None:
            fx_ret = float(fx_end / fx_start - 1)
            estimated_nav_for_return = official_nav * (1 + index_ret) * (1 + fx_ret) * (1 - fee_drag)

    result.update(
        {
            "navForPremium": float(estimated_nav_for_premium),
            "navForReturn": float(estimated_nav_for_return),
            "navForPremiumDate": target_date.strftime("%Y-%m-%d"),
            "navForPremiumSource": "estimated",
            "estimateNdxReturn": index_ret * 100,
            "estimateFxReturn": fx_ret * 100 if fx_ret is not None else None,
        }
    )
    return result


def session_anchor_nav(
    group: pd.DataFrame,
    session_date: pd.Timestamp,
    operating_fee: float | None,
    index_series: pd.DataFrame | None,
    fx: pd.DataFrame | None,
    index_exposure: float = 1.0,
    fixing: pd.DataFrame | None = None,
) -> dict | None:
    """A股交易日 session_date 开盘时，市场上能拿到的净值锚。

    QDII 净值有发布滞后：10-30 盘中只拿得到 10-28 的官方净值（10-29 的要等当晚才出），
    所以必须用「最近已发布净值 × 美股最近一个交易日的指数与汇率涨跌」预估，
    不能直接拿 10-29 的官方净值去配 10-30 的场内价。

    汇率项优先用央行中间价（fixing）：QDII 估值按中间价计，在岸即期与中间价
    每天有 ±5bp 的基差漂移，用即期会让锚出现 ~0.05% 的系统性误差；
    中间价缺失时回退在岸即期日线。
    """
    prior = group[group["date"] < session_date]
    if prior.empty:
        return None
    # 优先取紧邻前一交易日；若该行缺净值（节假日空档等），往前找最近一条有 nav
    # 的记录。最多回溯 10 个交易日：更早的缺口（如部分基金早期 nav 未回填）若用
    # 数月前的净值反推会产生系统性失真，宁可让该日序列缺失也不造假数据。
    ref = None
    for offset, (_, candidate) in enumerate(prior.iloc[::-1].iterrows()):
        if offset >= 10:
            break
        if clean_number(candidate.get("nav")) not in (None, 0) and isinstance(candidate.get("nav_date"), str):
            ref = candidate
            break
    if ref is None:
        return None
    base_nav = clean_number(ref.get("nav"))
    base_date = ref.get("nav_date")
    if base_nav in (None, 0) or not isinstance(base_date, str):
        return None
    # 锚点应落在「紧邻前一交易日」（QDII 净值滞后一天）：用该日美股收盘的指数
    # 把 nav_date 的净值外推到这一日。即使 nav 取自更早的记录，美股收盘日仍应是
    # 紧邻前一交易日，这样早期缺 nav 的日期也能补上且口径与正常日期一致。
    us_close = pd.Timestamp(prior.iloc[-1]["date"])
    # estimate_nav 的 value_at_or_before 按日期索引取数，fixing 若是 date 列形式先归一化
    fx_for_anchor = fx
    if fixing is not None and not fixing.empty:
        fx_for_anchor = fixing if "date" not in fixing.columns else fixing.set_index("date")
        if not isinstance(fx_for_anchor.index, pd.DatetimeIndex):
            fx_for_anchor.index = pd.to_datetime(fx_for_anchor.index)
    est = estimate_nav(
        official_nav=float(base_nav),
        official_nav_date=base_date,
        operating_fee=operating_fee,
        index_series=index_series,
        fx=fx_for_anchor,
        index_exposure=index_exposure,
        until=us_close,
    )
    anchor = clean_number(est.get("navForReturn"))
    if anchor in (None, 0):
        return None
    return {
        "anchorNav": anchor,
        "publishedNav": float(base_nav),
        "publishedNavDate": base_date,
        "usCloseDate": us_close.strftime("%Y-%m-%d"),
        "anchorIndexReturn": clean_number(est.get("estimateNdxReturn")),
        "anchorFxReturn": clean_number(est.get("estimateFxReturn")),
    }


def real_premium_at_china_close(close_value, anchor, future_change, fx_change):
    """15:00 真实溢价率：场内价 ÷ (净值锚 × 15:00汇率 × 15:00期货) − 1。"""
    if close_value in (None, 0) or anchor in (None, 0):
        return None
    if future_change is None or fx_change is None:
        return None
    marked = anchor * (1 + fx_change / 100) * (1 + future_change / 100)
    if marked <= 0:
        return None
    return (float(close_value) / marked - 1) * 100


def book_premium(close_value, anchor):
    """账面溢价率：场内价 ÷ 净值锚 − 1。

    净值锚 = 已公布净值 + 隔夜指数涨跌（账面部分），不叠加当日 15:00 的
    期货与汇率日内漂移。供趋势对比页展示，剔除期货/汇率噪音。
    """
    if close_value in (None, 0) or anchor in (None, 0):
        return None
    return (float(close_value) / float(anchor) - 1) * 100


# 趋势对比页分组中位数参考线：每个交易日对组内成分基金溢价取中位数。
# 纳指100组排除 159509(纳指科技)、161128(标普信息科技LOF)、161130(纳斯达克100LOF)，
# 只保留纯正的纳指100 ETF（12 支）；标普500、美国50 用全组。
MEDIAN_SERIES_DEFS = [
    ("nasdaq100", "MED_nasdaq100", "纳指100中位数", ("159509", "161128", "161130")),
    ("sp500", "MED_sp500", "标普500中位数", ()),
    ("usa50", "MED_usa50", "美国50中位数", ()),
]


def compute_group_median_series(history_export, group_key, exclude_codes=()):
    """按交易日对指定分组的溢价序列取中位数（偶数支取中间两支平均）。

    返回 ({"points": [...], "realPoints": [...]}, 成分数)。账面/真实两套口径分开算。
    """
    members = [
        f for f in history_export.values()
        if f.get("group") == group_key and f.get("code") not in exclude_codes
    ]
    result = {}
    for key in ("points", "realPoints"):
        by_date = {}
        for f in members:
            for p in f.get(key) or []:
                by_date.setdefault(p["date"], []).append(p["value"])
        series = []
        for day in sorted(by_date):
            vals = sorted(by_date[day])
            n = len(vals)
            med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
            series.append({"date": day, "value": round(med, 3)})
        result[key] = series
    return result, len(members)


def value_on_or_before(series: pd.DataFrame | None, date: pd.Timestamp) -> float | None:
    if series is None or series.empty:
        return None
    values = series.loc[series.index <= date]
    if values.empty:
        return None
    return clean_number(values.iloc[-1]["close"])


def nav_row_on_or_before(group: pd.DataFrame, date: pd.Timestamp) -> pd.Series | None:
    rows = group.dropna(subset=["nav", "nav_date"]).copy()
    rows["nav_date"] = pd.to_datetime(rows["nav_date"])
    rows = rows[rows["nav_date"] <= date]
    if rows.empty:
        return None
    return rows.sort_values(["nav_date", "date"]).iloc[-1]


def accum_value(row: pd.Series) -> float | None:
    value = clean_number(row.get("accum_nav"))
    if value is None:
        value = clean_number(row.get("nav"))
    return value


def estimate_accum_value(nav_info: dict, official_nav_row: pd.Series) -> float | None:
    nav = clean_number(nav_info.get("navForReturn"))
    if nav is None:
        nav = clean_number(nav_info.get("navForPremium"))
    official_nav = clean_number(official_nav_row.get("nav"))
    official_accum = accum_value(official_nav_row)
    if nav is None or official_nav in (None, 0) or official_accum is None:
        return None
    return nav * official_accum / official_nav


def period_nav_metrics(
    group: pd.DataFrame,
    start_date: str,
    end_date: str,
    end_accum: float | None,
    qqq: pd.DataFrame | None,
    fx: pd.DataFrame | None,
) -> dict:
    result = {
        "navReturn": None,
        "fxStrippedReturn": None,
        "qqqReturn": None,
        "trackingDiff": None,
    }
    start = pd.to_datetime(start_date)
    end = pd.to_datetime(end_date)
    start_row = nav_row_on_or_before(group, start)
    if start_row is None or end_accum is None:
        return result
    start_accum = accum_value(start_row)
    if start_accum in (None, 0):
        return result

    nav_return = end_accum / start_accum - 1
    qqq_start = value_on_or_before(qqq, start)
    qqq_end = value_on_or_before(qqq, end)
    fx_start = value_on_or_before(fx, start)
    fx_end = value_on_or_before(fx, end)
    qqq_return = qqq_end / qqq_start - 1 if qqq_start and qqq_end else None
    fx_return = fx_end / fx_start - 1 if fx_start and fx_end else None
    fx_stripped_return = (1 + nav_return) / (1 + fx_return) - 1 if fx_return is not None else None
    tracking_diff = fx_stripped_return - qqq_return if fx_stripped_return is not None and qqq_return is not None else None

    result.update(
        {
            "navReturn": nav_return * 100,
            "fxStrippedReturn": fx_stripped_return * 100 if fx_stripped_return is not None else None,
            "qqqReturn": qqq_return * 100 if qqq_return is not None else None,
            "trackingDiff": tracking_diff * 100 if tracking_diff is not None else None,
        }
    )
    return result


def fetch_margin_detail_sse(date_str: str) -> pd.DataFrame:
    """上交所融资融券明细（含 ETF）。融资偿还额仅沪市披露，深市需用余额差算净买入。"""
    df = ak.stock_margin_detail_sse(date=date_str)
    if df is None or df.empty:
        raise RuntimeError("返回空数据")
    df = df.rename(
        columns={
            "标的证券代码": "code",
            "融资余额": "rzye",
            "融资买入额": "rzbuy",
            "融资偿还额": "rzrepay",
        }
    )
    df["code"] = df["code"].astype(str).str.zfill(6)
    df = df[df["code"].isin(MARGIN_EXPECTED_SH)]
    if df.empty:
        raise RuntimeError("监控代码均不在明细中")
    return df[["code", "rzye", "rzbuy", "rzrepay"]]


def fetch_margin_detail_szse(date_str: str) -> pd.DataFrame:
    df = ak.stock_margin_detail_szse(date=date_str)
    if df is None or df.empty:
        raise RuntimeError("返回空数据")
    df = df.rename(columns={"证券代码": "code", "融资余额": "rzye", "融资买入额": "rzbuy"})
    df["code"] = df["code"].astype(str).str.zfill(6)
    df = df[df["code"].isin(MARGIN_EXPECTED_SZ)]
    if df.empty:
        raise RuntimeError("监控代码均不在明细中")
    df["rzrepay"] = None
    return df[["code", "rzye", "rzbuy", "rzrepay"]]


def load_margin_cache() -> pd.DataFrame:
    if MARGIN_CSV.exists():
        df = pd.read_csv(MARGIN_CSV, dtype={"code": str, "date": str})
        df["code"] = df["code"].str.zfill(6)
        return df
    return pd.DataFrame(columns=["date", "code", "rzye", "rzbuy", "rzrepay"])


def margin_day_markets(cache: pd.DataFrame, day_str: str) -> set[str]:
    codes = set(cache.loc[cache["date"] == day_str, "code"])
    markets = set()
    if codes & MARGIN_EXPECTED_SH:
        markets.add("sh")
    if codes & MARGIN_EXPECTED_SZ:
        markets.add("sz")
    return markets


def update_margin_cache(trading_dates: pd.Series) -> pd.DataFrame:
    """增量抓取最近 MARGIN_WINDOW_DAYS 个交易日的融资融券明细并落盘缓存。

    单个市场失败不影响另一市场：已缓存的市场不重抓，失败的市场下次运行自动补。
    抓取失败不抛异常（融资数据缺失只影响新增列，不影响页面其它部分）。
    """
    cache = load_margin_cache()
    window = [pd.Timestamp(day) for day in list(trading_dates)[-MARGIN_WINDOW_DAYS:]]
    fetchers = {"sh": fetch_margin_detail_sse, "sz": fetch_margin_detail_szse}
    for day in window:
        day_str = day.strftime("%Y-%m-%d")
        have = margin_day_markets(cache, day_str)
        for market, fetcher in fetchers.items():
            if market in have:
                continue
            try:
                day_df = retry_fetch(
                    f"融资融券明细 {market} {day_str}",
                    lambda f=fetcher, s=day.strftime("%Y%m%d"): f(s),
                    attempts=2,
                )
            except Exception as error:
                print(f"融资融券明细 {market} {day_str} 获取失败：{error}")
                continue
            day_df["date"] = day_str
            # 同市场已有部分行则先去掉，避免重复
            cache = cache[~((cache["date"] == day_str) & cache["code"].isin(set(day_df["code"])))]
            cache = pd.concat([cache, day_df], ignore_index=True)
            print(f"融资融券明细 {market} {day_str}：新增 {len(day_df)} 条")
    # 只保留最近 60 个日期，防止缓存无限增长
    if not cache.empty:
        keep = set(sorted(cache["date"].unique())[-60:])
        cache = cache[cache["date"].isin(keep)].sort_values(["date", "code"])
        cache.to_csv(MARGIN_CSV, index=False)
    return cache


def compute_margin_metrics(cache: pd.DataFrame) -> dict[str, dict]:
    """每只基金：融资余额(亿)、当日净买入(万)、近5/10日净买入(万)、数据日期。

    沪市有偿还额时当日净买入 = 买入额 − 偿还额（交易所口径）；
    其余（含深市）一律用余额差：净买入 = 最新余额 − N 个交易日前余额。
    """
    out: dict[str, dict] = {}
    for code, grp in cache.groupby("code"):
        grp = grp.sort_values("date").reset_index(drop=True)
        rzye = pd.to_numeric(grp["rzye"], errors="coerce")
        buy = pd.to_numeric(grp["rzbuy"], errors="coerce")
        repay = pd.to_numeric(grp["rzrepay"], errors="coerce")
        if len(grp) == 0 or pd.isna(rzye.iloc[-1]):
            continue

        def balance_diff_wan(lag: int) -> float | None:
            if len(rzye) <= lag or pd.isna(rzye.iloc[-1]) or pd.isna(rzye.iloc[-1 - lag]):
                return None
            return float((rzye.iloc[-1] - rzye.iloc[-1 - lag]) / 1e4)

        day_net = None
        if pd.notna(buy.iloc[-1]) and pd.notna(repay.iloc[-1]):
            day_net = float((buy.iloc[-1] - repay.iloc[-1]) / 1e4)
        else:
            day_net = balance_diff_wan(1)
        out[code] = {
            "marginBalanceYi": float(rzye.iloc[-1]) / 1e8,
            "marginDayNetBuyWan": day_net,
            "margin5dNetBuyWan": balance_diff_wan(5),
            "margin10dNetBuyWan": balance_diff_wan(10),
            "marginDataDate": str(grp["date"].iloc[-1]),
        }
    return out


def main() -> None:
    history = pd.read_csv(DATA_CSV, dtype={"code": str})
    history["date"] = pd.to_datetime(history["date"])
    fees = pd.read_csv(FEE_CSV, dtype={"code": str}) if FEE_CSV.exists() else pd.DataFrame()
    fee_map = {}
    return_2025_map = {}
    if not fees.empty:
        fee_map = dict(zip(fees["code"], fees["operating_fee_total"]))
        return_2025_map = dict(zip(fees["code"], fees["calendar_return_2025"]))

    if "group" not in history.columns:
        history["group"] = "nasdaq100"
    index_map, fx = load_estimation_series()
    # 净值锚的汇率项用央行中间价（QDII 估值口径），接口限流时回退在岸即期
    fixing = fetch_pbc_fixing_daily()
    if fixing is not None:
        last_date = fixing.index.max() if not isinstance(fixing, pd.DataFrame) or "date" not in fixing.columns else fixing["date"].max()
        last_value = fixing["close"].iloc[-1]
        print(f"净值锚汇率项采用：央行中间价，最新 {pd.Timestamp(last_date):%Y-%m-%d} {last_value}")
    else:
        print("中间价不可用，净值锚汇率项回退在岸即期")
    etfcom_map = load_etfcom_premium_map()
    future_fallbacks = load_future_fallbacks()
    benchmark_map = {}
    for group_key, meta in GROUPS.items():
        try:
            benchmark_map[group_key] = retry_fetch(
                f"{meta['benchmark_symbol']}历史行情",
                lambda symbol=meta["benchmark_symbol"]: fetch_yahoo_daily(symbol),
            )
        except Exception as error:
            print(f"{meta['benchmark_symbol']}历史行情获取失败，跟踪差将留空：{error}")
            benchmark_map[group_key] = None
    latest_date = history["date"].max()
    trading_dates = pd.Series(sorted(history["date"].unique()))
    prev_date = trading_dates.iloc[-2] if len(trading_dates) >= 2 else None

    # 融资融券：交易所每日披露，本地缓存增量抓取；失败只影响新增列，不阻断构建
    try:
        margin_cache = update_margin_cache(trading_dates)
        margin_map = compute_margin_metrics(margin_cache)
        if margin_map:
            margin_latest = max(item["marginDataDate"] for item in margin_map.values())
            print(f"融资融券数据：{len(margin_map)} 只，最新数据日 {margin_latest}")
        else:
            print("融资融券数据：暂无（新增列将显示 -）")
    except Exception as error:
        print(f"融资融券数据处理失败（新增列将显示 -）：{error}")
        margin_map = {}

    TREND_POINTS = 7
    # 近 N 个 A 股交易日（含最新）的 15:00 期货/汇率，一份缓存同时供「真实溢价率变化」、
    # 「近 7 日真实溢价率趋势」与「10/20/30 日真实溢价均值」使用，避免逐日重复拉分钟线。
    # 窗口按交易日只增不减：基准 2026-11-24 = 180，此后每多一个交易日 +1（181、182…）。
    # 这样历史数据持续累积，对比图表页可切换的最大窗口随之增长。
    # 主表均值仍只用最后 30 个点，趋势只取最后 7 个点。
    WINDOW_BASE_DATE = pd.Timestamp("2026-11-24").normalize()
    WINDOW_BASE_COUNT = 180
    trading_day_index = {day: idx for idx, day in enumerate(sorted(trading_dates))}
    base_idx = trading_day_index.get(WINDOW_BASE_DATE, len(trading_day_index) - 1)
    REAL_PREMIUM_WINDOW = WINDOW_BASE_COUNT + max(0, (len(trading_day_index) - 1) - base_idx)
    real_window_dates = [pd.Timestamp(day) for day in list(trading_dates)[-REAL_PREMIUM_WINDOW:]]
    trend_dates = real_window_dates[-TREND_POINTS:]
    future_change_cache: dict[tuple[str, pd.Timestamp], float | None] = {}
    fx_change_cache: dict[pd.Timestamp, float | None] = {}
    for day in real_window_dates:
        fx_change_cache[day] = china_close_fx_change(day)
        for group_key in GROUPS:
            symbol = GROUP_FUTURE_YAHOO_SYMBOLS.get(group_key, "NQ=F")
            cache_key = (symbol, day)
            if cache_key not in future_change_cache:
                future_change_cache[cache_key] = china_close_future_change(day, symbol)

    def future_change_for(day, group_key: str):
        if day is None:
            return None
        day = pd.Timestamp(day)
        symbol = GROUP_FUTURE_YAHOO_SYMBOLS.get(group_key, "NQ=F")
        cache_key = (symbol, day)
        if cache_key not in future_change_cache:
            future_change_cache[cache_key] = china_close_future_change(day, symbol)
        return future_change_cache[cache_key]

    def fx_change_for(day):
        if day is None:
            return None
        day = pd.Timestamp(day)
        if day not in fx_change_cache:
            fx_change_cache[day] = china_close_fx_change(day)
        return fx_change_cache[day]

    china_close_future_changes = {group_key: future_change_for(latest_date, group_key) for group_key in GROUPS}
    china_close_fx_change_pct = fx_change_for(latest_date)
    # 前一交易日：用于「真实溢价率变化」的对照组。
    # 休市时当前值只能锚定在最后一个A股时刻，此时要比的对象是再前一个交易日的15:00。
    prev_china_close_future_changes = (
        {group_key: future_change_for(prev_date, group_key) for group_key in GROUPS} if prev_date is not None else {}
    )
    prev_china_close_fx_change_pct = fx_change_for(prev_date) if prev_date is not None else None
    funds = []
    premium_history_export: dict[str, dict] = {}
    for code, group in history.sort_values("date").groupby("code"):
        group = group.sort_values("date").reset_index(drop=True)
        latest = group.iloc[-1]
        group_key = str(latest.get("group") or "nasdaq100")
        group_meta = GROUPS.get(group_key, GROUPS["nasdaq100"])
        valid_nav_rows = group.dropna(subset=["nav", "nav_date"]).copy()
        official_nav_row = valid_nav_rows.iloc[-1] if len(valid_nav_rows) else latest
        y2026 = group[group["date"].dt.year == 2026].copy()

        return_2026 = None
        max_dd_2026 = None
        latest_change_pct = None
        if len(y2026) >= 2:
            return_2026 = pct_return(float(y2026.iloc[0]["close"]), float(y2026.iloc[-1]["close"]))
            max_dd_2026 = max_drawdown(y2026["close"].astype(float))
        if len(group) >= 2:
            latest_change_pct = pct_return(float(group.iloc[-2]["close"]), float(group.iloc[-1]["close"]))

        last7 = group.iloc[-7:]
        etfcom = etfcom_map.get(code, {})
        operating_fee = float(fee_map[code]) if code in fee_map else None
        official_nav = clean_number(official_nav_row["nav"])
        official_nav_date = str(official_nav_row["nav_date"])
        if official_nav in (None, 0) and clean_number(etfcom.get("etfComUnitNav")) not in (None, 0):
            official_nav = clean_number(etfcom.get("etfComUnitNav"))
            official_nav_date = etfcom.get("etfComNavDate") or latest["date"].strftime("%Y-%m-%d")
        nav_info = estimate_nav(
            official_nav=float(official_nav) if official_nav is not None else float("nan"),
            official_nav_date=official_nav_date,
            operating_fee=operating_fee,
            index_series=index_map[code] if code in index_map else index_map.get(group_key),
            fx=fx,
            index_exposure=FUND_INDEX_EXPOSURES.get(code, 1.0),
        )
        latest_accum = estimate_accum_value(nav_info, official_nav_row)
        end_2025_row = nav_row_on_or_before(group, pd.Timestamp("2025-12-31"))
        end_2025_accum = accum_value(end_2025_row) if end_2025_row is not None else None
        metrics_2025 = period_nav_metrics(group, "2024-12-31", "2025-12-31", end_2025_accum, benchmark_map.get(group_key), fx)
        metrics_2026 = period_nav_metrics(
            group,
            "2025-12-31",
            str(nav_info.get("navForPremiumDate") or latest["date"].strftime("%Y-%m-%d")),
            latest_accum,
            benchmark_map.get(group_key),
            fx,
        )
        # Yesterday's close must use the premium observed at the China-market close.
        # Do not recompute it with the same-date US index close, which is published
        # after the domestic ETF has stopped trading and would distort the baseline.
        latest_premium = clean_number(latest.get("live_premium_pct"))
        estimated_nav_for_premium = nav_info.get("navForReturn") or nav_info.get("navForPremium")
        if clean_number(latest_premium) is None and clean_number(latest["close"]) is not None and clean_number(estimated_nav_for_premium) not in (None, 0):
            latest_premium = (float(latest["close"]) / float(estimated_nav_for_premium) - 1) * 100
        if clean_number(latest_premium) is None and clean_number(etfcom.get("etfComPremiumRate")) is not None:
            latest_premium = clean_number(etfcom.get("etfComPremiumRate"))

        # 前一交易日（A股口径）的收盘溢价，作为「真实溢价率变化」的对照组
        prev_rows = group[group["date"] < latest_date]
        prev_premium = None
        prev_row_date = None
        if len(prev_rows):
            prev_row = prev_rows.iloc[-1]
            prev_row_date = prev_row["date"]
            prev_premium = clean_number(prev_row.get("live_premium_pct"))
            if prev_premium is None:
                prev_nav = clean_number(prev_row.get("nav"))
                prev_close = clean_number(prev_row.get("close"))
                if prev_nav not in (None, 0) and prev_close is not None:
                    prev_premium = (float(prev_close) / float(prev_nav) - 1) * 100

        # 15:00 真实溢价：用「发布滞后 + 美股前一日涨跌」预估出的净值锚，
        # 再叠加当日 15:00 的期货与汇率变动。
        fund_index_series = index_map[code] if code in index_map else index_map.get(group_key)
        fund_exposure = FUND_INDEX_EXPOSURES.get(code, 1.0)
        anchor_now = session_anchor_nav(group, latest_date, operating_fee, fund_index_series, fx, fund_exposure, fixing)
        anchor_prev = (
            session_anchor_nav(group, prev_row_date, operating_fee, fund_index_series, fx, fund_exposure, fixing)
            if prev_row_date is not None
            else None
        )
        # 15:00 期货：默认用所属分组的期货；无对应期货的基金（标普信息科技）走代理分组，
        # 代理期货的涨跌还要乘 beta 才等于该基金跟踪指数的预估涨跌。
        future_key = FUND_FUTURE_PROXY.get(code, group_key)
        future_beta = FUND_FUTURE_BETA.get(code, 1.0)
        fut_now_raw = clean_json_value(china_close_future_changes.get(future_key))
        fut_prev_raw = clean_json_value(prev_china_close_future_changes.get(future_key))
        fut_now = clean_json_value(fut_now_raw * future_beta) if fut_now_raw is not None else None
        fut_prev = clean_json_value(fut_prev_raw * future_beta) if fut_prev_raw is not None else None
        fx_now = clean_json_value(china_close_fx_change_pct)
        fx_prev = clean_json_value(prev_china_close_fx_change_pct)
        prev_close_value = clean_number(prev_rows.iloc[-1]["close"]) if len(prev_rows) else None

        china_close_real_premium = real_premium_at_china_close(
            clean_number(latest["close"]),
            anchor_now["anchorNav"] if anchor_now else None,
            fut_now,
            fx_now,
        )
        prev_china_close_real_premium = real_premium_at_china_close(
            prev_close_value,
            anchor_prev["anchorNav"] if anchor_prev else None,
            fut_prev,
            fx_prev,
        )

        # 近 30 个 A 股交易日的 15:00 真实溢价率序列（口径与当日完全一致：锚 × 15:00 汇率 × 15:00 期货）。
        # 近7日趋势取最后 7 个点；10/20/30 日均值直接用该序列计算（与旧收盘溢价口径不同）。
        real_premium_points = []
        for series_day in real_window_dates:
            day_rows = group[group["date"] == series_day]
            if day_rows.empty:
                continue
            day_anchor = session_anchor_nav(
                group, series_day, operating_fee, fund_index_series, fx, fund_exposure, fixing
            )
            day_future_raw = future_change_for(series_day, future_key)
            day_future = clean_json_value(day_future_raw * future_beta) if day_future_raw is not None else None
            day_value = real_premium_at_china_close(
                clean_number(day_rows.iloc[0]["close"]),
                day_anchor["anchorNav"] if day_anchor else None,
                day_future,
                fx_change_for(series_day),
            )
            if day_value is not None:
                real_premium_points.append((series_day.strftime("%Y-%m-%d"), round(day_value, 3)))
        real_premium_trend = [{"date": day, "value": value} for day, value in real_premium_points[-TREND_POINTS:]]
        real_premium_values = [value for _, value in real_premium_points]
        # 账面溢价序列：收盘价 ÷ 净值锚 − 1，剔除当日 15:00 期货与汇率漂移。
        # 与 real_premium_points 用同一批交易日和同一净值锚，仅去掉期货/汇率两项。
        book_premium_points = []
        for series_day in real_window_dates:
            day_rows = group[group["date"] == series_day]
            if day_rows.empty:
                continue
            day_anchor = session_anchor_nav(
                group, series_day, operating_fee, fund_index_series, fx, fund_exposure, fixing
            )
            day_value = book_premium(
                clean_number(day_rows.iloc[0]["close"]),
                day_anchor["anchorNav"] if day_anchor else None,
            )
            if day_value is not None:
                book_premium_points.append((series_day.strftime("%Y-%m-%d"), round(day_value, 3)))
        # 导出两套序列：points=账面溢价（默认展示），realPoints=真实溢价（含期货/汇率）。
        # 供独立对比图表页头部切换；均按 30/45/60/90/180/… 天窗口共用。
        premium_history_export[code] = {
            "code": code,
            "name": str(latest.get("name") or code),
            "group": group_key,
            "points": [{"date": day, "value": value} for day, value in book_premium_points],
            "realPoints": [{"date": day, "value": value} for day, value in real_premium_points],
        }

        # 2026 年净值收益：官方净值只到 navDate（滞后一天），而 A股 latest_date 收盘时
        # 能拿到的净值口径是「锚 × 当日15:00汇率 × 当日15:00期货」（与真实溢价率同源同时点）。
        # 用它把收益延伸到最新交易日，避免拿当晚才有的官方净值算「未来收益」。
        nav_for_return = clean_number(nav_info.get("navForReturn")) or clean_number(nav_info.get("navForPremium"))
        latest_fitted_nav = None
        metrics_2026_fitted = dict.fromkeys(("navReturn", "fxStrippedReturn", "qqqReturn", "trackingDiff"))
        if (
            anchor_now
            and nav_for_return
            and fut_now is not None
            and fx_now is not None
        ):
            latest_fitted_nav = anchor_now["anchorNav"] * (1 + fx_now / 100) * (1 + fut_now / 100)
            if latest_fitted_nav > 0:
                latest_accum_fitted = latest_accum * latest_fitted_nav / nav_for_return
                metrics_2026_fitted = period_nav_metrics(
                    group,
                    "2025-12-31",
                    latest_date.strftime("%Y-%m-%d"),
                    latest_accum_fitted,
                    benchmark_map.get(group_key),
                    fx,
                )

        funds.append(
            {
                "code": code,
                "name": DISPLAY_NAMES.get(code, str(latest["name"])),
                "group": group_key,
                "groupName": group_meta["name"],
                "benchmarkSymbol": group_meta["benchmark_symbol"],
                "futureSecid": group_meta["future_secid"],
                "market": "sh" if code.startswith("5") else "sz",
                "secid": ("1." if code.startswith("5") else "0.") + code,
                "latestDate": latest["date"].strftime("%Y-%m-%d"),
                "latestClose": clean_number(latest["close"]),
                "latestChangePct": clean_number(latest_change_pct),
                "nav": clean_number(official_nav),
                "navDate": official_nav_date,
                **nav_info,
                "latestPremium": clean_number(latest_premium),
                "chinaCloseFutureChangePct": fut_now,
                "chinaCloseFutureRawPct": fut_now_raw,
                "futureBeta": future_beta if future_beta != 1.0 else None,
                "futureProxySymbol": future_key if future_beta != 1.0 else None,
                "chinaCloseFutureDate": latest_date.strftime("%Y-%m-%d"),
                "chinaCloseFxChangePct": clean_json_value(china_close_fx_change_pct),
                "prevDate": prev_row_date.strftime("%Y-%m-%d") if prev_row_date is not None else None,
                "prevPremium": clean_number(prev_premium),
                "prevChinaCloseFutureChangePct": fut_prev,
                "prevChinaCloseFutureRawPct": fut_prev_raw,
                "prevChinaCloseFxChangePct": clean_json_value(prev_china_close_fx_change_pct),
                "chinaCloseRealPremium": clean_number(china_close_real_premium),
                "prevChinaCloseRealPremium": clean_number(prev_china_close_real_premium),
                "realPremiumTrend": real_premium_trend,
                "sessionAnchorNav": clean_number(anchor_now["anchorNav"]) if anchor_now else None,
                "sessionAnchorPublishedNav": clean_number(anchor_now["publishedNav"]) if anchor_now else None,
                "sessionAnchorPublishedDate": anchor_now["publishedNavDate"] if anchor_now else None,
                "sessionAnchorUsCloseDate": anchor_now["usCloseDate"] if anchor_now else None,
                "sessionAnchorIndexReturn": clean_number(anchor_now["anchorIndexReturn"]) if anchor_now else None,
                "sessionAnchorFxReturn": clean_number(anchor_now["anchorFxReturn"]) if anchor_now else None,
                "etfComServerTime": etfcom.get("etfComServerTime"),
                "etfComIndexCode": etfcom.get("etfComIndexCode"),
                "etfComName": etfcom.get("etfComName"),
                "etfComPremiumRate": clean_number(etfcom.get("etfComPremiumRate")),
                "etfComLastPrice": clean_number(etfcom.get("etfComLastPrice")),
                "etfComChangeRate": clean_number(etfcom.get("etfComChangeRate")),
                "etfComDealBalance": clean_number(etfcom.get("etfComDealBalance")),
                "etfComUnitNav": clean_number(etfcom.get("etfComUnitNav")),
                "etfComNavDate": etfcom.get("etfComNavDate"),
                **(margin_map.get(code) or {}),
                "avg10Premium": clean_number(pd.Series(real_premium_values[-10:]).mean()) if real_premium_values else None,
                "avg20Premium": clean_number(pd.Series(real_premium_values[-20:]).mean()) if real_premium_values else None,
                "avg30Premium": clean_number(pd.Series(real_premium_values[-30:]).mean()) if real_premium_values else None,
                "avg7AmountWan": clean_number(last7["amount_wan"].mean()),
                "amountWan": clean_number(latest["amount_wan"]),
                "operatingFee": operating_fee,
                "return2025": clean_number(return_2025_map[code]) if code in return_2025_map else None,
                "return2026": clean_number(return_2026),
                "navReturn2025": clean_number(metrics_2025["navReturn"]),
                "navReturn2026": clean_number(metrics_2026["navReturn"]),
                "navReturn2026Fitted": clean_number(metrics_2026_fitted["navReturn"]),
                "fxStrippedReturn2026Fitted": clean_number(metrics_2026_fitted["fxStrippedReturn"]),
                "trackingDiff2026Fitted": clean_number(metrics_2026_fitted["trackingDiff"]),
                "latestFittedNav": clean_number(latest_fitted_nav),
                "latestFittedNavDate": latest_date.strftime("%Y-%m-%d") if latest_fitted_nav else None,
                "fxStrippedReturn2025": clean_number(metrics_2025["fxStrippedReturn"]),
                "fxStrippedReturn2026": clean_number(metrics_2026["fxStrippedReturn"]),
                "qqqReturn2025": clean_number(metrics_2025["qqqReturn"]),
                "qqqReturn2026": clean_number(metrics_2026["qqqReturn"]),
                "trackingDiff2025": clean_number(metrics_2025["trackingDiff"]),
                "trackingDiff2026": clean_number(metrics_2026["trackingDiff"]),
                "maxDrawdown2026": clean_number(max_dd_2026),
                "subscribeStatus": str(latest.get("subscribe_status", "")),
                "redeemStatus": str(latest.get("redeem_status", "")),
            }
        )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generatedAt": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "latestDate": latest_date.strftime("%Y-%m-%d"),
        "groups": {key: {**value, **future_fallbacks.get(key, {})} for key, value in GROUPS.items()},
        "funds": funds,
    }
    clean_payload = clean_json_value(enrich_payload(payload))
    json_text = json.dumps(
        clean_payload,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )
    OUT.write_text(json_text, encoding="utf-8")
    OUT_JS.write_text(f"window.NASDAQ_ETF_FUNDS = {json_text};\n", encoding="utf-8")
    print(f"Wrote {OUT}")
    print(f"Wrote {OUT_JS}")

    # 对比图表页数据：完整 N 日逐日账面溢价序列（30/45/60/90/180/… 天窗口共用，
    # N 按交易日只增不减）
    # 同时导出各分组中位数参考线（纳指100=12支纯纳指ETF、标普500、美国50）。
    median_series = []
    for gkey, mcode, mname, exclude in MEDIAN_SERIES_DEFS:
        series, member_count = compute_group_median_series(
            premium_history_export, gkey, exclude
        )
        median_series.append(
            {
                "code": mcode,
                "name": mname + "（" + str(member_count) + "支）",
                "group": gkey,
                "isMedian": True,
                "memberCount": member_count,
                "members": sorted(
                    f["code"] for f in premium_history_export.values()
                    if f.get("group") == gkey and f.get("code") not in exclude
                ),
                "points": series["points"],
                "realPoints": series["realPoints"],
            }
        )
    history_payload = clean_json_value(
        {
            "generatedAt": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
            "maxWindow": REAL_PREMIUM_WINDOW,
            "funds": sorted(
                premium_history_export.values(), key=lambda item: item["code"]
            ),
            "medianSeries": median_series,
        }
    )
    history_json = json.dumps(history_payload, ensure_ascii=False, indent=2, allow_nan=False)
    OUT_PREMIUM_HISTORY.write_text(history_json, encoding="utf-8")
    OUT_PREMIUM_HISTORY_JS.write_text(
        f"window.PREMIUM_HISTORY = {history_json};\n", encoding="utf-8"
    )
    print(f"Wrote {OUT_PREMIUM_HISTORY}")


if __name__ == "__main__":
    main()
