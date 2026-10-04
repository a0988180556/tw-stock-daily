import os
import re
import sys
import json
import html
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")
TOKEN = os.environ.get("TG_TOKEN")
CHAT_ID = os.environ.get("TG_CHAT_ID")
UA = {"User-Agent": "Mozilla/5.0"}

QUERIES = {
    "台股": ["台股", "加權指數 外資", "台積電", "台灣 央行 經濟"],
    "美股": ["美股 收盤", "那斯達克 道瓊", "聯準會 美股", "費半 輝達"],
}

LISTED_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"

# 簡稱只有兩個字的公司容易誤判,只放行這些常見的(可自行增減)
TWO_CHAR_OK = {
    "鴻海", "聯電", "華碩", "宏碁", "緯創", "廣達", "技嘉", "友達", "群創",
    "長榮", "陽明", "萬海", "中鋼", "台塑", "南亞", "研華", "華城", "緯穎",
    "仁寶", "英業達", "瑞昱", "旺宏", "華邦電",
}

# 公司清單抓不到時的備援
FALLBACK = {
    "台積電": "2330", "鴻海": "2317", "聯發科": "2454", "廣達": "2382",
    "聯電": "2303", "台達電": "2308", "中華電": "2412", "富邦金": "2881",
    "國泰金": "2882", "長榮": "2603", "緯創": "3231", "技嘉": "2376",
    "華碩": "2357", "日月光投控": "3711", "大立光": "3008",
}


