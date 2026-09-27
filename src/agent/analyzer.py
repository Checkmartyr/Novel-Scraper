"""Backward compatibility shim for src.agent.ch.analyzer."""

from src.agent.ch.analyzer import (
    ChapterAnalyzer,
    DOMStructurePlan,
    sanitize_css_selector,
)

__all__ = [
    "ChapterAnalyzer",
    "DOMStructurePlan",
    "sanitize_css_selector",
]
