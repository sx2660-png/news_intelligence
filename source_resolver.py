"""Resolve a single authoritative news source with Tavily and OpenRouter."""

from __future__ import annotations

import base64
import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_MAX_ATTEMPTS = 3
DEFAULT_MODEL = "qwen/qwen3.7-plus"
FALLBACK_MODELS: tuple[str, ...] = ()
MIN_CONFIDENCE = float(os.environ.get("SOURCE_MIN_CONFIDENCE", "0.75"))


def _api_key() -> str:
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def _tavily_api_key() -> str:
    return os.environ.get("TAVILY_API_KEY", "").strip()


def _request(messages: list[dict]) -> dict:
    """Ask the model to understand or judge supplied search results, never to search."""
    key = _api_key()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    payload = {
        "model": os.environ.get("SOURCE_SEARCH_MODEL", DEFAULT_MODEL),
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": int(os.environ.get("SOURCE_SEARCH_MAX_TOKENS", "600")),
        "reasoning": {"effort": os.environ.get("SOURCE_SEARCH_REASONING_EFFORT", "none"), "exclude": True},
    }
    req = Request(
        OPENROUTER_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": os.environ.get("APP_URL", "http://localhost"),
            "X-Title": "LIVELY Breaking News Studio",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        if exc.code == 403 and "not available in your region" in detail:
            requested = payload["model"]
            failures = []
            configured_fallback = os.environ.get("SOURCE_SEARCH_FALLBACK_MODEL") or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
            for fallback in (*FALLBACK_MODELS, configured_fallback):
                if fallback == requested:
                    continue
                retry_payload = dict(payload, model=fallback)
                retry_req = Request(req.full_url, data=json.dumps(retry_payload).encode("utf-8"), headers=dict(req.headers), method="POST")
                try:
                    with urlopen(retry_req, timeout=90) as response:
                        return json.loads(response.read().decode("utf-8"))
                except HTTPError as retry_exc:
                    failures.append(f"{fallback}: HTTP {retry_exc.code}")
                except URLError as retry_exc:
                    failures.append(f"{fallback}: {retry_exc.reason!r}")
            suffix = f" ({'; '.join(failures)})" if failures else ""
            raise RuntimeError(f"OpenRouter model unavailable in your region: {requested}{suffix}") from exc
        raise RuntimeError(f"OpenRouter HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"OpenRouter connection failed: {exc.reason!r}") from exc
    if data.get("error"):
        raise RuntimeError(data["error"].get("message") or "OpenRouter source analysis failed")
    return data


def _parse_response(data: dict) -> dict:
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("Source analysis returned no answer")
    content = (choices[0].get("message") or {}).get("content") or ""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, flags=re.S)
        if not match:
            raise RuntimeError("Source analysis did not return valid JSON")
        return json.loads(match.group(0))


def _tavily_search(query: str, domain_hint: str = "") -> list[dict]:
    key = _tavily_api_key()
    if not key:
        raise RuntimeError("TAVILY_API_KEY is not set")
    payload: dict = {
        "query": query[:1000],
        "topic": "general",
        "search_depth": "advanced",
        "chunks_per_source": 2,
        "max_results": 5,
        "include_raw_content": False,
        "include_answer": False,
    }
    domain = _domain(domain_hint)
    if domain:
        payload["include_domains"] = [domain]
    req = Request(
        TAVILY_SEARCH_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "news-intelligence/1.0",
        },
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(TAVILY_MAX_ATTEMPTS):
        try:
            with urlopen(req, timeout=60) as response:
                data = json.loads(response.read().decode("utf-8"))
            break
        except HTTPError as exc:
            # Retry transient gateway and rate-limit responses, but preserve
            # client errors such as invalid credentials or invalid requests.
            if exc.code not in {429, 500, 502, 503, 504} or attempt == TAVILY_MAX_ATTEMPTS - 1:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                raise RuntimeError(f"Tavily HTTP {exc.code}: {detail}") from exc
            last_error = exc
        except URLError as exc:
            last_error = exc
            if attempt == TAVILY_MAX_ATTEMPTS - 1:
                raise RuntimeError(f"Tavily connection failed after {TAVILY_MAX_ATTEMPTS} attempts: {exc.reason!r}") from exc
        time.sleep(0.5 * (2 ** attempt))
    else:
        raise RuntimeError(f"Tavily search failed: {last_error!r}")
    return [
        {"url": item.get("url", ""), "title": item.get("title", ""), "content": item.get("content", ""), "score": item.get("score", 0)}
        for item in data.get("results", [])
        if item.get("url")
    ]


def _input_parts(*, text: str, url: str, image_bytes: bytes | None, mime_type: str) -> list[dict]:
    parts: list[dict] = []
    if url:
        parts.append({"type": "text", "text": f"输入 URL：{url}"})
    if text:
        parts.append({"type": "text", "text": f"输入文本：\n{text[:12000]}"})
    if image_bytes:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}})
    return parts


