import re
import json
import base64
import hashlib
import logging
from typing import Optional, Tuple, List, Dict, Any
import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

from src.handlers.base import BasePlatformHandler

logger = logging.getLogger("handlers.nekopost")

NEKOPOST_KEY = "AeyTest"
NEKOPOST_URL_REGEX = re.compile(
    r"^https?://(?:www\.)?nekopost\.net/(?:novel|original_novel|manga)/(\d+)(?:/([^/?#]+))?",
    re.IGNORECASE,
)

# In-memory cache for project info and chapter lists: project_id -> (project_dict, chapters_list)
_project_cache: Dict[int, Tuple[Dict[str, Any], List[Dict[str, Any]]]] = {}


def is_nekopost_url(url: str) -> bool:
    """Check if URL targets Nekopost novel or manga."""
    if not url:
        return False
    return bool(NEKOPOST_URL_REGEX.search(url.strip()))


def parse_nekopost_url(url: str) -> Tuple[Optional[int], Optional[str]]:
    """Extract project_id and optional chapter identifier from a Nekopost URL."""
    m = NEKOPOST_URL_REGEX.search(url.strip())
    if not m:
        return None, None
    project_id = int(m.group(1))
    chapter_part = m.group(2)
    if chapter_part and (chapter_part.startswith("#") or chapter_part.lower() in ("img", "comment")):
        chapter_part = None
    return project_id, chapter_part


def evp_bytes_to_key(password: bytes, salt: bytes, key_len: int = 32, iv_len: int = 16) -> Tuple[bytes, bytes]:
    """Derive key and IV using OpenSSL / CryptoJS EVP_BytesToKey (MD5)."""
    d = b""
    d_i = b""
    while len(d) < key_len + iv_len:
        d_i = hashlib.md5(d_i + password + salt).digest()
        d += d_i
    return d[:key_len], d[key_len : key_len + iv_len]


def decrypt_cryptojs_aes(encrypted_b64: str, password: str = NEKOPOST_KEY) -> dict:
    """Decrypt OpenSSL/CryptoJS Salted__ format ciphertext with given passphrase."""
    raw = base64.b64decode(encrypted_b64.strip())
    if len(raw) < 16 or raw[:8] != b"Salted__":
        raise ValueError("Invalid CryptoJS ciphertext format (missing 'Salted__' header)")
    salt = raw[8:16]
    ciphertext = raw[16:]
    key, iv = evp_bytes_to_key(password.encode("utf-8"), salt, 32, 16)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    pad_len = padded[-1]
    if pad_len < 1 or pad_len > 16:
        raise ValueError("Invalid PKCS7 padding on decrypted payload")
    plaintext = padded[:-pad_len].decode("utf-8")
    return json.loads(plaintext)


async def fetch_nekopost_toc_data(project_id: int) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Fetch novel details and chapter list via Nekopost detail2 API."""
    if project_id in _project_cache:
        return _project_cache[project_id]

    url = "https://www.nekopost.net/api/project/detail2"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "Referer": f"https://www.nekopost.net/novel/{project_id}",
        "Content-Type": "application/json",
        "Accept": "*/*",
    }
    payload = {"pid": project_id}

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()

    project_info = data.get("projectInfo", {})
    project = project_info.get("Project", {})
    raw_chapters = project_info.get("ListChapter", [])

    def parse_no(c: dict) -> float:
        try:
            return float(c.get("ChapterNo", 0))
        except (ValueError, TypeError):
            return 0.0

    # Sort chapters in chronological order (0.1, 1, 2, ...)
    sorted_chapters = sorted(raw_chapters, key=parse_no)

    _project_cache[project_id] = (project, sorted_chapters)
    return project, sorted_chapters


async def fetch_nekopost_chapter_content(project_id: int, chapter_id: int) -> str:
    """Fetch and decrypt chapter story HTML from Nekopost handler/cinfo."""
    url = "https://www.nekopost.net/handler/cinfo"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "Referer": f"https://www.nekopost.net/novel/{project_id}",
        "Content-Type": "application/json",
        "Accept": "*/*",
    }
    payload = {"p": project_id, "c": chapter_id}

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        ciphertext = r.text

    decrypted = decrypt_cryptojs_aes(ciphertext)
    return decrypted.get("novelContent", "")


def build_nekopost_toc_html(
    project_id: int,
    project: Dict[str, Any],
    chapters: List[Dict[str, Any]],
    base_url: str = "",
) -> str:
    """Construct an enriched HTML document containing novel metadata and chapter links."""
    novel_title = project.get("projectName") or f"Nekopost Novel {project_id}"
    author = project.get("authorName") or ""
    description = project.get("info") or ""
    origin = "https://www.nekopost.net"

    links_html = []
    embedded_chapters = []
    for idx, ch in enumerate(chapters, start=1):
        ch_no = ch.get("ChapterNo", str(idx))
        ch_name = ch.get("ChapterName") or ""
        ch_id = ch.get("ChapterID")
        ch_title = f"Chapter {ch_no} - {ch_name}".strip(" - ") if ch_name else f"Chapter {ch_no}"
        ch_url = f"{origin}/novel/{project_id}/{ch_no}"
        links_html.append(f'<a href="{ch_url}" class="chapter-link">{ch_title}</a>')
        embedded_chapters.append({
            "index": idx,
            "chapter_no": ch_no,
            "chapter_name": ch_name,
            "chapter_id": ch_id,
            "title": ch_title,
            "url": ch_url,
        })

    json_payload = {
        "platform": "nekopost",
        "project_id": project_id,
        "novel_title": novel_title,
        "author": author,
        "description": description,
        "total_chapters": len(chapters),
        "chapters": embedded_chapters,
    }

    html = f"""<!DOCTYPE html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <title>{novel_title} | Nekopost</title>
