import os, json, smtplib, feedparser, requests, gspread
from bs4 import BeautifulSoup
from anthropic import Anthropic
from google.oauth2.service_account import Credentials
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import date
import google.generativeai as genai

# ── 設定 ──────────────────────────────────────────
RSS_FEEDS = [
    "https://simpleflying.com/feed/",
    "https://skybrary.aero/",
    "https://avherald.com/feed",      # 可自己增減
]

SHEET_ID = os.environ["SHEET_ID"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_PASS = os.environ["GMAIL_APP_PASSWORD"]
client = Anthropic()

# ── Google Sheet 讀取待讀URL ───────────────────────
def get_sheet():
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"]
    )
    return gspread.authorize(creds).open_by_key(SHEET_ID).sheet1

def get_pending_urls(sheet):
    rows = sheet.get_all_values()
    pending = []
    for i, row in enumerate(rows[1:], start=2):  # 跳過標題列
        if len(row) >= 2 and row[1].strip() == "待處理" and row[0].strip():
            pending.append((i, row[0].strip()))
    return pending

def mark_done(sheet, row_index):
    sheet.update_cell(row_index, 2, "已完成")

# ── 爬取內文 ──────────────────────────────────────
def fetch_text(url):
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)[:4000]
    except:
        return ""

# ── Claude 翻譯+摘要 ──────────────────────────────

genai.configure(api_key=os.environ["GEMINI_API_KEY"])

def summarize(title, text):
    model = genai.GenerativeModel("gemini-1.5-flash")
    prompt = f"""以下是一篇航空新聞，請用繁體中文輸出：

標題：{title}
內文：{text}

請依以下格式輸出：
【中文標題】（翻譯標題）
【摘要】（3句話內說明這篇新聞的重點）
【為什麼值得關注】（1句話，對航空從業人員或關注者的意義）"""
    
    response = model.generate_content(prompt)
    return response.text

# ── 抓 RSS ────────────────────────────────────────
def fetch_rss():
    articles = []
    for url in RSS_FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries[:5]:  # 每個來源最多取5篇
            articles.append({
                "title": entry.get("title", ""),
                "url": entry.get("link", ""),
                "source": feed.feed.get("title", url)
            })
    return articles

# ── 組合 Email ────────────────────────────────────
def send_email(sections):
    today = date.today().strftime("%Y/%m/%d")
    body = f"<h2>✈️ 航空情報日報 {today}</h2><hr>"
    for s in sections:
        body += f"""
<p><b>來源：</b>{s['source']} ｜ <a href="{s['url']}">{s['url']}</a></p>
<pre style="font-family:sans-serif;white-space:pre-wrap">{s['summary']}</pre>
<hr>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"✈️ 航空情報日報 {today}"
    msg["From"] = GMAIL_USER
    msg["To"] = GMAIL_USER
    msg.attach(MIMEText(body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_PASS)
        s.send_message(msg)

# ── 主流程 ────────────────────────────────────────
def main():
    sheet = get_sheet()
    sections = []

    # 處理手動丟的URL
    pending = get_pending_urls(sheet)
    for row_idx, url in pending:
        text = fetch_text(url)
        if text:
            summary = summarize(url, text)
            sections.append({"source": "📌 手動加入", "url": url, "summary": summary})
            mark_done(sheet, row_idx)

    # 處理 RSS
    for article in fetch_rss():
        text = fetch_text(article["url"])
        if text:
            summary = summarize(article["title"], text)
            sections.append({"source": article["source"], "url": article["url"], "summary": summary})

    if sections:
        send_email(sections)
        print(f"完成，共處理 {len(sections)} 篇")
    else:
        print("今天沒有新文章")

if __name__ == "__main__":
    main()
