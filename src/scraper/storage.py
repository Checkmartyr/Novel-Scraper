import re
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List
import aiofiles

from src.config import OUTPUT_DIR, INCLUDE_FRONTMATTER, ROMANIZE_FOLDER
from src.agent.code_generator import ExtractedChapter
from src.utils.romanizer import romanize_text

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
    ):
        self.base_output_dir = base_output_dir or OUTPUT_DIR
        self.include_frontmatter = include_frontmatter
        self.romanize_folder = romanize_folder

    def get_novel_dir(self, novel_title: str) -> Path:
        """Get or create novel directory (romanized if enabled)."""
        folder_name = romanize_text(novel_title) if self.romanize_folder else novel_title
        safe_title = sanitize_filename(folder_name)
        novel_dir = self.base_output_dir / safe_title
        novel_dir.mkdir(parents=True, exist_ok=True)
        return novel_dir

    async def save_chapter(
        self,
        novel_title: str,
        chapter_index: int,
        chapter_title: str,
        chapter: ExtractedChapter,
        source_url: str,
    ) -> Path:
        """Save chapter as Markdown file with clean raw text or optional YAML frontmatter."""
        novel_dir = self.get_novel_dir(novel_title)
        raw_ch_title = chapter_title or chapter.title or f"Chapter {chapter_index}"
        clean_ch_title = re.sub(r"\s*[（\(\[]\s*\d+\s*/\s*\d+\s*[）\)\]]", "", raw_ch_title).strip() or raw_ch_title
        safe_title = sanitize_filename(clean_ch_title)
        
        # 4-digit zero-padded index: e.g. 0001 - Title.md
        filename = f"{chapter_index:04d} - {safe_title}.md"
        filepath = novel_dir / filename
        
        timestamp = datetime.now(timezone.utc).isoformat()
        
        if self.include_frontmatter:
            content_lines = [
                "---",
                f'novel: "{novel_title}"',
                f"chapter_number: {chapter_index}",
                f'chapter_title: "{safe_title}"',
                f'source_url: "{source_url}"',
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
        
        async with aiofiles.open(filepath, "w", encoding="utf-8") as f:
            await f.write(full_text)
            
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
        completed_chapters: int = 0,
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
        final_completed = completed_chapters if completed_chapters else existing_data.get("completed_chapters", 0)

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

        async with aiofiles.open(metadata_file, "w", encoding="utf-8") as f:
            await f.write(json.dumps(data, indent=2, ensure_ascii=False))

        return metadata_file
