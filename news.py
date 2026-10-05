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
