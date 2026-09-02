"""NYULIVE draft publishing through the official WeChat API or ``wechat_oa_mcp``."""

from __future__ import annotations

import html
import json
import mimetypes
import os
import re
from pathlib import Path

import requests


def _post_json(url: str, payload: dict, *, timeout: int = 30) -> requests.Response:
    """POST JSON with real UTF-8 Chinese.

    ``requests``' ``json=`` helper defaults to ``ensure_ascii=True``, which
    turns 学 into ``\\u5b66``. WeChat draft APIs often store those escapes
    literally in the title/body preview.
    """
    return requests.post(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=timeout,
    )


class WeChatPublisherError(RuntimeError):
    """A user-safe explanation of a WeChat/MCP failure."""


def _setting(name: str) -> str:
    return os.environ.get(name, "").strip()


def is_configured() -> bool:
    return bool(_setting("WECHAT_APP_ID") and _setting("WECHAT_APP_SECRET"))


def _digest(text: str) -> str:
    return re.sub(r"\s+", "", text)[:54]


def _blank_cover_bytes() -> tuple[bytes, str, str]:
    """WeChat news drafts require thumb_media_id; use a plain white placeholder."""
    from io import BytesIO

    from PIL import Image

    # Recommended cover aspect is wide; keep it plain so it reads as "no cover".
    image = Image.new("RGB", (900, 383), color=(255, 255, 255))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue(), "blank_cover.jpg", "image/jpeg"


def _read_image(path: str) -> tuple[bytes, str, str]:
    file_path = Path(path)
    if not file_path.is_file():
        raise WeChatPublisherError(f"图片不存在：{file_path.name}")
    content_type = mimetypes.guess_type(file_path.name)[0] or "image/png"
    return file_path.read_bytes(), file_path.name, content_type


def _upload_material(
    base: str,
    access_token: str,
    *,
    image_bytes: bytes,
    image_name: str,
    content_type: str,
) -> str:
    """Upload permanent image material and return media_id."""
    response = requests.post(
        f"{base}/cgi-bin/material/add_material",
        params={"access_token": access_token, "type": "image"},
        files={"media": (image_name, image_bytes, content_type)},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    media_id = data.get("media_id")
    if not media_id:
        raise WeChatPublisherError(data.get("errmsg") or "微信公众号封面上传失败")
    return media_id


def _upload_thumb(base: str, access_token: str, image_path: str | None = None) -> str:
    """Upload cover material. Prefer blank placeholder when no cover is provided."""
    if image_path:
        image_bytes, image_name, content_type = _read_image(image_path)
    else:
        image_bytes, image_name, content_type = _blank_cover_bytes()
    return _upload_material(
        base,
        access_token,
        image_bytes=image_bytes,
        image_name=image_name,
        content_type=content_type,
    )


def _upload_content_image(base: str, access_token: str, image_path: str) -> str:
    """Upload an article-body image and return the WeChat CDN URL."""
    image_bytes, image_name, content_type = _read_image(image_path)
    response = requests.post(
        f"{base}/cgi-bin/media/uploadimg",
        params={"access_token": access_token},
        files={"media": (image_name, image_bytes, content_type)},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    url = data.get("url")
    if not url:
        raise WeChatPublisherError(data.get("errmsg") or f"正文图片上传失败：{Path(image_path).name}")
    return url


def _image_content_html(image_urls: list[str]) -> str:
    parts = []
    for url in image_urls:
        safe_url = html.escape(url, quote=True)
        parts.append(
            f'<p style="margin:0;padding:0;text-align:center;">'
            f'<img src="{safe_url}" style="max-width:100%;height:auto;display:block;margin:0 auto;" />'
            f"</p>"
        )
    return "".join(parts)


def _official_create_draft(
    article: dict,
    *,
    article_image_path: str,
    source_image_path: str | None = None,
) -> dict:
    """Create a draft: generated title + article/source images as body content."""
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

        # Title stays the generated Chinese title. Body is only the two images.
        # WeChat still requires thumb_media_id for news drafts, so upload a blank
        # placeholder and hide it in the article body.
        thumb_media_id = _upload_thumb(base, access_token, image_path=None)
        content_paths = [article_image_path]
        if source_image_path:
            content_paths.append(source_image_path)
        content_urls = [_upload_content_image(base, access_token, path) for path in content_paths]

        title = str(article.get("title") or "").strip()
        item = {
            "title": title,
            "author": _setting("WECHAT_AUTHOR") or "NYULIVE",
            "digest": _digest(title),
            "content": _image_content_html(content_urls),
            "thumb_media_id": thumb_media_id,
            "show_cover_pic": 0,
            "need_open_comment": int(_setting("WECHAT_OPEN_COMMENT") or "0"),
            "only_fans_can_comment": 0,
        }
        source_url = str(article.get("source_url") or "").strip()
        if source_url.startswith(("https://", "http://")):
            item["content_source_url"] = source_url
        draft_response = _post_json(
            f"{base}/cgi-bin/draft/add?access_token={access_token}",
            {"articles": [item]},
            timeout=30,
        )
        draft_response.raise_for_status()
        draft_data = draft_response.json()
        if not draft_data.get("media_id"):
            raise WeChatPublisherError(draft_data.get("errmsg") or "微信公众号草稿创建失败")
        return {
            "draft_media_id": draft_data["media_id"],
            "image_media_id": thumb_media_id,
            "content_image_urls": content_urls,
        }
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
    *,
    article_image_path: str | None = None,
    source_image_path: str | None = None,
    image_url: str | None = None,
    image_path: str | None = None,
) -> dict:
    """Create a draft through the official API (default) or MCP proxy; never publishes it."""
    if not is_configured():
        raise WeChatPublisherError("尚未配置 WECHAT_APP_ID 和 WECHAT_APP_SECRET")

    title = str(article.get("title") or "").strip()
    if not title:
        raise WeChatPublisherError("文章标题不能为空")
    if len(title) > 64:
        raise WeChatPublisherError("标题超过微信公众号草稿限制（64 个字符）")

    resolved_article_image = article_image_path or image_path
    if not resolved_article_image:
        raise WeChatPublisherError("请先生成正文图片")

    # Use the official API by default. The old MCP package depends on a
    # fixed third-party proxy which is no longer reachable.
    if _setting("WECHAT_USE_MCP_PROXY").lower() not in {"1", "true", "yes"}:
        return _official_create_draft(
            article,
            article_image_path=resolved_article_image,
            source_image_path=source_image_path,
        )

    if not image_url or not image_url.startswith(("https://", "http://")):
        raise WeChatPublisherError("MCP 模式需要可供外部读取的封面 URL，请配置 APP_URL 和 WECHAT_COVER_TOKEN")

    body = str(article.get("body") or "").strip()
    if not body:
        raise WeChatPublisherError("MCP 模式仍需要文章正文")

    get_access_token, create_mcp_draft = _mcp_tools()
    token_result = get_access_token({"AppID": _setting("WECHAT_APP_ID"), "AppSecret": _setting("WECHAT_APP_SECRET")})
    if not token_result.get("success") or not token_result.get("access_token"):
        raise WeChatPublisherError(token_result.get("error") or "未能取得微信公众号访问凭证")

    payload = {
        "access_token": token_result["access_token"],
        "image_url": image_url,
        "title": title,
        "content": _image_content_html([image_url]),
        "author": _setting("WECHAT_AUTHOR") or "NYULIVE",
        "digest": _digest(title),
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
