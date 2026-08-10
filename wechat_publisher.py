"""NYULIVE draft publishing through the ``wechat_oa_mcp`` package."""

from __future__ import annotations

import html
import os
import re


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


def _mcp_tools():
    try:
        from wechat_oa_mcp import WeChat_create_draft, WeChat_get_access_token
    except ImportError as exc:
        raise WeChatPublisherError("未安装 wechat_oa_mcp；请重新部署 Railway 服务") from exc
    return WeChat_get_access_token, WeChat_create_draft


def create_draft(article: dict, image_url: str) -> dict:
    """Create a draft through the selected MCP package; never publishes it."""
    if not is_configured():
        raise WeChatPublisherError("尚未配置 WECHAT_APP_ID 和 WECHAT_APP_SECRET")
    if not image_url.startswith(("https://", "http://")):
        raise WeChatPublisherError("缺少可供公众号读取的封面 URL")

    title = str(article.get("title") or "").strip()
    body = str(article.get("body") or "").strip()
    if not title or not body:
        raise WeChatPublisherError("文章标题和正文不能为空")
    if len(title) > 64:
        raise WeChatPublisherError("标题超过微信公众号草稿限制（64 个字符）")

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
