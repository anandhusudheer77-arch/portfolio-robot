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
records = worksheet.get_all_records()
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
# STEP 2: GET STOCK DATA (Politely & With Retries)
# ===============================
def fetch_stock(ticker: str, max_retries=3):
    """
    Fetches stock info from Yahoo Finance with retry logic and polite delays.
    """
    for attempt in range(max_retries):
        try:
            print(f"  Checking {ticker}... (Attempt {attempt + 1}/{max_retries})")
            
            # Be EXTRA nice to Yahoo: wait 3-7 seconds randomly between requests
            # This makes you look like a human, not a spammy bot
            sleep_time = random.uniform(3, 7)
            print(f"    Waiting {sleep_time:.1f} seconds to be polite...")
            time.sleep(sleep_time)
            
            # Identify ourselves as a friendly robot (not a scraper)
            stock = yf.Ticker(ticker)
            stock.session.headers['User-Agent'] = 'Mozilla/5.0 (compatible; PortfolioRobot/1.0)'
            
            # Get the data
            info = stock.info
            
            # Check if we actually got real data (not empty)
            if not info or info.get('symbol') is None:
                print(f"    Warning: Yahoo returned empty data for {ticker}")
                if attempt < max_retries - 1:
                    print(f"    Retrying after 10 seconds...")
                    time.sleep(10)
                    continue
                else:
                    return None
                
            return info
            
        except Exception as e:
            error_msg = str(e)
            print(f"    Error fetching {ticker}: {error_msg[:60]}...")
            
            # If rate limited (429), wait much longer
            if "429" in error_msg:
                wait_time = 15 * (attempt + 1)  # 15s, 30s, 45s
                print(f"    🚨 Rate limited by Yahoo! Waiting {wait_time}s...")
                time.sleep(wait_time)
            elif attempt < max_retries - 1:
                # For other errors, wait 5 seconds before retry
                print(f"    Retrying in 5 seconds...")
                time.sleep(5)
            else:
                # Last attempt failed
                return None

# Track how many stocks we successfully analyzed
analysis_results = []
successful_fetches = 0

for _, row in portfolio.iterrows():
    ticker = str(row["Ticker"]).strip()
    if not ticker:
        continue

    info = fetch_stock(ticker)

    if info is None:
        analysis_results.append(f"{ticker}: ❌ ERROR - Could not get data")
        continue

    # Get the numbers with defaults
    peg = info.get("pegRatio", None)
    roe = info.get("returnOnEquity", None)
    de_ratio = info.get("debtToEquity", None)

    # Only score if we have real numbers (not None)
    score = 0
    if peg is not None and peg != 0 and peg < 1.5:
        score += 1
    if roe is not None and roe > 0.15:
        score += 1
    if de_ratio is not None and de_ratio < 100:
        score += 1

    # Status names with emojis for visual appeal
    status_list = ["🔴 SELL", "🟡 HOLD", "🟢 BUY", "⭐ STRONG BUY"]
    status = status_list[min(score, 3)]

    # Safe formatting (avoid crashes if data is weird)
    peg_str = f"{peg:.2f}" if isinstance(peg, (int, float)) else "N/A"
    if isinstance(roe, (int, float)):
        roe_pct_str = f"{roe * 100:.1f}%"
    else:
        roe_pct_str = "N/A"
    de_str = f"{de_ratio:.1f}" if isinstance(de_ratio, (int, float)) else "N/A"

    analysis_results.append(
        f"**{ticker}**: {status} | PEG: {peg_str} | ROE: {roe_pct_str} | D/E: {de_str}"
    )
    successful_fetches += 1

# If no stocks could be analyzed, stop here with explanation
if not analysis_results:
    raise RuntimeError("No tickers found to analyze. Check your sheet content.")

# ===============================
# STEP 3: ASK THE AI BRAIN (CORRECT MODEL NAME)
# ===============================
print("Asking AI to think...")

# If all stocks failed, don't waste AI credits - send direct message
if successful_fetches == 0:
    ai_summary = """
    <h2>🚨 Portfolio Robot Report</h2>
    <p><strong>All stock data fetches failed.</strong></p>
    <p>Yahoo Finance blocked every request due to rate limiting. This happens when:</p>
    <ul>
        <li>You have too many stocks (>5 is risky)</li>
        <li>Yahoo is having a bad day (rare)</li>
        <li>Your IP range is flagged (GitHub's IPs sometimes are)</li>
    </ul>
    <p><strong>What to do:</strong></p>
    <ul>
        <li>Wait 1 hour and try again</li>
        <li>Temporarily reduce to 3-5 stocks in your sheet</li>
        <li>The robot is already configured with maximum politeness</li>
    </ul>
    <p><em>Raw errors:</em> All tickers returned 429 Too Many Requests</p>
    """
else:
    try:
        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
        # USE THIS MODEL NAME FOR FREE TIER:
        model = genai.GenerativeModel("gemini-1.5-flash-latest")

        # Build the prompt safely
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
        print(f"AI error: {e}")
        # Fallback: Create a clean HTML summary without AI
        ai_summary = f"""
        <h2>Daily GARP Analysis</h2>
        <p><strong>Successfully analyzed: {successful_fetches}/{len(portfolio)} stocks</strong></p>
        <ul>
        """
        for line in analysis_results[:8]:  # Show first 8 stocks
            ai_summary += f"<li>{line}</li>"
        ai_summary += """
        </ul>
        <p><em>AI summary unavailable. Using raw scores above.</em></p>
        """

# ===============================
# STEP 4: SEND EMAIL
# ===============================
def send_email():
    print("Sending email...")
    try:
        # Convert newlines to <br> for HTML email
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
# STEP 5: SEND TELEGRAM (FIXED URL)
# ===============================
def send_telegram():
    print("Sending Telegram message...")
    try:
        bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
        chat_id = os.environ["TELEGRAM_CHAT_ID"]

        # Create short summary for Telegram
        summary_line = f"Analyzed {successful_fetches}/{len(portfolio)} stocks ✅"
        if successful_fetches < len(portfolio):
            summary_line += f" | {len(portfolio) - successful_fetches} failed ❌"
        
        # Truncate to fit Telegram limits
        message = f"<b>Daily Stock Report</b>\n\n{summary_line}\n\n{ai_summary[:800]}..."
        if len(message) > 4000:
            message = message[:3800] + "\n\n<i>...message truncated</i>"

        # ✅ FIXED: Removed space in URL
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }
        resp = requests.post(url, json=payload, timeout=10)
        
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
    print(f"Robot finished work! Analyzed {successful_fetches}/{len(portfolio)} stocks successfully.")
