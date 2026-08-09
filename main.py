import os, json, smtplib, feedparser, requests, gspread, time
from google import genai
from google.genai import types
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import date

# ── 設定 ──────────────────────────────────────────

RSS_FEEDS = [
    "https://worldairlinenews.com/feed",
    "https://asianaviation.com/feed/",
    "https://simpleflying.com/feed/",
    "https://airlinereporter.com/feed",
    "https://theaircurrent.com/feed/",
    "https://feeds.feedburner.com/AirlineGeeks",
]

MAX_ARTICLES_PER_FEED = 15
MAX_RSS_SECTIONS = 8
MAX_ARTICLES_PER_SOURCE = 4

SHEET_ID = os.environ["SHEET_ID"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_PASS = os.environ["GMAIL_APP_PASSWORD"]

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])


# ── Gemini 呼叫（含自動換模型與 retry） ────────────

def call_gemini(prompt, primary="gemini-3.1-flash-lite", fallback="gemini-3.5-flash-lite"):
    for model in [primary, fallback]:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt
                )
                return response.text
            except Exception as e:
                if "429" in str(e) or "503" in str(e):
                    wait = 30 * (attempt + 1)  # 30秒、60秒、90秒
                    print(f"    → {model} 超過限制，等待{wait}秒...")
                    time.sleep(wait)
                elif "404" in str(e):
                    print(f"    → {model} 不存在，換備用模型")
                    break
                else:
                    print(f"    → 錯誤：{e}")
                    break
    return ""


# ── Google Sheet ───────────────────────────────────

def get_sheet():
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])

    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
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
        r = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")

        # 移除明確不是文章內容的元素
        for tag in soup([
            "script",
            "style",
            "nav",
            "footer",
            "header",
            "aside",
            "form",
            "noscript",
            "iframe"
        ]):
            tag.decompose()

        # 優先抓 <article>
        content = soup.find("article")

        # 如果沒有 <article>，再找常見的文章容器
        if not content:
            content = (
                soup.select_one(".entry-content")
                or soup.select_one(".post-content")
                or soup.select_one(".article-content")
                or soup.select_one(".article-body")
                or soup.select_one("main")
            )

        # 最後才退回整頁
        if not content:
            content = soup

        # 只移除非常明確的非正文區塊
        for tag in content.select(
            ".advertisement, .advertising, .ads, "
            ".newsletter, .subscribe, "
            ".social-share, .share-buttons, "
            ".related-posts, .comments"
        ):
            tag.decompose()

        return content.get_text(
            separator=" ",
            strip=True
        )[:4000]

    except Exception as e:
        print(f"    → 抓取失敗：{e}")
        return ""


# ── Gemini：批次判斷是否民航相關 ──────────────────

def classify_articles(articles):
    """
    一次把所有候選文章的標題交給 Gemini。
    只用標題判斷，不抓原文，因此速度和 token 成本較低。
    """

    if not articles:
        return []

    lines = []

    for i, article in enumerate(articles, start=1):
        lines.append(
            f"{i}. [{article['source']}] {article['title']}"
        )

    prompt = f"""
你是一名航空新聞編輯。

以下是今天從多家航空媒體 RSS 抓到的新聞標題。

請判斷哪些新聞與「民用航空」有關。

【判斷原則】
包含：
- 商業航空
- 民航公司
- 民用機場
- 客機
- 貨機
- 航空公司營運
- 航線
- 航空產業
- 飛安
- 民航事故或事件
- 飛機製造商的民航業務
- 任何涉及民航客機的重大事件，即使事件同時涉及軍機、軍事或其他領域，也算民航相關

例如：
「一架客機與戰鬥機差點相撞」
→ YES，因為涉及民航客機與飛安。

排除：
- 純軍用航空
- 純戰鬥機新聞
- 純軍事武器
- 純軍事無人機
- 太空
- 與民航沒有直接關係的國防新聞

請不要根據來源判斷，只根據新聞標題判斷。

只輸出符合條件的文章編號，每個編號用逗號分隔。
不要輸出其他文字。

例如：
1,3,7,12,15

新聞列表：
{chr(10).join(lines)}
"""

    result = call_gemini(
        prompt,
        primary="gemini-3.1-flash-lite",
        fallback="gemini-3.5-flash"
    )

    if not result:
        return []

    selected_indexes = set()

    for part in result.replace("\n", ",").split(","):
        part = part.strip()

        try:
            index = int(part)

            if 1 <= index <= len(articles):
                selected_indexes.add(index - 1)

        except ValueError:
            continue

    return [
        articles[i]
        for i in sorted(selected_indexes)
    ]


