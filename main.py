import pandas as pd
import yfinance as yf
import gspread
import os, time, json, urllib.parse
import smtplib
from email.mime.text import MIMEText
import requests
import google.generativeai as genai
from datetime import datetime

# --- STEP 1: READ YOUR SPREADSHEET ---
print("Robot is reading your spreadsheet...")
with open('credentials.json', 'w') as f:
    json.dump(json.loads(os.environ["GCP_CREDENTIALS_JSON"]), f)

gc = gspread.service_account(filename='credentials.json')
worksheet = gc.open("PortfolioDB").worksheet("Sheet1")  # Note: Sheet1, not Portfolio
records = worksheet.get_all_records()
portfolio = pd.DataFrame(records)
print(f"Found {len(portfolio)} stocks in your portfolio")

# --- STEP 2: GET STOCK DATA ---
def fetch_stock(ticker):
    print(f"  Checking {ticker}...")
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        time.sleep(2)  # Wait 2 seconds to be nice to Yahoo
        return info
    except:
        return None

analysis_results = []
for _, row in portfolio.iterrows():
    ticker = row['Ticker']
    info = fetch_stock(ticker)
    
    if info is None:
        analysis_results.append(f"{ticker}: ERROR - Could not get data")
        continue
    
    # Get the numbers
    peg = info.get('pegRatio', 999)
    roe = info.get('returnOnEquity', 0)
    de_ratio = info.get('debtToEquity', 999)
    
    # Calculate score (simple version)
    score = 0
    if peg and peg < 1.5: score += 1
    if roe and roe > 0.15: score += 1
    if de_ratio and de_ratio < 100: score += 1
    
    status = ["SELL", "HOLD", "BUY", "STRONG BUY"][min(score, 3)]
    
    analysis_results.append(
        f"{ticker}: {status} | PEG: {peg:.2f} | ROE: {roe*100:.1f}% | D/E: {de_ratio}"
    )

# --- STEP 3: ASK THE AI BRAIN ---
print("Asking AI to think...")
try:
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel('gemini-1.5-flash')
    
    prompt = f"""You are my stock analyst. Here's my portfolio data:
{'\n'.join(analysis_results)}

Write a short email summary. Tell me:
1. My best stock right now
2. My biggest risk
3. One thing I should do today

Keep it under 200 words."""
    
    response = model.generate_content(prompt)
    ai_summary = response.text
except Exception as e:
    ai_summary = f"AI failed. Here is raw data:\n{'\n'.join(analysis_results)}"

# --- STEP 4: SEND EMAIL ---
def send_email():
    print("Sending email...")
    try:
        msg = MIMEText(ai_summary, 'html')
        msg['Subject'] = f"📊 Daily GARP Report | {datetime.now().strftime('%b %d')}"
        msg['From'] = os.environ["EMAIL_USER"]
        msg['To'] = os.environ["EMAIL_TO"]
        
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(os.environ["EMAIL_USER"], os.environ["EMAIL_PASS"])
            server.send_message(msg)
        print("✅ Email sent!")
    except Exception as e:
        print(f"❌ Email failed: {e}")

# --- STEP 5: SEND TELEGRAM ---
def send_telegram():
    print("Sending Telegram message...")
    try:
        bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
        chat_id = os.environ["TELEGRAM_CHAT_ID"]
        
        message = f"<b>Daily Stock Report</b>\n\n{ai_summary[:800]}..."
        
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
        requests.post(url, json=payload)
        print("✅ Telegram sent!")
    except Exception as e:
        print(f"❌ Telegram failed: {e}")

# --- RUN EVERYTHING ---
send_email()
send_telegram()
print("Robot finished work!")