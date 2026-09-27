"""State definition for LangGraph TOC extraction agent."""

from typing import List, Optional, Dict, Any
from typing_extensions import TypedDict
from pydantic import BaseModel, Field

from src.agent.classifier import ChapterLink


class TocState(TypedDict):
    """The graph state tracked across all nodes in the TOC extraction workflow."""
    
    # URL and current DOM representation
    url: str
    html: str
    
    # Metadata extracted or identified
    novel_title: str
    author: Optional[str]
    description: Optional[str]
    
    # Ground truth / Claimed capacity indicators (e.g. "全111話", "111 chapters")
    claimed_chapter_count: Optional[int]
    
    # Extracted chapter results
    extracted_chapters: List[ChapterLink]
    
    # Method / strategy that provided current chapters
    extraction_strategy: str
    
    # Audit & evaluation metrics
    confidence_score: float
    has_unexpanded_sections: bool
    has_pagination: bool
    pagination_urls: List[str]
    is_complete: bool
    
    # Self-healing synthesis
    heal_attempted: bool
    custom_container_selector: Optional[str]
    custom_link_selector: Optional[str]

    # Lifecycle control
    iteration: int
    max_iterations: int
    issues: List[str]
    logs: List[str]