# ── Gemini：統一選出今天最值得看的 8 篇 ───────────

def select_top_articles(articles):
    """
    從已確認與民航相關的候選新聞中，
    統一挑選最多 8 篇。

    同一來源最多 4 篇，但不要求每個來源平均分配。
    """

    if not articles:
        return []

    lines = []

    for i, article in enumerate(articles, start=1):
        summary = article.get("summary_raw", "")

        # RSS summary 太長時只保留前 600 字
        summary = BeautifulSoup(
            summary,
            "html.parser"
        ).get_text(" ", strip=True)[:600]

        lines.append(
            f"{i}. [{article['source']}]\n"
            f"標題：{article['title']}\n"
            f"RSS摘要：{summary}"
        )

    prompt = f"""
    
你是一名航空新聞電子報的總編輯。
今天要製作一封「航空時事日報」，最多刊登 8 篇新聞。
以下是已經確認與民用航空相關的候選新聞。
請從中挑選「今天最值得航空從業人員或航空新聞讀者閱讀」的最多 8 篇。

【選稿原則】

1. 優先重大、具產業影響力或資訊價值的新聞時事。
2. 優先：
   - 飛安事件
   - 重大事故
   - 航空公司重大策略或營運變化
   - 航線重大變化
   - 機場重大事件
   - Boeing、Airbus 等飛機製造商的重要民航消息
   - FAA、EASA 等監管機構的重要決策
   - 航空產業重大趨勢
   - 對航空業可能有長期影響的事件
3. 瑣碎性、純促銷、純廣告、價值較低的新聞可以排除，但也不用太嚴格。
4. 同一來源最多可以選 4 篇，但只有在該來源確實有多篇高價值新聞時才選到 4 篇。不需要平均分配媒體來源。
5. 不要為了湊來源多樣性而選擇明顯較差的新聞。
6. 如果高品質新聞主要集中在少數來源，可以讓這些來源佔較多篇數。
7. 如果有多篇新聞報導的是同一事件，只選其中一篇（選資訊最豐富的）。
8. 最多 8 篇，不足 8 篇時不要硬湊。

請只輸出最後選中的文章編號，用逗號分隔。
不要輸出解釋。

例如：
2,4,7,9,12,15,18,21

候選新聞：

{chr(10).join(lines)}
"""

    result = call_gemini(
        prompt,
        primary="gemini-3.1-flash-lite",
        fallback="gemini-3.5-flash"
    )

    if not result:
        return []

    selected_indexes = []

    for part in result.replace("\n", ",").split(","):
        part = part.strip()

        try:
            index = int(part)

            if 1 <= index <= len(articles):
                actual_index = index - 1

                if actual_index not in selected_indexes:
                    selected_indexes.append(actual_index)

        except ValueError:
            continue

    # Python 再做一次來源上限保護
    final_articles = []
    source_count = {}

    for index in selected_indexes:
        article = articles[index]
        source = article["source"]

        if source_count.get(source, 0) >= MAX_ARTICLES_PER_SOURCE:
            continue

        final_articles.append(article)
        source_count[source] = source_count.get(source, 0) + 1

        if len(final_articles) >= MAX_RSS_SECTIONS:
            break

    return final_articles


# ── 翻譯 + 摘要 ───────────────────────────────────

def summarize(title, text):
    prompt = f"""以下是一篇航空新聞，請用繁體中文輸出：

標題：{title}
內文：{text}

不要使用任何 Markdown 語法（不要用 ** 、 * 、 # 等符號）。
可以使用 HTML/CSS 語法。請勿增加粗體、斜體以及底線以外的格式。
請依以下格式輸出：

【標題】（翻譯標題）
【摘要】（3句話內說明這篇新聞的重點）
【為什麼值得關注】（1句話，對航空從業人員或關注者的意義）
【潛在提問】（1-3個問題，並引導思考，或是附上建議作答方向）

可參考以下範例：

【標題】倫敦蓋威克機場（London Gatwick）在法律挑戰遭駁回後，將推進第二跑道擴建計畫

【摘要】
倫敦蓋威克機場因一項針對其擴建計畫的法律挑戰遭法院駁回，得以繼續推進第二跑道興建工程......

【為什麼值得關注】
這標誌著在環境法規與地方阻力壓力下，大型機場擴建仍有推進的可能......

【潛在提問】

1. 問題1，引導1
2. 問題2，引導2
3. 問題3，引導3"""

    return call_gemini(
        prompt,
        primary="gemini-3.5-flash-lite",
        fallback="gemini-3.1-flash-lite"
    )


