import pytest
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from pathlib import Path

from src.main import run_headless

TOC_HTML = """
<!DOCTYPE html>
<html>
<head><title>Immortal Legend - Table of Contents</title></head>
<body>
  <h1>Immortal Legend</h1>
  <p class="author">Author: Astral Walker</p>
  <div class="chapters">
    <a href="/chapter/1">Chapter 1: The Mountain Peak</a>
    <a href="/chapter/2">Chapter 2: The Spirit Beast</a>
    <a href="/chapter/3">Chapter 3: The Secret Vault</a>
    <a href="/chapter/4">Chapter 4: The Tribulation</a>
    <a href="/chapter/5">Chapter 5: Ascension</a>
  </div>
</body>
</html>
"""

def make_chapter_html(num: int, title: str):
    next_link = f'<a href="/chapter/{num+1}">Next Chapter</a>' if num < 5 else ''
    return f"""
<!DOCTYPE html>
<html>
<head><title>Immortal Legend - Chapter {num}: {title}</title></head>
<body>
  <div class="nav"><a href="/toc">Table of Contents</a> {next_link}</div>
  <h1 class="chapter-title">Chapter {num}: {title}</h1>
  <div id="chapter-content">
    <p>This is paragraph 1 of chapter {num} with the profound dao wisdom.</p>
    <p>The cosmic energy condensed into seven radiant pearls orbiting the cultivator.</p>
    <p>He inhaled deeply, drawing the vast starlight into his dantian core.</p>
    <p>After an eternity of silence, the breakthrough barrier shattered with thunderous resonance.</p>
  </div>
</body>
</html>
"""

class MockNovelHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/toc" or self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(TOC_HTML.encode("utf-8"))
        elif self.path.startswith("/chapter/"):
            num_str = self.path.split("/")[-1]
            try:
                num = int(num_str)
                titles = {
                    1: "The Mountain Peak",
                    2: "The Spirit Beast",
                    3: "The Secret Vault",
                    4: "The Tribulation",
                    5: "Ascension"
                }
                title = titles.get(num, f"Chapter {num}")
                content = make_chapter_html(num, title)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(content.encode("utf-8"))
            except ValueError:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()
            
    def log_message(self, format, *args):
        pass  # Quiet logging during test

@pytest.mark.asyncio
async def test_end_to_end_mock_scrape(tmp_path, monkeypatch):
    # Start local mock HTTP server
    server = HTTPServer(("127.0.0.1", 8989), MockNovelHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    
    # Configure output directory to tmp_path
    monkeypatch.setattr("src.main.NovelStorage", lambda: __import__("src.scraper.storage", fromlist=["NovelStorage"]).NovelStorage(base_output_dir=tmp_path))
    
    try:
        # Run headless pipeline
        await run_headless("http://127.0.0.1:8989/toc", concurrency=2)
        
        # Verify novel folder created
        novel_folder = tmp_path / "Immortal Legend"
        assert novel_folder.is_dir()
        
        # Verify metadata.json
        meta_file = novel_folder / "metadata.json"
        assert meta_file.is_file()
        
        # Verify all 5 chapters downloaded
        chapter_files = sorted(list(novel_folder.glob("*.md")))
        assert len(chapter_files) == 5
        
        first_ch = chapter_files[0].read_text(encoding="utf-8")
        assert "Chapter 1 The Mountain Peak" in first_ch
        assert "profound dao wisdom" in first_ch
        assert "chapter_number: 1" not in first_ch
        assert "---" not in first_ch
        
    finally:
        server.shutdown()
