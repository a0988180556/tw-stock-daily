import os
import re
import io
import sys
import requests
import pandas as pd
import yfinance as yf
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from datetime import datetime
from zoneinfo import ZoneInfo

plt.rcParams["font.sans-serif"] = ["WenQuanYi Micro Hei", "Noto Sans CJK TC", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

TZ = ZoneInfo("Asia/Taipei")
TOKEN = os.environ.get("TG_TOKEN")
CHAT_ID = os.environ.get("TG_CHAT_ID")
FORCE = os.environ.get("FORCE_SEND") == "true"
UA = {"User-Agent": "Mozilla/5.0"}


# ---------- Telegram ----------
def tg_text(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=30)
    r.raise_for_status()


def tg_photo(buf):
    buf.seek(0)
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    r = requests.post(
        url,
        data={"chat_id": CHAT_ID},
        files={"photo": ("chart.png", buf, "image/png")},
        timeout=60,
    )
    r.raise_for_status()


# ---------- 工具 ----------
def strip_html(s):
    return re.sub(r"<[^>]*>", "", str(s)).strip()


def num(s):
    return float(strip_html(s).replace(",", ""))


def safe(fn, label):
    try:
        return fn()
    except Exception as e:
        print(f"[{label}] 取得失敗: {e}")
        return None


def get_json(url, params):
    r = requests.get(url, params=params, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()


# ---------- 1. 指數與技術指標 ----------
def get_index():
    df = yf.download("^TWII", period="1y", progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna()


def add_indicators(df):
    df = df.copy()
    for n in (5, 20, 60):
        df[f"MA{n}"] = df["Close"].rolling(n).mean()

    delta = df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
    rs = avg_gain / avg_loss
    df["RSI"] = 100 - 100 / (1 + rs)

    low9 = df["Low"].rolling(9).min()
    high9 = df["High"].rolling(9).max()
    rsv = (df["Close"] - low9) / (high9 - low9) * 100
    k, d = 50.0, 50.0
    ks, ds = [], []
    for v in rsv:
        if pd.isna(v):
            ks.append(float("nan"))
            ds.append(float("nan"))
            continue
        k = k * 2 / 3 + v / 3
        d = d * 2 / 3 + k / 3
        ks.append(k)
        ds.append(d)
    df["K"] = ks
    df["D"] = ds
    return df


# ---------- 2. 三大法人 ----------
def get_institutional(ymd):
    url = "https://www.twse.com.tw/rwd/zh/fund/BFI82U"
    j = get_json(url, {"response": "json", "dayDate": ymd, "date": ymd, "type": "day"})
    if j.get("stat") != "OK":
        raise RuntimeError(f"stat={j.get('stat')}")
    rows = j["data"]
    scale = 1e8 if num(rows[0][1]) > 1e7 else 1.0
    out = {"外資": 0.0, "投信": 0.0, "自營商": 0.0, "合計": 0.0}
    for r in rows:
        name = re.sub(r"\s", "", strip_html(r[0]))
        diff = num(r[3]) / scale
        if name.startswith("外資"):
            out["外資"] += diff
        elif name.startswith("投信"):
            out["投信"] += diff
        elif name.startswith("自營商"):
            out["自營商"] += diff
        elif name.startswith("合計"):
            out["合計"] = diff
    return out


# ---------- 3. 類股強弱 ----------
def get_sectors(ymd):
    url = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
    j = get_json(url, {"date": ymd, "type": "IND", "response": "json"})
    if j.get("stat") != "OK":
        raise RuntimeError(f"stat={j.get('stat')}")
    rows = []
    for t in j.get("tables", []):
        title = t.get("title", "")
        if "價格指數" not in title or "報酬" in title:
            continue
        fields = t.get("fields", [])
        i_sign = next(i for i, f in enumerate(fields) if "漲跌(" in f)
        i_pct = next(i for i, f in enumerate(fields) if "百分比" in f)
        for r in t["data"]:
            name = strip_html(r[0])
            if not name.endswith("類指數") or "報酬" in name:
                continue
            try:
                pct = num(r[i_pct])
            except ValueError:
                continue
            if "-" in strip_html(r[i_sign]):
                pct = -abs(pct)
            rows.append((name.replace("類指數", ""), pct))
    if not rows:
        raise RuntimeError("找不到類股資料")
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows


# ---------- 3b. 上漲、下跌家數(上市 + 上櫃) ----------
def get_breadth(ymd):
    """上市:取證交所每日收盤行情中的「漲跌證券數」(股票欄)。"""
    j = None
    for base in (
        "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
        "https://www.twse.com.tw/exchangeReport/MI_INDEX",
    ):
        try:
            j = get_json(base, {"response": "json", "date": ymd, "type": "ALLBUT0999"})
            break
        except Exception as e:
            print(f"[MI_INDEX {ymd}] {base} 失敗: {e}")
    if not j or j.get("stat") != "OK":
        raise RuntimeError(f"MI_INDEX stat={j.get('stat') if j else None}")

    candidates = [(t.get("fields", []), t.get("data", [])) for t in j.get("tables", [])]
    for k, v in j.items():  # 舊版格式:dataN / fieldsN
        if k.startswith("data") and k[4:].isdigit():
            candidates.append((j.get("fields" + k[4:], []), v))

    for fields, rows in candidates:
        if not rows or not isinstance(rows[0], list):
            continue
        if not any(strip_html(r[0]).startswith("上漲") for r in rows):
            continue
        col = next((i for i, f in enumerate(fields) if "股票" in f), len(fields) - 1)
        res = {}
        for r in rows:
            name = strip_html(r[0])
            cell = strip_html(r[col]).replace(" ", "")
            m = re.match(r"([\d,]+)(?:\((\d+)\))?", cell)
            if not m:
                continue
            n = int(m.group(1).replace(",", ""))
            lim = int(m.group(2)) if m.group(2) else 0
            if name.startswith("上漲"):
                res["up"], res["up_limit"] = n, lim
            elif name.startswith("下跌"):
                res["down"], res["down_limit"] = n, lim
            elif name.startswith("持平"):
                res["flat"] = n
        if "up" in res and "down" in res:
            res.setdefault("flat", 0)
            res.setdefault("up_limit", 0)
            res.setdefault("down_limit", 0)
            return res
    raise RuntimeError("找不到漲跌證券數表")


def get_tpex_breadth(day):
    """上櫃:由櫃買中心個股收盤行情逐檔計算(只計 4 位數代號的一般股票)。"""
    attempts = [
        (
            "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc",
            {"date": day.strftime("%Y/%m/%d"), "type": "EW", "response": "json"},
        ),
        (  # 舊版網址,當備援
            "https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php",
            {"l": "zh-tw", "d": f"{day.year - 1911}/{day.month:02d}/{day.day:02d}", "se": "EW"},
        ),
    ]
    rows, cols = None, None
    for url, params in attempts:
        try:
            j = get_json(url, params)
        except Exception as e:
            print(f"[櫃買 {url}] 失敗: {e}")
            continue
        if j.get("tables"):
            t = j["tables"][0]
            fields = [str(f).strip() for f in t.get("fields", [])]
            if "代號" in fields and t.get("data"):
                rows = t["data"]
                cols = (
                    fields.index("代號"),
                    next(i for i, f in enumerate(fields) if f.startswith("收盤")),
                    next(i for i, f in enumerate(fields) if f.startswith("漲跌")),
                )
                break
        elif j.get("aaData"):
            rows, cols = j["aaData"], (0, 2, 3)
            break
    if not rows:
        raise RuntimeError("櫃買中心沒有資料(休市或尚未公布)")

    i_code, i_close, i_chg = cols
    res = {"up": 0, "down": 0, "flat": 0, "up_limit": 0, "down_limit": 0}
    for r in rows:
        code = str(r[i_code]).strip()
        if not (len(code) == 4 and code.isdigit()):
            continue
        try:
            close = num(r[i_close])
            chg = num(str(r[i_chg]).replace("+", ""))
        except ValueError:
            continue
        pct = chg / (close - chg) * 100 if close - chg > 0 else 0
        if chg > 0:
            res["up"] += 1
            if pct >= 9.5:
                res["up_limit"] += 1
        elif chg < 0:
            res["down"] += 1
            if pct <= -9.5:
                res["down_limit"] += 1
        else:
            res["flat"] += 1
    if res["up"] + res["down"] + res["flat"] < 300:
        raise RuntimeError("上櫃檔數異常偏少,可能欄位解析有誤")
    return res


def get_breadth_all(ymd):
    day = datetime.strptime(ymd, "%Y%m%d").date()
    twse = get_breadth(ymd)
    otc = safe(lambda: get_tpex_breadth(day), "上櫃家數")
    keys = ("up", "down", "flat", "up_limit", "down_limit")
    total = {k: twse[k] + (otc[k] if otc else 0) for k in keys}
    total["twse"], total["otc"] = twse, otc
    return total


def fmt_breadth_line(label, b):
    return (
        f"{label} 上漲 {b['up']}(漲停 {b['up_limit']})|"
        f"下跌 {b['down']}(跌停 {b['down_limit']})|持平 {b['flat']}"
    )


def breadth_text(b, index_chg):
    up, dn = b["up"], b["down"]
    if up > dn * 1.5:
        s = "上漲家數明顯多於下跌家數,盤面普遍偏多。"
    elif dn > up * 1.5:
        s = "下跌家數明顯多於上漲家數,盤面普遍偏空。"
    else:
        s = "漲跌家數相近,個股漲跌互見。"
    if index_chg > 0 and dn > up:
        s += "指數上漲但多數個股下跌,漲勢集中在少數權值股。"
    elif index_chg < 0 and up > dn:
        s += "指數下跌但多數個股上漲,跌勢集中在少數權值股。"
    return s


# ---------- 4. 圖表 ----------
def make_chart(df):
    d = df.tail(60)
    fig, axes = plt.subplots(
        3, 1, figsize=(9, 9), sharex=True,
        gridspec_kw={"height_ratios": [3, 1, 1]},
    )
    ax = axes[0]
    ax.plot(d.index, d["Close"], label="加權指數", color="black", linewidth=1.6)
    ax.plot(d.index, d["MA5"], label="近1週平均", linewidth=1)
    ax.plot(d.index, d["MA20"], label="近1個月平均", linewidth=1)
    ax.plot(d.index, d["MA60"], label="近1季平均", linewidth=1)
    ax.set_title(f"加權指數走勢  {d.index[-1].date()}")
    ax.legend(loc="upper left", ncol=4, fontsize=9)
    ax.grid(alpha=0.3)

    axes[1].plot(d.index, d["RSI"], color="purple")
    axes[1].axhline(70, color="red", linestyle="--", linewidth=0.8)
    axes[1].axhline(30, color="green", linestyle="--", linewidth=0.8)
    axes[1].set_ylabel("買氣熱度(RSI)")
    axes[1].set_ylim(0, 100)
    axes[1].grid(alpha=0.3)

    axes[2].plot(d.index, d["K"], label="K")
    axes[2].plot(d.index, d["D"], label="D")
    axes[2].axhline(80, color="red", linestyle="--", linewidth=0.8)
    axes[2].axhline(20, color="green", linestyle="--", linewidth=0.8)
    axes[2].set_ylabel("短線動能(KD)")
    axes[2].set_ylim(0, 100)
    axes[2].legend(loc="upper left", fontsize=9)
    axes[2].grid(alpha=0.3)

    fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return buf


# ---------- 5. 口語化技術面 + 總結 ----------
def trend_text(f):
    c = f["close"]
    pos = [c > f["ma5"], c > f["ma20"], c > f["ma60"]]
    if all(pos) and f["ma5"] > f["ma20"] > f["ma60"]:
        return "指數站在近一週、近一個月、近一季的平均價之上,而且短期平均高於長期平均,整體走勢偏強。"
    if all(pos):
        return "指數站在近一週、近一個月、近一季的平均價之上,走勢偏強。"
    if not any(pos):
        return "指數落在近一週、近一個月、近一季的平均價之下,走勢偏弱。"
    parts = [
        f"{'站上' if pos[0] else '跌破'}近一週平均價",
        f"{'站上' if pos[1] else '跌破'}近一個月平均價",
        f"{'站上' if pos[2] else '跌破'}近一季平均價",
    ]
    return "多空訊號不一:" + "、".join(parts) + ",目前偏向整理。"


def rsi_text(f):
    r = f["rsi"]
    if r >= 70:
        return f"短線買氣偏熱(買氣指標 RSI {r:.0f}),漲多之後要留意拉回整理。"
    if r <= 30:
        return f"短線賣壓偏重(買氣指標 RSI {r:.0f}),已有超跌跡象,留意是否止穩。"
    return f"短線買氣中性(買氣指標 RSI {r:.0f})。"


def kd_text(f):
    k, d = f["k"], f["d"]
    if k >= 80:
        return f"短線動能指標(KD)在高檔(K {k:.0f}、D {d:.0f}),動能強但高檔容易震盪。"
    if k <= 20:
        return f"短線動能指標(KD)在低檔(K {k:.0f}、D {d:.0f}),短線偏弱,留意是否止跌。"
    if k > d:
        return f"短線動能指標(KD)偏多(K {k:.0f} 高於 D {d:.0f})。"
    return f"短線動能指標(KD)偏空(K {k:.0f} 低於 D {d:.0f})。"


def tech_lines(f):
    return [
        trend_text(f),
        f"參考:近一週平均約 {f['ma5']:,.0f} 點、近一個月約 {f['ma20']:,.0f} 點、近一季約 {f['ma60']:,.0f} 點。",
        rsi_text(f),
        kd_text(f),
    ]


def template_summary(f):
    p = []
    direction = "上漲" if f["chg"] > 0 else ("下跌" if f["chg"] < 0 else "持平")
    p.append(f"加權指數收{f['close']:,.0f}點,{direction}{abs(f['chg']):,.0f}點({f['pct']:+.2f}%)。")
    if f.get("breadth"):
        b = f["breadth"]
        scope = "上市櫃" if b["otc"] else "上市"
        p.append(f"{scope}股票上漲{b['up']}家、下跌{b['down']}家。" + breadth_text(b, f["chg"]))
    p.append(trend_text(f))
    p.append(rsi_text(f))
    p.append(kd_text(f))
    if f.get("inst"):
        fr = f["inst"]["外資"]
        p.append(f"外資{'買超' if fr > 0 else '賣超'}{abs(fr):,.0f}億元。")
    if f.get("sectors"):
        p.append(f"類股以{f['sectors'][0][0]}表現最強,{f['sectors'][-1][0]}最弱。")
    p.append(f"隔日留意指數能否守住近一週平均價約 {f['ma5']:,.0f} 點,以及近一個月平均價約 {f['ma20']:,.0f} 點。")
    return "".join(p)


def llm_summary(facts):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    prompt = (
        "你是台股盤後分析助理。請只根據下列數據,用繁體中文寫約180字的大盤總結,"
        "語氣口語、讓一般投資人看得懂;不要出現 5MA、20MA、60MA、均線這類縮寫或術語,"
        "技術面請用「近一週、近一個月、近一季的平均價」來描述。"
        "如果有上漲、下跌家數,請一併說明盤面是普遍上漲還是少數權值股帶動。"
        "最後一句寫隔日觀察重點。不可編造數據中沒有的資訊,不要給買賣建議。\n\n" + facts
    )
    r = requests.post(
        url,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


# ---------- 組報告 ----------
def build_report(df, inst, sectors, breadth):
    last, prev = df.iloc[-1], df.iloc[-2]
    f = {
        "close": float(last["Close"]),
        "chg": float(last["Close"] - prev["Close"]),
        "ma5": float(last["MA5"]),
        "ma20": float(last["MA20"]),
        "ma60": float(last["MA60"]),
        "rsi": float(last["RSI"]),
        "k": float(last["K"]),
        "d": float(last["D"]),
        "inst": inst,
        "sectors": sectors,
        "breadth": breadth,
    }
    f["pct"] = f["chg"] / float(prev["Close"]) * 100
    date_str = df.index[-1].date()

    lines = [f"📊 台股盤後報告 {date_str}", ""]
    lines.append("【大盤】")
    lines.append(f"加權指數 {f['close']:,.2f}")
    lines.append(f"漲跌 {f['chg']:+,.2f}({f['pct']:+.2f}%)")
    lines.append("")

    lines.append("【漲跌家數】")
    if breadth:
        lines.append(fmt_breadth_line("上市", breadth["twse"]))
        if breadth["otc"]:
            lines.append(fmt_breadth_line("上櫃", breadth["otc"]))
            lines.append(fmt_breadth_line("合計", breadth))
        else:
            lines.append("上櫃:資料取得失敗(以下判讀僅依上市)")
        lines.append(breadth_text(breadth, f["chg"]))
    else:
        lines.append("(資料取得失敗)")
    lines.append("")

    lines.append("【三大法人買賣超(億元)】")
    if inst:
        lines.append(f"外資 {inst['外資']:+,.1f}")
        lines.append(f"投信 {inst['投信']:+,.1f}")
        lines.append(f"自營商 {inst['自營商']:+,.1f}")
        lines.append(f"合計 {inst['合計']:+,.1f}")
    else:
        lines.append("(資料取得失敗)")
    lines.append("")

    lines.append("【類股強弱】")
    if sectors:
        top, bottom = sectors[:5], sectors[-5:][::-1]
        lines.append("強:" + "、".join(f"{n}{p:+.2f}%" for n, p in top))
        lines.append("弱:" + "、".join(f"{n}{p:+.2f}%" for n, p in bottom))
    else:
        lines.append("(資料取得失敗)")
    lines.append("")

    lines.append("【技術面】")
    lines.extend(tech_lines(f))
    lines.append("")

    facts = "\n".join(lines)
    summary = safe(lambda: llm_summary(facts), "LLM") or template_summary(f)
    lines.append("【總結】")
    lines.append(summary)
    lines.append("")
    lines.append("※ 資料僅供參考,非投資建議")
    return "\n".join(lines)


def main():
    df = add_indicators(get_index())
    today = datetime.now(TZ).date()
    if df.index[-1].date() != today and not FORCE:
        print(f"今日({today})無新資料,可能休市,不發報告。")
        return
    ymd = df.index[-1].strftime("%Y%m%d")
    inst = safe(lambda: get_institutional(ymd), "三大法人")
    sectors = safe(lambda: get_sectors(ymd), "類股")
    breadth = safe(lambda: get_breadth_all(ymd), "漲跌家數")
    tg_text(build_report(df, inst, sectors, breadth))
    chart = safe(lambda: make_chart(df), "圖表")
    if chart:
        tg_photo(chart)
    print("已發送")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("執行失敗:", e)
        try:
            tg_text(f"⚠️ 台股報告今日執行失敗:{e}")
        except Exception:
            pass
        sys.exit(1)
