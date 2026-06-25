#!/usr/bin/env python3
from __future__ import annotations

from datetime import date
from pathlib import Path

import akshare as ak
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUT_CSV = DATA_DIR / "nasdaq_qdii_live_premium.csv"
SUMMARY_CSV = DATA_DIR / "summary.csv"

START = "2025-01-01"
NAV_START = "2024-12-01"
END = date.today().isoformat()

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

FILL_FROM_EXISTING_COLS = [
    "nav_date",
    "nav",
    "live_premium_pct",
    "accum_nav",
    "nav_growth_pct",
    "subscribe_status",
    "redeem_status",
]


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


def fetch_nav_df(code: str) -> pd.DataFrame:
    df = ak.fund_etf_fund_info_em(
        fund=code,
        start_date=NAV_START.replace("-", ""),
        end_date=END.replace("-", ""),
    ).copy()
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
    df["nav_date"] = pd.to_datetime(df["nav_date"])
    df["nav"] = pd.to_numeric(df["nav"], errors="coerce")
    return df.dropna(subset=["nav_date", "nav"]).sort_values("nav_date")


def build_one(code: str, name: str, group: str) -> pd.DataFrame:
    prices = fetch_price_df(code)
    navs = fetch_nav_df(code)
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


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = pd.read_csv(OUT_CSV, dtype={"code": str}) if OUT_CSV.exists() else pd.DataFrame()
    frames = []
    summary_rows = []

    for fund in FUNDS:
        code = fund["code"]
        name = fund["name"]
        group = fund["group"]
        print(f"Fetching {code} {name}")
        df = build_one(code, name, group)
        df = fill_missing_from_existing(df, existing, code)
        frames.append(df)
        summary_rows.append(
            {
                "code": code,
                "name": name,
                "group": group,
                "start_date": df["date"].iloc[0] if len(df) else "",
                "end_date": df["date"].iloc[-1] if len(df) else "",
                "trading_days": len(df),
                "latest_premium_pct": round(df["live_premium_pct"].iloc[-1], 4) if len(df) else "",
                "avg_premium_pct": round(df["live_premium_pct"].mean(), 4) if len(df) else "",
                "avg_amount_wan": round(df["amount_wan"].mean(), 2) if len(df) else "",
            }
        )

    all_df = pd.concat(frames, ignore_index=True).sort_values(["date", "code"])
    all_df.to_csv(OUT_CSV, index=False)
    pd.DataFrame(summary_rows).to_csv(SUMMARY_CSV, index=False)
    print(f"Updated {OUT_CSV}")


if __name__ == "__main__":
    main()
