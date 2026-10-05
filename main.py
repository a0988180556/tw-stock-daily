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
def build_report(df, inst, sectors):
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
    }
    f["pct"] = f["chg"] / float(prev["Close"]) * 100
    date_str = df.index[-1].date()

    lines = [f"📊 台股盤後報告 {date_str}", ""]
    lines.append("【大盤】")
    lines.append(f"加權指數 {f['close']:,.2f}")
    lines.append(f"漲跌 {f['chg']:+,.2f}({f['pct']:+.2f}%)")
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
    tg_text(build_report(df, inst, sectors))
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
