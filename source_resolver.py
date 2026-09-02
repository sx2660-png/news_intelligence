"""Resolve a single authoritative news source with Tavily and OpenRouter."""

from __future__ import annotations

import base64
import gzip
import json
import os
import re
import time
from html import unescape
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
TAVILY_MAX_ATTEMPTS = 3
DEFAULT_MODEL = "qwen/qwen3.7-plus"
FALLBACK_MODELS: tuple[str, ...] = ()
MIN_CONFIDENCE = float(os.environ.get("SOURCE_MIN_CONFIDENCE", "0.75"))
MAX_EXTRACT_CHARS = 15000
_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh;q=0.8",
}


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


def _tavily_search(query: str, domain_hint: str = "", *, include_raw_content: bool = False) -> list[dict]:
    key = _tavily_api_key()
    if not key:
        raise RuntimeError("TAVILY_API_KEY is not set")
    payload: dict = {
        "query": query[:1000],
        "topic": "general",
        "search_depth": "advanced",
        "chunks_per_source": 2,
        "max_results": 5,
        "include_raw_content": include_raw_content,
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
        {
            "url": item.get("url", ""),
            "title": item.get("title", ""),
            "content": item.get("content", ""),
            "raw_content": item.get("raw_content") or "",
            "score": item.get("score", 0),
        }
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


def _normalize_extracted_text(text: str) -> str:
    text = unescape(text or "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:MAX_EXTRACT_CHARS]


class _HTMLTextExtractor(HTMLParser):
    _SKIP_TAGS = {"script", "style", "svg", "iframe"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self._in_ld_json = False
        self.title = ""
        self.og_title = ""
        self.description = ""
        self.article_body = ""
        self.parts: list[str] = []
        self._ld_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        mapping = {key.lower(): (value or "") for key, value in attrs}
        if tag == "script":
            if "ld+json" in mapping.get("type", "").lower():
                self._in_ld_json = True
                return
            self._skip_depth += 1
            return
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "meta":
            prop = (mapping.get("property") or mapping.get("name") or "").lower()
            content = mapping.get("content", "").strip()
            if prop in {"og:title", "twitter:title"} and content:
                self.og_title = content
            elif prop in {"og:description", "twitter:description", "description"} and content:
                self.description = self.description or content
            return
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr", "article", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_ld_json:
            self._in_ld_json = False
            self._ingest_ld_json()
            self._ld_chunks = []
            return
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_ld_json:
            self._ld_chunks.append(data)
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title += text
            return
        if self._skip_depth:
            return
        self.parts.append(text + " ")

    def _ingest_ld_json(self) -> None:
        try:
            payload = json.loads("".join(self._ld_chunks))
        except json.JSONDecodeError:
            return
        items = payload if isinstance(payload, list) else [payload]
        expanded: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                expanded.extend(node for node in graph if isinstance(node, dict))
            expanded.append(item)
        for item in expanded:
            types = item.get("@type") or ""
            if isinstance(types, list):
                types = " ".join(str(value) for value in types)
            types = str(types).lower()
            if not any(token in types for token in ("article", "news", "blog", "report")):
                continue
            self.og_title = self.og_title or str(item.get("headline") or item.get("name") or "")
            self.article_body = self.article_body or str(item.get("articleBody") or item.get("description") or "")

    def result(self) -> dict:
        title = (self.og_title or self.title).strip()
        title = re.sub(r"\s+", " ", unescape(title))
        text = self.article_body or "".join(self.parts)
        if self.description and self.description not in text:
            text = f"{self.description}\n\n{text}".strip()
        return {"title": title, "text": _normalize_extracted_text(text)}


def _tavily_extract(url: str) -> dict:
    key = _tavily_api_key()
    if not key:
        raise RuntimeError("TAVILY_API_KEY is not set")
    payload = {
        "urls": [url],
        "include_images": False,
        "extract_depth": "advanced",
        "timeout": 60,
        "format": "markdown",
    }
    req = Request(
        TAVILY_EXTRACT_URL,
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
            with urlopen(req, timeout=90) as response:
                data = json.loads(response.read().decode("utf-8"))
            break
        except HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == TAVILY_MAX_ATTEMPTS - 1:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                raise RuntimeError(f"Tavily extract HTTP {exc.code}: {detail}") from exc
            last_error = exc
        except URLError as exc:
            last_error = exc
            if attempt == TAVILY_MAX_ATTEMPTS - 1:
                raise RuntimeError(f"Tavily extract failed after {TAVILY_MAX_ATTEMPTS} attempts: {exc.reason!r}") from exc
        time.sleep(0.5 * (2 ** attempt))
    else:
        raise RuntimeError(f"Tavily extract failed: {last_error!r}")
    results = data.get("results") or []
    if not results:
        failed = data.get("failed_results") or []
        detail = (failed[0].get("error") if failed else "") or "empty extract"
        raise RuntimeError(f"Tavily extract failed: {detail}")
    item = results[0]
    text = _normalize_extracted_text(str(item.get("raw_content") or ""))
    if not text:
        raise RuntimeError("Tavily extract returned empty article text")
    return {"title": "", "text": text, "url": item.get("url") or url}


def _tavily_search_content(url: str) -> dict:
    results = _tavily_search(url, include_raw_content=True)
    if not results:
        raise RuntimeError("Tavily search returned no page content")
    cleaned = _clean_url(url)
    host = _domain(url)
    match = next((item for item in results if _clean_url(item["url"]) == cleaned), None)
    if not match and host:
        match = next((item for item in results if _domain(item["url"]) == host), None)
    match = match or results[0]
    text = _normalize_extracted_text(str(match.get("raw_content") or match.get("content") or ""))
    if len(text) < 30:
        text = _normalize_extracted_text("\n\n".join(
            str(item.get("raw_content") or item.get("content") or "") for item in results
        ))
    if len(text) < 30:
        raise RuntimeError("Tavily search returned insufficient page content")
    return {"title": str(match.get("title") or ""), "text": text, "url": match.get("url") or url}


def _http_extract(url: str) -> dict:
    req = Request(url, headers=_HTTP_HEADERS, method="GET")
    try:
        with urlopen(req, timeout=30) as response:
            raw = response.read()
            content_type = (response.headers.get_content_type() or "").lower()
            charset = response.headers.get_content_charset() or "utf-8"
            encoding = (response.headers.get("Content-Encoding") or "").lower()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"读取链接失败 HTTP {exc.code}: {detail or exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(f"无法访问该链接: {exc.reason!r}") from exc
    if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    html = raw.decode(charset, errors="replace")
    if content_type and content_type not in {"text/html", "application/xhtml+xml", "text/plain", "application/json"}:
        text = _normalize_extracted_text(html)
        if not text:
            raise RuntimeError(f"该链接返回了无法读取的内容类型: {content_type}")
        return {"title": "", "text": text, "url": url}
    parser = _HTMLTextExtractor()
    parser.feed(html)
    parsed = parser.result()
    if len(parsed["text"]) < 30:
        raise RuntimeError("未能从该链接提取到文章正文")
    parsed["url"] = url
    return parsed


def extract_url_content(url: str) -> dict:
    """Fetch readable article text from a pasted URL. Never raises for empty pages."""
    cleaned = _clean_url(url)
    if not cleaned.startswith(("http://", "https://")):
        raise ValueError("来源链接必须以 http:// 或 https:// 开头")
    errors: list[str] = []
    for extractor in (_tavily_extract, _http_extract, _tavily_search_content):
        try:
            extracted = extractor(cleaned)
        except Exception as exc:
            errors.append(f"{extractor.__name__}: {exc}")
            continue
        if extracted.get("text") and len(str(extracted["text"]).strip()) >= 30:
            return extracted
        errors.append(f"{extractor.__name__}: empty text")
    return {
        "title": "",
        "text": f"来源链接：{cleaned}",
        "url": cleaned,
        "extract_errors": errors,
    }


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
