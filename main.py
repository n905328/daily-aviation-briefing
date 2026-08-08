import os, json, smtplib, feedparser, requests, gspread
from google import genai
from google.genai import types
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import date

# ── 設定 ──────────────────────────────────────────
RSS_FEEDS = [
    "https://theaircurrent.com/feed/",
    "https://simpleflying.com/feed/",
    "https://www.aviationpros.com/rss/",
    "https://feeds.feedburner.com/AirlineGeeks",
    "https://www.ch-aviation.com/portal/rss",
    "https://avherald.com/h?subscribe=newsfeed&opt=0&lang=0",
]

SHEET_ID = os.environ["SHEET_ID"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_PASS = os.environ["GMAIL_APP_PASSWORD"]

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# ── Google Sheet ───────────────────────────────────
def get_sheet():
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"]
    )
    return gspread.authorize(creds).open_by_key(SHEET_ID)

def get_pending_urls(sheet):
    rows = sheet.get_all_values()
    pending = []
    for i, row in enumerate(rows[1:], start=2):
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

# ── 過濾非民航新聞 ────────────────────────────────
def is_aviation_related(title, text):
    try:
        prompt = (
            f"標題：{title}\n內容：{text[:500]}\n"
            f"請只回答 yes 或 no：這篇新聞是否與民用航空相關？"
            f"（包含：商業航空、民航公司、民用機場、客機、貨機、飛安事故）"
            f"（不包含：軍用飛機、戰鬥機、無人機軍事用途、太空）"
        )
        response = client.models.generate_content(
            model="models/gemini-1.5-flash",
            contents=prompt
        )
        return "yes" in response.text.lower()
    except:
        return True

# ── Gemini 翻譯+摘要 ──────────────────────────────
def summarize(title, text):
    try:
        prompt = f"""以下是一篇航空新聞，請用繁體中文輸出：

標題：{title}
內文：{text}

請依以下格式輸出，不要使用任何 Markdown 語法（不要用 ** 、 * 、 # 等符號），若要增加格式，可以使用 HTML：

【標題】（翻譯標題）
【摘要】（3句話內說明這篇新聞的重點）
【為什麼值得關注】（1句話，對航空從業人員或關注者的意義）
【潛在提問】（1-3個問題，並附上建議作答方向/引導思考）"""
        response = client.models.generate_content(
            model="models/gemini-1.5-flash",
            contents=prompt
        )
        return response.text
    except Exception as e:
        return f"摘要失敗：{e}"

# ── 抓 RSS ────────────────────────────────────────
def fetch_rss():
    articles = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:10]:
                articles.append({
                    "title": entry.get("title", ""),
                    "url": entry.get("link", ""),
                    "source": feed.feed.get("title", url),
                    "summary_raw": entry.get("summary", "")
                })
        except:
            continue
    return articles

# ── 寄 Email ──────────────────────────────────────
def send_email(sections):
    today = date.today().strftime("%Y/%m/%d")
    body = f"<h2>✈️ 航空情報日報 {today}</h2><hr>"
    for s in sections:
        body += f"""
<p><b>來源：</b>{s['source']} ｜ <a href="{s['url']}">{s['url']}</a></p>
<p style="font-family:sans-serif; line-height:1.8">{s['summary'].replace(chr(10), '<br>')}</p>
<hr>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"✈️ 航空情報日報 {today}"
    msg["From"] = GMAIL_USER
    msg["To"] = GMAIL_USER
    msg.attach(MIMEText(body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_PASS)
        s.send_message(msg)

# ── 去重複 ────────────────────────────────────────
def get_processed_urls(sheet_file):
    try:
        ws = sheet_file.worksheet("已處理")
    except:
        ws = sheet_file.add_worksheet("已處理", rows=1000, cols=1)
    return set(row[0] for row in ws.get_all_values() if row)

def save_processed_url(sheet_file, url):
    ws = sheet_file.worksheet("已處理")
    ws.append_row([url])

# ── 主流程 ────────────────────────────────────────
def main():
    print("開始執行")
    sf = get_sheet()
    sheet = sf.sheet1
    processed = get_processed_urls(sf)
    print(f"已處理URL數：{len(processed)}")
    sections = []

    # 手動 URL
    pending = get_pending_urls(sheet)
    print(f"待處理URL數：{len(pending)}")
    for row_idx, url in pending:
        if url in processed:
            mark_done(sheet, row_idx)
            continue
        text = fetch_text(url)
        if text:
            summary = summarize(url, text)
            sections.append({"source": "📌 手動加入", "url": url, "summary": summary})
            mark_done(sheet, row_idx)
            save_processed_url(sf, url)

    # RSS
    articles = fetch_rss()
    print(f"RSS抓到文章數：{len(articles)}")
    for article in articles:
        print(f"  - {article['title']}")
        if article["url"] in processed:
            print(f"    → 已處理，跳過")
            continue
        text = fetch_text(article["url"])
        content = text if len(text) > 200 else article.get("summary_raw", "")
        print(f"    → 內容長度：{len(content)}")
        if content:
            if is_aviation_related(article["title"], content):
                summary = summarize(article["title"], content)
                sections.append({"source": article["source"], "url": article["url"], "summary": summary})
                save_processed_url(sf, article["url"])
            else:
                print(f"    → 非民航相關，跳過")
                save_processed_url(sf, article["url"])

    print(f"處理完成，共 {len(sections)} 篇")
    if sections:
        send_email(sections)
        print("信件已寄出")
    else:
        print("沒有新文章，不寄信")

if __name__ == "__main__":
    main()
