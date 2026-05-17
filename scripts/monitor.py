#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
DATA_CSV = ROOT / "data" / "nasdaq_qdii_live_premium.csv"
FEE_CSV = ROOT / "data" / "nasdaq_etf_fee.csv"
STATE_FILE = ROOT / "data" / "push_state.json"

FUNDS = {
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


@dataclass
class FundSignal:
    code: str
    name: str
    price: float
    nav: float
    nav_date: str
    current_premium: float
    avg20_premium: float
    relative_deviation: float
    avg7_amount_wan: float
    operating_fee: float | None


def pct(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}%"


def pct_point(value: float, digits: int = 2) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.{digits}f}pct"


def amount_text(amount_wan: float) -> str:
    return f"{amount_wan / 10000:.2f}亿" if amount_wan >= 10000 else f"{amount_wan:.0f}万"


def in_a_share_time(now: datetime | None = None) -> bool:
    current = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if current.weekday() >= 5:
        return False
    t = current.time()
    return (dtime(9, 45) <= t <= dtime(11, 30)) or (dtime(13, 0) <= t <= dtime(14, 55))


def secid(code: str) -> str:
    return ("1." if code.startswith("5") else "0.") + code


def sina_symbol(code: str) -> str:
    return ("sh" if code.startswith("5") else "sz") + code


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_eastmoney_prices(codes: list[str]) -> dict[str, float]:
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    prices: dict[str, float] = {}
    chunks = [codes[i : i + 4] for i in range(0, len(codes), 4)]
    for chunk in chunks:
        params = {
            "fltt": "2",
            "invt": "2",
            "fields": "f12,f14,f2,f3,f4,f5,f6",
            "secids": ",".join(secid(code) for code in chunk),
        }
        for attempt in range(3):
            try:
                response = requests.get(
                    url,
                    params=params,
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
                    timeout=10,
                )
                response.raise_for_status()
                for item in response.json()["data"]["diff"]:
                    if item.get("f2") not in (None, "-"):
                        prices[str(item["f12"]).zfill(6)] = float(item["f2"])
                break
            except Exception as error:
                if attempt == 2:
                    print(f"东方财富分组 {chunk} 抓取失败：{error}")
                time.sleep(0.5)
    return prices


def fetch_sina_prices(codes: list[str]) -> dict[str, float]:
    if not codes:
        return {}
    url = "https://hq.sinajs.cn/list=" + ",".join(sina_symbol(code) for code in codes)
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
        timeout=15,
    )
    response.raise_for_status()
    prices: dict[str, float] = {}
    for code in codes:
        marker = f"hq_str_{sina_symbol(code)}=\""
        start = response.text.find(marker)
        if start < 0:
            continue
        start += len(marker)
        end = response.text.find('"', start)
        fields = response.text[start:end].split(",")
        if len(fields) > 3 and fields[3]:
            prices[code] = float(fields[3])
    return prices


def fetch_realtime_prices(codes: list[str]) -> dict[str, float]:
    prices = fetch_eastmoney_prices(codes)
    missing = [code for code in codes if code not in prices]
    if missing:
        try:
            prices.update(fetch_sina_prices(missing))
        except Exception as error:
            print(f"新浪备用行情抓取失败：{error}")
    return prices


def build_signals(holding_code: str) -> tuple[FundSignal, list[FundSignal]]:
    history = pd.read_csv(DATA_CSV, dtype={"code": str})
    latest_date = history["date"].max()
    latest = history[history["date"] == latest_date].copy()

    fees: dict[str, float] = {}
    if FEE_CSV.exists():
        fee_df = pd.read_csv(FEE_CSV, dtype={"code": str})
        fees = dict(zip(fee_df["code"], fee_df["operating_fee_total"]))

    prices = fetch_realtime_prices(sorted(FUNDS))
    signals: list[FundSignal] = []
    for code, group in history.sort_values("date").groupby("code"):
        group = group.sort_values("date").reset_index(drop=True)
        latest_row = latest[latest["code"] == code].iloc[0]
        current_price = prices.get(code, float(latest_row["close"]))
        current_premium = (current_price / float(latest_row["nav"]) - 1) * 100
        prev20 = group.iloc[-21:-1] if len(group) >= 21 else group.iloc[:-1]
        last7 = group.iloc[-7:]
        avg20 = float(prev20["live_premium_pct"].mean())
        signals.append(
            FundSignal(
                code=code,
                name=FUNDS.get(code, str(latest_row["name"])),
                price=current_price,
                nav=float(latest_row["nav"]),
                nav_date=str(latest_row["nav_date"]),
                current_premium=current_premium,
                avg20_premium=avg20,
                relative_deviation=current_premium - avg20,
                avg7_amount_wan=float(last7["amount_wan"].mean()),
                operating_fee=fees.get(code),
            )
        )

    by_code = {signal.code: signal for signal in signals}
    if holding_code not in by_code:
        raise ValueError(f"当前持仓代码 {holding_code} 不在监控池中")
    return by_code[holding_code], signals