</head>
<body>
  <div class="novel-header">
    <h1 class="novel-title">{novel_title}</h1>
    <p class="novel-author">{author}</p>
    <div class="novel-description">{description}</div>
  </div>
  <div class="chapter-list">
    {''.join(links_html)}
  </div>
  <script id="__NEKOPOST_DATA__" type="application/json">
{json.dumps(json_payload, ensure_ascii=False, indent=2)}
  </script>
</body>
</html>"""
    return html


def build_nekopost_chapter_html(
    project_id: int,
    chapter_no: str,
    chapter_title: str,
    content_html: str,
    novel_title: str = "",
) -> str:
    """Construct a clean HTML document for a single chapter."""
    full_title = f"{chapter_title} - {novel_title}" if novel_title else chapter_title
    html = f"""<!DOCTYPE html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <title>{full_title} | Nekopost</title>
</head>
<body>
  <div class="chapter-container">
    <h1 class="chapter-title">{chapter_title}</h1>
    <div id="chapter-content">
      {content_html}
    </div>
  </div>
</body>
</html>"""
    return html


async def handle_nekopost_url(url: str) -> Optional[str]:
    """High-level router: given any Nekopost URL, return clean synthetic HTML for TOC or Chapter."""
    project_id, chapter_part = parse_nekopost_url(url)
    if not project_id:
        return None

    try:
        project, chapters = await fetch_nekopost_toc_data(project_id)
        novel_title = project.get("projectName") or f"Nekopost Novel {project_id}"

        # If chapter_part is present, resolve chapter
        if chapter_part:
            target_ch = None
            for ch in chapters:
                if str(ch.get("ChapterNo")) == chapter_part or str(ch.get("ChapterID")) == chapter_part:
                    target_ch = ch
                    break
            if not target_ch and chapters:
                try:
                    idx = int(float(chapter_part))
                    if 1 <= idx <= len(chapters):
                        target_ch = chapters[idx - 1]
                except ValueError:
                    pass

            if target_ch:
                chapter_id = target_ch["ChapterID"]
                ch_no = target_ch.get("ChapterNo", chapter_part)
                ch_name = target_ch.get("ChapterName") or ""
                ch_title = f"Chapter {ch_no} - {ch_name}".strip(" - ") if ch_name else f"Chapter {ch_no}"
                content_html = await fetch_nekopost_chapter_content(project_id, chapter_id)
                return build_nekopost_chapter_html(project_id, str(ch_no), ch_title, content_html, novel_title)

        return build_nekopost_toc_html(project_id, project, chapters, base_url=url)
    except Exception as e:
        logger.warning(f"Error in handle_nekopost_url for {url}: {e}")
        return None


class NekopostHandler(BasePlatformHandler):
    """Platform handler for Nekopost."""

    def matches(self, url: str) -> bool:
        return is_nekopost_url(url)

    async def handle(self, url: str, obscura_client: Optional[Any] = None) -> Optional[str]:
        return await handle_nekopost_url(url)
