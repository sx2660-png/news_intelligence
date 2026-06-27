"""
Content Filter & WeChat News Article Generator
===============================================
Reads emails_output.json, filters for real news / announcements
(university notices, safety alerts, institutional updates, NYC events, etc.),
then uses OpenAI to rewrite each one as a Chinese WeChat news article
in the style of 情报特刊 — formal yet readable, suitable for mobile.

Usage:
    export OPENAI_API_KEY="sk-..."
    python content_generator.py

Output: articles_output.json  (filtered + rewritten articles)
"""

import json
import os
import re
import sys
from datetime import datetime
from openai import OpenAI

# ── Config ─────────────────────────────────────────────────────────────

INPUT_FILE  = "emails_output.json"
OUTPUT_FILE = "articles_output.json"

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENAI_MODEL       = os.environ.get("OPENAI_MODEL", "qwen/qwen3.7-plus")

# ── Filter rules ───────────────────────────────────────────────────────

# Senders / domains to always skip (pure system / transactional mail)
SKIP_SENDER_PATTERNS = [
    r"no-reply@accounts\.google\.com",
    r"no-reply@google\.com",
    r"ads-account-noreply@google\.com",
    r"noreply@",
    r"donotreply@",
    r"mailer-daemon@",
    r"postmaster@",
]

# Keywords that flag an email as pure account/system noise (hard skip)
SYSTEM_SUBJECT_PATTERNS = [
    r"security alert",
    r"sign.?in attempt",
    r"2.step verification",
    r"passkey",
    r"app password",
    r"review your google account",
    r"finish setting up",
    r"partner ads setting",
    r"update.*account",
]

# Keywords in subject / body that indicate real news or announcements
NEWS_KEYWORDS = [
    # Institutional news
    "alert", "notice", "announcement", "update", "statement", "press release",
    "university", "nyu", "school", "campus",
    "closure", "closed", "suspend", "cancel", "emergency", "threat", "safety",
    "policy", "regulation", "law", "government", "official",
    # NYC / world events
    "new york", "nyc", "manhattan", "brooklyn",
    "mayor", "city", "transit", "mta", "subway",
    # Lifestyle / culture news
    "restaurant", "food", "drink", "bar", "brunch", "coffee", "chef", "menu",
    "dining", "opening", "open", "launch", "pop-up",
    "event", "concert", "show", "exhibit", "museum", "gallery", "festival",
    "nightlife", "rooftop", "weekend", "things to do",
    "guide", "roundup", "best of", "top ", "must-try", "hidden gem",
    # General news signals
    "report", "breaking", "exclusive", "develop", "happen", "occur",
]

MIN_BODY_LENGTH = 150   # skip near-empty emails


def is_relevant(email: dict) -> bool:
    """Return True if the email looks like real news or an announcement."""
    sender  = email.get("sender",  "").lower()
    subject = email.get("subject", "").lower()
    body    = email.get("body",    "")

    # Hard skip: known system sender domains
    for pattern in SKIP_SENDER_PATTERNS:
        if re.search(pattern, sender, re.I):
            return False

    # Hard skip: system / account-management subjects
    for pattern in SYSTEM_SUBJECT_PATTERNS:
        if re.search(pattern, subject, re.I):
            return False

    # Skip very short bodies
    if len(body.strip()) < MIN_BODY_LENGTH:
        return False

    # Must match at least one news keyword
    combined = (subject + " " + body[:3000]).lower()
    return any(kw in combined for kw in NEWS_KEYWORDS)


# ── WeChat article generator ───────────────────────────────────────────

