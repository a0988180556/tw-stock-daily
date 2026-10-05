import os
import html
import datetime
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]        # 依你現有的 Secrets 名稱調整
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]    # 依你現有的 Secrets 名稱調整

TW = datetime.timezone(datetime.timedelta(hours=8))
RSS = "https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"


def fetch_news(query, n=5):
    url = RSS.format(q=quote(query))
    r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    items, seen = [], set()
    for it in ET.fromstring(r.content).iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        source = (it.findtext("source") or "").strip()
        if not title or not link:
            continue
        # Google News 標題結尾會帶「 - 來源」，來源另外顯示，所以去掉
        if source and title.endswith(f" - {source}"):
            title = title[: -len(f" - {source}")].strip()
        key = title[:20]  # 簡單去除重複標題
        if key in seen:
            continue
        seen.add(key)
        try:
            t = parsedate_to_datetime(it.findtext("pubDate")).astimezone(TW)
            when = t.strftime("%m/%d %H:%M")
        except Exception:
            when = ""
        items.append({"title": title, "link": link, "source": source, "when": when})
        if len(items) == n:
            break
    return items


def format_block(header, items):
    lines = [f"<b>{header}</b>"]
    for i, it in enumerate(items, 1):
        # 標題本身是超連結，不另外顯示長網址
        lines.append(
            f'{i}. <a href="{html.escape(it["link"], quote=True)}">{html.escape(it["title"])}</a>'
        )
        meta = " ".join(x for x in (it["source"], it["when"]) if x)
        if meta:
            lines.append(f"   {html.escape(meta)}")
        lines.append("")
    if not items:
        lines.append("（今日暫無資料）")
    return "\n".join(lines).rstrip()


def main():
    tw = fetch_news("台股 when:1d", 5)
    us = fetch_news("美股 道瓊 那斯達克 when:1d", 5)

    today = datetime.datetime.now(TW)
    text = "\n\n".join([
        f"📰 <b>盤前新聞 {today:%Y-%m-%d}</b>",
        format_block("🇹🇼 台股重要新聞", tw),
        format_block("🇺🇸 美股重要新聞", us),
        "※ 新聞整理僅供參考，非投資建議",
    ])

    resp = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    resp.raise_for_status()


if __name__ == "__main__":
    main()
