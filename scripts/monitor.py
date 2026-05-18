#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import smtplib
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
DATA_CSV = ROOT / "data" / "nasdaq_qdii_live_premium.csv"
FEE_CSV = ROOT / "data" / "nasdaq_etf_fee.csv"
WEB_DATA_JSON = ROOT / "docs" / "data" / "funds.json"
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
    avg10_premium: float
    avg20_premium: float
    avg30_premium: float
    relative_deviation: float
    avg7_amount_wan: float
    operating_fee: float | None


def pct(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}%"


def pct_point(value: float, digits: int = 2) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.{digits}f}pct"


def amount_text(amount_wan: float) -> str:
    return f"{amount_wan / 10000:.2f}亿" if amount_wan >= 10000 else f"{amount_wan:.2f}万"


def fee_text(value: float | None) -> str:
    return pct(value) if value is not None else "-"


def in_a_share_time(now: datetime | None = None) -> bool:
    current = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if current.weekday() >= 5:
        return False
    t = current.time()
    return dtime(9, 31) <= t <= dtime(14, 55)


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


def load_web_metrics() -> dict[str, dict]:
    if not WEB_DATA_JSON.exists():
        return {}
    try:
        data = json.loads(WEB_DATA_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    funds = data.get("funds", [])
    if not isinstance(funds, list):
        return {}
    return {str(fund.get("code")).zfill(6): fund for fund in funds if fund.get("code")}


def number_or_none(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) else None


def build_signals(holding_code: str) -> tuple[FundSignal, list[FundSignal]]:
    holding_code = str(holding_code).strip().zfill(6)
    history = pd.read_csv(DATA_CSV, dtype={"code": str})
    history["code"] = history["code"].astype(str).str.zfill(6)
    web_metrics = load_web_metrics()
    history_by_code = {code: group.sort_values("date").reset_index(drop=True) for code, group in history.sort_values("date").groupby("code")}

    fees: dict[str, float] = {}
    if FEE_CSV.exists():
        fee_df = pd.read_csv(FEE_CSV, dtype={"code": str})
        fee_df["code"] = fee_df["code"].astype(str).str.zfill(6)
        fees = dict(zip(fee_df["code"], fee_df["operating_fee_total"]))

    prices = fetch_realtime_prices(sorted(FUNDS))
    signals: list[FundSignal] = []
    monitor_codes = sorted(set(FUNDS).intersection(set(history_by_code) | set(web_metrics)))
    for code in monitor_codes:
        group = history_by_code.get(code)
        metrics = web_metrics.get(code, {})
        latest_row = group.iloc[-1] if group is not None and not group.empty else None

        latest_close = number_or_none(metrics.get("latestClose"))
        if latest_close is None and latest_row is not None:
            latest_close = float(latest_row["close"])
        if latest_close is None:
            continue

        current_price = prices.get(code, latest_close)
        nav_for_premium = number_or_none(metrics.get("navForPremium"))
        if nav_for_premium is None and latest_row is not None:
            nav_for_premium = float(latest_row["nav"])
        if nav_for_premium in (None, 0):
            continue

        nav_for_premium_date = metrics.get("navForPremiumDate")
        if not nav_for_premium_date and latest_row is not None:
            nav_for_premium_date = str(latest_row["nav_date"])
        nav_for_premium_source = metrics.get("navForPremiumSource")
        current_premium = (current_price / nav_for_premium - 1) * 100
        avg10 = number_or_none(metrics.get("avg10Premium"))
        avg20 = number_or_none(metrics.get("avg20Premium"))
        avg30 = number_or_none(metrics.get("avg30Premium"))
        if (avg10 is None or avg20 is None or avg30 is None) and group is not None and not group.empty:
            latest_premium = (latest_close / nav_for_premium - 1) * 100
            premium_history = group.copy()
            premium_history["premium_for_mean"] = premium_history["live_premium_pct"]
            premium_history.loc[premium_history.index[-1], "premium_for_mean"] = latest_premium
            premium_history = premium_history.dropna(subset=["premium_for_mean"])
            avg10 = float(premium_history.iloc[-10:]["premium_for_mean"].mean())
            avg20 = float(premium_history.iloc[-20:]["premium_for_mean"].mean())
            avg30 = float(premium_history.iloc[-30:]["premium_for_mean"].mean())
        if avg10 is None or avg20 is None or avg30 is None:
            continue

        avg7_amount_wan = number_or_none(metrics.get("avg7AmountWan"))
        if avg7_amount_wan is None and group is not None and not group.empty:
            avg7_amount_wan = float(group.iloc[-7:]["amount_wan"].mean())
        if avg7_amount_wan is None:
            avg7_amount_wan = 0.0

        nav_label = str(nav_for_premium_date)
        if nav_for_premium_source == "estimated":
            nav_label += "估"
        signals.append(
            FundSignal(
                code=code,
                name=FUNDS.get(code, str(latest_row["name"])),
                price=current_price,
                nav=nav_for_premium,
                nav_date=nav_label,
                current_premium=current_premium,
                avg10_premium=avg10,
                avg20_premium=avg20,
                avg30_premium=avg30,
                relative_deviation=current_premium - avg20,
                avg7_amount_wan=avg7_amount_wan,
                operating_fee=number_or_none(metrics.get("operatingFee")) or fees.get(code),
            )
        )

    by_code = {signal.code: signal for signal in signals}
    if holding_code not in by_code:
        available = "、".join(sorted(by_code)) or "空"
        raise ValueError(f"当前持仓代码 {holding_code} 不在监控池中；当前可用代码：{available}")
    return by_code[holding_code], signals


def render_candidate(index: int, current: FundSignal, candidate: FundSignal) -> str:
    switch_advantage = current.relative_deviation - candidate.relative_deviation
    return f"""
{index}. {candidate.name} {candidate.code}
> 当前溢价：{pct(candidate.current_premium)}
> 10日均值：{pct(candidate.avg10_premium)}
> 20日均值：{pct(candidate.avg20_premium)}
> 30日均值：{pct(candidate.avg30_premium)}
> 相对偏离：{pct_point(candidate.relative_deviation)}
> 切换优势：{pct_point(switch_advantage)}
> 7日日均成交额：{amount_text(candidate.avg7_amount_wan)}
> 运作费率：{fee_text(candidate.operating_fee)}
"""


def render_message(current: FundSignal, candidates: list[FundSignal]) -> tuple[str, str]:
    best = candidates[0]
    switch_advantage = current.relative_deviation - best.relative_deviation
    title = f"纳指ETF切换提醒：{current.name} → {best.name}"
    candidate_blocks = "\n".join(render_candidate(index, current, candidate) for index, candidate in enumerate(candidates, start=1))
    content = f"""
## 纳指ETF持仓切换提醒

**当前持有：{current.name} {current.code}**
> 当前溢价：{pct(current.current_premium)}
> 10日均值：{pct(current.avg10_premium)}
> 20日均值：{pct(current.avg20_premium)}
> 30日均值：{pct(current.avg30_premium)}
> 相对偏离：{pct_point(current.relative_deviation)}
> 7日日均成交额：{amount_text(current.avg7_amount_wan)}
> 运作费率：{fee_text(current.operating_fee)}

**切换优势：{pct_point(switch_advantage)}**

**结论：可以考虑从 {current.name} 切到 {best.name}。**
> 溢价净值日：{best.nav_date}

**前5个最佳候选**
{candidate_blocks}

执行建议：不要开盘前10分钟硬切；用限价单，单笔资金可分2-3次成交。
"""
    return title, content


def send_wecom(webhook: str, content: str) -> dict:
    response = requests.post(
        webhook,
        json={"msgtype": "markdown", "markdown": {"content": content}},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def send_email(subject: str, content: str) -> None:
    smtp_host = os.getenv("EMAIL_SMTP_HOST", "smtp.qq.com")
    smtp_port = int(os.getenv("EMAIL_SMTP_PORT", "465"))
    email_from = os.getenv("EMAIL_FROM", "")
    email_password = os.getenv("EMAIL_PASSWORD", "")
    email_to = os.getenv("EMAIL_TO", email_from)

    if not email_from or not email_password or not email_to:
        raise ValueError("邮件配置不完整，需要 EMAIL_FROM、EMAIL_PASSWORD、EMAIL_TO")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = email_from
    message["To"] = email_to
    message.set_content(content)

    with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=20) as smtp:
        smtp.login(email_from, email_password)
        smtp.send_message(message)


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
    parser.add_argument("--cooldown-minutes", type=int, default=int(os.getenv("PUSH_COOLDOWN_MINUTES", "120")))
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

    top_candidates = sorted(candidates, key=lambda s: current.relative_deviation - s.relative_deviation, reverse=True)[:5]
    best = top_candidates[0]
    advantage = current.relative_deviation - best.relative_deviation

    print(f"当前持有：{current.name} {current.code}")
    print(
        f"当前溢价：{pct(current.current_premium)} "
        f"10日均值：{pct(current.avg10_premium)} "
        f"20日均值：{pct(current.avg20_premium)} "
        f"30日均值：{pct(current.avg30_premium)} "
        f"相对偏离：{pct_point(current.relative_deviation)}"
    )
    print(f"最佳候选：{best.name} {best.code}")
    print(
        f"当前溢价：{pct(best.current_premium)} "
        f"10日均值：{pct(best.avg10_premium)} "
        f"20日均值：{pct(best.avg20_premium)} "
        f"30日均值：{pct(best.avg30_premium)} "
        f"相对偏离：{pct_point(best.relative_deviation)}"
    )
    print("前5个最佳候选：")
    for index, candidate in enumerate(top_candidates, start=1):
        candidate_advantage = current.relative_deviation - candidate.relative_deviation
        print(
            f"{index}. {candidate.name} {candidate.code} "
            f"当前溢价：{pct(candidate.current_premium)} "
            f"10日均值：{pct(candidate.avg10_premium)} "
            f"20日均值：{pct(candidate.avg20_premium)} "
            f"30日均值：{pct(candidate.avg30_premium)} "
            f"相对偏离：{pct_point(candidate.relative_deviation)} "
            f"切换优势：{pct_point(candidate_advantage)}"
        )
    print(f"切换优势：{pct_point(advantage)} 阈值：{pct_point(args.threshold)}")

    if advantage < args.threshold:
        print("结论：不推送，切换优势不足。")
        return

    title, content = render_message(current, top_candidates)
    if args.dry_run:
        print("DRY RUN：满足条件，但不推送。")
        print(title)
        print(content)
        return

    email_from = os.getenv("EMAIL_FROM")
    email_password = os.getenv("EMAIL_PASSWORD")
    email_to = os.getenv("EMAIL_TO")
    wecom_webhook = os.getenv("WECOM_WEBHOOK")
    pushplus_token = os.getenv("PUSHPLUS_TOKEN")
    if not ((email_from and email_password and email_to) or wecom_webhook or pushplus_token):
        print("满足条件，但未设置邮件、WECOM_WEBHOOK 或 PUSHPLUS_TOKEN。")
        return

    key = f"{current.code}->{best.code}"
    now = time.time()
    state = load_state()
    if now - state.get(key, 0) < args.cooldown_minutes * 60:
        print(f"满足条件，但仍在冷却期内，未重复推送：{key}")
        return

    if email_from and email_password and email_to:
        send_email(title, content)
        print(f"邮件已发送到：{email_to}")
    elif wecom_webhook:
        result = send_wecom(wecom_webhook, content)
        print("企业微信返回：", result)
    else:
        result = send_pushplus(title, content, pushplus_token or "")
        print("PushPlus返回：", result)
    state[key] = now
    save_state(state)


if __name__ == "__main__":
    main()
