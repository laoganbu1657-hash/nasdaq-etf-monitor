"""Exchange subscription limits and locally available historical peaks."""
import json
import re
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]


def number(value):
    match = re.search(r"-?[\d,]+(?:\.\d+)?", str(value))
    return float(match[0].replace(",", "")) if match else None


def sse_query(sql, **params):
    response = requests.get(
        "https://query.sse.com.cn/commonQuery.do",
        params={"sqlId": sql, "isPagination": "false", **params},
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.sse.com.cn/"}, timeout=15,
    )
    response.raise_for_status()
    return response.json().get("result") or []


def exchange_limit(code):
    if code == "161128":
        return {"subscriptionLimitNote": "LOF，不适用ETF申赎清单"}
    if code.startswith("5"):
        rows = sse_query("COMMON_SSE_CP_JJLB_ETFJJGK_GGSGSHQD_JBXX_C", FUNDID2=code)
        if not rows:
            return {}
        row = rows[0]
        limits = [(number(row.get(key)), label) for key, label in
                  [("CREATION_LIMIT", "累计可申购上限"), ("NET_CREATION_LIMIT", "净申购上限")]]
        finite = [(value, label) for value, label in limits if value is not None]
        qty, label = min(finite) if finite else (None, "不设上限/未公布")
        nav = number(row.get("NAV"))
        result = dict(subscriptionLimitShares=qty, subscriptionLimitNav=nav,
                      subscriptionLimitDate=row.get("TRADING_DAY"),
                      subscriptionLimitNote=label,
                      subscriptionLimitSource=f"https://www.sse.com.cn/assortment/fund/list/etfinfo/basic/index.shtml?FUNDID={code}")
        try:
            sizes = sse_query("COMMON_SSE_ZQPZ_ETFZL_ETFJBXX_JJGM_MOREN_L", SEC_CODE=code)
            if sizes:
                size = max(sizes, key=lambda x: x.get("STAT_DATE", ""))
                shares = number(size.get("TOT_VOL"))
                if shares is not None:
                    result.update(exchangeShares=shares * 10000, scaleDate=size.get("STAT_DATE"))
        except Exception:
            pass
    else:
        result = {}
        for offset in range(8):
            day = pd.Timestamp.now(tz="Asia/Shanghai").normalize() - pd.Timedelta(days=offset)
            url = f"https://reportdocs.static.szse.cn/files/text/etf/ETF{code}{day:%Y%m%d}.txt"
            response = requests.get(url, timeout=10)
            if response.status_code != 200:
                continue
            text = response.content.decode("gb18030", errors="replace")
            qty_match = re.search(r"^\s*当天净申购的基金份额上限[：:]\s*([^\r\n]+)", text, re.M)
            nav_match = re.search(r"基金份额净值[：:]\s*([\d.]+)", text)
            if not qty_match:
                continue
            total_match = re.search(r"^\s*当天累计可申购的基金份额上限[：:]\s*([^\r\n]+)", text, re.M)
            finite = [(number(match[1]), label) for match, label in
                      [(qty_match, "净申购上限"), (total_match, "累计可申购上限")]
                      if match and number(match[1]) is not None]
            qty, label = min(finite) if finite else (None, "不设上限")
            nav = number(nav_match[1]) if nav_match else None
            result = dict(subscriptionLimitShares=qty, subscriptionLimitNav=nav,
                          subscriptionLimitDate=f"{day:%Y-%m-%d}", subscriptionLimitNote=label,
                          subscriptionLimitSource=url)
            break
    if result.get("subscriptionLimitShares") is not None and result.get("subscriptionLimitNav") is not None:
        result["subscriptionLimitAmount"] = result["subscriptionLimitShares"] * result["subscriptionLimitNav"]
    return result


def enrich_payload(payload):
    history = pd.read_csv(ROOT / "data/nasdaq_qdii_live_premium.csv", dtype={"code": str})
    sz_sizes = {}
    try:
        response = requests.get("https://fund.szse.cn/api/report/ShowReport",
                                params={"SHOWTYPE": "xlsx", "CATALOGID": "1000_lf", "TABKEY": "tab1"},
                                headers={"Referer": "https://fund.szse.cn/marketdata/fundslist/index.html", "User-Agent": "Mozilla/5.0"}, timeout=20)
        response.raise_for_status()
        table = pd.read_excel(BytesIO(response.content), dtype={"基金代码": str})
        for _, row in table.iterrows():
            sz_sizes[str(row["基金代码"]).zfill(6)] = number(row.get("当前规模(份)", row.get("基金份额")))
    except Exception as error:
        print(f"深市场内份额暂不可用：{error}")
    def enrich(fund):
        rows = history[history.code.eq(fund["code"])]
        fund["historyStartDate"] = rows.date.min()
        fund["historyHighPrice"] = number(pd.to_numeric(rows.high, errors="coerce").max())
        fund["historyHighClose"] = number(pd.to_numeric(rows.close, errors="coerce").max())
        fund["historyHighNav"] = number(pd.to_numeric(rows.nav, errors="coerce").max())
        if fund["code"] == "161128":
            fund["operatingFee"] = 1.0
        try:
            fund.update(exchange_limit(fund["code"]))
        except Exception as error:
            print(f"{fund['code']} 交易所清单暂不可用：{error}")
        if sz_sizes.get(fund["code"]) is not None:
            fund.update(exchangeShares=sz_sizes[fund["code"]], scaleDate=pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d"))
        return fund
    with ThreadPoolExecutor(max_workers=6) as pool:
        payload["funds"] = list(pool.map(enrich, payload["funds"]))
    return payload


if __name__ == "__main__":
    path = ROOT / "docs/data/funds.json"
    payload = enrich_payload(json.loads(path.read_text()))
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    path.write_text(text, encoding="utf-8")
    (path.parent / "funds.js").write_text(f"window.NASDAQ_ETF_FUNDS = {text};\n", encoding="utf-8")
