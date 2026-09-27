"""Agent reasoning, classification, and code synthesis modules.

Subpackages:
- toc: Table of Contents LangGraph Agent
- ch: Chapter DOM analysis, code generation, review loop, and self-healing
- llm: LLM client, interactions model, and token metrics
"""

from src.agent.classifier import PageClassifier, ChapterLink, ClassificationResult
from src.agent.domain_memory import DomainMemory, DomainRecipe, ChapterRecipeConfig, TocRecipeConfig

# Re-exports from toc, ch, and llm for convenience
from src.agent.toc.agent import TocAgent
from src.agent.ch import (
    ChapterAnalyzer,
    DOMStructurePlan,
    ChapterCodeGenerator,
    ExtractedChapter,
    ParserVerificationResult,
    ExtractionObserver,
    ExtractionReview,
    ReviewLoopOrchestrator,
    ReviewLoopResult,
    SelfHealer,
)
from src.agent.llm import (
    LLMClient,
    ChatGeminiInteractions,
    token_tracker,
    TokenTracker,
)

__all__ = [
    # Classification & Domain Memory
    "PageClassifier",
    "ChapterLink",
    "ClassificationResult",
    "DomainMemory",
    "DomainRecipe",
    "ChapterRecipeConfig",
    "TocRecipeConfig",
    # TOC Agent
    "TocAgent",
    # Chapter Agent
    "ChapterAnalyzer",
    "DOMStructurePlan",
    "ChapterCodeGenerator",
    "ExtractedChapter",
    "ParserVerificationResult",
    "ExtractionObserver",
    "ExtractionReview",
    "ReviewLoopOrchestrator",
    "ReviewLoopResult",
    "SelfHealer",
    # LLM Subsystem
    "LLMClient",
    "ChatGeminiInteractions",
    "token_tracker",
    "TokenTracker",
]
