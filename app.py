#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Flask web interface for the Gmail news pipeline."""

from __future__ import annotations

import base64
import hashlib
import hmac
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
import source_resolver
import wechat_publisher


BASE_DIR = Path(__file__).parent.resolve()
# Railway containers are ephemeral. Set DATA_DIR to a mounted volume
# (for example /data) so history, fetch state, and generated images survive redeploys.
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

EMAILS_FILE = DATA_DIR / gmail_scraper.OUTPUT_FILE
ARTICLES_FILE = DATA_DIR / content_generator.OUTPUT_FILE
OUTPUT_DIR = DATA_DIR / output_to_images.OUTPUT_DIR
FETCH_STATE_FILE = DATA_DIR / "fetch_state.json"
HISTORY_FILE = DATA_DIR / "history.json"

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
    allowed_endpoints = {"login", "health", "static", "wechat_cover"}
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
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _make_article(email: dict, source: dict | None = None) -> dict:
    api_key = os.environ.get("OPENROUTER_API_KEY") or content_generator.OPENROUTER_API_KEY
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    client = OpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")
    generation_input = dict(email)
    if source:
        facts = "\n".join(f"- {item}" for item in (source.get("facts") or []))
        generation_input["body"] = f"{email.get('body', '')}\n\n已核验来源事实：\n{facts}"
    title, body = content_generator.generate_article(client, generation_input)
    article = {
        "uid": email["uid"],
        "date": email["date"],
        "subject": email["subject"],
        "sender": email["sender"],
        "title": title,
        "body": body,
        "original_body": email["body"],
    }
    if source:
        article.update(_source_fields(source))
    return article


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


def _make_manual_article(source_type: str, source_text: str, source_title: str = "", source_url: str = "", resolved: dict | None = None) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    source = {
        "uid": f"manual-{int(datetime.now(timezone.utc).timestamp() * 1000)}",
        "date": now,
        "subject": source_title or "手动导入",
        "sender": source_url or f"Manual {source_type}",
        "body": source_text,
    }
    article = _make_article(source, source=resolved)
    article["source_type"] = source_type
    if source_url and not article.get("source_url"):
        article["source_url"] = source_url
    return article


def _save_single_article(article: dict) -> None:
    _write_json(ARTICLES_FILE, [article])


def _current_article() -> dict | None:
    articles = _read_json(ARTICLES_FILE, [])
    return articles[0] if articles else None


def _normalize_source_url(url: str) -> str:
    return (url or "").strip().rstrip("/").split("#", 1)[0]


def _source_id(url: str) -> str:
    return hashlib.sha256(_normalize_source_url(url).encode("utf-8")).hexdigest()


def _history_id(article: dict) -> str:
    """Use article identity so every generated story has its own History item."""
    uid = str(article.get("uid") or "")
    if uid:
        return f"article-{uid}"
    source_url = _normalize_source_url(article.get("source_url", ""))
    if source_url:
        return _source_id(source_url)
    fingerprint = f"{article.get('title', '')}\n{article.get('body', '')}"
    return f"article-{hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()}"


def _source_fields(source: dict) -> dict:
    return {
        "source_id": source["source_id"],
        "source_url": source["source_url"],
        "source_title": source.get("source_title", ""),
        "source_confidence": source.get("source_confidence", 0),
        "source_method": source.get("source_method", ""),
        "source_candidates": source.get("source_candidates", []),
    }


def _source_from_manual_url(url: str, title: str = "") -> dict:
    normalized = _normalize_source_url(url)
    source_title = title or "手动添加来源"
    return {
        "source_id": _source_id(normalized),
        "source_url": normalized,
        "source_title": source_title,
        "source_confidence": 1.0,
        "source_method": "manual_source_url",
        "source_candidates": [{"url": normalized, "title": source_title}],
    }


def _resolve_source_for_record(*, source_type: str, text: str = "", url: str = "", image_bytes: bytes | None = None, mime_type: str = "") -> dict:
    resolved = source_resolver.resolve_source(
        source_type=source_type,
        text=text,
        url=url,
        image_bytes=image_bytes,
        mime_type=mime_type,
    )
    selected_url = _normalize_source_url(resolved.get("source_url", ""))
    if not selected_url:
        raise ValueError("未能高置信度确认官方来源，请检查候选结果后重试")
    resolved["source_url"] = selected_url
    resolved["source_id"] = _source_id(selected_url)
    return resolved


