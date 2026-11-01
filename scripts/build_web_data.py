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
OUT = ROOT / "docs" / "data" / "funds.json"
OUT_JS = ROOT / "docs" / "data" / "funds.js"

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
}

XUEQIU_INDEX_SYMBOLS = {".SP500-45"}

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


def load_estimation_series() -> tuple[dict[str, pd.DataFrame], pd.DataFrame | None]:
    try:
        index_map = {}
        for group_key, meta in GROUPS.items():
            index_map[group_key] = fetch_group_index_history(meta)
        for code, symbol in FUND_INDEX_SYMBOLS.items():
            if symbol in XUEQIU_INDEX_SYMBOLS:
                fetcher = lambda symbol=symbol: fetch_xueqiu_index(symbol)
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


def fetch_yahoo_minute_close_at_china_close(symbol: str, market_date: pd.Timestamp) -> float | None:
    """Return the 15:00 Asia/Shanghai one-minute close for a futures symbol."""
    target = pd.Timestamp(market_date).normalize() + pd.Timedelta(hours=7)
    start = int((target - pd.Timedelta(minutes=10)).tz_localize("UTC").timestamp())
    end = int((target + pd.Timedelta(minutes=11)).tz_localize("UTC").timestamp())
    response = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"period1": start, "period2": end, "interval": "1m", "includePrePost": "true"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    response.raise_for_status()
    result = response.json().get("chart", {}).get("result") or []
    if not result:
        return None
    payload = result[0]
    timestamps = payload.get("timestamp") or []
    closes = ((payload.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    for timestamp, close in zip(timestamps, closes):
        point = pd.Timestamp(timestamp, unit="s", tz="UTC").tz_convert(None)
        if point == target:
            return clean_number(close)
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
    response = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"period1": start, "period2": end, "interval": "1m", "includePrePost": "true"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    response.raise_for_status()
    result = response.json().get("chart", {}).get("result") or []
    if not result:
        return None
    closes = ((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    values = [clean_number(value) for value in closes]
    values = [value for value in values if value not in (None, 0)]
    if not values:
        return None
    return clean_number(pd.Series(values).median())


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
    ref = prior.iloc[-1]
    base_nav = clean_number(ref.get("nav"))
    base_date = ref.get("nav_date")
    if base_nav in (None, 0) or not isinstance(base_date, str):
        return None
    # 该记录对应的美股收盘日就是 ref 的 A股日期（净值滞后一天）
    us_close = pd.Timestamp(ref["date"])
    fx_for_anchor = fixing if fixing is not None and not fixing.empty else fx
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

    # 近 7 个 A 股交易日（含最新）的 15:00 期货/汇率，一份缓存同时供「真实溢价率变化」
    # 与「近 7 日真实溢价率趋势」使用，避免逐日重复拉分钟线。
    TREND_POINTS = 7
    trend_dates = [pd.Timestamp(day) for day in list(trading_dates)[-TREND_POINTS:]]
    future_change_cache: dict[tuple[str, pd.Timestamp], float | None] = {}
    fx_change_cache: dict[pd.Timestamp, float | None] = {}
    for day in trend_dates:
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

        # 近 7 个 A 股交易日的 15:00 真实溢价率（口径与当日完全一致：锚 × 15:00 汇率 × 15:00 期货），
        # 供前端画迷你折线图看趋势。历史日同样用「当时能拿到的最新已发布净值」做锚。
        real_premium_trend = []
        for trend_day in trend_dates:
            day_rows = group[group["date"] == trend_day]
            if day_rows.empty:
                continue
            day_anchor = session_anchor_nav(
                group, trend_day, operating_fee, fund_index_series, fx, fund_exposure, fixing
            )
            day_future_raw = future_change_for(trend_day, future_key)
            day_future = clean_json_value(day_future_raw * future_beta) if day_future_raw is not None else None
            day_value = real_premium_at_china_close(
                clean_number(day_rows.iloc[0]["close"]),
                day_anchor["anchorNav"] if day_anchor else None,
                day_future,
                fx_change_for(trend_day),
            )
            if day_value is not None:
                real_premium_trend.append({"date": trend_day.strftime("%Y-%m-%d"), "value": round(day_value, 3)})

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

        premium_history = group.copy()
        premium_history["premium_for_mean"] = premium_history["live_premium_pct"]
        premium_history.loc[premium_history.index[-1], "premium_for_mean"] = latest_premium
        premium_history = premium_history.dropna(subset=["premium_for_mean"])
        prev10 = premium_history.iloc[-10:]
        prev20 = premium_history.iloc[-20:]
        prev30 = premium_history.iloc[-30:]

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
                "avg10Premium": clean_number(prev10["premium_for_mean"].mean()),
                "avg20Premium": clean_number(prev20["premium_for_mean"].mean()),
                "avg30Premium": clean_number(prev30["premium_for_mean"].mean()),
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


if __name__ == "__main__":
    main()