# ── 抓 RSS ────────────────────────────────────────

def fetch_rss():
    articles = []

    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)

            source = feed.feed.get("title", url)

            print(f"  → {source}")

            for entry in feed.entries[:MAX_ARTICLES_PER_FEED]:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()

                if not title or not link:
                    continue

                articles.append({
                    "title": title,
                    "url": link,
                    "source": source,
                    "summary_raw": entry.get("summary", "")
                })

        except Exception as e:
            print(f"    → RSS 讀取失敗：{e}")
            continue

    return articles


# ── 寄 Email ──────────────────────────────────────

def send_email(sections):
    today = date.today().strftime("%Y/%m/%d")
    body = f"<h2>✈️ 航空時事日報 {today}</h2><hr>"
    for s in sections:
        body += f"""
<p><b>來源：</b>{s['source']} ｜ <a href="{s['url']}">{s['url']}</a></p>
<p style="font-family:sans-serif; line-height:1.8">{s['summary'].replace(chr(10), '<br>')}</p>
<hr>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"✈️ 航空時事日報 {today}"
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
        ws = sheet_file.add_worksheet(
            "已處理",
            rows=1000,
            cols=1
        )

    return set(
        row[0]
        for row in ws.get_all_values()
        if row
    )


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

    # ── 手動 URL（不限制篇數） ─────────────────────

    pending = get_pending_urls(sheet)

    print(f"待處理URL數：{len(pending)}")

    for row_idx, url in pending:

        if url in processed:
            mark_done(sheet, row_idx)
            continue

        text = fetch_text(url)

        if text:
            summary = summarize(url, text)

            sections.append({
                "source": "📌 手動加入",
                "url": url,
                "summary": summary
            })

            mark_done(sheet, row_idx)
            save_processed_url(sf, url)

    # ── RSS：先全部抓回來 ─────────────────────────

    articles = fetch_rss()

    print(f"RSS抓到文章數：{len(articles)}")

    # 移除已處理文章
    candidates = [
        article
        for article in articles
        if article["url"] not in processed
    ]

    print(f"扣除已處理後：{len(candidates)} 篇")

    if candidates:

        # ── 第一步：只看標題，判斷是否民航相關 ────

        print("開始用 Gemini 判斷民航相關性...")

        aviation_candidates = classify_articles(candidates)

        print(
            f"民航相關候選："
            f"{len(aviation_candidates)} 篇"
        )

        # ── 第二步：統一選出最值得刊出的 8 篇 ────

        print("開始統一選稿...")

        selected_articles = select_top_articles(
            aviation_candidates
        )

        print(
            f"最終選出："
            f"{len(selected_articles)} 篇"
        )

        for article in selected_articles:
            print(
                f"  - [{article['source']}] "
                f"{article['title']}"
            )

        selected_urls = {
            article["url"]
            for article in selected_articles
        }

        # ── 第三步：只有入選文章才抓完整原文 ──

        rss_sections = []

        for article in selected_articles:

            print(f"\n  - {article['title']}")

            text = fetch_text(article["url"])

            content = (
                text
                if len(text) > 200
                else article.get("summary_raw", "")
            )

            print(f"    → 內容長度：{len(content)}")

            if not content:
                print("    → 沒有內容，跳過")
                continue

            # Gemini 閱讀完整原文並摘要
            summary = summarize(
                article["title"],
                content
            )

            rss_sections.append({
                "source": article["source"],
                "url": article["url"],
                "summary": summary
            })

            save_processed_url(
                sf,
                article["url"]
            )

        sections.extend(rss_sections)

        # ── 將這次看到但沒有入選的候選也標記已處理 ──
        #
        # 這樣隔天不會一直重複看到同一篇舊新聞。
        # 但如果文章已經入選，上面已經存過，這裡不會重複存。

        for article in candidates:
            if article["url"] not in selected_urls:
                save_processed_url(
                    sf,
                    article["url"]
                )

    print(f"處理完成，共 {len(sections)} 篇")

    if sections:
        send_email(sections)
        print("信件已寄出")
    else:
        print("沒有新文章，不寄信")


if __name__ == "__main__":
    main()
