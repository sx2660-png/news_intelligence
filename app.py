#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Flask web interface for the Gmail news pipeline."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from openai import OpenAI

import content_generator
import gmail_scraper
import output_to_images


BASE_DIR = Path(__file__).parent.resolve()
EMAILS_FILE = BASE_DIR / gmail_scraper.OUTPUT_FILE
ARTICLES_FILE = BASE_DIR / content_generator.OUTPUT_FILE
OUTPUT_DIR = BASE_DIR / output_to_images.OUTPUT_DIR

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-news-intelligence-session-key")


def _auth_enabled() -> bool:
    return os.environ.get("APP_AUTH_ENABLED", "1").lower() not in {"0", "false", "no", "off"}


def _is_authenticated() -> bool:
    return session.get("authenticated") is True


@app.before_request
def require_login():
    if not _auth_enabled():
        return None
    allowed_endpoints = {"login", "health", "static"}
    if request.endpoint in allowed_endpoints:
        return None
    if request.path in ("/healthz",):
        return None
    if not _is_authenticated():
        return redirect(url_for("login"))
    return None


def _read_json(path: Path, default):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _parse_date(value: str) -> float:
    if not value:
        return 0.0
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except Exception:
            return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _relevant_emails() -> list[dict]:
    emails = _read_json(EMAILS_FILE, [])
    relevant = [email for email in emails if content_generator.is_relevant(email)]
    relevant.sort(key=lambda email: _parse_date(email.get("date", "")), reverse=True)
    return relevant


def _email_summary(email: dict) -> dict:
    body = (email.get("body") or "").strip().replace("\n", " ")
    return {
        "uid": email.get("uid", ""),
        "date": email.get("date", ""),
        "subject": email.get("subject", ""),
        "sender": email.get("sender", ""),
        "preview": body[:260],
    }


def _find_email(uid: str) -> dict | None:
    for email in _read_json(EMAILS_FILE, []):
        if str(email.get("uid")) == str(uid):
            return email
    return None


def _make_article(email: dict) -> dict:
    api_key = os.environ.get("OPENROUTER_API_KEY") or content_generator.OPENROUTER_API_KEY
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    client = OpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")
    title, body = content_generator.generate_article(client, email)
    return {
        "uid": email["uid"],
        "date": email["date"],
        "subject": email["subject"],
        "sender": email["sender"],
        "title": title,
        "body": body,
        "original_body": email["body"],
    }


def _save_single_article(article: dict) -> None:
    _write_json(ARTICLES_FILE, [article])


def _current_article() -> dict | None:
    articles = _read_json(ARTICLES_FILE, [])
    return articles[0] if articles else None


def _latest_image_path(result: dict) -> str:
    files = result.get("generated_files") or []
    return files[0] if files else ""


def _generate_image_for_current_article(school: str | None = None) -> dict:
    result = output_to_images.generate_images(
        input_file=str(ARTICLES_FILE),
        output_base_dir=str(OUTPUT_DIR),
        school_override=school or None,
    )
    if not result.get("success"):
        raise RuntimeError(result.get("error") or "Image generation failed")
    return {
        "output_dir": result.get("output_dir", ""),
        "image_path": _latest_image_path(result),
        "total": result.get("total", 0),
    }


@app.get("/")
def index():
    return render_template("dashboard.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not _auth_enabled():
        session["authenticated"] = True
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        expected_user = os.environ.get("APP_USERNAME", "admin")
        expected_password = os.environ.get("APP_PASSWORD", "admin123")
        if username == expected_user and password == expected_password:
            session["authenticated"] = True
            return redirect(url_for("index"))
        error = "Invalid username or password"

    return render_template("login.html", error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/health")
@app.get("/healthz")
def health():
    return jsonify({"ok": True})


@app.get("/api/state")
def state():
    articles = _read_json(ARTICLES_FILE, [])
    return jsonify(
        {
            "emails_count": len(_read_json(EMAILS_FILE, [])),
            "relevant": [_email_summary(email) for email in _relevant_emails()],
            "current_article": articles[0] if articles else None,
        }
    )


@app.post("/api/fetch")
def fetch():
    mail = gmail_scraper.connect_gmail()
    try:
        emails = gmail_scraper.fetch_all_emails(mail)
    finally:
        mail.logout()

    _write_json(EMAILS_FILE, emails)
    relevant = [_email_summary(email) for email in _relevant_emails()]
    return jsonify({"emails_count": len(emails), "relevant": relevant})


@app.post("/api/generate")
def generate():
    payload = request.get_json(silent=True) or {}
    uid = payload.get("uid")
    school = payload.get("school")

    email = _find_email(uid) if uid else (_relevant_emails()[0] if _relevant_emails() else None)
    if not email:
        return jsonify({"error": "No matching email found"}), 404

    article = _make_article(email)
    _save_single_article(article)
    image = _generate_image_for_current_article(school=school)
    return jsonify({"article": article, "image": image})


@app.post("/api/regenerate-copy")
def regenerate_copy():
    payload = request.get_json(silent=True) or {}
    uid = payload.get("uid")
    article = (_read_json(ARTICLES_FILE, []) or [None])[0]
    uid = uid or (article or {}).get("uid")
    email = _find_email(uid)
    if not email:
        return jsonify({"error": "No matching email found"}), 404

    article = _make_article(email)
    _save_single_article(article)
    return jsonify({"article": article})


@app.post("/api/regenerate-image")
def regenerate_image():
    payload = request.get_json(silent=True) or {}
    school = payload.get("school")
    if not ARTICLES_FILE.exists():
        return jsonify({"error": "No article exists yet"}), 404
    image = _generate_image_for_current_article(school=school)
    return jsonify({"image": image})


@app.post("/api/article")
def update_article():
    payload = request.get_json(silent=True) or {}
    current = _current_article()
    if not current:
        return jsonify({"error": "No article exists yet"}), 404

    title = (payload.get("title") or "").strip()
    body = (payload.get("body") or "").strip()
    if not title or not body:
        return jsonify({"error": "Title and body are required"}), 400

    current["title"] = title
    current["body"] = body
    _save_single_article(current)
    return jsonify({"article": current})


@app.get("/api/image")
def image():
    path = request.args.get("path", "")
    resolved = Path(path).resolve()
    if not path or not resolved.exists() or OUTPUT_DIR not in resolved.parents:
        return jsonify({"error": "Image not found"}), 404
    return send_file(resolved)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
