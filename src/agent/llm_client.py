import json
import logging
from typing import Type, TypeVar, Optional, Any, Dict
from pydantic import BaseModel

try:
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.output_parsers import PydanticOutputParser
    from langchain_core.runnables import Runnable
except ImportError as e:
    raise ImportError(
        "Required dependency 'langchain_core' is missing in the current Python environment.\n"
        "Please run: uv tool install --editable . --force\n"
        "Or: uv sync"
    ) from e

from src.agent.interactions_model import ChatGeminiInteractions
from src.config import GEMINI_API_KEY, GEMINI_MODEL

logger = logging.getLogger("agent.llm")

T = TypeVar("T", bound=BaseModel)

class LLMClient:
    """LangChain client powered by Gemini Interactions API with fallback support."""
    
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or GEMINI_API_KEY
        self.model = model or GEMINI_MODEL
        self.last_interaction_id: Optional[str] = None
        self._chat_model = ChatGeminiInteractions(
            model=self.model,
            api_key=self.api_key,
        ) if self.api_key else None
        
        if not self._chat_model:
            logger.warning("GEMINI_API_KEY not set. Operating in heuristic fallback mode.")

    @property
    def is_available(self) -> bool:
        return self._chat_model is not None

    def get_chat_model(self, previous_interaction_id: Optional[str] = None) -> ChatGeminiInteractions:
        """Get LangChain ChatGeminiInteractions model with optional interaction chaining."""
        return ChatGeminiInteractions(
            model=self.model,
            api_key=self.api_key,
            previous_interaction_id=previous_interaction_id,
        )

    async def generate_json(
        self,
        prompt_text: str,
        schema: Type[T],
        system_instruction: str = "You are an expert AI assistant that outputs strictly valid JSON.",
        previous_interaction_id: Optional[str] = None
    ) -> T:
        """Run LangChain LCEL chain with Gemini Interactions API and PydanticOutputParser."""
        if not self.is_available:
            raise RuntimeError("Gemini client unavailable (missing GEMINI_API_KEY).")
            
        parser = PydanticOutputParser(pydantic_object=schema)
        prompt = ChatPromptTemplate.from_messages([
            ("system", "{system_instruction}\n{format_instructions}"),
            ("human", "{input}"),
        ])
        
        chat_model = self.get_chat_model(previous_interaction_id=previous_interaction_id)
        
        # Build LangChain LCEL pipeline: prompt | chat_model | parser
        chain: Runnable = prompt | chat_model | parser
        
        try:
            result = await chain.ainvoke({
                "system_instruction": system_instruction,
                "format_instructions": parser.get_format_instructions(),
                "input": prompt_text,
            })
            self.last_interaction_id = chat_model.last_interaction_id
            return result
        except Exception as e:
            logger.error(f"LangChain Gemini Interactions generation failed: {e}")
            raise

    async def generate_text(
        self,
        prompt_text: str,
        system_instruction: str = "You are an expert coding assistant.",
        previous_interaction_id: Optional[str] = None
    ) -> str:
        """Generate text using LangChain with Gemini Interactions API."""
        if not self.is_available:
            raise RuntimeError("Gemini client unavailable (missing GEMINI_API_KEY).")
            
        chat_model = self.get_chat_model(previous_interaction_id=previous_interaction_id)
        prompt = ChatPromptTemplate.from_messages([
            ("system", "{system_instruction}"),
            ("human", "{input}"),
        ])
        chain = prompt | chat_model
        
        response = await chain.ainvoke({
            "system_instruction": system_instruction,
            "input": prompt_text,
        })
        self.last_interaction_id = chat_model.last_interaction_id
        return response.content if isinstance(response.content, str) else str(response.content)