def _try_resolve_source(*, source_type: str, text: str = "", url: str = "", image_bytes: bytes | None = None, mime_type: str = "") -> tuple[dict | None, dict]:
    """Best-effort official source lookup. Low confidence never blocks generation."""
    empty = {"found": False, "message": "未找到合适的联网来源，建议手动添加来源。"}
    try:
        source = _resolve_source_for_record(
            source_type=source_type,
            text=text,
            url=url,
            image_bytes=image_bytes,
            mime_type=mime_type,
        )
        return source, {"found": True, "message": "已找到一个联网来源。", "url": source["source_url"]}
    except ValueError:
        return None, empty
    except Exception:
        app.logger.exception("Source lookup failed; continuing without a verified source")
        return None, empty


def _make_email_article_with_source(email: dict) -> dict:
    """Generate mail copy even when the web source cannot be verified."""
    source, source_lookup = _try_resolve_source(
        source_type="email",
        text=f"{email.get('subject', '')}\n{email.get('body', '')}",
    )
    article = _make_article(email, source=source)
    article["source_lookup"] = source_lookup
    return article


def _load_history() -> list[dict]:
    history = _read_json(HISTORY_FILE, []) or []
    return history if isinstance(history, list) else []


def _save_history(article: dict, image: dict | None = None) -> dict:
    source_url = _normalize_source_url(article.get("source_url", ""))
    now = datetime.now(timezone.utc).isoformat()
    item = dict(article)
    item["source_id"] = _history_id(article)
    item["source_url"] = source_url
    item["updated_at"] = now
    existing = _load_history()
    for index, previous in enumerate(existing):
        if previous.get("source_id") == item["source_id"] or (item.get("uid") and previous.get("uid") == item["uid"]):
            item["created_at"] = previous.get("created_at", now)
            if image:
                item["image"] = image
            elif previous.get("image"):
                item["image"] = previous["image"]
            existing[index] = item
            _write_json(HISTORY_FILE, existing)
            return item
    item["created_at"] = now
    if image:
        item["image"] = image
    existing.insert(0, item)
    _write_json(HISTORY_FILE, existing)
    return item


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
        "source_image_path": result.get("source_image_path", ""),
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
            "history": _load_history(),
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
        "history": _load_history(),
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

    article = _make_email_article_with_source(email)
    _save_single_article(article)
    image = _generate_image_for_current_article(school=school)
    article["image"] = image
    _save_single_article(article)
    _save_history(article, image=image)
    return jsonify({"article": article, "image": image, "history": _load_history()})


@app.post("/api/manual-import")
def manual_import():
    source_type = (request.form.get("source_type") or "text").strip().lower()
    source_text = (request.form.get("text") or "").strip()
    source_title = (request.form.get("title") or "").strip()
    source_url = (request.form.get("url") or "").strip()

    try:
        image_bytes = None
        mime_type = ""
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
        elif source_type not in {"text", "url"}:
            return jsonify({"error": "不支持的导入类型"}), 400

        if source_type == "url":
            if not source_url:
                return jsonify({"error": "请输入一个来源 URL"}), 400
            extracted = source_resolver.extract_url_content(source_url)
            source_text = extracted.get("text") or ""
            source_title = source_title or extracted.get("title") or ""
            if extracted.get("extract_errors"):
                app.logger.warning("URL extract fallbacks: %s", "; ".join(extracted["extract_errors"]))
        elif len(source_text) < 30:
            return jsonify({"error": "内容太短，请至少提供 30 个字符"}), 400

        # Generate from the imported article first. Source lookup is advisory
        # and must not stop copy or image generation.
        article = _make_manual_article(source_type, source_text, source_title, source_url, resolved=None)
        source, source_lookup = _try_resolve_source(
            source_type=source_type,
            text=source_text,
            url=source_url,
            image_bytes=image_bytes,
            mime_type=mime_type,
        )
        if not source and source_type == "url":
            source = _source_from_manual_url(source_url, source_title)
            source_lookup = {
                "found": True,
                "message": "未确认到高置信官方来源，已使用导入的链接。",
                "url": source["source_url"],
            }
        if source:
            article.update(_source_fields(source))
        article["source_lookup"] = source_lookup
        _save_single_article(article)
        image = _generate_image_for_current_article()
        article["image"] = image
        _save_single_article(article)
        _save_history(article, image=image)
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

    article = _make_email_article_with_source(email)
    _save_single_article(article)
    _save_history(article)
    return jsonify({"article": article, "history": _load_history()})


@app.post("/api/regenerate-image")
def regenerate_image():
    payload = request.get_json(silent=True) or {}
    school = payload.get("school")
    if not ARTICLES_FILE.exists():
        return jsonify({"error": "No article exists yet"}), 404
    image = _generate_image_for_current_article(school=school)
    current = _current_article()
    if current:
        current["image"] = image
        _save_single_article(current)
    if current:
        _save_history(current, image=image)
    return jsonify({"image": image, "history": _load_history()})


