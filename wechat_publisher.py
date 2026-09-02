"""NYULIVE draft publishing through the official WeChat API or ``wechat_oa_mcp``."""

from __future__ import annotations

import html
import mimetypes
import os
import re
from pathlib import Path

import requests


class WeChatPublisherError(RuntimeError):
    """A user-safe explanation of a WeChat/MCP failure."""


def _setting(name: str) -> str:
    return os.environ.get(name, "").strip()


def is_configured() -> bool:
    return bool(_setting("WECHAT_APP_ID") and _setting("WECHAT_APP_SECRET"))


def _html_content(text: str) -> str:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text.strip()) if part.strip()]
    return "".join(f"<p>{html.escape(paragraph).replace(chr(10), '<br>')}</p>" for paragraph in paragraphs)


def _digest(text: str) -> str:
    return re.sub(r"\s+", "", text)[:54]


def _load_cover_bytes(image_path: str | None = None, image_url: str | None = None) -> tuple[bytes, str, str]:
    """Return (bytes, filename, content_type) from a local file, or fall back to URL."""
    if image_path:
        path = Path(image_path)
        if not path.is_file():
            raise WeChatPublisherError("本地封面图不存在，请先重新生成封面")
        content_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        return path.read_bytes(), path.name or "cover.jpg", content_type

    if image_url and image_url.startswith(("https://", "http://")):
        image_response = requests.get(image_url, timeout=30)
        image_response.raise_for_status()
        content_type = image_response.headers.get("Content-Type", "image/jpeg")
        return image_response.content, "cover.jpg", content_type

    raise WeChatPublisherError("缺少封面图文件")


def _official_create_draft(
    article: dict,
    image_path: str | None = None,
    image_url: str | None = None,
) -> dict:
    """Create a draft through WeChat's official API, without the old MCP proxy."""
    base = _setting("WECHAT_API_BASE_URL") or "https://api.weixin.qq.com"
    try:
        token_response = requests.get(
            f"{base}/cgi-bin/token",
            params={
                "grant_type": "client_credential",
                "appid": _setting("WECHAT_APP_ID"),
                "secret": _setting("WECHAT_APP_SECRET"),
            },
            timeout=20,
        )
        token_response.raise_for_status()
        token_data = token_response.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise WeChatPublisherError(token_data.get("errmsg") or "未能取得微信公众号访问凭证")

        # Prefer a local file. Fetching our own Railway public URL from the
        # same gunicorn worker deadlocks and times out.
        image_bytes, image_name, content_type = _load_cover_bytes(image_path=image_path, image_url=image_url)
        upload_response = requests.post(
            f"{base}/cgi-bin/material/add_material",
            params={"access_token": access_token},
            data={"type": "image"},
            files={"media": (image_name, image_bytes, content_type)},
            timeout=60,
        )
        upload_response.raise_for_status()
        upload_data = upload_response.json()
        image_media_id = upload_data.get("media_id")
        if not image_media_id:
            raise WeChatPublisherError(upload_data.get("errmsg") or "微信公众号封面上传失败")

        title = str(article.get("title") or "").strip()
        body = str(article.get("body") or "").strip()
        item = {
            "title": title,
            "author": _setting("WECHAT_AUTHOR") or "NYULIVE",
            "digest": _digest(body),
            "content": _html_content(body),
            "thumb_media_id": image_media_id,
            "show_cover_pic": 1,
            "need_open_comment": int(_setting("WECHAT_OPEN_COMMENT") or "0"),
            "only_fans_can_comment": 0,
        }
        source_url = str(article.get("source_url") or "").strip()
        if source_url.startswith(("https://", "http://")):
            item["content_source_url"] = source_url
        draft_response = requests.post(
            f"{base}/cgi-bin/draft/add",
            params={"access_token": access_token},
            json={"articles": [item]},
            timeout=30,
        )
        draft_response.raise_for_status()
        draft_data = draft_response.json()
        if not draft_data.get("media_id"):
            raise WeChatPublisherError(draft_data.get("errmsg") or "微信公众号草稿创建失败")
        return {"draft_media_id": draft_data["media_id"], "image_media_id": image_media_id}
    except WeChatPublisherError:
        raise
    except requests.RequestException as exc:
        raise WeChatPublisherError(f"微信公众号接口请求失败：{exc}") from exc


def _mcp_tools():
    try:
        from wechat_oa_mcp import WeChat_create_draft, WeChat_get_access_token
    except ImportError as exc:
        raise WeChatPublisherError("未安装 wechat_oa_mcp；请重新部署 Railway 服务") from exc
    return WeChat_get_access_token, WeChat_create_draft


def create_draft(
    article: dict,
    image_url: str | None = None,
    image_path: str | None = None,
) -> dict:
    """Create a draft through the official API (default) or MCP proxy; never publishes it."""
    if not is_configured():
        raise WeChatPublisherError("尚未配置 WECHAT_APP_ID 和 WECHAT_APP_SECRET")

    title = str(article.get("title") or "").strip()
    body = str(article.get("body") or "").strip()
    if not title or not body:
        raise WeChatPublisherError("文章标题和正文不能为空")
    if len(title) > 64:
        raise WeChatPublisherError("标题超过微信公众号草稿限制（64 个字符）")

    # Use the official API by default. The old MCP package depends on a
    # fixed third-party proxy which is no longer reachable.
    if _setting("WECHAT_USE_MCP_PROXY").lower() not in {"1", "true", "yes"}:
        return _official_create_draft(article, image_path=image_path, image_url=image_url)

    if not image_url or not image_url.startswith(("https://", "http://")):
        raise WeChatPublisherError("MCP 模式需要可供外部读取的封面 URL，请配置 APP_URL 和 WECHAT_COVER_TOKEN")

    get_access_token, create_mcp_draft = _mcp_tools()
    token_result = get_access_token({"AppID": _setting("WECHAT_APP_ID"), "AppSecret": _setting("WECHAT_APP_SECRET")})
    if not token_result.get("success") or not token_result.get("access_token"):
        raise WeChatPublisherError(token_result.get("error") or "未能取得微信公众号访问凭证")

    payload = {
        "access_token": token_result["access_token"],
        "image_url": image_url,
        "title": title,
        "content": _html_content(body),
        "author": _setting("WECHAT_AUTHOR") or "NYULIVE",
        "digest": _digest(body),
        "need_open_comment": int(_setting("WECHAT_OPEN_COMMENT") or "0"),
    }
    source_url = str(article.get("source_url") or "").strip()
    if source_url.startswith(("https://", "http://")):
        payload["content_source_url"] = source_url

    draft_result = create_mcp_draft(payload)
    if not draft_result.get("success") or not draft_result.get("draft_media_id"):
        raise WeChatPublisherError(draft_result.get("error") or "微信公众号草稿创建失败")
    return {
        "draft_media_id": draft_result["draft_media_id"],
        "image_media_id": draft_result.get("image_media_id", ""),
    }
