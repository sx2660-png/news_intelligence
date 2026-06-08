# News Intelligence — Gmail Scraper & WeChat Article Generator

A two-step pipeline that logs into a Gmail inbox, scrapes all emails, filters for real news and announcements, then rewrites them as Chinese WeChat public-account articles in the style of **情报特刊** — using an LLM via OpenRouter.

---

## Project Structure

```
news_intelligence/
├── gmail_scraper.py        # Step 1: fetch all emails from Gmail via IMAP
├── content_generator.py    # Step 2: filter news emails and rewrite as 情报特刊 articles
├── emails_output.json      # Output of step 1
├── articles_output.json    # Output of step 2
├── .env.example            # Environment variable template
├── .env                    # Your local credentials (never commit this)
├── requirements.txt        # Python dependencies
└── README.md
```

---

## Prerequisites

- Python 3.10+
- A Gmail account with **2-Step Verification** enabled
- A Gmail **App Password** (not your regular login password)
  - Generate one at: https://myaccount.google.com/apppasswords
- An **OpenRouter API key** for LLM-powered article generation
  - Get one at: https://openrouter.ai/keys

---

## Setup

1. **Clone / copy** this folder to your machine.

2. **Install dependencies:**

   ```bash
   pip3 install openai
   ```

3. **Create your `.env` file** from the template:

   ```bash
   cp .env.example .env
   ```

4. **Edit `.env`** and fill in your credentials:

   ```bash
   export GMAIL_USER="your@gmail.com"
   export GMAIL_PASSWORD="xxxx xxxx xxxx xxxx"   # 16-char App Password
   export OPENROUTER_API_KEY="sk-or-..."
   ```

5. **Load the variables** into your shell:

   ```bash
   source .env
   ```

---

## Usage

### Step 1 — Scrape Gmail

```bash
python3 gmail_scraper.py
```

Connects to `imap.gmail.com` over SSL, fetches every email in INBOX, and saves to `emails_output.json`.

**Output format (`emails_output.json`):**
```json
[
  {
    "uid": "3",
    "date": "2026-04-16T16:27:53",
    "subject": "Following up on Wednesday's NYU Alert Messages",
    "sender": "\"NYU Department of Campus Safety\" <emergencymanagement@nyu.edu>",
    "body": "..."
  }
]
```

### Step 2 — Generate Articles

```bash
python3 content_generator.py
```

Reads `emails_output.json`, filters out system/transactional mail, and rewrites each news email as a 情报特刊-style Chinese article using `qwen/qwen3.7-plus` via OpenRouter.

**Output format (`articles_output.json`):**
```json
[
  {
    "uid": "3",
    "date": "2026-04-16T16:27:53",
    "subject": "Following up on Wednesday's NYU Alert Messages",
    "sender": "...",
    "title": "NYU 校园安全部提醒师生核查紧急警报系统联系方式",
    "body": "2026年4月15日下午6时，NYU Department of Campus Safety...",
    "original_body": "..."
  }
]
```

---

## Filter Logic

`content_generator.py` automatically skips:
- Google / system account notification emails
- Emails with subjects matching security alerts, 2-step verification, account setup, etc.
- Emails with fewer than 150 characters in the body

It keeps emails that contain news-relevant keywords (campus alerts, events, restaurant openings, NYC announcements, etc.).

---

## LLM Model

Default model: `qwen/qwen3.7-plus` (via OpenRouter).  
Override with an environment variable:

```bash
export OPENAI_MODEL="anthropic/claude-opus-4.8-fast"
```

---

## Notes

- **Never commit `.env`** — it contains real credentials.
- To scrape a folder other than INBOX, edit `fetch_all_emails()`:
  ```python
  fetch_all_emails(mail, mailbox="[Gmail]/All Mail")
  ```
- Large inboxes may take a few minutes. Progress is printed every 50 emails.
- Qwen's chain-of-thought `<think>` blocks are automatically stripped from output.