SYSTEM_PROMPT = """你是「情报特刊」公众号的资深编辑，专注报道与纽约大学（NYU）及纽约相关的新闻资讯。
读者群体：在纽约或关注纽约的中文读者。

写作风格要求（参照范例）：
- 以一个简洁有力的粗体标题开头（格式：**标题**），标题准确概括核心事件
- 正文分 3-5 个段落，每段聚焦一个要点
- 语气正式但不生硬，像严肃媒体的新闻报道，不加感叹号、不使用网络语
- 时间、地点、人物、事件经过、影响、后续进展——按重要性依次呈现
- 保留英文机构名、职位名、邮箱等专有名词，其余使用简体中文
- 全文 250-400 字，适合手机阅读
- 严格只基于原文事实，不编造、不推测
- 绝对不要出现任何联系方式、求助/客服指引或热线相关内容：包括邮箱地址、电话号码、网址、"请发送邮件至…""请联系…""校方建议联系…""请登录…核实/更新手机号码""如有疑问请致电…"等。即使原文包含这些信息，也一律删除，不要改写保留。正文只报道新闻事实本身，以事实陈述自然收尾"""

USER_PROMPT_TEMPLATE = """请根据以下邮件内容，改写成一篇情报特刊风格的中文新闻报道。

邮件主题：{subject}
发件人：{sender}
发送日期：{date}

原文内容：
{body}

输出格式：
第一行：**新闻标题**
空一行
正文段落（不加任何"正文："前缀）"""


def parse_title_body(raw: str) -> tuple[str, str]:
    """Split the LLM output into (title, body) on the first blank line."""
    # Strip any stray markdown bold markers from the title
    lines = raw.strip().splitlines()
    title = lines[0].strip().lstrip("#").strip("* ").strip()
    # body starts after the first blank line
    body_lines = []
    in_body = False
    for line in lines[1:]:
        if not in_body and line.strip() == "":
            in_body = True
            continue
        if in_body:
            body_lines.append(line)
    body = "\n".join(body_lines).strip()
    return title, body


def generate_article(client: OpenAI, email: dict) -> tuple[str, str]:
    """Call OpenRouter to rewrite one email. Returns (title, body)."""
    body_excerpt = email["body"][:3000]  # stay within token budget

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": USER_PROMPT_TEMPLATE.format(
                    subject=email["subject"],
                    sender=email["sender"],
                    date=email["date"],
                    body=body_excerpt,
                ),
            },
        ],
        temperature=0.7,
        max_tokens=800,
    )
    raw = response.choices[0].message.content.strip()
    # Strip <think>...</think> reasoning blocks (Qwen chain-of-thought)
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
    return parse_title_body(raw)


# ── Main ───────────────────────────────────────────────────────────────

def main():
    # Validate API key
    if not OPENROUTER_API_KEY:
        print(
            "[!] OPENROUTER_API_KEY not set.\n"
            "    export OPENROUTER_API_KEY='sk-or-...'\n"
            "    Get a key at: https://openrouter.ai/keys\n"
        )
        sys.exit(1)

    # Load emails
    if not os.path.exists(INPUT_FILE):
        print(f"[!] {INPUT_FILE} not found. Run gmail_scraper.py first.")
        sys.exit(1)

    with open(INPUT_FILE, encoding="utf-8") as f:
        emails = json.load(f)

    print(f"[i] Loaded {len(emails)} emails from {INPUT_FILE}")

    # Filter
    relevant = [e for e in emails if is_relevant(e)]
    print(f"[i] {len(relevant)} emails passed the relevance filter")

    if not relevant:
        print("[!] No news emails found in the current inbox.")
        print("    All emails appear to be system/transactional messages.")
        sys.exit(0)

    # Generate articles
    client = OpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
    )
    results = []

    for idx, email in enumerate(relevant, 1):
        print(f"  [{idx}/{len(relevant)}] Generating: {email['subject'][:60]}")
        try:
            title, body = generate_article(client, email)
            results.append({
                "uid":           email["uid"],
                "date":          email["date"],
                "subject":       email["subject"],
                "sender":        email["sender"],
                "title":         title,
                "body":          body,
                "original_body": email["body"],
            })
        except Exception as e:
            print(f"  [!] Failed for uid={email['uid']}: {e}")

    # Save
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n[✓] Saved {len(results)} articles to {OUTPUT_FILE}")

    # Preview
    if results:
        print("\n── Article preview (first item) ──")
        first = results[0]
        print(f"  Subject : {first['subject']}")
        print(f"  Date    : {first['date']}")
        print(f"\n【标题】{first['title']}\n")
        print(first['body'])
        print()


if __name__ == "__main__":
    main()
