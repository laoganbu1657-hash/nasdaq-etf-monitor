#!/usr/bin/env python3
from __future__ import annotations

from datetime import date
from io import StringIO
from pathlib import Path
import re

import akshare as ak
import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUT_CSV = DATA_DIR / "nasdaq_qdii_live_premium.csv"
SUMMARY_CSV = DATA_DIR / "summary.csv"

START = "2025-01-01"
NAV_START = "2024-12-01"
END = date.today().isoformat()
SCRIPT_VERSION = "nav-fallback-v4-2026-07-28"

FUNDS = [
    {"code": "159501", "name": "纳指ETF嘉实", "group": "nasdaq100"},
    {"code": "159509", "name": "纳指科技ETF景顺", "group": "nasdaq100"},
    {"code": "159513", "name": "纳斯达克100ETF大成", "group": "nasdaq100"},
    {"code": "159632", "name": "纳斯达克ETF华安", "group": "nasdaq100"},
    {"code": "159659", "name": "纳斯达克100ETF招商", "group": "nasdaq100"},
    {"code": "159660", "name": "纳指ETF汇添富", "group": "nasdaq100"},
    {"code": "159696", "name": "纳指ETF易方达", "group": "nasdaq100"},
    {"code": "159941", "name": "纳指ETF广发", "group": "nasdaq100"},
    {"code": "513100", "name": "纳指ETF国泰", "group": "nasdaq100"},
    {"code": "513110", "name": "纳指ETF华泰柏瑞", "group": "nasdaq100"},
    {"code": "513300", "name": "纳斯达克ETF华夏", "group": "nasdaq100"},
    {"code": "513390", "name": "纳指100ETF博时", "group": "nasdaq100"},
    {"code": "513870", "name": "纳指ETF富国", "group": "nasdaq100"},
    {"code": "513500", "name": "标普500ETF博时", "group": "sp500"},
    {"code": "159612", "name": "标普500ETF国泰", "group": "sp500"},
    {"code": "513650", "name": "标普500ETF南方", "group": "sp500"},
    {"code": "159655", "name": "标普500ETF华夏", "group": "sp500"},
    {"code": "161128", "name": "标普信息科技LOF", "group": "nasdaq100"},
]

NAV_COLS = [
    "nav_date",
    "nav",
    "accum_nav",
    "nav_growth_pct",
    "subscribe_status",
    "redeem_status",
]

FILL_FROM_EXISTING_COLS = ["live_premium_pct"] + NAV_COLS


def market_symbol(code: str) -> str:
    return ("sh" if code.startswith("5") else "sz") + code


def fetch_price_df(code: str) -> pd.DataFrame:
    df = ak.fund_etf_hist_sina(symbol=market_symbol(code)).copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df[(df["date"] >= pd.Timestamp(START)) & (df["date"] <= pd.Timestamp(END))]
    df = df.sort_values("date")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def empty_nav_df() -> pd.DataFrame:
    return pd.DataFrame(columns=NAV_COLS).astype(
        {
            "nav_date": "datetime64[ns]",
            "nav": "float64",
            "accum_nav": "float64",
            "nav_growth_pct": "float64",
            "subscribe_status": "object",
            "redeem_status": "object",
        }
    )


def nav_df_from_existing(existing: pd.DataFrame, code: str) -> pd.DataFrame:
    if existing.empty:
        return empty_nav_df()
    old = existing[existing["code"].astype(str).str.zfill(6) == code].copy()
    if old.empty:
        return empty_nav_df()
    for col in NAV_COLS:
        if col not in old.columns:
            old[col] = pd.NA
    old = old[NAV_COLS].copy()
    old["nav_date"] = pd.to_datetime(old["nav_date"], errors="coerce")
    old["nav"] = pd.to_numeric(old["nav"], errors="coerce")
    old["accum_nav"] = pd.to_numeric(old["accum_nav"], errors="coerce")
    old["nav_growth_pct"] = pd.to_numeric(old["nav_growth_pct"], errors="coerce")
    return old.dropna(subset=["nav_date", "nav"]).drop_duplicates("nav_date", keep="last").sort_values("nav_date")


def normalize_nav_df(df: pd.DataFrame) -> pd.DataFrame:
    for col in NAV_COLS:
        if col not in df.columns:
            df[col] = pd.NA
    df = df[NAV_COLS].copy()
    df["nav_date"] = pd.to_datetime(df["nav_date"], errors="coerce")
    df["nav"] = pd.to_numeric(df["nav"], errors="coerce")
    df["accum_nav"] = pd.to_numeric(df["accum_nav"], errors="coerce")
    df["nav_growth_pct"] = pd.to_numeric(
        df["nav_growth_pct"].astype(str).str.replace("%", "", regex=False),
        errors="coerce",
    )
    return df.dropna(subset=["nav_date", "nav"]).drop_duplicates("nav_date", keep="last").sort_values("nav_date")


