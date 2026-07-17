#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Flask web interface for the Gmail news pipeline."""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone
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
FETCH_STATE_FILE = BASE_DIR / "fetch_state.json"

# Fallback window (days) used the very first time we fetch, before any
# last-fetch timestamp has been recorded.
DEFAULT_FETCH_DAYS = 7
MAX_MANUAL_IMAGE_BYTES = 10 * 1024 * 1024

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


def _load_fetch_state() -> dict:
    state = _read_json(FETCH_STATE_FILE, {}) or {}
    state.setdefault("last_fetch", None)
    state.setdefault("archived_uids", [])
    state.setdefault("email_labels", {})
    return state


def _save_fetch_state(state: dict) -> None:
    _write_json(FETCH_STATE_FILE, state)


def _archived_uids() -> set[str]:
    return {str(uid) for uid in _load_fetch_state().get("archived_uids", [])}


def _email_labels(uid: str) -> list[str]:
    labels = _load_fetch_state().get("email_labels", {}).get(str(uid), [])
    return sorted({str(label) for label in labels if str(label).strip()})


def _archive_uid(uid: str, label: str | None = None) -> None:
    state = _load_fetch_state()
    archived = {str(u) for u in state.get("archived_uids", [])}
    archived.add(str(uid))
    state["archived_uids"] = sorted(archived)

    if label:
        email_labels = state.setdefault("email_labels", {})
        labels = {str(item) for item in email_labels.get(str(uid), [])}
        labels.add(label)
        email_labels[str(uid)] = sorted(labels)

    _save_fetch_state(state)


def _restore_uid(uid: str) -> None:
    state = _load_fetch_state()
    archived = {str(u) for u in state.get("archived_uids", [])}
    archived.discard(str(uid))
    state["archived_uids"] = sorted(archived)

    email_labels = state.setdefault("email_labels", {})
    labels = {str(item) for item in email_labels.get(str(uid), [])}
    labels.discard("processed")
    if labels:
        email_labels[str(uid)] = sorted(labels)
    else:
        email_labels.pop(str(uid), None)

    _save_fetch_state(state)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _merge_emails(existing: list[dict], fetched: list[dict]) -> list[dict]:
    """Merge freshly fetched emails into the cached list, keyed by uid.

    Newly fetched versions overwrite older cached copies of the same uid.
    Archived emails are kept in the cache (soft delete) so they can be
    restored later; they are simply hidden from the relevant list.
    """
    by_uid: dict[str, dict] = {}
    for email in existing:
        by_uid[str(email.get("uid"))] = email
    for email in fetched:
        by_uid[str(email.get("uid"))] = email
    return list(by_uid.values())


def _archived_emails() -> list[dict]:
    archived = _archived_uids()
    emails = _read_json(EMAILS_FILE, [])
    matched = [e for e in emails if str(e.get("uid")) in archived]
    matched.sort(key=lambda e: _parse_date(e.get("date", "")), reverse=True)
    return matched


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
    archived = _archived_uids()
    relevant = [
        email
        for email in emails
        if str(email.get("uid")) not in archived and content_generator.is_relevant(email)
    ]
    relevant.sort(key=lambda email: _parse_date(email.get("date", "")), reverse=True)
    return relevant


def _email_summary(email: dict) -> dict:
    uid = str(email.get("uid", ""))
    body = (email.get("body") or "").strip().replace("\n", " ")
    return {
        "uid": uid,
        "date": email.get("date", ""),
        "subject": email.get("subject", ""),
        "sender": email.get("sender", ""),
        "preview": body[:260],
        "labels": _email_labels(uid),
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


def _extract_image_text(image_bytes: bytes, mime_type: str) -> str:
    api_key = os.environ.get("OPENROUTER_API_KEY") or content_generator.OPENROUTER_API_KEY
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    client = OpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")
    encoded = base64.b64encode(image_bytes).decode("ascii")
    response = client.chat.completions.create(
        model=os.environ.get("MANUAL_VISION_MODEL", "google/gemini-2.5-flash"),
        messages=[{"role": "user", "content": [
            {"type": "text", "text": "请完整提取这张图片中的新闻事实和可见文字。不要推测，不要改写，不要描述界面。"},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}},
        ]}],
        temperature=0.1,
        max_tokens=1800,
    )
    return (response.choices[0].message.content or "").strip()


def _make_manual_article(source_type: str, source_text: str, source_title: str = "", source_url: str = "") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    source = {
        "uid": f"manual-{int(datetime.now(timezone.utc).timestamp() * 1000)}",
        "date": now,
        "subject": source_title or "手动导入",
        "sender": source_url or f"Manual {source_type}",
        "body": source_text,
    }
    article = _make_article(source)
    article["source_type"] = source_type
    article["source_url"] = source_url
    return article


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
            "archived": [_email_summary(email) for email in _archived_emails()],
            "current_article": articles[0] if articles else None,
            "last_fetch": _load_fetch_state().get("last_fetch"),
        }
    )


