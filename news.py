import os
import re
import sys
import json
import html
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlparse
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


# ---------- 新聞來源過濾:排除社群、論壇、部落格、比價網站等非正式新聞 ----------
# 來源名稱含下列文字就排除(可自行增減)
BLOCKED_NAMES = (
    "股市同學會", "同學會", "比價王", "實價登錄", "痞客邦", "方格子", "部落格",
    "批踢踢", "巴哈姆特", "facebook", "instagram", "threads", "youtube",
    "dcard", "mobile01", "ptt", "medium", "tiktok", "reddit",
)
# 來源網域符合下列就排除(可自行增減)
BLOCKED_DOMAINS = (
    "facebook.com", "instagram.com", "threads.net", "threads.com",
    "youtube.com", "youtu.be", "x.com", "twitter.com", "ptt.cc", "dcard.tw",
    "mobile01.com", "pixnet.net", "vocus.cc", "medium.com", "blogspot.com",
    "wordpress.com", "tiktok.com", "reddit.com", "5168.com.tw",
)


def _host(text):
    text = (text or "").strip().lower()
    host = urlparse(text).netloc if "://" in text else text
    host = host.split("/")[0]
    return host[4:] if host.startswith("www.") else host


def is_blocked_source(name, url=""):
    n = (name or "").lower()
    if any(b.lower() in n for b in BLOCKED_NAMES):
        return True
    for h in (_host(url), _host(name)):
        if h and any(h == d or h.endswith("." + d) for d in BLOCKED_DOMAINS):
            return True
    return False


# ---------- 短網址:純文字網址,Telegram 可點,複製到 LINE 也會自動變連結 ----------
_SHORT_CACHE = {}


def _try_shorten(api, params):
    try:
        r = requests.get(api, params=params, headers=UA, timeout=10)
        text = r.text.strip()
        if r.ok and text.startswith("http") and " " not in text:
            return text
    except Exception:
        pass
    return None


def short_url(url):
    if url in _SHORT_CACHE:
        return _SHORT_CACHE[url]
    out = (
        _try_shorten("https://is.gd/create.php", {"format": "simple", "url": url})
        or _try_shorten("https://tinyurl.com/api-create.php", {"url": url})
        or url  # 兩個服務都失敗時,退回原網址
    )
    _SHORT_CACHE[url] = out
    return out


# ---------- 相似主題去重 ----------
STOP_TERMS = (
    "台股", "美股", "大盤", "收盤", "盤前", "盤後", "指數", "今日", "今天",
    "最新", "快訊", "新聞",
)
SIMILAR_OVERLAP = 0.3  # 標題用字重疊比例超過這個值,視為同一主題


def _grams(title):
    t = title.lower()
    for w in STOP_TERMS:
        t = t.replace(w, "")
    t = re.sub(r"[\W_]+", "", t)
    return {t[i : i + 2] for i in range(len(t) - 1)}


def _big_numbers(title):
    # 例如「5萬」「719億」「1.89兆」,同樣在講某個點位或金額的新聞通常是同一主題
    return {a + b for a, b in re.findall(r"(\d+(?:\.\d+)?)([萬億兆])", title)}


def too_similar(a, b):
    ga, gb = _grams(a), _grams(b)
    if ga and gb and len(ga & gb) / min(len(ga), len(gb)) >= SIMILAR_OVERLAP:
        return True
    return bool(_big_numbers(a) & _big_numbers(b))


def diversify(picks, pool, n=5):
    """picks: [(item, 摘要)],依重要性排序。移除主題相似者,不足 n 則從 pool 依序補上。"""
    out = []

    def ok(it):
        return all(not too_similar(it["title"], o["title"]) for o, _ in out)

    for it, summary in picks:
        if ok(it):
            out.append((it, summary))
        if len(out) >= n:
            return out
    used = {id(o) for o, _ in out}
    for it in pool:
        if id(it) not in used and ok(it):
            out.append((it, ""))
        if len(out) >= n:
            break
    return out


def tg_text(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    r = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
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
        src_el = it.find("source")
        src = (src_el.text or "").strip() if src_el is not None else ""
        src_url = src_el.get("url", "") if src_el is not None else ""
        if is_blocked_source(src, src_url):
            continue
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
        f"以下是{group}相關的新聞標題。請挑出對今天台股開盤前最重要的5則,依重要性排序。"
        "5則必須是5個不同主題:同樣在講某個指數點位(例如台股逼近5萬點)、"
        "同一檔個股的同一消息、同一場會議或同一份財報,都算同一主題,只能選其中一則;"
        "盡量涵蓋大盤、個股、總經、國際等不同面向。只能根據標題,不可編造內容。"
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


def url_line(it):
    """縮短後的純文字網址(Telegram 可點,複製到 LINE 也會自動變連結)"""
    return html.escape(short_url(it["link"]))


def fmt_group(title, picks):
    lines = [f"<b>{html.escape(title)}</b>"]
    if not picks:
        lines.append("(目前沒有抓到新聞)")
    for n, (it, s) in enumerate(picks, 1):
        t = it["time"].strftime("%m/%d %H:%M") if it["time"] else ""
        lines.append(f"{n}. {html.escape(it['title'])}")
        if s:
            lines.append(f"   ▸ {html.escape(s)}")
        lines.append(f"   {html.escape(it['source'])} {t}".rstrip())
        lines.append(f"   {url_line(it)}")
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
    lines = ["<b>🔎 新聞熱度關注清單(依今早新聞提及次數)</b>"]
    if not ranked:
        lines.append("(今天的新聞中沒有明顯被提及的個股)")
    for n, (nm, v) in enumerate(ranked, 1):
        lines.append(f"{n}. {html.escape(nm)} {v['code']}|提及 {v['count']} 則")
        if notes.get(nm):
            lines.append(f"   ▸ {html.escape(str(notes[nm]))}")
        it = v["items"][0]
        lines.append(f"   {html.escape(it['title'])}")
        lines.append(f"   {url_line(it)}")
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
        picks = safe(lambda: pick_with_llm(group, items), f"LLM {group}") or []
        picks = diversify(picks, items, 5)
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
            tg_text(f"⚠️ 盤前新聞今日執行失敗:{html.escape(str(e))}")
        except Exception:
            pass
        sys.exit(1)
