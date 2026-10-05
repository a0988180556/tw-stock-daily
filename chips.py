import os
import io
import re
import sys
import json
import time
import requests
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

plt.rcParams["font.sans-serif"] = ["WenQuanYi Micro Hei", "Noto Sans CJK TC", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

TZ = ZoneInfo("Asia/Taipei")
TOKEN = os.environ.get("TG_TOKEN")
CHAT_ID = os.environ.get("TG_CHAT_ID")
FORCE = os.environ.get("FORCE_SEND") == "true"
UA = {"User-Agent": "Mozilla/5.0"}
TOP_N = 20
HISTORY_DAYS = 4  # 另外往前抓 4 個交易日,合計 5 日算連續天數
RED, GREEN = "#d62728", "#2e8b57"  # 台股慣例:紅=買超/上漲,綠=賣超/下跌


def tg_text(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=30)
    r.raise_for_status()


def tg_album(bufs):
    media, files = [], {}
    for i, b in enumerate(bufs):
        b.seek(0)
        files[f"p{i}"] = (f"chips{i}.png", b, "image/png")
        media.append({"type": "photo", "media": f"attach://p{i}"})
    url = f"https://api.telegram.org/bot{TOKEN}/sendMediaGroup"
    r = requests.post(
        url,
        data={"chat_id": CHAT_ID, "media": json.dumps(media)},
        files=files,
        timeout=120,
    )
    r.raise_for_status()


def safe(fn, label):
    try:
        return fn()
    except Exception as e:
        print(f"[{label}] 失敗: {e}")
        return None


def strip_html(s):
    return re.sub(r"<[^>]*>", "", str(s)).strip()


def num(s):
    return float(strip_html(s).replace(",", ""))


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


def get_prices(ymd):
    """用指定日期的每日收盤行情,算出當日漲跌幅(%)。"""
    j = None
    for base in (
        "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
        "https://www.twse.com.tw/exchangeReport/MI_INDEX",
    ):
        try:
            r = requests.get(
                base,
                params={"response": "json", "date": ymd, "type": "ALLBUT0999"},
                headers=UA,
                timeout=60,
            )
            r.raise_for_status()
            j = r.json()
            break
        except Exception as e:
            print(f"[MI_INDEX {ymd}] {base} 失敗: {e}")
    if not j or j.get("stat") != "OK":
        raise RuntimeError(f"MI_INDEX stat={j.get('stat') if j else None}")

    candidates = [(t.get("fields", []), t.get("data", [])) for t in j.get("tables", [])]
    if j.get("fields9"):  # 舊版格式
        candidates.append((j["fields9"], j.get("data9", [])))

    for fields, rows in candidates:
        if "證券代號" in fields and "收盤價" in fields and "漲跌價差" in fields:
            i_code = fields.index("證券代號")
            i_close = fields.index("收盤價")
            i_chg = fields.index("漲跌價差")
            i_sign = next(i for i, f in enumerate(fields) if f.startswith("漲跌("))
            out = {}
            for r in rows:
                code = str(r[i_code]).strip()
                try:
                    close = num(r[i_close])
                    chg = num(r[i_chg])
                except ValueError:
                    continue
                s = strip_html(r[i_sign])
                sign = -1 if "-" in s else (1 if "+" in s else 0)
                change = sign * chg
                prev = close - change
                if prev > 0:
                    out[code] = change / prev * 100
            if out:
                return out
    raise RuntimeError("找不到個股收盤行情表")


def streak(code, key, sign, series):
    n = 0
    for d in series:
        v = d.get(code, {}).get(key)
        if v is None or v * sign <= 0:
            break
        n += 1
    return n


def build_rows(cur, key, sign, series, prices):
    rows = sorted(cur.items(), key=lambda kv: kv[1][key], reverse=sign > 0)[:TOP_N]
    out = []
    for n, (code, v) in enumerate(rows, 1):
        s = streak(code, key, sign, series)
        streak_txt = "-"
        if s >= 2:
            streak_txt = f"{s}天" + ("+" if s == len(series) else "")
        pct = prices.get(code)
        pct_txt = "-"
        if pct is not None:
            pct_txt = f"{pct:+.1f}%"
            if pct >= 9.5:
                pct_txt += " 漲停"
            elif pct <= -9.5:
                pct_txt += " 跌停"
        out.append([str(n), f"{v['name']} {code}", f"{v[key]:+,.0f}", streak_txt, pct_txt])
    return out


def table_image(title, rows, color, streak_label):
    fig, ax = plt.subplots(figsize=(7, 10))
    ax.axis("off")
    ax.set_title(title, fontsize=15, color=color, fontweight="bold", pad=14)
    tbl = ax.table(
        cellText=rows,
        colLabels=["#", "股票", "買賣超(張)", streak_label, "當日漲跌"],
        loc="upper center",
        cellLoc="center",
        colWidths=[0.08, 0.32, 0.22, 0.17, 0.21],
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(11)
    tbl.scale(1, 1.55)
    for (r, c), cell in tbl.get_celld().items():
        if r == 0:
            cell.set_facecolor(color)
            cell.get_text().set_color("white")
            cell.get_text().set_fontweight("bold")
            continue
        txt = cell.get_text().get_text()
        if c == 2:
            cell.get_text().set_color(color)
            cell.get_text().set_fontweight("bold")
        elif c == 4 and len(txt) > 1:
            cell.get_text().set_color(RED if txt.startswith("+") else GREEN)
        if r % 2 == 0:
            cell.set_facecolor("#f5f5f5")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)
    return buf


def fmt_list(title, rows):
    lines = [title]
    for r in rows:
        extra = "" if r[3] == "-" else f" 連續{r[3]}"
        extra += "" if r[4] == "-" else f" {r[4]}"
        lines.append(f"{r[0]}. {r[1]}  {r[2]}{extra}")
    return "\n".join(lines)


def main():
    today = datetime.now(TZ).date()
    day, cur = get_latest(today)
    if not cur:
        print("沒有個股籌碼資料(休市或尚未公布),不發送。")
        return
    series = [cur] + get_history(day, HISTORY_DAYS)

    time.sleep(2)
    prices = safe(lambda: get_prices(day.strftime("%Y%m%d")), "當日漲跌幅") or {}

    specs = [
        (f"投信買超 Top{TOP_N}  {day}", "it", +1, RED, "連續買超"),
        (f"投信賣超 Top{TOP_N}  {day}", "it", -1, GREEN, "連續賣超"),
        (f"外資買超 Top{TOP_N}  {day}", "fi", +1, RED, "連續買超"),
        (f"外資賣超 Top{TOP_N}  {day}", "fi", -1, GREEN, "連續賣超"),
    ]
    tables = [
        (title, build_rows(cur, key, sign, series, prices), color, label)
        for title, key, sign, color, label in specs
    ]

    images = [safe(lambda: table_image(*t), "表格圖") for t in tables]
    if all(images):
        tg_album(images)
    else:  # 圖片失敗時退回文字版(分兩則,避免超過長度限制)
        for pair in (tables[:2], tables[2:]):
            parts = [f"📈 個股籌碼 {day}(單位:張)", ""]
            for title, rows, _c, _l in pair:
                parts.append(fmt_list(f"【{title}】", rows))
                parts.append("")
            tg_text("\n".join(parts))

    tg_text(
        "※ 外資指外陸資(不含外資自營商)。\n"
        "※ 連續天數以近 5 個交易日計,「5天+」代表至少連續 5 天。\n"
        "※ 當日漲跌為當天收盤價相對前一交易日。\n"
        "※ 籌碼資料僅供參考,非投資建議。"
    )
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
