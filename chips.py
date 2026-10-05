import os
import re
import sys
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")
TOKEN = os.environ.get("TG_TOKEN")
CHAT_ID = os.environ.get("TG_CHAT_ID")
FORCE = os.environ.get("FORCE_SEND") == "true"
UA = {"User-Agent": "Mozilla/5.0"}
TOP_N = 10
HISTORY_DAYS = 4  # 另外往前抓 4 個交易日,合計 5 日算連買連賣


def tg_text(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=30)
    r.raise_for_status()


def safe(fn, label):
    try:
        return fn()
    except Exception as e:
        print(f"[{label}] 失敗: {e}")
        return None


def num(s):
    return float(re.sub(r"<[^>]*>", "", str(s)).replace(",", "").strip())


def fetch_t86(ymd):
    j = None
    for base in (
        "https://www.twse.com.tw/rwd/zh/fund/T86",
        "https://www.twse.com.tw/fund/T86",
    ):
        try:
            r = requests.get(
                base,
                params={"response": "json", "date": ymd, "selectType": "ALLBUT0999"},
                headers=UA,
                timeout=30,
            )
            r.raise_for_status()
            j = r.json()
            break
        except Exception as e:
            print(f"[T86 {ymd}] {base} 失敗: {e}")
    if not j or j.get("stat") != "OK":
        return None
    fields, rows = j["fields"], j["data"]
    i_code = next(i for i, f in enumerate(fields) if "證券代號" in f)
    i_name = next(i for i, f in enumerate(fields) if "證券名稱" in f)
    i_fi = next(
        i for i, f in enumerate(fields)
        if f.startswith(("外陸資買賣超", "外資及陸資買賣超", "外資買賣超"))
    )
    i_it = next(i for i, f in enumerate(fields) if f.startswith("投信買賣超"))
    out = {}
    for r in rows:
        code = str(r[i_code]).strip()
        if not (len(code) == 4 and code.isdigit()):  # 只留一般個股,排除 ETF
            continue
        try:
            out[code] = {
                "name": str(r[i_name]).strip(),
                "fi": num(r[i_fi]) / 1000,
                "it": num(r[i_it]) / 1000,
            }
        except ValueError:
            continue
    return out or None


def get_latest(today):
    ymd = today.strftime("%Y%m%d")
    attempts = 1 if FORCE else 3
    for a in range(attempts):
        d = fetch_t86(ymd)
        if d:
            return today, d
        if a < attempts - 1:
            print("尚未公布或休市,5 分鐘後重試")
            time.sleep(300)
    if FORCE:  # 測試模式:往前找最近一個有資料的日子
        for back in range(1, 8):
            day = today - timedelta(days=back)
            time.sleep(2)
            d = fetch_t86(day.strftime("%Y%m%d"))
            if d:
                return day, d
    return None, None


def get_history(day, n):
    hist, cur = [], day
    for _ in range(14):
        if len(hist) >= n:
            break
        cur -= timedelta(days=1)
        if cur.weekday() >= 5:
            continue
        time.sleep(2)  # 證交所會限制請求頻率
        d = fetch_t86(cur.strftime("%Y%m%d"))
        if d:
            hist.append(d)
    return hist


def get_prices():
    r = requests.get(
        "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
        headers=UA,
        timeout=30,
    )
    r.raise_for_status()
    out = {}
    for d in r.json():
        try:
            close = float(str(d["ClosingPrice"]).replace(",", ""))
            chg = float(str(d["Change"]).replace(",", "").replace("+", ""))
            prev = close - chg
            if prev > 0:
                out[d["Code"]] = chg / prev * 100
        except (KeyError, ValueError):
            continue
    return out


def streak(code, key, sign, series):
    n = 0
    for d in series:
        v = d.get(code, {}).get(key)
        if v is None or v * sign <= 0:
            break
        n += 1
    return n


def fmt_list(title, cur, key, sign, series, prices):
    rows = sorted(cur.items(), key=lambda kv: kv[1][key], reverse=sign > 0)[:TOP_N]
    lines = [title]
    for n, (code, v) in enumerate(rows, 1):
        tag = ""
        s = streak(code, key, sign, series)
        if s >= 2:
            plus = "+" if s == len(series) else ""
            tag += f" 連{'買' if sign > 0 else '賣'}{s}{plus}天"
        pct = prices.get(code)
        if pct is not None:
            tag += f" {pct:+.1f}%"
            if pct >= 9.5:
                tag += "🔺漲停"
            elif pct <= -9.5:
                tag += "🔻跌停"
        lines.append(f"{n}. {v['name']} {code}  {v[key]:+,.0f}{tag}")
    return "\n".join(lines)


def main():
    today = datetime.now(TZ).date()
    day, cur = get_latest(today)
    if not cur:
        print("沒有個股籌碼資料(休市或尚未公布),不發送。")
        return
    series = [cur] + get_history(day, HISTORY_DAYS)
    prices = (safe(get_prices, "股價") or {}) if day == today else {}

    parts = [f"📈 個股籌碼 {day}(單位:張)", ""]
    parts.append(fmt_list("【投信買超 Top10】", cur, "it", +1, series, prices))
    parts.append("")
    parts.append(fmt_list("【外資買超 Top10】", cur, "fi", +1, series, prices))
    parts.append("")
    parts.append(fmt_list("【外資賣超 Top10】", cur, "fi", -1, series, prices))
    parts.append("")
    parts.append("※ 外資指外陸資(不含外資自營商);連買/連賣以近 5 個交易日計。")
    parts.append("※ 籌碼資料僅供參考,非投資建議。")
    tg_text("\n".join(parts))
    print("已發送")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("執行失敗:", e)
        try:
            tg_text(f"⚠️ 個股籌碼今日執行失敗:{e}")
        except Exception:
            pass
        sys.exit(1)
