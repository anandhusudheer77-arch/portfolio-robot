import os
import time
import json
import smtplib
from datetime import datetime

import pandas as pd
import yfinance as yf
import gspread
import requests
import google.generativeai as genai
from email.mime.text import MIMEText

# ===============================
# STEP 1: READ YOUR SPREADSHEET
# ===============================
print("Robot is reading your spreadsheet...")

# Write service account credentials from env var to a file
with open("credentials.json", "w") as f:
    json.dump(json.loads(os.environ["GCP_CREDENTIALS_JSON"]), f)

# Authorize gspread with service account
gc = gspread.service_account(filename="credentials.json")

SPREADSHEET_NAME = "PortfolioDB"
SHEET_NAME = "Sheet1"

worksheet = gc.open(SPREADSHEET_NAME).worksheet(SHEET_NAME)
records = worksheet.get_all_records()  # [{'Ticker': 'INFY.NS', ...}, ...]
portfolio = pd.DataFrame(records)

# Clean column names
portfolio.columns = [c.strip() for c in portfolio.columns]

required_cols = {"Ticker", "Shares", "Avg_Cost"}
missing = required_cols - set(portfolio.columns)
if missing:
    raise ValueError(
        f"Missing columns in sheet: {missing}. "
        f"Current columns: {portfolio.columns.tolist()}"
    )

print(f"Found {len(portfolio)} stocks in your portfolio")
print("Columns detected:", portfolio.columns.tolist())

# ===============================
# STEP 2: GET STOCK PRICE DATA
# ===============================
def fetch_stock_history(ticker: str, max_retries: int = 3):
    """
    Fetch 1-year daily price history for a ticker.
    Uses chart endpoint instead of quoteSummary to avoid 429 issues.
    """
    for attempt in range(max_retries):
        try:
            print(f"  Checking {ticker} (attempt {attempt + 1}/{max_retries})...")
            stock = yf.Ticker(ticker)
            # 1 year of daily data
            hist = stock.history(period="1y", interval="1d")
            if hist.empty:
                print("    No price history returned.")
                return None
            return hist
        except Exception as e:
            msg = str(e)
            print(f"    Error fetching {ticker}: {msg}")

            # Backoff for 429 or timeouts
            if "Too Many Requests" in msg or "429" in msg:
                wait = 10 * (attempt + 1)
                print(f"    Got 429, sleeping {wait}s before retry...")
                time.sleep(wait)
                continue
            if "timed out" in msg:
                wait = 5 * (attempt + 1)
                print(f"    Timeout, sleeping {wait}s before retry...")
                time.sleep(wait)
                continue

            # Other errors: do not retry endlessly
            return None

    print(f"    Failed to fetch {ticker} after {max_retries} retries.")
    return None


def pct_return(hist: pd.DataFrame, days: int):
    """Compute percentage return over last `days` trading days."""
    if len(hist) <= days:
        return None
    current_price = hist["Close"].iloc[-1]
    past_price = hist["Close"].iloc[-(days + 1)]
    if past_price == 0:
        return None
    return (current_price - past_price) / past_price


def fmt_pct(x):
    return f"{x * 100:.1f}%" if isinstance(x, (int, float)) else "N/A"


analysis_results = []

for _, row in portfolio.iterrows():
    ticker = str(row["Ticker"]).strip()
    if not ticker:
        continue

    hist = fetch_stock_history(ticker)

    if hist is None:
        analysis_results.append(f"{ticker}: ERROR - Could not get price data")
        continue

    # Current price
    current_price = hist["Close"].iloc[-1]

    # 1-month (~22 trading days) and 6-month (~132 trading days) returns
    ret_1m = pct_return(hist, 22)
    ret_6m = pct_return(hist, 132)

    # Volatility = std dev of daily returns
    daily_ret = hist["Close"].pct_change().dropna()
    vol = float(daily_ret.std()) if not daily_ret.empty else None

    # Simple scoring logic
    score = 0
    if ret_6m is not None and ret_6m > 0.10:  # > +10% in 6 months
        score += 1
    if ret_1m is not None and ret_1m > 0.03:  # > +3% in 1 month
        score += 1
    if vol is not None and vol < 0.03:        # relatively low volatility
        score += 1

    status_list = ["SELL", "HOLD", "BUY", "STRONG BUY"]
    status = status_list[min(score, 3)]

    vol_str = f"{vol:.4f}" if isinstance(vol, (int, float)) else "N/A"

    analysis_results.append(
        f"{ticker}: {status} | Price: {current_price:.2f} | "
        f"1M: {fmt_pct(ret_1m)} | 6M: {fmt_pct(ret_6m)} | Vol: {vol_str}"
    )

if not analysis_results:
    raise RuntimeError("No tickers analyzed. Check your sheet content.")

# Debug (optional) – uncomment if you want to see in logs
# print("\n=== ANALYSIS RESULTS ===")
# for line in analysis_results:
#     print(line)

# ===============================
# STEP 3: ASK THE AI BRAIN
# ===============================
print("Asking AI to think...")

try:
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    # Use a model supported by google-generativeai==0.3.2
    model = genai.GenerativeModel("gemini-pro")

    analysis_str = "\n".join(analysis_results)
    prompt = f"""You are my stock analyst. Here's my portfolio data:
{analysis_str}

Write a short email summary. Tell me:
1. My best stock right now
2. My biggest risk
3. One thing I should do today

Keep it under 200 words."""
    response = model.generate_content(prompt)
    ai_summary = response.text
except Exception as e:
    newline = "\n"
    ai_summary = "AI failed. Here is raw data:\n" + newline.join(analysis_results)
    print(f"AI error: {e}")

# ===============================
# STEP 4: SEND EMAIL
# ===============================
def send_email():
    print("Sending email...")
    try:
        body_html = ai_summary.replace("\n", "<br>")
        msg = MIMEText(body_html, "html")
        msg["Subject"] = f"📊 Daily GARP Report | {datetime.now().strftime('%b %d')}"
        msg["From"] = os.environ["EMAIL_USER"]
        msg["To"] = os.environ["EMAIL_TO"]

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(os.environ["EMAIL_USER"], os.environ["EMAIL_PASS"])
            server.send_message(msg)

        print("✅ Email sent!")
    except Exception as e:
        print(f"❌ Email failed: {e}")

# ===============================
# STEP 5: SEND TELEGRAM
# ===============================
def send_telegram():
    print("Sending Telegram message...")
    try:
        bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
        chat_id = os.environ["TELEGRAM_CHAT_ID"]

        message = f"<b>Daily Stock Report</b>\n\n{ai_summary}"
        if len(message) > 4000:
            message = message[:3800] + "\n\n..."

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }
        resp = requests.post(url, json=payload)
        if resp.status_code == 200:
            print("✅ Telegram sent!")
        else:
            print(f"❌ Telegram API error: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"❌ Telegram failed: {e}")

# ===============================
# STEP 6: RUN EVERYTHING
# ===============================
if __name__ == "__main__":
    send_email()
    send_telegram()
    print("Robot finished work!")