@app.post("/api/article")
def update_article():
    payload = request.get_json(silent=True) or {}
    current = _current_article()
    if not current:
        return jsonify({"error": "No article exists yet"}), 404

    title = (payload.get("title") or "").strip()
    body = (payload.get("body") or "").strip()
    source_url = (payload.get("source_url") or "").strip()
    if not title or not body:
        return jsonify({"error": "Title and body are required"}), 400

    current["title"] = title
    current["body"] = body
    if source_url:
        normalized_source_url = _normalize_source_url(source_url)
        if not normalized_source_url.startswith(("http://", "https://")):
            return jsonify({"error": "来源链接必须以 http:// 或 https:// 开头"}), 400
        source_title = current.get("source_title") or "手动添加来源"
        current.update({
            "source_id": _source_id(normalized_source_url),
            "source_url": normalized_source_url,
            "source_title": source_title,
            "source_confidence": 1.0,
            "source_method": "manual_source_url",
            "source_candidates": [{"url": normalized_source_url, "title": source_title}],
            "source_lookup": {"found": True, "message": "已手动添加来源。", "url": normalized_source_url},
        })
    _save_single_article(current)
    _save_history(current)
    return jsonify({"article": current, "history": _load_history()})


@app.post("/api/wechat/draft")
def create_wechat_draft():
    """Send the currently reviewed article to the configured account's drafts."""
    current = _current_article()
    if not current:
        return jsonify({"error": "No article exists yet"}), 404

    image_info = current.get("image") or {}
    image_path = str(image_info.get("image_path") or "")
    source_image_path = str(image_info.get("source_image_path") or "")
    resolved_image = Path(image_path).resolve() if image_path else None
    resolved_source = Path(source_image_path).resolve() if source_image_path else None

    if (
        not resolved_image
        or not resolved_image.exists()
        or OUTPUT_DIR not in resolved_image.parents
    ):
        return jsonify({"error": "请先生成正文图片"}), 422
    if (
        not resolved_source
        or not resolved_source.exists()
        or OUTPUT_DIR not in resolved_source.parents
    ):
        return jsonify({"error": "请先生成来源图片后再同步到微信草稿"}), 422

    # Official API uploads local images directly. Only the legacy MCP
    # proxy needs a public APP_URL that an external service can fetch.
    use_mcp = os.environ.get("WECHAT_USE_MCP_PROXY", "").strip().lower() in {"1", "true", "yes"}
    cover_url = None
    if use_mcp:
        app_url = os.environ.get("APP_URL", "").strip().rstrip("/")
        cover_token = os.environ.get("WECHAT_COVER_TOKEN", "").strip()
        if not app_url or not cover_token:
            return jsonify({"error": "MCP 模式请配置 APP_URL 和 WECHAT_COVER_TOKEN"}), 422
        cover_url = f"{app_url}{url_for('wechat_cover', token=cover_token)}"

    try:
        draft = wechat_publisher.create_draft(
            current,
            article_image_path=str(resolved_image),
            source_image_path=str(resolved_source),
            image_url=cover_url,
            image_path=str(resolved_image),
        )
    except wechat_publisher.WeChatPublisherError as exc:
        # Preserve the provider's actionable error (for example an IP
        # whitelist or access-token error) instead of hiding it behind a
        # generic 422 response.
        app.logger.error("WeChat draft creation rejected: %s", exc)
        return jsonify({"error": str(exc)}), 502
    except Exception:
        app.logger.exception("Creating WeChat draft failed")
        return jsonify({"error": "同步到微信公众号草稿箱失败，请检查服务日志和微信公众号 IP 白名单"}), 502

    current["wechat_draft"] = {
        **draft,
        "account": os.environ.get("WECHAT_AUTHOR", "NYULIVE"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_single_article(current)
    _save_history(current)
    return jsonify({"draft": current["wechat_draft"], "article": current, "history": _load_history()})


@app.get("/public/wechat-cover")
def wechat_cover():
    """Serve only the current cover when the MCP shared token matches."""
    expected_token = os.environ.get("WECHAT_COVER_TOKEN", "").strip()
    supplied_token = request.args.get("token", "")
    if not expected_token or not hmac.compare_digest(supplied_token, expected_token):
        return jsonify({"error": "Image not found"}), 404

    current = _current_article() or {}
    image_path = str((current.get("image") or {}).get("image_path") or "")
    resolved_image = Path(image_path).resolve()
    if not image_path or not resolved_image.exists() or OUTPUT_DIR not in resolved_image.parents:
        return jsonify({"error": "Image not found"}), 404
    return send_file(resolved_image)


@app.get("/api/history")
def history():
    return jsonify({"history": _load_history()})


@app.get("/api/history/<source_id>")
def history_detail(source_id: str):
    item = next((entry for entry in _load_history() if entry.get("source_id") == source_id), None)
    if not item:
        return jsonify({"error": "History item not found"}), 404
    return jsonify(item)


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