def _clean_url(value: str) -> str:
    return (value or "").strip().rstrip(".,;:)]}>）】」』/")


def _domain(value: str) -> str:
    candidate = (value or "").strip().lower()
    if not candidate:
        return ""
    parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
    return parsed.netloc.removeprefix("www.")


def resolve_source(*, source_type: str, text: str = "", url: str = "", image_bytes: bytes | None = None, mime_type: str = "") -> dict:
    """Return normalized facts and one source selected only from Tavily results."""
    understanding_prompt = """你是新闻来源核验编辑。根据输入内容提取事件事实，并生成一个可用于网页搜索的精确英文或原文检索词。
只输出 JSON，不要 Markdown：
{"title":"事件标题","facts":["事实1"],"entities":["实体"],"date":"日期","search_query":"检索词","domain_hint":"官方域名或空"}"""
    understood = _parse_response(_request([{
        "role": "user",
        "content": [{"type": "text", "text": understanding_prompt}, *_input_parts(text=text, url=url, image_bytes=image_bytes, mime_type=mime_type)],
    }]))
    query = str(understood.get("search_query") or understood.get("title") or text[:300]).strip()
    if not query:
        raise RuntimeError("Unable to derive a Tavily search query from the input")
    candidates = _tavily_search(query, str(understood.get("domain_hint") or ""))
    if not candidates:
        raise ValueError("Tavily 未找到可核验的来源，请调整输入内容后重试")

    selection_prompt = """你是新闻来源核验编辑。根据事件事实，从 Tavily 返回的候选中选出唯一最权威、最直接报道该事件的 URL。
规则：只能选择候选列表中的 URL；优先官方机构页面；若没有高置信度匹配，source_url 置空。
只输出 JSON，不要 Markdown：
{"source_url":"候选中的唯一URL或空","source_title":"来源标题","source_confidence":0.0}"""
    selection = _parse_response(_request([{
        "role": "user",
        "content": f"{selection_prompt}\n\n事件：\n{json.dumps(understood, ensure_ascii=False)}\n\n候选来源：\n{json.dumps(candidates, ensure_ascii=False)}",
    }]))
    candidate_urls = {_clean_url(item["url"]): item for item in candidates}
    selected_url = _clean_url(str(selection.get("source_url") or ""))
    if selected_url not in candidate_urls:
        selected_url = ""
    try:
        confidence = float(selection.get("source_confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0
    if confidence < MIN_CONFIDENCE:
        selected_url = ""
    if selected_url:
        selected_title = str(selection.get("source_title") or candidate_urls[selected_url].get("title") or "")
    else:
        selected_title = ""
    understood.update({
        "source_url": selected_url,
        "source_title": selected_title,
        "source_confidence": confidence,
        "source_method": "tavily_search_qwen_selection",
        "source_candidates": [candidate_urls[selected_url]] if selected_url else [],
    })
    return understood
