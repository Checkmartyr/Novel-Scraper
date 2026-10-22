import re
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List
import aiofiles
from src.scraper.runtime import atomic_write, atomic_json, canonical_url
import hashlib

from src.config import OUTPUT_DIR, INCLUDE_FRONTMATTER, ROMANIZE_FOLDER
from src.agent.code_generator import ExtractedChapter
from src.utils.romanizer import romanize_text
from src.utils.title_cleaner import clean_chapter_title

def sanitize_filename(name: str) -> str:
    """Sanitize string to be safe across Windows and POSIX filesystems."""
    # Replace illegal filesystem characters: \ / : * ? " < > |
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name)
    # Collapse multiple spaces
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "Untitled"

class NovelStorage:
    """Manages directory creation, chapter saving, and metadata tracking."""
    
    def __init__(
        self,
        base_output_dir: Optional[Path] = None,
        include_frontmatter: bool = INCLUDE_FRONTMATTER,
        romanize_folder: bool = ROMANIZE_FOLDER,
        direct_output: bool = False,
        overwrite: bool = True,
        state_dir: Optional[Path] = None,
    ):
        self.base_output_dir = base_output_dir or OUTPUT_DIR
        self.include_frontmatter = include_frontmatter
        self.romanize_folder = romanize_folder
        self.direct_output = direct_output
        self.overwrite = overwrite
        self.state_dir = state_dir or self.base_output_dir / ".scraper"
        self.sources_path = self.state_dir / "sources.json"
        self.sources_dir = self.state_dir / "receipts"
        self.sources = json.loads(self.sources_path.read_text(encoding="utf-8")) if self.sources_path.exists() else {}
        self._load_receipts()
        self._sources_scope = None
        self._directory_index = {}
        self._directory_stamp = None

    def _load_receipts(self):
        for path in self.sources_dir.glob("*.json"):
            receipt = json.loads(path.read_text(encoding="utf-8"))
            self.sources[receipt.pop("source_url")] = receipt
        self._source_owners = {value["file"]: key for key, value in self.sources.items()}

    def get_novel_dir(self, novel_title: str) -> Path:
        """Get or create novel directory (romanized if enabled)."""
        if self.direct_output:
            self.base_output_dir.mkdir(parents=True, exist_ok=True)
            return self.base_output_dir
        folder_name = romanize_text(novel_title) if self.romanize_folder else novel_title
        safe_title = sanitize_filename(folder_name)
        novel_dir = self.base_output_dir / safe_title
        novel_dir.mkdir(parents=True, exist_ok=True)
        if self._sources_scope != novel_dir:
            scope = self.state_dir / hashlib.sha256(str(novel_dir.resolve()).encode()).hexdigest()[:12]
            self.sources_path = scope / "sources.json"
            self.sources_dir = scope / "receipts"
            self.sources = json.loads(self.sources_path.read_text(encoding="utf-8")) if self.sources_path.exists() else {}
            self._load_receipts()
            self._sources_scope = novel_dir
        return novel_dir

    async def save_chapter(
        self,
        novel_title: str,
        chapter_index: int,
        chapter_title: str,
        chapter: ExtractedChapter,
        source_url: str,
        content_sha256: Optional[str] = None,
    ) -> Path:
        """Save chapter as Markdown file with clean raw text or optional YAML frontmatter."""
        if not chapter.success or chapter.is_paywalled:
            raise ValueError(chapter.error or "Unverified or paywalled chapter cannot be saved")
        novel_dir = self.get_novel_dir(novel_title)
        raw_ch_title = chapter_title or chapter.title or f"Chapter {chapter_index}"
        clean_ch_title = clean_chapter_title(raw_ch_title) or raw_ch_title
        safe_title = sanitize_filename(clean_ch_title)
        
        # 4-digit zero-padded index: e.g. 0001 - Title.md
        filename = f"{chapter_index:04d} - {safe_title}.md"
        filepath = novel_dir / filename
        source_key = canonical_url(source_url)
        existing = self.sources.get(source_key)
        if existing and existing.get("file"):
            previous = novel_dir / existing["file"]
            if previous.parent == novel_dir:
                filepath = previous
        stamp = (novel_dir, novel_dir.stat().st_mtime_ns)
        if stamp != self._directory_stamp:
            self._directory_index = {}
            for path in novel_dir.glob("*.md"):
                match = re.match(r"^(\d+) - ", path.name)
                if match:
                    self._directory_index.setdefault(int(match.group(1)), []).append(path)
            self._directory_stamp = stamp
        collisions = self._directory_index.get(chapter_index, [])
        for collision in collisions:
            owner = self._source_owners.get(collision.name)
            if owner and self.sources[owner].get("state") == "pending":
                digest = hashlib.sha256(collision.read_bytes()).hexdigest()
                if digest not in {self.sources[owner].get("sha256"), self.sources[owner].get("previous_sha256")}:
                    owner = None
            if owner != source_key:
                raise FileExistsError(f"Chapter index {chapter_index} already belongs to another or unknown source")
        if filepath.exists() and not self.overwrite:
            raise FileExistsError(f"Overwrite disabled: {filepath.name}")
        
        timestamp = datetime.now(timezone.utc).isoformat()
        
        if self.include_frontmatter:
            content_lines = [
                "---",
                f'novel: {json.dumps(novel_title, ensure_ascii=False)}',
                f"chapter_number: {chapter_index}",
                f'chapter_title: {json.dumps(safe_title, ensure_ascii=False)}',
                f'source_url: {json.dumps(source_url, ensure_ascii=False)}',
                f"word_count: {chapter.word_count}",
                f"char_count: {chapter.char_count}",
                f'scraped_at: "{timestamp}"',
                "---",
                "",
                f"# {safe_title}",
                "",
                chapter.content_markdown,
                ""
            ]
        else:
            content_lines = [
                f"# {safe_title}",
                "",
                chapter.content_markdown,
                ""
            ]
        
        full_text = "\n".join(content_lines)
        
        # Write the source receipt before the file so a crash after replacement is recoverable.
        previous_hash = hashlib.sha256(filepath.read_bytes()).hexdigest() if filepath.exists() else None
        self.sources[source_key] = {"file": filepath.name, "sha256": hashlib.sha256(full_text.encode("utf-8")).hexdigest(),
                                    "content_sha256": content_sha256,
                                    "state": "pending", "previous_sha256": previous_hash}
        self._source_owners[filepath.name] = source_key
        atomic_json(self.sources_dir / (hashlib.sha256(source_key.encode()).hexdigest() + ".json"),
                    {"source_url": source_key, **self.sources[source_key]})
        atomic_write(filepath, full_text, overwrite=self.overwrite)
        self.sources[source_key]["state"] = "committed"
        atomic_json(self.sources_dir / (hashlib.sha256(source_key.encode()).hexdigest() + ".json"),
                    {"source_url": source_key, **self.sources[source_key]})
        self._directory_index[chapter_index] = [filepath]
        self._directory_stamp = (novel_dir, novel_dir.stat().st_mtime_ns)
            
        return filepath

    def format_chapter_records(
        self,
        chapters: Optional[List[Any]],
        default_downloaded: bool = False,
    ) -> List[Dict[str, Any]]:
        """Format a list of ChapterLink objects or dicts into standard chapter metadata records."""
        if not chapters:
            return []
        records = []
        for ch in chapters:
            if hasattr(ch, "index") and hasattr(ch, "title") and hasattr(ch, "url"):
                records.append({
                    "index": ch.index,
                    "title": ch.title,
                    "url": ch.url,
                    "downloaded": default_downloaded,
                    "success": default_downloaded,
                })
            elif isinstance(ch, dict):
                rec = dict(ch)
                if "downloaded" not in rec and "success" in rec:
                    rec["downloaded"] = rec["success"]
                elif "downloaded" in rec and "success" not in rec:
                    rec["success"] = rec["downloaded"]
                elif "downloaded" not in rec and "success" not in rec:
                    rec["downloaded"] = default_downloaded
                    rec["success"] = default_downloaded
                records.append(rec)
        return records

    async def save_metadata(
        self,
        novel_title: str,
        source_url: str,
        author: Optional[str] = None,
        description: Optional[str] = None,
        total_chapters: int = 0,
        completed_chapters: Optional[int] = None,
        chapter_index_list: Optional[List[Any]] = None,
        token_usage: Optional[Dict[str, Any]] = None,
        extraction_strategy: Optional[str] = None,
        audit_passed: Optional[bool] = None,
    ) -> Path:
        """Save or update novel metadata.json summary file."""
        novel_dir = self.get_novel_dir(novel_title)
        metadata_file = novel_dir / "metadata.json"

        existing_data: Dict[str, Any] = {}
        if metadata_file.exists():
            try:
                async with aiofiles.open(metadata_file, "r", encoding="utf-8") as f:
                    content = await f.read()
                    existing_data = json.loads(content)
            except Exception:
                existing_data = {}

        romanized_title = romanize_text(novel_title)
        created_at = existing_data.get("created_at") or datetime.now(timezone.utc).isoformat()
        
        # Format chapters if provided
        formatted_chapters = (
            self.format_chapter_records(chapter_index_list)
            if chapter_index_list is not None
            else existing_data.get("chapters", [])
        )
        
        # Calculate total chapters if not explicitly provided
        final_total = total_chapters or len(formatted_chapters) or existing_data.get("total_chapters", 0)
        final_completed = completed_chapters if completed_chapters is not None else existing_data.get("completed_chapters", 0)

        data = {
            "novel_title": novel_title,
            "romanized_title": romanized_title,
            "author": author if author is not None else existing_data.get("author"),
            "description": description if description is not None else existing_data.get("description"),
            "source_url": source_url,
            "total_chapters": final_total,
            "completed_chapters": final_completed,
            "extraction_strategy": extraction_strategy if extraction_strategy is not None else existing_data.get("extraction_strategy"),
            "audit_passed": audit_passed if audit_passed is not None else existing_data.get("audit_passed"),
            "token_usage": token_usage if token_usage is not None else existing_data.get("token_usage", {}),
            "created_at": created_at,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "chapters": formatted_chapters
        }

        atomic_json(metadata_file, data)

        return metadata_file
