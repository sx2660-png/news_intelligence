#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Run the full news pipeline:
1) Fetch Gmail messages
2) Pick the latest relevant news emails
3) Generate Chinese WeChat copy
4) Render WeChat-style images
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from email.utils import parsedate_to_datetime

from openai import OpenAI

import content_generator
import gmail_scraper
import output_to_images


def _parse_date(value: str) -> datetime:
    if not value:
        return datetime.min
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        try:
            return parsedate_to_datetime(value)
        except Exception:
            return datetime.min


def _load_emails(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: str, data: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_emails(output_file: str) -> list[dict]:
    mail = gmail_scraper.connect_gmail()
    try:
        emails = gmail_scraper.fetch_all_emails(mail)
    finally:
        mail.logout()
        print("[✓] Logged out")

    gmail_scraper.save_results(emails, output_file)
    return emails


def generate_articles(emails: list[dict], output_file: str, max_articles: int) -> list[dict]:
    if not content_generator.OPENROUTER_API_KEY:
        print("[!] OPENROUTER_API_KEY not set.")
        sys.exit(1)

    relevant = [email for email in emails if content_generator.is_relevant(email)]
    relevant.sort(key=lambda email: _parse_date(email.get("date", "")), reverse=True)
    selected = relevant[:max_articles]

    print(f"[i] Relevant emails: {len(relevant)}")
    print(f"[i] Selected latest emails: {len(selected)}")

    if not selected:
        _save_json(output_file, [])
        print("[!] No relevant news emails found.")
        return []

    client = OpenAI(
        api_key=content_generator.OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
    )

    articles: list[dict] = []
    for idx, email in enumerate(selected, 1):
        print(f"  [{idx}/{len(selected)}] Generating: {email.get('subject', '')[:60]}")
        title, body = content_generator.generate_article(client, email)
        articles.append(
            {
                "uid": email["uid"],
                "date": email["date"],
                "subject": email["subject"],
                "sender": email["sender"],
                "title": title,
                "body": body,
                "original_body": email["body"],
            }
        )

    _save_json(output_file, articles)
    print(f"[✓] Saved {len(articles)} articles to {output_file}")
    return articles


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Gmail → article → image pipeline")
    parser.add_argument("--emails", default=gmail_scraper.OUTPUT_FILE)
    parser.add_argument("--articles", default=content_generator.OUTPUT_FILE)
    parser.add_argument("--out", default=output_to_images.OUTPUT_DIR)
    parser.add_argument("--school", help="Override school detection for image generation")
    parser.add_argument("--max-articles", type=int, default=1,
                        help="Number of latest relevant emails to generate. Default: 1")
    parser.add_argument("--skip-scrape", action="store_true",
                        help="Use existing emails JSON instead of fetching Gmail")
    parser.add_argument("--skip-images", action="store_true",
                        help="Generate article JSON only")
    args = parser.parse_args()

    if args.max_articles < 1:
        print("[!] --max-articles must be at least 1")
        sys.exit(1)

    if args.skip_scrape:
        if not os.path.exists(args.emails):
            print(f"[!] {args.emails} not found.")
            sys.exit(1)
        emails = _load_emails(args.emails)
        print(f"[i] Loaded {len(emails)} emails from {args.emails}")
    else:
        emails = fetch_emails(args.emails)

    articles = generate_articles(emails, args.articles, args.max_articles)
    if not articles or args.skip_images:
        return

    result = output_to_images.generate_images(
        input_file=args.articles,
        output_base_dir=args.out,
        school_override=args.school,
    )
    if not result["success"]:
        print(f"[!] Image generation failed: {result['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
