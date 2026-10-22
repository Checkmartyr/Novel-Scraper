"""Sanitized recipes for API exchanges witnessed while browsing a novel site.

This module describes and validates replayable requests; transport integration is
owned by the retrieval layer. Credentials are intentionally never recipe data.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_SECRET_KEY = re.compile(r"(?:authorization|cookie|token|secret|password|passwd|api[-_]?key|session|credential|signature|sig)", re.I)
_SENSITIVE_VALUE = re.compile(r"(?:bearer\s+\S+|eyJ[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]+|-----BEGIN [A-Z ]*PRIVATE KEY-----)", re.I)


def _secret_value(value: Any) -> bool:
    return isinstance(value, str) and bool(_SENSITIVE_VALUE.search(value))


def _safe_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("API recipe endpoint must be an absolute HTTP(S) URL")
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _SECRET_KEY.search(k) and not _secret_value(v)]
    # User-info is never useful recipe data and commonly contains credentials.
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, host, parts.path, urlencode(pairs, doseq=True), ""))


def _safe_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _safe_json(v) for k, v in value.items() if not _SECRET_KEY.search(str(k)) and not _secret_value(v)}
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    if _secret_value(value):
        return None
    return value


class ApiRecipe(BaseModel):
    """Replayable, credential-free description of one observed API request."""
    model_config = ConfigDict(extra="forbid")
    method: str
    endpoint: str
    request_body: Any = None
    request_headers: dict[str, str] = Field(default_factory=dict)
    response_mapping: dict[str, str] = Field(default_factory=dict)
    representation: str = "json"
    page_evidence: str
    trusted_read_only: bool = False

    @field_validator("method", mode="before")
    @classmethod
    def uppercase_method(cls, value):
        return str(value).upper()

    @field_validator("endpoint")
    @classmethod
    def safe_endpoint(cls, value):
        clean = _safe_url(value)
        if clean != value:
            raise ValueError("endpoint contains secret-bearing request values")
        return value

    @field_validator("page_evidence")
    @classmethod
    def safe_evidence(cls, value):
        clean = _safe_url(value)
        if clean != value:
            raise ValueError("page evidence contains secret-bearing request values")
        return value

    @field_validator("request_body", mode="before")
    @classmethod
    def clean_body(cls, value):
        clean = _safe_json(value)
        if clean != value:
            raise ValueError("request body contains secret-bearing values")
        return value

    @field_validator("request_headers", mode="before")
    @classmethod
    def clean_headers(cls, value):
        value = value or {}
        allowed = {"accept", "content-type"}
        cleaned = {str(k): str(v) for k, v in value.items()
                   if str(k).lower() in allowed and not _secret_value(v)}
        if cleaned != value:
            raise ValueError("request headers must omit credentials and unrelated headers")
        return value

    @model_validator(mode="after")
    def validate_recipe(self):
        if self.method not in {"GET", "POST"}:
            raise ValueError("unsupported API recipe method")
        if not self.response_mapping:
            raise ValueError("API recipe requires an observed response mapping")
        if any(_SECRET_KEY.search(str(key)) or _SECRET_KEY.search(str(path))
               for key, path in self.response_mapping.items()):
            raise ValueError("response mappings cannot select credential fields")
        if self.method == "POST" and not self.trusted_read_only:
            raise ValueError("POST recipe requires a trusted read-only declaration")
        if self.representation not in {"json", "text", "paragraphs", "html"}:
            raise ValueError("unsupported API response representation")
        return self


def graphql_query_is_replayable(source: str) -> bool:
    """Accept one explicit or shorthand GraphQL query; reject unsafe syntax."""
    if not isinstance(source, str) or not source.strip():
        return False
    # Tokenize comments, quoted strings (including escaped quotes), names, and
    # punctuation so keywords inside strings/comments do not affect validation.
    token_re = re.compile(r'\s+|#[^\n]*|"(?:\\.|[^"\\])*"|\.\.\.|[_A-Za-z][_0-9A-Za-z]*|[!$():=@\[\]{|},.]|-?\d+(?:\.\d+)?')
    tokens, pos = [], 0
    while pos < len(source):
        match = token_re.match(source, pos)
        if not match:
            return False
        token = match.group(0)
        pos = match.end()
        if token.isspace() or token.startswith("#"):
            continue
        tokens.append(token)
    names = [token for token in tokens if re.fullmatch(r"[_A-Za-z][_0-9A-Za-z]*", token)]
    if any(name in {"mutation", "subscription", "fragment"} for name in names):
        return False
    if any(token == "..." for token in tokens):
        return False
    operations = [i for i, token in enumerate(tokens) if token in {"query", "mutation", "subscription"}]
    if operations:
        if len(operations) != 1 or operations[0] != 0 or tokens[operations[0]] != "query":
            return False
        # No second top-level operation is accepted; mutation/subscription was
        # already rejected even if lexically nested in an unusual position.
    elif not tokens or tokens[0] != "{":
        return False
    # Persisted-only requests have no query source and are rejected by caller.
    depth = 0
    roots = 0
    for token in tokens:
        if token == "{":
            if depth == 0:
                roots += 1
                if roots > 1:
                    return False
            depth += 1
        elif token == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def capture_api_recipe(*, method: str, url: str, headers: Optional[dict] = None,
                       response_body: Any, page_url: str,
                       request_body: Any = None,
                       mapping: Optional[dict[str, str]] = None,
                       representation: str = "json",
                       trusted_read_only: bool = False) -> Optional[ApiRecipe]:
    """Build a safe recipe only from an observed, supported exchange."""
    method = (method or "GET").upper()
    if method not in {"GET", "POST"}:
        return None
    if method == "POST" and not trusted_read_only:
        return None
    body = request_body
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (ValueError, TypeError):
            return None
    if method == "POST":
        if isinstance(body, list) or not isinstance(body, dict):
            return None
        query = body.get("query")
        if query is not None:
            if not isinstance(query, str) or not graphql_query_is_replayable(query):
                return None
            if "variables" in body and not isinstance(body["variables"], dict):
                return None
            if "extensions" in body:  # persisted/opaque query conventions are not replayed
                return None
        elif "variables" in body or "extensions" in body:
            return None
    try:
        endpoint = _safe_url(url)
        evidence = _safe_url(page_url)
        safe_body = _safe_json(body)
        if method == "POST" and safe_body != body:
            return None
        safe_headers = {str(k): str(v) for k, v in (headers or {}).items()
                        if str(k).lower() in {"accept", "content-type"} and not _secret_value(v)}
        clean_response = _safe_json(response_body)
        if not mapping:
            return None
        if isinstance(clean_response, dict) and clean_response.get("errors") and not clean_response.get("data"):
            return None
        return ApiRecipe(method=method, endpoint=endpoint, request_body=safe_body,
                         request_headers=safe_headers, response_mapping=mapping or {},
                         representation=representation, page_evidence=evidence,
                         trusted_read_only=bool(trusted_read_only and method == "POST"))
    except (ValueError, TypeError):
        return None


def sanitize_api_request(*, method: str, url: str, headers: Optional[dict] = None,
                        request_body: Any = None, page_url: str,
                        trusted_read_only: bool = False) -> Optional[dict[str, Any]]:
    """Return only the safe, replayable request fields for a witnessed call."""
    probe = capture_api_recipe(method=method, url=url, headers=headers,
        request_body=request_body, response_body={}, page_url=page_url,
        mapping={"observed": ""}, trusted_read_only=trusted_read_only)
    if not probe:
        return None
    return {"method": probe.method, "url": probe.endpoint, "body": probe.request_body,
            "headers": probe.request_headers, "source_page_url": probe.page_evidence,
            "trusted_read_only": probe.trusted_read_only}


def _path_value(data: Any, path: str) -> Any:
    current = data
    for part in path.split(".") if path else []:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current if path else data


def render_api_chapter(value: Any, *, representation: str) -> str:
    """Render supported API chapter bodies without losing paragraph order."""
    if representation == "text":
        if not isinstance(value, str):
            raise ValueError("text representation must be a string")
        return value.strip()
    if representation == "paragraphs":
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError("paragraph representation must be an ordered string array")
        from html import escape
        return "\n\n".join(escape(item.strip(), quote=False) for item in value if item.strip())
    if representation == "html":
        if not isinstance(value, str) or not value.strip():
            raise ValueError("HTML representation must be non-empty HTML")
        soup = BeautifulSoup(value, "lxml")
        if soup.find(["script", "style", "iframe", "object", "embed", "form"]):
            raise ValueError("HTML contains active or form content")
        blocks = []
        for node in soup.select("p,div,li,blockquote,h1,h2,h3,h4,h5,h6"):
            if node.find_parent(["p", "div", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"]):
                continue
            if node.find(["p", "div", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"]):
                continue
            text = node.get_text(" ", strip=True)
            if text:
                blocks.append(text)
        if not blocks:
            text = soup.get_text(" ", strip=True)
            if text:
                blocks = [text]
        return "\n\n".join(blocks)
    raise ValueError("unsupported API chapter representation")


def replay_api_chapter(recipe: ApiRecipe, response_body: Any) -> dict[str, Any]:
    """Apply captured response mappings and return fidelity-preserving fields."""
    if not isinstance(response_body, (dict, list)):
        raise ValueError("API response must be structured data")
    if isinstance(response_body, dict) and response_body.get("errors") and not response_body.get("data"):
        raise ValueError("GraphQL returned errors without usable data")
    title = _path_value(response_body, recipe.response_mapping.get("title", ""))
    content = _path_value(response_body, recipe.response_mapping.get("content", ""))
    if isinstance(title, str):
        title = title.strip()
    if not isinstance(title, str) or not title or content is None:
        raise ValueError("API response does not match the observed chapter mapping")
    return {"title": title, "content": render_api_chapter(content, representation=recipe.representation),
            "partial": bool(isinstance(response_body, dict) and response_body.get("errors")),
            "provenance": {"kind": "api", "endpoint": recipe.endpoint, "page_evidence": recipe.page_evidence}}