def render_message(current: FundSignal, candidate: FundSignal, switch_advantage: float) -> tuple[str, str]:
    title = f"纳指ETF切换提醒：{current.name} → {candidate.name}"
    content = f"""
<h3>纳指ETF持仓切换提醒</h3>
<p><b>当前持有：{current.name} {current.code}</b><br>
当前溢价：{pct(current.current_premium)}<br>
20日均值：{pct(current.avg20_premium)}<br>
相对偏离：{pct_point(current.relative_deviation)}</p>

<p><b>候选切换：{candidate.name} {candidate.code}</b><br>
当前溢价：{pct(candidate.current_premium)}<br>
20日均值：{pct(candidate.avg20_premium)}<br>
相对偏离：{pct_point(candidate.relative_deviation)}</p>

<p><b>切换优势：{pct_point(switch_advantage)}</b></p>

<p><b>结论：可以考虑从 {current.name} 切到 {candidate.name}。</b><br>
候选7日日均成交额：{amount_text(candidate.avg7_amount_wan)}<br>
候选运作费：{pct(candidate.operating_fee) if candidate.operating_fee is not None else "-"}<br>
候选使用净值日：{candidate.nav_date}</p>

<p>执行建议：不要开盘前10分钟硬切；用限价单，单笔资金可分2-3次成交。</p>
"""
    return title, content


def send_pushplus(title: str, content: str, token: str) -> dict:
    response = requests.post(
        "https://www.pushplus.plus/send",
        json={"token": token, "title": title, "content": content, "template": "html"},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="纳指ETF持仓切换微信提醒")
    parser.add_argument("--holding", default=os.getenv("CURRENT_HOLDING_CODE", "159660"))
    parser.add_argument("--threshold", type=float, default=float(os.getenv("SWITCH_THRESHOLD", "1.2")))
    parser.add_argument("--min-amount-wan", type=float, default=float(os.getenv("MIN_AVG7_AMOUNT_WAN", "10000")))
    parser.add_argument("--max-candidate-premium", type=float, default=float(os.getenv("MAX_CANDIDATE_PREMIUM", "3.0")))
    parser.add_argument("--cooldown-minutes", type=int, default=int(os.getenv("PUSH_COOLDOWN_MINUTES", "60")))
    parser.add_argument("--ignore-market-time", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.ignore_market_time and not in_a_share_time():
        print("非A股监控时间，退出。")
        return

    current, signals = build_signals(args.holding)
    candidates = [
        s
        for s in signals
        if s.code != current.code
        and s.avg7_amount_wan >= args.min_amount_wan
        and s.current_premium <= args.max_candidate_premium
    ]
    if not candidates:
        print("没有满足流动性和绝对溢价过滤条件的候选。")
        return

    best = sorted(candidates, key=lambda s: current.relative_deviation - s.relative_deviation, reverse=True)[0]
    advantage = current.relative_deviation - best.relative_deviation

    print(f"当前持有：{current.name} {current.code}")
    print(f"当前溢价：{pct(current.current_premium)} 20日均值：{pct(current.avg20_premium)} 相对偏离：{pct_point(current.relative_deviation)}")
    print(f"最佳候选：{best.name} {best.code}")
    print(f"当前溢价：{pct(best.current_premium)} 20日均值：{pct(best.avg20_premium)} 相对偏离：{pct_point(best.relative_deviation)}")
    print(f"切换优势：{pct_point(advantage)} 阈值：{pct_point(args.threshold)}")

    if advantage < args.threshold:
        print("结论：不推送，切换优势不足。")
        return

    title, content = render_message(current, best, advantage)
    if args.dry_run:
        print("DRY RUN：满足条件，但不推送。")
        print(title)
        print(content)
        return

    token = os.getenv("PUSHPLUS_TOKEN")
    if not token:
        print("满足条件，但未设置 PUSHPLUS_TOKEN。")
        return

    key = f"{current.code}->{best.code}"
    now = time.time()
    state = load_state()
    if now - state.get(key, 0) < args.cooldown_minutes * 60:
        print(f"满足条件，但仍在冷却期内，未重复推送：{key}")
        return

    result = send_pushplus(title, content, token)
    state[key] = now
    save_state(state)
    print("PushPlus返回：", result)


if __name__ == "__main__":
    main()
