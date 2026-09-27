import asyncio
import json
import sys
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

from bs4 import BeautifulSoup
from src.core.obscura_client import ObscuraClient

async def main():
    client = ObscuraClient()
    try:
        url = "https://kakuyomu.jp/works/16817330663722833570"
        print(f"Fetching {url} using Playwright with networkidle...")
        html = await client.fetch_html(url, stealth=True)
        soup = BeautifulSoup(html, "lxml")
        
        # 1. Clean Title
        raw_title = soup.title.get_text(strip=True) if soup.title else "Kakuyomu Novel"
        novel_title = raw_title.replace(" - カクヨム", "").strip()
        
        # 2. Extract from Next.js Apollo state
        next_data_script = soup.find("script", id="__NEXT_DATA__")
        chapters = []
        
        if next_data_script and next_data_script.string:
            data = json.loads(next_data_script.string)
            apollo = data.get("props", {}).get("pageProps", {}).get("__APOLLO_STATE__", {})
            
            # Find work id
            work_id = "16817330663722833570"
            work_obj = apollo.get(f"Work:{work_id}", {})
            if work_obj.get("title"):
                novel_title = work_obj["title"]
                
            # Collect ordered episode refs from TableOfContentsChapter
            toc_chapter = apollo.get("TableOfContentsChapter:", {})
            episode_unions = toc_chapter.get("episodeUnions", [])
            
            # If tableOfContentsV2 has multiple chapters/volumes
            if not episode_unions and "tableOfContentsV2" in work_obj:
                for toc_ref in work_obj["tableOfContentsV2"]:
                    ref_key = toc_ref.get("__ref")
                    if ref_key and ref_key in apollo:
                        episode_unions.extend(apollo[ref_key].get("episodeUnions", []))
                        
            print(f"Found {len(episode_unions)} episode references in Apollo state.")
            
            for ep_ref in episode_unions:
                ref_key = ep_ref.get("__ref")
                if not ref_key or ref_key not in apollo:
                    continue
                ep_data = apollo[ref_key]
                ep_id = ep_data.get("id")
                ep_title = ep_data.get("title", "")
                ep_pub = ep_data.get("publishedAt", "")
                ep_url = f"https://kakuyomu.jp/works/{work_id}/episodes/{ep_id}"
                chapters.append({
                    "id": ep_id,
                    "title": ep_title,
                    "published_at": ep_pub,
                    "url": ep_url,
                })

        print(f"Successfully resolved {len(chapters)} chapters!")
        
        out_file = "kakuyomu_chapters.txt"
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(f"Novel Title   : {novel_title}\n")
            f.write(f"Source URL    : {url}\n")
            f.write(f"Total Chapters: {len(chapters)}\n")
            f.write("=" * 90 + "\n\n")
            for idx, ch in enumerate(chapters, 1):
                pub_info = f"  [{ch['published_at']}]" if ch['published_at'] else ""
                f.write(f"{idx:04d}. {ch['title']}{pub_info}\n")
                f.write(f"      {ch['url']}\n\n")

        print(f"All {len(chapters)} chapters exported to {out_file}!")
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(main())
