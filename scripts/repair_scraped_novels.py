"""Script to repair, re-order, and clean scraped novel chapters on disk."""

import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from rich.console import Console
from rich.table import Table

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.title_cleaner import (
    clean_chapter_title,
    clean_chapter_content,
    extract_chapter_number,
    detect_and_fix_reverse_order,
)
from src.scraper.storage import sanitize_filename

console = Console()
logger = logging.getLogger("repair_novels")


class NovelRepairer:
    """Repairs scraped novel folders: fixes reverse ordering, deforms in titles, and cleans disclaimers."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    def repair_directory(self, novel_dir: Path) -> Dict[str, Any]:
        """Repair a single novel directory."""
        novel_dir = Path(novel_dir)
        if not novel_dir.exists() or not novel_dir.is_dir():
            console.print(f"[bold red]Directory does not exist:[/bold red] {novel_dir}")
            return {"success": False, "error": "Not a directory"}

        console.print(f"\n[bold cyan]=== Repairing: {novel_dir.name} ===[/bold cyan]")

        # 1. Load metadata.json if present
        meta_file = novel_dir / "metadata.json"
        metadata: Optional[Dict[str, Any]] = None
        if meta_file.exists():
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
            except Exception as e:
                console.print(f"[yellow]Failed to read metadata.json: {e}[/yellow]")

        # 2. Gather all .md files
        md_files = sorted(
            [f for f in novel_dir.glob("*.md") if not f.name.startswith("_")],
            key=lambda p: p.name,
        )

        if not md_files:
            console.print("[yellow]No markdown files found in directory.[/yellow]")
            return {"success": True, "files_repaired": 0}

        console.print(f"Found [green]{len(md_files)}[/green] chapter files.")

        # 3. Parse each file
        parsed_chapters: List[Dict[str, Any]] = []
        for file_path in md_files:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

            # Extract header and frontmatter
            frontmatter = ""
            body = content
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    frontmatter = parts[1]
                    body = parts[2]

            # Find title from `# Title`
            title_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
            if title_match:
                extracted_title = title_match.group(1).strip()
            else:
                # Fallback to filename without index
                stem = file_path.stem
                stem_sub = re.sub(r"^\d+\s*[-_]?\s*", "", stem).strip()
                extracted_title = stem_sub or stem

            # Corresponding metadata chapter if available
            ch_url = ""
            if metadata and "chapters" in metadata:
                # Match by index from filename prefix
                prefix_m = re.match(r"^(\d+)", file_path.name)
                if prefix_m:
                    f_idx = int(prefix_m.group(1))
                    for m_ch in metadata["chapters"]:
                        if m_ch.get("index") == f_idx:
                            ch_url = m_ch.get("url", "")
                            break

            ch_num = extract_chapter_number(extracted_title, ch_url)

            parsed_chapters.append({
                "old_path": file_path,
                "original_title": extracted_title,
                "chapter_number": ch_num,
                "url": ch_url,
                "frontmatter": frontmatter,
                "body": body,
            })

        # 4. Check for reverse chronological order
        valid_nums = [c["chapter_number"] for c in parsed_chapters if c["chapter_number"] is not None]
        is_reverse = False
        if len(valid_nums) >= 2:
            first_n = valid_nums[0]
            last_n = valid_nums[-1]
            if first_n > last_n:
                is_reverse = True
            else:
                # Count decreasing pairs
                decreases = sum(1 for i in range(len(valid_nums) - 1) if valid_nums[i] > valid_nums[i + 1])
                if decreases > len(valid_nums) * 0.6:
                    is_reverse = True

        if is_reverse:
            console.print("[bold magenta][REVERSED] Detected reverse chronological order! Inverting to 1..N reading order...[/bold magenta]")
            parsed_chapters.reverse()
        else:
            console.print("[green][OK] Ordering is already ascending reading order.[/green]")

        # 5. Clean each chapter and prepare output files
        staging_dir = novel_dir / "_repaired_staging"
        if not self.dry_run:
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
            staging_dir.mkdir(parents=True, exist_ok=True)

        new_chapter_metadata: List[Dict[str, Any]] = []

        table = Table(title=f"Repair Summary: {novel_dir.name}", show_header=True, header_style="bold magenta")
        table.add_column("Index", style="dim", width=6)
        table.add_column("Old Filename", style="red", max_width=40)
        table.add_column("Clean Title", style="green", max_width=45)

        for new_idx, item in enumerate(parsed_chapters, start=1):
            clean_title = clean_chapter_title(item["original_title"])
            safe_title = sanitize_filename(clean_title)
            new_filename = f"{new_idx:04d} - {safe_title}.md"

            # Clean body: strip disclaimers and paywall notices
            body_text = item["body"].strip()
            # Split into paragraphs
            raw_paras = [p.strip() for p in body_text.split("\n\n") if p.strip()]
            
            # Remove existing top heading from paragraphs
            if raw_paras and raw_paras[0].startswith("# "):
                raw_paras = raw_paras[1:]

            clean_paras, is_paywalled = clean_chapter_content(raw_paras)
            clean_body = "\n\n".join(clean_paras)

            # Rebuild file content
            if item["frontmatter"]:
                # Update chapter_number and chapter_title in frontmatter
                fm = item["frontmatter"]
                fm = re.sub(r'chapter_number:\s*\d+', f'chapter_number: {new_idx}', fm)
                fm = re.sub(r'chapter_title:\s*".*?"', f'chapter_title: "{safe_title}"', fm)
                file_content = f"---{fm}---\n\n# {safe_title}\n\n{clean_body}\n"
            else:
                file_content = f"# {safe_title}\n\n{clean_body}\n"

            if not self.dry_run:
                target_path = staging_dir / new_filename
                with open(target_path, "w", encoding="utf-8") as f:
                    f.write(file_content)

            # Prepare metadata entry
            new_chapter_metadata.append({
                "index": new_idx,
                "title": clean_title,
                "url": item["url"],
                "downloaded": True,
                "success": True,
                "is_paywalled": is_paywalled,
            })

            if new_idx <= 5 or new_idx > len(parsed_chapters) - 5 or new_idx % 25 == 0:
                table.add_row(f"{new_idx:04d}", item["old_path"].name, clean_title)

        console.print(table)

        # 6. Apply file changes (replace old .md files with staged)
        if not self.dry_run:
            # Delete old md files
            for old_f in md_files:
                try:
                    old_f.unlink()
                except Exception as e:
                    console.print(f"[red]Failed to remove old file {old_f.name}: {e}[/red]")

            # Move staged files to novel_dir
            for staged_f in staging_dir.glob("*.md"):
                shutil.move(str(staged_f), str(novel_dir / staged_f.name))

            shutil.rmtree(staging_dir)

            # 7. Update metadata.json
            if metadata:
                metadata["chapters"] = new_chapter_metadata
                metadata["total_chapters"] = len(new_chapter_metadata)
                metadata["completed_chapters"] = len(new_chapter_metadata)
                with open(meta_file, "w", encoding="utf-8") as f:
                    json.dump(metadata, f, indent=2, ensure_ascii=False)
                console.print("[green][OK] Updated metadata.json with normalized titles and indices.[/green]")

        console.print(f"[bold green]Successfully repaired {len(parsed_chapters)} chapters in '{novel_dir.name}'![/bold green]\n")
        return {"success": True, "files_repaired": len(parsed_chapters), "is_reversed": is_reverse}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Repair scraped novel chapters on disk.")
    parser.add_argument("--novel", type=str, help="Specific novel directory name inside novels/ to repair.")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without modifying files.")
    parser.add_argument("--all", action="store_true", help="Repair all directories inside novels/")
    args = parser.parse_args()

    novels_base = PROJECT_ROOT / "novels"
    repairer = NovelRepairer(dry_run=args.dry_run)

    if args.novel:
        target = novels_base / args.novel if not Path(args.novel).is_absolute() else Path(args.novel)
        repairer.repair_directory(target)
    elif args.all:
        for nd in sorted(novels_base.iterdir()):
            if nd.is_dir() and not nd.name.startswith("."):
                repairer.repair_directory(nd)
    else:
        # Default targeted repairs for the two user-specified novels
        targets = [
            novels_base / "The heroine insists on being my master",
            novels_base / "I Was Mistaken for a Saint in a Dark Fantasy",
        ]
        for t in targets:
            if t.exists():
                repairer.repair_directory(t)
            else:
                console.print(f"[yellow]Target path not found: {t}[/yellow]")


if __name__ == "__main__":
    main()
