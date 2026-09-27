"""Backward compatibility shim for src.agent.ch.code_generator."""

from src.agent.ch.code_generator import (
    ChapterCodeGenerator,
    ExtractedChapter,
    ParserVerificationResult,
    DEFAULT_NOISE_SELECTORS,
)

__all__ = [
    "ChapterCodeGenerator",
    "ExtractedChapter",
    "ParserVerificationResult",
    "DEFAULT_NOISE_SELECTORS",
]
