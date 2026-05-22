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
}

FUND_INDEX_SYMBOLS = {}

GROUPS = {
    "nasdaq100": {
        "name": "纳指100",
        "index_symbol": ".NDX",
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
        "benchmark_symbol": "SPY",
        "future_secid": "103.ES00Y",
        "tencent_future_symbol": "hf_ES",
        "sina_future_symbol": "hf_ES",
        "etfcom_index_codes": ["SPX", "SPTR500N"],
        "default_holding": "513500",
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


def load_estimation_series() -> tuple[dict[str, pd.DataFrame], pd.DataFrame | None]:
    try:
        index_map = {}
        for group_key, meta in GROUPS.items():
            index_map[group_key] = retry_fetch(
                f"{meta['name']}历史行情",
                lambda symbol=meta["index_symbol"]: fetch_us_index_history(symbol),
            )
        for code, symbol in FUND_INDEX_SYMBOLS.items():
            fetcher = (lambda symbol=symbol: fetch_yahoo_daily(symbol, adjusted=False)) if symbol.startswith("^") else (
                lambda symbol=symbol: fetch_us_index_history(symbol)
            )
            index_map[code] = retry_fetch(f"{code}历史行情 {symbol}", fetcher)

        try:
            fx = retry_fetch("美元人民币中间价历史行情", fetch_usdcnyc_history).copy()
        except Exception as error:
            print(f"美元人民币中间价获取失败，改用Yahoo CNY=X：{error}")
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


def fetch_yahoo_daily(symbol: str, adjusted: bool = True) -> pd.DataFrame:
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
    return df.dropna(subset=["date", "close"]).sort_values("date").set_index("date")


def estimate_nav(
    official_nav: float,
    official_nav_date: str,
    operating_fee: float | None,
    index_series: pd.DataFrame | None,
    fx: pd.DataFrame | None,
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
    index_start = value_at_or_before(index_series, nav_date)
    if not target_dates or index_start is None:
        return result

    target_date = target_dates[-1]
    index_end = value_at_or_before(index_series, target_date)
    if index_end is None:
        return result
    index_ret = float(index_end / index_start - 1)
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
        operating_fee = float(fee_map[code]) if code in fee_map else None
        nav_info = estimate_nav(
            official_nav=float(official_nav_row["nav"]),
            official_nav_date=str(official_nav_row["nav_date"]),
            operating_fee=operating_fee,
            index_series=index_map[code] if code in index_map else index_map.get(group_key),
            fx=fx,
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
        latest_premium = None
        estimated_nav_for_premium = nav_info.get("navForReturn") or nav_info.get("navForPremium")
        if clean_number(latest["close"]) is not None and clean_number(estimated_nav_for_premium) not in (None, 0):
            latest_premium = (float(latest["close"]) / float(estimated_nav_for_premium) - 1) * 100
        etfcom = etfcom_map.get(code, {})

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
                "nav": clean_number(official_nav_row["nav"]),
                "navDate": str(official_nav_row["nav_date"]),
                **nav_info,
                "latestPremium": clean_number(latest_premium),
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
    clean_payload = clean_json_value(payload)
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
