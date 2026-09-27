"""Backward compatibility shim for src.agent.llm.client."""

from src.agent.llm.client import (
    ChatGeminiInteractions,
    LLMClient,
    T,
)

__all__ = [
    "ChatGeminiInteractions",
    "LLMClient",
    "T",
]
