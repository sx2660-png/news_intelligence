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
import logging
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
ARTICLE_MAX_TOKENS = int(os.environ.get("ARTICLE_MAX_TOKENS", "2000"))
ARTICLE_FALLBACK_MODEL = os.environ.get(
    "ARTICLE_FALLBACK_MODEL",
    os.environ.get("MANUAL_VISION_MODEL", "google/gemini-2.5-flash"),
)
log = logging.getLogger(__name__)

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
- 人名必须以原文写法逐字保留：不要翻译、音译、缩写、中文化、改写姓名称谓，也不要根据常识补全名字。首次出现时直接使用原文姓名；后续如原文只写姓氏或名字，也维持原文对应的写法。
- 保留真正的专有名词：机构名、项目名、专有事件名和人名可保留英文；职位、身份、通用名词必须翻译为简体中文
- 例如：“Tokyo Governor”应写为“东京都知事”，“NYU President”应写为“纽约大学校长”，不要把职位原样留在中文标题或正文中
- 全文 250-400 字，适合手机阅读
- 严格只基于原文事实，不编造、不推测
- 绝对不要出现任何联系方式、求助/客服指引或热线相关内容：包括邮箱地址、电话号码、网址、"请发送邮件至…""请联系…""校方建议联系…""请登录…核实/更新手机号码""如有疑问请致电…"等。即使原文包含这些信息，也一律删除，不要改写保留。正文只报道新闻事实本身，以事实陈述自然收尾"""

USER_PROMPT_TEMPLATE = """请根据以下邮件内容，改写成一篇情报特刊风格的中文新闻报道。

邮件主题：{subject}
发件人：{sender}
发送日期：{date}

原文内容：
{body}

重要翻译要求：标题和正文中的职位、官职和通用身份必须使用中文。例如 Tokyo Governor 翻译为“东京都知事”，不要写成 Tokyo Governor。仅对真正的机构名、项目名和人名保留英文。所有人名必须严格沿用原文拼写和形式，不翻译、不音译、不缩写、不补全。

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
    # Models occasionally omit the required blank line. Treat the remaining
    # lines as the body instead of reporting a false empty-response failure.
    if not in_body:
        body_lines = lines[1:]
    body = "\n".join(body_lines).strip()
    return title, body


def _message_text(message) -> str:
    if message is None:
        return ""
    chunks: list[str] = []
    content = getattr(message, "content", None)
    if isinstance(content, str):
        chunks.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                chunks.append(str(part.get("text") or ""))
            else:
                chunks.append(str(getattr(part, "text", None) or part or ""))
    if not any(item.strip() for item in chunks):
        for attr in ("reasoning", "reasoning_content"):
            value = getattr(message, attr, None)
            if isinstance(value, str) and value.strip():
                chunks.append(value)
                break
    return "\n".join(item for item in chunks if item).strip()


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def _title_and_body(raw: str, fallback_title: str = "") -> tuple[str, str]:
    title, body = parse_title_body(raw)
    if title and body:
        return title, body
    if title and not body:
        return (fallback_title or title), title
    return "", ""


def _create_article_completion(client: OpenAI, *, model: str, messages: list[dict], disable_reasoning: bool):
    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": ARTICLE_MAX_TOKENS,
    }
    if disable_reasoning:
        kwargs["extra_body"] = {
            "reasoning": {
                "effort": os.environ.get("ARTICLE_REASONING_EFFORT", "none"),
                "exclude": True,
            }
        }
    try:
        return client.chat.completions.create(**kwargs)
    except Exception:
        if disable_reasoning:
            kwargs.pop("extra_body", None)
            return client.chat.completions.create(**kwargs)
        raise


def generate_article(client: OpenAI, email: dict) -> tuple[str, str]:
    """Call OpenRouter to rewrite one email. Returns (title, body).

    Qwen via OpenRouter often spends the token budget on hidden reasoning and
    returns an empty content field. Disable reasoning, retry, then fall back.
    """
    body_excerpt = str(email.get("body") or "")[:8000]
    fallback_title = str(email.get("subject") or "").strip() or "新闻速览"
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": USER_PROMPT_TEMPLATE.format(
                subject=email.get("subject") or "",
                sender=email.get("sender") or "",
                date=email.get("date") or "",
                body=body_excerpt,
            ),
        },
    ]
    models = [OPENAI_MODEL]
    if ARTICLE_FALLBACK_MODEL and ARTICLE_FALLBACK_MODEL not in models:
        models.append(ARTICLE_FALLBACK_MODEL)

    last_error: Exception | None = None
    for model in models:
        disable_reasoning = model == OPENAI_MODEL or "qwen" in model.lower()
        for attempt in range(2):
            try:
                response = _create_article_completion(
                    client,
                    model=model,
                    messages=messages,
                    disable_reasoning=disable_reasoning,
                )
            except Exception as exc:
                last_error = exc
                log.warning("Article generation failed (%s attempt %s): %s", model, attempt + 1, exc)
                continue
            choices = getattr(response, "choices", None) or []
            message = getattr(choices[0], "message", None) if choices else None
            raw = _strip_think(_message_text(message))
            if not raw:
                finish_reason = getattr(choices[0], "finish_reason", None) if choices else None
                last_error = RuntimeError("OpenRouter 未返回文章正文，请稍后重试")
                log.warning(
                    "Empty article content from %s (attempt %s, finish_reason=%s)",
                    model,
                    attempt + 1,
                    finish_reason,
                )
                continue
            title, body = _title_and_body(raw, fallback_title)
            if title and body:
                return title, body
            last_error = RuntimeError("OpenRouter 返回了内容，但无法解析出标题和正文，请重试")

    if last_error:
        raise last_error
    raise RuntimeError("OpenRouter 未返回文章正文，请稍后重试")


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
