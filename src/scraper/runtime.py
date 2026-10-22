"""Small shared contracts for bounded extraction and crash-safe checkpoints."""
import hashlib
import json
import os
import re
import math
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from bs4 import BeautifulSoup


@dataclass(frozen=True)
class ScraperLimits:
    toc_cycles: int = 8
    review_iterations: int = 6
    repair_attempts: int = 3
    discovery_seconds: float = 600
    toc_pages: int = 1000
    browser_pages: int = 2
    request_attempts: int = 3
    no_progress_attempts: int = 2
    cleanup_seconds: float = 5
    candidate_limit: int = 6
    visual_attempts: int = 1

    @classmethod
    def from_env(cls):
        mode = os.getenv("NOVEL_SCRAPER_RECOVERY_MODE", "standard")
        if mode not in {"standard", "maximum"}:
            raise ValueError("NOVEL_SCRAPER_RECOVERY_MODE must be standard or maximum")
        values = ({"discovery_seconds": 900, "toc_cycles": 16, "repair_attempts": 5,
                   "candidate_limit": 10, "visual_attempts": 2} if mode == "maximum" else {})
        for name, field in cls.__dataclass_fields__.items():
            value = os.getenv(f"NOVEL_SCRAPER_{name.upper()}")
            if value is not None:
                parsed = float(value) if isinstance(field.default, float) or name == "discovery_seconds" else int(value)
                if not math.isfinite(parsed) or parsed <= 0:
                    raise ValueError(f"NOVEL_SCRAPER_{name.upper()} must be positive")
                values[name] = parsed
        return cls(**values)


def atomic_write(path: Path, content: str, overwrite: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".scraper-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        elif os.name == "nt":
            os.rename(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path: Path, value) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2))


def safe_error(error) -> str:
    message = str(error) or type(error).__name__
    for key, value in os.environ.items():
        if key.upper().endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")) and len(value) > 5:
            message = message.replace(value, "[redacted]")
    return message


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(query), ""))


def fingerprint(html: str, *, page_analysis=None) -> str:
    if page_analysis is None:
        soup = BeautifulSoup(html, "lxml")
        matches = soup.select("h1, h2, article, main, div[id], div[class], section")
    else:
        matches = page_analysis.select("h1, h2, article, main, div[id], div[class], section")
    structure = [(tag.name, re.sub(r"\d+", "{id}", tag.get("id", "")), sorted(tag.get("class", [])))
                 for tag in matches]
    return hashlib.sha256(json.dumps(structure, sort_keys=True).encode()).hexdigest()[:16]


def belongs_to_observed_novel(source: str, candidate: str) -> bool:
    """Reject conflicting route identities; keep ambiguous layouts for later validation."""
    origin, target = urlsplit(source), urlsplit(candidate)
    from src.scraper.recovery import current_scope
    scope = current_scope()
    if target.scheme in {"http", "https"} and scope and scope.allows(source, candidate):
        return True
    if target.scheme not in {"http", "https"} or (target.hostname or "").lower().removeprefix("www.") != (origin.hostname or "").lower().removeprefix("www."):
        return False
    source_parts, target_parts = origin.path.strip("/").split("/"), target.path.strip("/").split("/")
    if len(source_parts) >= 2 and source_parts[0] in {"novel", "book", "works", "fiction", "series", "b"}:
        if source_parts[0] == "novel":
            if target_parts[0] == "read":
                return len(target_parts) >= 2 and source_parts[1] == target_parts[1]
            if target_parts[0] in {"", "genre", "genres", "category", "categories", "tag", "tags", "author", "authors", "search", "ranking", "rankings", "browse", "login", "register"}:
                return False
        if len(target_parts) < 2 or target_parts[0] != source_parts[0]:
            return True
        def identity(part):
            match = re.search(r"_(\d{8,})$", part)
            return match.group(1) if match else part
        return identity(source_parts[1]) == identity(target_parts[1])
    if re.fullmatch(r"n\d+[a-z]+", source_parts[0], re.I):
        return source_parts[0] == target_parts[0]
    prefix = list(source_parts)
    source_query, target_query = dict(parse_qsl(origin.query)), dict(parse_qsl(target.query))
    # Legacy portals use sibling PHP scripts for the catalog and chapter while
    # retaining a novel id in the query (for example Dek-D view/viewlongc).
    if prefix[-1].lower().endswith(".php"):
        prefix.pop()
        if source_query.get("id") and target_query.get("id") != source_query["id"]:
            return False
    if prefix[-1].lower() in {"toc", "catalog", "contents", "index"} or (len(prefix) > 2 and (prefix[-1].isdigit() or re.search(r"^(chapter|episode|ch)[-_]?\d+", prefix[-1], re.I))):
        prefix.pop()
    if len(prefix) >= 2 and target_parts[0] == prefix[0]:
        return target_parts[:len(prefix)] == prefix
    return all(target_query.get(key, value) == value for key, value in source_query.items()
               if key.lower() in {"novel", "novelid", "book", "bookid", "storyid"})


def access_error(html: str, *, page_analysis=None):
    soup = page_analysis.soup if page_analysis is not None else BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    if any(re.search(r"https?://(?:router\.)?parklogic\.com/", script.get("src", "") + script.get_text())
           for script in soup.select("script")) and not soup.select_one("article p, .reader-content p, #chapter-content p"):
        return "Source domain serves a parking redirect instead of chapter content"
    if any(word in title for word in ("just a moment", "security verification", "access denied")) or soup.select_one("#challenge-form, .cf-chl-widget, iframe[src*='captcha']"):
        return "CAPTCHA or browser security challenge requires user action"
    if soup.select_one("input[type='password']") and not soup.select_one("#chapter-content, #novel_honbun, article p"):
        return "Sign-in required or authorized session expired"
    return None


def route_family(url: str) -> str:
    parts = urlsplit(url)
    path = re.sub(r"\d+|[0-9a-f]{16,}", "{id}", parts.path)
    return parts.netloc.lower() + path


class ChapterCheckpoint:
    def __init__(self, directory: Path, job_id: str):
        self.path = directory / "checkpoints" / f"{job_id}.json"
        self.records_dir = directory / "checkpoints" / job_id
        self.cancel_path = directory / "jobs" / f"{job_id}.cancel"
        self.records = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        for path in self.records_dir.glob("*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            self.records[canonical_url(record["url"])] = record

    def verified(self, url: str, output_dir: Path):
        record = self.records.get(canonical_url(url), {})
        filename = record.get("file")
        if record.get("status") not in {"saved", "skipped"} or not filename:
            return None
        path = output_dir / filename
        if path.parent != output_dir or not path.is_file():
            return None
        return record if hashlib.sha256(path.read_bytes()).hexdigest() == record.get("sha256") else None

    def save(self, url: str, record: dict):
        self.records[canonical_url(url)] = record
        atomic_json(self.records_dir / (hashlib.sha256(canonical_url(url).encode()).hexdigest() + ".json"), record)

    def cancelled(self):
        return self.cancel_path.exists()


class DestinationLock:
    """Native process-owned lock; a crash releases ownership automatically."""
    def __init__(self, state_dir: Path, destination: Path):
        key = os.path.normcase(str(destination.resolve()))
        self.path = state_dir / "locks" / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
        self.stream = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        if stream.tell() == 0:
            stream.write(b"1")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            raise BlockingIOError("Another extraction process owns this destination") from error
        self.stream = stream

    def release(self):
        if self.stream:
            try:
                self.stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()
                self.stream = None
