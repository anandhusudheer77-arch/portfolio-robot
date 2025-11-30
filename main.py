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
with open('credentials.json', 'w') as f:
    json.dump(json.loads(os.environ["GCP_CREDENTIALS_JSON"]), f)

# Authorize gspread with service account
gc = gspread.service_account(filename='credentials.json')

# Open spreadsheet and worksheet
SPREADSHEET_NAME = "PortfolioDB"
SHEET_NAME = "Sheet1"
worksheet = gc.open(SPREADSHEET_NAME).worksheet(SHEET_NAME)

# Get all records (first row is treated as header)
records = worksheet.get_all_records()  # [{'Ticker': 'AAPL', 'Shares': 10, 'Avg_Cost': 150}, ...]
portfolio = pd.DataFrame(records)

# Clean column names (remove extra spaces, etc.)
portfolio.columns = [c.strip() for c in portfolio.columns]

# Sanity check: make sure required columns exist
required_cols = {"Ticker", "Shares", "Avg_Cost"}
missing = required_cols - set(portfolio.columns)
if missing:
    raise ValueError(f"Missing columns in sheet: {missing}. "
                     f"Current columns: {portfolio.columns.tolist()}")

print(f"Found {len(portfolio)} stocks in your portfolio")
print("Columns detected:", portfolio.columns.tolist())

# ===============================
# STEP 2: GET STOCK DATA
# ===============================
def fetch_stock(ticker: str):
    print(f"  Checking {ticker}...")
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        # Be nice to Yahoo Finance
        time.sleep(2)
        return info
    except Exception as e:
        print(f"    Error fetching {ticker}: {e}")
        return None

analysis_results = []

for _, row in portfolio.iterrows():
    ticker = str(row["Ticker"]).strip()
    if not ticker:
        continue

    info = fetch_stock(ticker)

    if info is None:
        analysis_results.append(f"{ticker}: ERROR - Could not get data")
        continue

    # Get the numbers with defaults
    peg = info.get("pegRatio", None)
    roe = info.get("returnOnEquity", None)   # usually in decimal (0.18 = 18%)
    de_ratio = info.get("debtToEquity", None)

    # Calculate simple score
    score = 0

    if peg is not None and peg != 0 and peg < 1.5:
        score += 1
    if roe is not None and roe > 0.15:
        score += 1
    if de_ratio is not None and de_ratio < 100:
        score += 1

    status_list = ["SELL", "HOLD", "BUY", "STRONG BUY"]
    status = status_list[min(score, 3)]

    # Safe formatting
    peg_str = f"{peg:.2f}" if isinstance(peg, (int, float)) else "N/A"
    if isinstance(roe, (int, float)):
        roe_pct_str = f"{roe * 100:.1f}%"
    else:
        roe_pct_str = "N/A"
    de_str = f"{de_ratio:.1f}" if isinstance(de_ratio, (int, float)) else "N/A"

    analysis_results.append(
        f"{ticker}: {status} | PEG: {peg_str} | ROE: {roe_pct_str} | D/E: {de_str}"
    )

# If nothing was analyzed (e.g. empty sheet), bail out early
if not analysis_results:
    raise RuntimeError("No tickers found to analyze. Check your sheet content.")

# ===============================
# STEP 3: ASK THE AI BRAIN
# ===============================
print("Asking AI to think...")

try:
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel("gemini-1.5-flash")

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
        # Convert newlines to <br> so it looks nicer as HTML
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

        # Truncate message if too long
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