def fetch_nav_api_df(code: str) -> pd.DataFrame:
    rows = []
    page = 1
    while True:
        response = requests.get(
            "https://api.fund.eastmoney.com/f10/lsjz",
            params={
                "fundCode": code,
                "pageIndex": page,
                "pageSize": 200,
                "startDate": NAV_START,
                "endDate": END,
            },
            headers={"User-Agent": "Mozilla/5.0", "Referer": f"https://fundf10.eastmoney.com/jjjz_{code}.html"},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json().get("Data") or {}
        items = data.get("LSJZList") or []
        if not items:
            break
        rows.extend(
            {
                "nav_date": item.get("FSRQ"),
                "nav": item.get("DWJZ"),
                "accum_nav": item.get("LJJZ"),
                "nav_growth_pct": item.get("JZZZL"),
                "subscribe_status": item.get("SGZT"),
                "redeem_status": item.get("SHZT"),
            }
            for item in items
        )
        pages = int(data.get("Pages") or page)
        if page >= pages:
            break
        page += 1
    return normalize_nav_df(pd.DataFrame(rows)) if rows else empty_nav_df()


def fetch_nav_table_df(code: str) -> pd.DataFrame:
    frames = []
    page = 1
    while True:
        params = {
            "type": "lsjz",
            "code": code,
            "page": page,
            "per": 200,
            "sdate": NAV_START,
            "edate": END,
        }
        response = requests.get(
            "https://fundf10.eastmoney.com/F10DataApi.aspx",
            params=params,
            headers={"User-Agent": "Mozilla/5.0", "Referer": f"https://fundf10.eastmoney.com/jjjz_{code}.html"},
            timeout=20,
        )
        response.raise_for_status()
        response.encoding = "utf-8"
        try:
            tables = pd.read_html(StringIO(response.text))
        except ValueError:
            tables = []
        if not tables:
            break
        frames.append(tables[0])
        pages_match = re.search(r"pages:(\d+)", response.text)
        pages = int(pages_match.group(1)) if pages_match else page
        if page >= pages:
            break
        page += 1

    if not frames:
        return empty_nav_df()
    df = pd.concat(frames, ignore_index=True).copy()
    df = df.rename(
        columns={
            "净值日期": "nav_date",
            "单位净值": "nav",
            "累计净值": "accum_nav",
            "日增长率": "nav_growth_pct",
            "申购状态": "subscribe_status",
            "赎回状态": "redeem_status",
        }
    )
    return normalize_nav_df(df)


def fetch_nav_df(code: str, existing: pd.DataFrame) -> pd.DataFrame:
    for source_name, fetcher in [
        ("东方财富 JSON", fetch_nav_api_df),
        ("东方财富网页", fetch_nav_table_df),
    ]:
        try:
            df = fetcher(code)
        except Exception as error:
            print(f"Warning: {code} {source_name}历史净值获取失败：{error}")
            continue
        if not df.empty:
            return df

    fallback = nav_df_from_existing(existing, code)
    if not fallback.empty:
        latest = fallback["nav_date"].iloc[-1].strftime("%Y-%m-%d")
        print(f"Warning: {code} 历史净值接口为空，沿用本地已有净值到 {latest}")
        return fallback
    print(f"Warning: {code} 历史净值接口为空，且本地没有可沿用净值")
    return empty_nav_df()


def build_one(code: str, name: str, group: str, existing: pd.DataFrame) -> pd.DataFrame:
    prices = fetch_price_df(code)
    navs = fetch_nav_df(code, existing)
    if navs.empty:
        merged = prices.copy()
        for col in NAV_COLS:
            merged[col] = pd.NA
    else:
        merged = pd.merge_asof(
            prices.sort_values("date"),
            navs.sort_values("nav_date"),
            left_on="date",
            right_on="nav_date",
            direction="backward",
            allow_exact_matches=False,
        )
    merged.insert(0, "group", group)
    merged.insert(0, "name", name)
    merged.insert(0, "code", code)
    merged["amount_wan"] = merged["amount"] / 10000
    merged["live_premium_pct"] = (merged["close"] / merged["nav"] - 1) * 100
    merged["date"] = merged["date"].dt.strftime("%Y-%m-%d")
    merged["nav_date"] = merged["nav_date"].dt.strftime("%Y-%m-%d")
    return merged[
        [
            "code",
            "name",
            "group",
            "date",
            "close",
            "amount",
            "amount_wan",
            "nav_date",
            "nav",
            "live_premium_pct",
            "open",
            "high",
            "low",
            "volume",
            "accum_nav",
            "nav_growth_pct",
            "subscribe_status",
            "redeem_status",
        ]
    ]


def fill_missing_from_existing(df: pd.DataFrame, existing: pd.DataFrame, code: str) -> pd.DataFrame:
    if existing.empty:
        return df
    old = existing[existing["code"].astype(str).str.zfill(6) == code].copy()
    if old.empty:
        return df
    keep_cols = ["date"] + [col for col in FILL_FROM_EXISTING_COLS if col in old.columns and col in df.columns]
    old = old[keep_cols].drop_duplicates("date", keep="last").add_suffix("_old")
    old = old.rename(columns={"date_old": "date"})
    merged = df.merge(old, on="date", how="left")
    for col in FILL_FROM_EXISTING_COLS:
        old_col = f"{col}_old"
        if col not in merged.columns or old_col not in merged.columns:
            continue
        if col in {"nav", "live_premium_pct", "accum_nav", "nav_growth_pct"}:
            merged[col] = pd.to_numeric(merged[col], errors="coerce")
            merged[old_col] = pd.to_numeric(merged[old_col], errors="coerce")
        else:
            merged[col] = merged[col].replace({"": pd.NA, "nan": pd.NA, "NaT": pd.NA})
            merged[old_col] = merged[old_col].replace({"": pd.NA, "nan": pd.NA, "NaT": pd.NA})
        merged[col] = merged[col].combine_first(merged[old_col])
    return merged[[col for col in df.columns]]


def existing_fund_df(existing: pd.DataFrame, code: str, name: str, group: str) -> pd.DataFrame:
    if existing.empty:
        return pd.DataFrame()
    df = existing[existing["code"].astype(str).str.zfill(6) == code].copy()
    if df.empty:
        return df
    df["code"] = code
    df["name"] = name
    df["group"] = group
    return df.sort_values("date")


def summary_for_df(code: str, name: str, group: str, df: pd.DataFrame) -> dict:
    live_premium = pd.to_numeric(df["live_premium_pct"], errors="coerce") if "live_premium_pct" in df else pd.Series(dtype="float64")
    amount_wan = pd.to_numeric(df["amount_wan"], errors="coerce") if "amount_wan" in df else pd.Series(dtype="float64")
    latest_premium = live_premium.dropna().iloc[-1] if not live_premium.dropna().empty else ""
    return {
        "code": code,
        "name": name,
        "group": group,
        "start_date": df["date"].iloc[0] if len(df) and "date" in df else "",
        "end_date": df["date"].iloc[-1] if len(df) and "date" in df else "",
        "trading_days": len(df),
        "latest_premium_pct": round(float(latest_premium), 4) if latest_premium != "" else "",
        "avg_premium_pct": round(float(live_premium.mean()), 4) if not live_premium.dropna().empty else "",
        "avg_amount_wan": round(float(amount_wan.mean()), 2) if not amount_wan.dropna().empty else "",
    }


def main() -> None:
    print(f"Update data script version: {SCRIPT_VERSION}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = pd.read_csv(OUT_CSV, dtype={"code": str}) if OUT_CSV.exists() else pd.DataFrame()
    frames = []
    summary_rows = []

    for fund in FUNDS:
        code = fund["code"]
        name = fund["name"]
        group = fund["group"]
        print(f"Fetching {code} {name}")
        try:
            df = build_one(code, name, group, existing)
            df = fill_missing_from_existing(df, existing, code)
        except Exception as error:
            print(f"Warning: {code} {name} 更新失败，沿用本地已有数据：{error}")
            df = existing_fund_df(existing, code, name, group)
        if df.empty:
            print(f"Warning: {code} {name} 没有可写入数据，跳过")
            continue
        frames.append(df)
        summary_rows.append(summary_for_df(code, name, group, df))

    if not frames:
        raise RuntimeError("所有基金都没有可写入数据，请检查数据源或仓库内历史 CSV")
    all_df = pd.concat(frames, ignore_index=True).sort_values(["date", "code"])
    all_df.to_csv(OUT_CSV, index=False)
    pd.DataFrame(summary_rows).to_csv(SUMMARY_CSV, index=False)
    print(f"Updated {OUT_CSV}")


if __name__ == "__main__":
    main()
