from pathlib import Path
import os
from dotenv import load_dotenv

# Load .env file
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = Path(__file__).resolve().parent
BIN_DIR = SRC_DIR / "bin"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(BASE_DIR / "novels")))
LOGS_DIR = Path(os.getenv("LOGS_DIR", str(BASE_DIR / "logs")))
RECIPES_DIR = Path(os.getenv("RECIPES_DIR", str(SRC_DIR / "recipes")))

# Obscura binary configuration
OBSCURA_BIN_PATH = os.getenv("OBSCURA_BIN_PATH", "")

# LLM Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

# Scraping settings
DEFAULT_CONCURRENCY = int(os.getenv("DEFAULT_CONCURRENCY", "3"))
MIN_DELAY_SECONDS = float(os.getenv("MIN_DELAY_SECONDS", "0.5"))
MAX_DELAY_SECONDS = float(os.getenv("MAX_DELAY_SECONDS", "1.5"))
NAVIGATION_TIMEOUT = int(os.getenv("NAVIGATION_TIMEOUT", "30"))
WAIT_UNTIL = os.getenv("WAIT_UNTIL", "networkidle")
INCLUDE_FRONTMATTER = os.getenv("INCLUDE_FRONTMATTER", "false").lower() in ("true", "1", "yes")
ROMANIZE_FOLDER = os.getenv("ROMANIZE_FOLDER", "true").lower() in ("true", "1", "yes")
MAX_REVIEW_ITERATIONS = int(os.getenv("MAX_REVIEW_ITERATIONS", "32"))
MIN_QUALITY_SCORE = float(os.getenv("MIN_QUALITY_SCORE", "0.85"))

# GitHub release URL for Obscura
OBSCURA_GITHUB_REPO = os.getenv("OBSCURA_GITHUB_REPO", "h4ckf0r0day/obscura")
