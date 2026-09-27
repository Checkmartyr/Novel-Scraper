import logging
from pathlib import Path
import pytest
from src.utils.logger import clean_markup, setup_file_logging, TextualLogBridge


def test_clean_markup():
    assert clean_markup("[green]Hello[/green]") == "Hello"
    assert clean_markup("[bold cyan][Review Loop 1/3][/bold cyan] OK") == "[Review Loop 1/3] OK"
    assert clean_markup("Plain text without tags") == "Plain text without tags"


def test_setup_file_logging(tmp_path: Path):
    logs_dir = tmp_path / "custom_logs"
    session_log, latest_log = setup_file_logging(logs_dir=logs_dir, session_prefix="test_tui")

    assert session_log.exists()
    assert latest_log.exists()
    assert session_log.parent == logs_dir

    test_logger = logging.getLogger("test_module")
    test_logger.info("Test info message")
    test_logger.warning("Test warning alert")
    test_logger.error("Test error failure")

    # Flush handlers
    for h in logging.getLogger().handlers:
        h.flush()

    session_content = session_log.read_text(encoding="utf-8")
    latest_content = latest_log.read_text(encoding="utf-8")

    for content in (session_content, latest_content):
        assert "Test info message" in content
        assert "Test warning alert" in content
        assert "Test error failure" in content
        assert "[INFO   ]" in content or "[INFO]" in content
        assert "[WARN" in content
        assert "[ERROR  ]" in content or "[ERROR]" in content


def test_utf8_logging(tmp_path: Path):
    logs_dir = tmp_path / "utf8_logs"
    session_log, latest_log = setup_file_logging(logs_dir=logs_dir, session_prefix="test_utf8")

    test_logger = logging.getLogger("test_utf8")
    thai_text = "ระบบ! ฉันไม่ขอเป็นนักบุญหญิงแล้วนะ!"
    japanese_text = "異世界は平和でした"
    chinese_text = "不落风"

    test_logger.info(f"Thai: {thai_text} | JP: {japanese_text} | CN: {chinese_text}")

    for h in logging.getLogger().handlers:
        h.flush()

    content = session_log.read_text(encoding="utf-8")
    assert thai_text in content
    assert japanese_text in content
    assert chinese_text in content


def test_textual_log_bridge():
    recorded = []

    def mock_callback(msg: str, level: str):
        recorded.append((msg, level))

    bridge = TextualLogBridge(log_callback=mock_callback, level=logging.INFO)
    bridge.setFormatter(logging.Formatter("%(message)s"))

    record_info = logging.LogRecord(
        name="test", level=logging.INFO, pathname="", lineno=0, msg="Bridge info", args=(), exc_info=None
    )
    bridge.emit(record_info)

    record_err = logging.LogRecord(
        name="test", level=logging.ERROR, pathname="", lineno=0, msg="Bridge err", args=(), exc_info=None
    )
    bridge.emit(record_err)

    assert len(recorded) == 2
    assert recorded[0] == ("Bridge info", "info")
    assert recorded[1] == ("Bridge err", "error")
