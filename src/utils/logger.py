import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, Callable

from src.config import LOGS_DIR

_MARKUP_RE = re.compile(r"\[/?[a-zA-Z0-9_\- #]+\]")


def clean_markup(text: str) -> str:
    """Strip Rich/Textual markup tags from text for plain-text file logging."""
    return _MARKUP_RE.sub("", text)


def setup_file_logging(
    logs_dir: Optional[Path] = None,
    session_prefix: str = "tui",
    level: int = logging.INFO,
) -> Tuple[Path, Path]:
    """
    Set up persistent UTF-8 file logging in the target logs directory.
    
    Creates:
    1. Timestamped session log: logs/{session_prefix}_{YYYYMMDD_HHMMSS}.log
    2. Latest session log: logs/{session_prefix}_latest.log
    
    Returns:
        (session_log_path, latest_log_path)
    """
    target_dir = Path(logs_dir or LOGS_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_log_path = target_dir / f"{session_prefix}_{timestamp}.log"
    latest_log_path = target_dir / f"{session_prefix}_latest.log"

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-7s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove existing novel scraper file handlers if any to avoid duplicates in tests/re-inits
    for h in list(root_logger.handlers):
        if getattr(h, "_novel_scraper_file_handler", False):
            root_logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass

    # 1. Session log handler (append)
    session_handler = logging.FileHandler(
        filename=str(session_log_path),
        mode="a",
        encoding="utf-8",
        errors="replace",
    )
    session_handler.setFormatter(formatter)
    session_handler.setLevel(level)
    setattr(session_handler, "_novel_scraper_file_handler", True)
    root_logger.addHandler(session_handler)

    # 2. Latest log handler (mode='w' to always reflect latest run)
    latest_handler = logging.FileHandler(
        filename=str(latest_log_path),
        mode="w",
        encoding="utf-8",
        errors="replace",
    )
    latest_handler.setFormatter(formatter)
    latest_handler.setLevel(level)
    setattr(latest_handler, "_novel_scraper_file_handler", True)
    root_logger.addHandler(latest_handler)

    logger = logging.getLogger("logger")
    logger.info(
        f"Logging initialized. Session log: '{session_log_path.name}', Latest log: '{latest_log_path.name}'"
    )

    return session_log_path, latest_log_path


class TextualLogBridge(logging.Handler):
    """
    Logging handler that forwards records from Python standard logging
    to Textual TUI's log_msg callback.
    """

    def __init__(self, log_callback: Callable[[str, str], None], level: int = logging.INFO):
        super().__init__(level=level)
        self.log_callback = log_callback
        self._in_emit = False

    def emit(self, record: logging.LogRecord) -> None:
        # Prevent recursive emission if log_callback logs
        if self._in_emit:
            return
        self._in_emit = True
        try:
            msg = self.format(record)
            level = "info"
            if record.levelno >= logging.ERROR:
                level = "error"
            elif record.levelno >= logging.WARNING:
                level = "warning"
            self.log_callback(msg, level)
        except Exception:
            self.handleError(record)
        finally:
            self._in_emit = False