@app.post("/api/fetch")
def fetch():
    payload = request.get_json(silent=True) or {}
    state = _load_fetch_state()

    now = datetime.now(timezone.utc)

    # Resolve the [since, before) window. Day granularity; the client may
    # override, otherwise we default from the last fetch to now.
    since = _parse_iso(payload.get("since"))
    if since is None:
        since = _parse_iso(state.get("last_fetch"))
    if since is None:
        since = now - timedelta(days=DEFAULT_FETCH_DAYS)

    before = _parse_iso(payload.get("before"))

    mail = gmail_scraper.connect_gmail()
    try:
        # Add a one-day pad on `before` because IMAP BEFORE is exclusive and
        # day-granular, so "now" should still include today's mail.
        fetched = gmail_scraper.fetch_emails(
            mail,
            since=since,
            before=(before + timedelta(days=1)) if before else None,
        )
    finally:
        mail.logout()

    existing = _read_json(EMAILS_FILE, [])
    existing_uids = {str(email.get("uid")) for email in existing}
    merged = _merge_emails(existing, fetched)
    _write_json(EMAILS_FILE, merged)

    state["last_fetch"] = now.isoformat()
    _save_fetch_state(state)

    relevant_emails = _relevant_emails()
    relevant = [_email_summary(email) for email in relevant_emails]
    new_relevant = [
        _email_summary(email)
        for email in relevant_emails
        if str(email.get("uid")) not in existing_uids
    ]
    return jsonify(
        {
            "emails_count": len(merged),
            "fetched_count": len(fetched),
            "relevant": relevant,
            "new_relevant": new_relevant,
            "last_fetch": state["last_fetch"],
        }
    )


@app.get("/api/email/<uid>")
def email_detail(uid: str):
    email = _find_email(uid)
    if not email:
        return jsonify({"error": "Email not found"}), 404
    return jsonify(
        {
            "uid": email.get("uid", ""),
            "date": email.get("date", ""),
            "subject": email.get("subject", ""),
            "sender": email.get("sender", ""),
            "body": email.get("body", ""),
        }
    )


def _archive_response() -> dict:
    return {
        "emails_count": len(_read_json(EMAILS_FILE, [])),
        "relevant": [_email_summary(email) for email in _relevant_emails()],
        "archived": [_email_summary(email) for email in _archived_emails()],
    }


@app.post("/api/archive")
def archive_email():
    """Soft-delete: flag the uid as archived. The email data is kept so it
    can be restored later."""
    payload = request.get_json(silent=True) or {}
    uid = str(payload.get("uid") or "").strip()
    if not uid:
        return jsonify({"error": "uid is required"}), 400

    label = (payload.get("label") or "").strip() or None
    _archive_uid(uid, label=label)

    return jsonify(_archive_response())


@app.post("/api/restore")
def restore_email():
    """Undo a soft delete: remove the uid from the archived set."""
    payload = request.get_json(silent=True) or {}
    uid = str(payload.get("uid") or "").strip()
    if not uid:
        return jsonify({"error": "uid is required"}), 400

    _restore_uid(uid)

    return jsonify(_archive_response())


@app.post("/api/archive-processed")
def archive_processed_email():
    payload = request.get_json(silent=True) or {}
    uid = str(payload.get("uid") or "").strip()
    if not uid:
        return jsonify({"error": "uid is required"}), 400
    if not _find_email(uid):
        return jsonify({"error": "Email not found"}), 404

    _archive_uid(uid, label="processed")
    return jsonify(_archive_response())


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


@app.post("/api/manual-import")
def manual_import():
    source_type = (request.form.get("source_type") or "text").strip().lower()
    source_text = (request.form.get("text") or "").strip()
    source_title = (request.form.get("title") or "").strip()
    source_url = ""

    try:
        if source_type == "image":
            upload = request.files.get("image")
            if not upload or not upload.filename:
                return jsonify({"error": "请选择一张图片"}), 400
            mime_type = (upload.mimetype or "").lower()
            if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
                return jsonify({"error": "仅支持 JPG、PNG 或 WebP 图片"}), 400
            image_bytes = upload.read(MAX_MANUAL_IMAGE_BYTES + 1)
            if len(image_bytes) > MAX_MANUAL_IMAGE_BYTES:
                return jsonify({"error": "图片不能超过 10 MB"}), 400
            source_text = _extract_image_text(image_bytes, mime_type)
        elif source_type != "text":
            return jsonify({"error": "不支持的导入类型"}), 400

        if len(source_text) < 30:
            return jsonify({"error": "内容太短，请至少提供 30 个字符"}), 400

        article = _make_manual_article(source_type, source_text, source_title, source_url)
        _save_single_article(article)
        image = _generate_image_for_current_article()
        return jsonify({"article": article, "image": image, "extracted_text": source_text})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 422
    except Exception as exc:
        app.logger.exception("Manual import failed")
        return jsonify({"error": f"导入失败：{exc}"}), 502


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


@app.get("/api/image/download")
def image_download():
    path = request.args.get("path", "")
    resolved = Path(path).resolve()
    if not path or not resolved.exists() or OUTPUT_DIR not in resolved.parents:
        return jsonify({"error": "Image not found"}), 404
    return send_file(resolved, as_attachment=True, download_name=resolved.name)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
