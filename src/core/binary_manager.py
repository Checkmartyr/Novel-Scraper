import os
import sys
import platform
import shutil
import zipfile
import tarfile
from pathlib import Path
from typing import Optional, Callable
import httpx
from rich.console import Console

from src.config import BIN_DIR, OBSCURA_BIN_PATH, OBSCURA_GITHUB_REPO

console = Console()

def get_platform_asset_pattern() -> str:
    """Return the release asset filename pattern for current OS and architecture."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    
    if system == "windows":
        # Windows x86_64
        return "obscura-x86_64-windows-stealth.zip"
    elif system == "linux":
        if "aarch64" in machine or "arm" in machine:
            return "obscura-aarch64-linux-stealth.tar.gz"
        return "obscura-x86_64-linux-stealth.tar.gz"
    elif system == "darwin":
        if "arm" in machine or "aarch64" in machine:
            return "obscura-aarch64-macos-stealth.tar.gz"
        return "obscura-x86_64-macos-stealth.tar.gz"
    else:
        raise RuntimeError(f"Unsupported operating system: {system}")

def find_obscura() -> Optional[Path]:
    """Check if Obscura binary exists on system or in local bin dir."""
    # 1. Check explicit config
    if OBSCURA_BIN_PATH and Path(OBSCURA_BIN_PATH).is_file():
        return Path(OBSCURA_BIN_PATH)
    
    binary_name = "obscura.exe" if sys.platform == "win32" else "obscura"
    
    # 2. Check local BIN_DIR
    local_bin = BIN_DIR / binary_name
    if local_bin.is_file():
        return local_bin
    
    # 3. Check system PATH
    system_path = shutil.which("obscura") or shutil.which("obscura.exe")
    if system_path:
        return Path(system_path)
    
    return None

def download_obscura(progress_callback: Optional[Callable[[int, int], None]] = None) -> Path:
    """Download and extract Obscura release from GitHub."""
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    asset_name = get_platform_asset_pattern()
    
    api_url = f"https://api.github.com/repos/{OBSCURA_GITHUB_REPO}/releases/latest"
    headers = {"User-Agent": "Novel-Scraping-Agent/1.0"}
    
    with httpx.Client(timeout=30.0, headers=headers, follow_redirects=True) as client:
        resp = client.get(api_url)
        resp.raise_for_status()
        release_data = resp.json()
        
        # Locate target asset URL
        download_url = None
        for asset in release_data.get("assets", []):
            if asset.get("name") == asset_name:
                download_url = asset.get("browser_download_url")
                break
        
        # Fallback to non-stealth if stealth asset missing
        if not download_url:
            fallback_name = asset_name.replace("-stealth", "")
            for asset in release_data.get("assets", []):
                if asset.get("name") == fallback_name:
                    download_url = asset.get("browser_download_url")
                    asset_name = fallback_name
                    break
        
        if not download_url:
            raise RuntimeError(f"Could not find download asset {asset_name} in release {release_data.get('tag_name')}")
        
        archive_path = BIN_DIR / asset_name
        
        # Stream download
        with client.stream("GET", download_url) as stream:
            stream.raise_for_status()
            total_bytes = int(stream.headers.get("content-length", 0))
            downloaded = 0
            
            with open(archive_path, "wb") as f:
                for chunk in stream.iter_bytes(chunk_size=65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total_bytes > 0:
                        progress_callback(downloaded, total_bytes)
        
        # Extract archive
        if asset_name.endswith(".zip"):
            with zipfile.ZipFile(archive_path, "r") as zip_ref:
                zip_ref.extractall(BIN_DIR)
        elif asset_name.endswith(".tar.gz"):
            with tarfile.open(archive_path, "r:gz") as tar_ref:
                tar_ref.extractall(BIN_DIR)
        
        # Clean archive
        try:
            archive_path.unlink()
        except OSError:
            pass
        
    binary_name = "obscura.exe" if sys.platform == "win32" else "obscura"
    bin_path = BIN_DIR / binary_name
    if not bin_path.is_file():
        # Check subdirectories if zip extracted into a folder
        found = list(BIN_DIR.rglob(binary_name))
        if found:
            shutil.move(str(found[0]), str(bin_path))
    
    if not bin_path.is_file():
        raise RuntimeError(f"Extraction failed: {bin_path} not found in {BIN_DIR}")
    
    # Set executable permissions on unix
    if sys.platform != "win32":
        bin_path.chmod(0o755)
        worker_bin = BIN_DIR / "obscura-worker"
        if worker_bin.is_file():
            worker_bin.chmod(0o755)
            
    return bin_path

def ensure_obscura(progress_callback: Optional[Callable[[int, int], None]] = None) -> Path:
    """Ensure Obscura is available, downloading it automatically if missing."""
    existing = find_obscura()
    if existing:
        return existing
    
    console.print("[cyan]Obscura binary not found. Downloading latest stealth release...[/cyan]")
    return download_obscura(progress_callback)

if __name__ == "__main__":
    path = ensure_obscura()
    console.print(f"[green]Obscura verified at: {path}[/green]")
