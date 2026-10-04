import os
import sys
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")
TOKEN = os.environ.get("TG_TOKEN")
CHAT_ID = os.environ.get("TG_CHAT_ID")
FORCE = os.environ.get("FORCE_SEND") == "true"


def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=30)
    r.raise_for_status()


def get_index() -> pd.DataFrame:
    df = yf.download("^TWII", period="3mo", progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna()
    df["MA5"] = df["Close"].rolling(5).mean()
    df["MA20"] = df["Close"].rolling(20).mean()
    df["MA60"] = df["Close"].rolling(60).mean()
    return df


def build_report(df: pd.DataFrame) -> str:
    last, prev = df.iloc[-1], df.iloc[-2]
    close = float(last["Close"])
    chg = close - float(prev["Close"])
    pct = chg / float(prev["Close"]) * 100
    ma20 = float(last["MA20"])
    ma5 = float(last["MA5"])
    trend = "站上" if close > ma20 else "跌破"
    short = "多方短線占優" if close > ma5 else "短線偏弱"
    data_date = df.index[-1].date()

    return (
        f"📊 台股盤後報告 {data_date}\n"
        f"加權指數:{close:,.2f}\n"
        f"漲跌:{chg:+,.2f}({pct:+.2f}%)\n"
        f"5MA {ma5:,.0f}|20MA {ma20:,.0f}\n"
        f"目前{trend}月線,{short}。\n\n"
        f"※ 資料僅供參考,非投資建議"
    )


def main():
    df = get_index()
    today = datetime.now(TZ).date()
    if df.index[-1].date() != today and not FORCE:
        print(f"今日({today})無新資料,可能休市,不發報告。")
        return
    send_telegram(build_report(df))
    print("已發送")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("執行失敗:", e)
        try:
            send_telegram(f"⚠️ 台股報告今日執行失敗:{e}")
        except Exception:
            pass
        sys.exit(1)
