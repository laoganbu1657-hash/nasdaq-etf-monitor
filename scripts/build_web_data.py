#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import time
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


def load_estimation_series() -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    try:
        ndx = retry_fetch("纳指100历史行情", lambda: ak.index_us_stock_sina(".NDX")).copy()
        ndx["date"] = pd.to_datetime(ndx["date"])
        ndx["close"] = pd.to_numeric(ndx["close"], errors="coerce")
        ndx = ndx.dropna(subset=["date", "close"]).sort_values("date").set_index("date")

        fx = retry_fetch("美元人民币中间价历史行情", fetch_usdcnyc_history).copy()
        fx = fx.dropna(subset=["date", "close"]).sort_values("date").set_index("date")
        return ndx, fx
    except Exception as error:
        print(f"估算净值数据源获取失败：{error}")
        return None, None


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
    response = requests.get(
        url,
        params=params,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()["data"]["klines"]
    rows = [item.split(",") for item in data]
    df = pd.DataFrame(rows)
    return pd.DataFrame(
        {
            "date": pd.to_datetime(df[0], errors="coerce"),
            "close": pd.to_numeric(df[2], errors="coerce"),
        }
    )


def estimate_nav(
    official_nav: float,
    official_nav_date: str,
    operating_fee: float | None,
    ndx: pd.DataFrame | None,
    fx: pd.DataFrame | None,
) -> dict:
    result = {
        "navForPremium": official_nav,
        "navForPremiumDate": official_nav_date,
        "navForPremiumSource": "official",
        "estimateNdxReturn": None,
        "estimateFxReturn": None,
    }
    if ndx is None or fx is None:
        return result

    nav_date = pd.to_datetime(official_nav_date)
    common_dates = sorted(set(ndx.index).intersection(set(fx.index)))
    target_dates = [d for d in common_dates if d > nav_date]
    if not target_dates or nav_date not in ndx.index or nav_date not in fx.index:
        return result

    target_date = target_dates[-1]
    ndx_ret = float(ndx.loc[target_date, "close"] / ndx.loc[nav_date, "close"] - 1)
    fx_ret = float(fx.loc[target_date, "close"] / fx.loc[nav_date, "close"] - 1)
    fee_days = max((target_date - nav_date).days, 1)
    fee_drag = ((operating_fee or 0) / 100) / 365 * fee_days
    estimated_nav = official_nav * (1 + ndx_ret) * (1 + fx_ret) * (1 - fee_drag)

    result.update(
        {
            "navForPremium": float(estimated_nav),
            "navForPremiumDate": target_date.strftime("%Y-%m-%d"),
            "navForPremiumSource": "estimated",
            "estimateNdxReturn": ndx_ret * 100,
            "estimateFxReturn": fx_ret * 100,
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

    ndx, fx = load_estimation_series()
    latest_date = history["date"].max()
    funds = []
    for code, group in history.sort_values("date").groupby("code"):
        group = group.sort_values("date").reset_index(drop=True)
        latest = group.iloc[-1]
        valid_nav_rows = group.dropna(subset=["nav", "nav_date"]).copy()
        official_nav_row = valid_nav_rows.iloc[-1] if len(valid_nav_rows) else latest
        y2026 = group[group["date"].dt.year == 2026].copy()

        return_2026 = None
        max_dd_2026 = None
        if len(y2026) >= 2:
            return_2026 = pct_return(float(y2026.iloc[0]["close"]), float(y2026.iloc[-1]["close"]))
            max_dd_2026 = max_drawdown(y2026["close"].astype(float))

        last7 = group.iloc[-7:]
        operating_fee = float(fee_map[code]) if code in fee_map else None
        nav_info = estimate_nav(
            official_nav=float(official_nav_row["nav"]),
            official_nav_date=str(official_nav_row["nav_date"]),
            operating_fee=operating_fee,
            ndx=ndx,
            fx=fx,
        )
        latest_premium = None
        if clean_number(latest["close"]) is not None and clean_number(nav_info["navForPremium"]) not in (None, 0):
            latest_premium = (float(latest["close"]) / float(nav_info["navForPremium"]) - 1) * 100

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
                "market": "sh" if code.startswith("5") else "sz",
                "secid": ("1." if code.startswith("5") else "0.") + code,
                "latestDate": latest["date"].strftime("%Y-%m-%d"),
                "latestClose": clean_number(latest["close"]),
                "nav": clean_number(official_nav_row["nav"]),
                "navDate": str(official_nav_row["nav_date"]),
                **nav_info,
                "latestPremium": clean_number(latest_premium),
                "avg10Premium": clean_number(prev10["premium_for_mean"].mean()),
                "avg20Premium": clean_number(prev20["premium_for_mean"].mean()),
                "avg30Premium": clean_number(prev30["premium_for_mean"].mean()),
                "avg7AmountWan": clean_number(last7["amount_wan"].mean()),
                "amountWan": clean_number(latest["amount_wan"]),
                "operatingFee": operating_fee,
                "return2025": clean_number(return_2025_map[code]) if code in return_2025_map else None,
                "return2026": clean_number(return_2026),
                "maxDrawdown2026": clean_number(max_dd_2026),
                "subscribeStatus": str(latest.get("subscribe_status", "")),
                "redeemStatus": str(latest.get("redeem_status", "")),
            }
        )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generatedAt": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "latestDate": latest_date.strftime("%Y-%m-%d"),
        "funds": funds,
    }
    json_text = json.dumps(
        payload,
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