def tg_text(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    r = requests.post(
        url,
        data={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": "true"},
        timeout=30,
    )
    r.raise_for_status()


def safe(fn, label):
    try:
        return fn()
    except Exception as e:
        print(f"[{label}] 失敗: {e}")
        return None


def fetch(query, days):
    q = quote(f"{query} when:{days}d")
    url = f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = []
    for it in root.iter("item"):
        title = html.unescape((it.findtext("title") or "").strip())
        link = (it.findtext("link") or "").strip()
        src = (it.findtext("source") or "").strip()
        try:
            t = parsedate_to_datetime(it.findtext("pubDate")).astimezone(TZ)
        except Exception:
            t = None
        if title and link:
            items.append({"title": title, "link": link, "time": t, "source": src})
    return items


def clean_title(title, source):
    suffix = f" - {source}"
    if source and title.endswith(suffix):
        return title[: -len(suffix)]
    return title


def collect(queries, since, days, limit=30):
    feeds = []
    for q in queries:
        res = safe(lambda: fetch(q, days), f"抓取 {q}")
        if res:
            feeds.append(res)
    seen, out = set(), []
    longest = max((len(f) for f in feeds), default=0)
    for i in range(longest):
        for f in feeds:
            if i >= len(f):
                continue
            it = f[i]
            if it["time"] and it["time"] < since:
                continue
            title = clean_title(it["title"], it["source"])
            key = re.sub(r"\W", "", title)[:14]
            if key in seen:
                continue
            seen.add(key)
            it["title"] = title
            out.append(it)
            if len(out) >= limit:
                return out
    return out


def gemini(prompt):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    r = requests.post(
        url,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def pick_with_llm(group, items):
    if len(items) <= 5:
        return None
    listing = "\n".join(
        f"{i}. {it['title']}({it['source']})" for i, it in enumerate(items)
    )
    text = gemini(
        f"以下是{group}相關的新聞標題。請挑出對今天台股開盤前最重要、"
        "且彼此不是同一事件的5則,依重要性排序。只能根據標題,不可編造內容。"
        '只輸出JSON陣列,格式:[{"id":編號,"summary":"30字內繁體中文重點"}]\n\n'
        + listing
    )
    if not text:
        return None
    arr = json.loads(re.search(r"\[.*\]", text, re.S).group(0))
    chosen = []
    for a in arr:
        i = int(a["id"])
        if 0 <= i < len(items):
            chosen.append((items[i], str(a.get("summary", "")).strip()))
    return chosen[:5] or None


def fmt_group(title, picks):
    lines = [title]
    if not picks:
        lines.append("(目前沒有抓到新聞)")
    for n, (it, s) in enumerate(picks, 1):
        t = it["time"].strftime("%m/%d %H:%M") if it["time"] else ""
        lines.append(f"{n}. {it['title']}")
        if s:
            lines.append(f"   ▸ {s}")
        lines.append(f"   {it['source']} {t}".rstrip())
        lines.append(f"   {it['link']}")
        lines.append("")
    return "\n".join(lines)


# ---------- 關注個股(依新聞提及次數) ----------
def load_names():
    def get():
        r = requests.get(LISTED_URL, headers=UA, timeout=30)
        r.raise_for_status()
        return r.json()

    data = safe(get, "上市公司清單")
    names = {}
    for d in data or []:
        short = re.sub(r"[-*]?KY$", "", str(d.get("公司簡稱", "")).strip())
        code = str(d.get("公司代號", "")).strip()
        if code and short and (len(short) >= 3 or short in TWO_CHAR_OK):
            names[short] = code
    return names or dict(FALLBACK)


def build_watchlist(items):
    names = load_names()
    hits = {}
    for it in items:
        for nm, code in names.items():
            if nm in it["title"]:
                h = hits.setdefault(nm, {"code": code, "count": 0, "items": []})
                h["count"] += 1
                h["items"].append(it)
    ranked = sorted(hits.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
    return ranked[:5]


def explain_watchlist(ranked):
    blocks = "\n\n".join(
        f"{nm}({v['code']}):\n" + "\n".join(f"- {i['title']}" for i in v["items"][:4])
        for nm, v in ranked
    )
    text = gemini(
        "以下是各檔台股出現在今早新聞標題的相關報導。請為每檔用繁體中文寫一句"
        "20字內的『消息重點』,只能根據標題,不可編造,"
        "不可出現買進、賣出、看好、看壞、目標價等字眼。"
        '只輸出JSON物件,格式:{"股票名稱":"重點"}\n\n' + blocks
    )
    if not text:
        return {}
    return json.loads(re.search(r"\{.*\}", text, re.S).group(0))


def fmt_watch(ranked, notes):
    lines = ["🔎 新聞熱度關注清單(依今早新聞提及次數)"]
    if not ranked:
        lines.append("(今天的新聞中沒有明顯被提及的個股)")
    for n, (nm, v) in enumerate(ranked, 1):
        lines.append(f"{n}. {nm} {v['code']}|提及 {v['count']} 則")
        if notes.get(nm):
            lines.append(f"   ▸ {notes[nm]}")
        it = v["items"][0]
        lines.append(f"   {it['title']}")
        lines.append(f"   {it['link']}")
        lines.append("")
    lines.append("※ 僅依新聞提及次數整理,不代表推薦或買賣建議;")
    lines.append("   被報導可能是利多也可能是利空,請點進新聞自行判斷。")
    return "\n".join(lines)


def main():
    now = datetime.now(TZ)
    days = 3 if now.weekday() == 0 else 1
    since = now - timedelta(days=days)
    wd = "一二三四五六日"[now.weekday()]
    tg_text(f"🌅 盤前新聞 {now:%Y-%m-%d}(週{wd})")

    pool = []
    for group, qs in QUERIES.items():
        items = collect(qs, since, days)
        pool += items
        picks = safe(lambda: pick_with_llm(group, items), f"LLM {group}")
        if not picks:
            picks = [(it, "") for it in items[:5]]
        flag = "🇹🇼" if group == "台股" else "🇺🇸"
        tg_text(fmt_group(f"{flag} {group}重要新聞", picks))

    ranked = safe(lambda: build_watchlist(pool), "關注清單") or []
    notes = safe(lambda: explain_watchlist(ranked), "關注清單重點") if ranked else {}
    tg_text(fmt_watch(ranked, notes or {}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("執行失敗:", e)
        try:
            tg_text(f"⚠️ 盤前新聞今日執行失敗:{e}")
        except Exception:
            pass
        sys.exit(1)
