import json
import logging
import asyncio
import os
from typing import Type, TypeVar, Optional, Any, Dict, List, Mapping
from pydantic import BaseModel, Field, ConfigDict

try:
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.output_parsers import PydanticOutputParser
    from langchain_core.runnables import Runnable
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import (
        BaseMessage,
        AIMessage,
        SystemMessage,
        HumanMessage,
    )
    from langchain_core.outputs import ChatResult, ChatGeneration
    from langchain_core.callbacks import CallbackManagerForLLMRun, AsyncCallbackManagerForLLMRun
except ImportError as e:
    raise ImportError(
        "Required dependency 'langchain_core' is missing in the current Python environment.\n"
        "Please run: uv tool install --editable . --force\n"
        "Or: uv sync"
    ) from e

from google import genai
from src.config import GEMINI_API_KEY, GEMINI_MODEL
from src.agent.llm.tracker import token_tracker

logger = logging.getLogger("agent.llm")

T = TypeVar("T", bound=BaseModel)


def _load_nousetsu_llm_integration():
    """Load NouSetsu routing only when this scraper runs inside NouSetsu."""
    try:
        from nousetsu.agents.llm import get_llm
        from nousetsu.scraper import resolve_scraper_llm_settings
    except ModuleNotFoundError as exc:
        if exc.name and (exc.name == "nousetsu" or exc.name.startswith("nousetsu.")):
            return None
        raise
    return get_llm, resolve_scraper_llm_settings


def _has_configured_provider(model: Any) -> bool:
    """Treat NouSetsu's deterministic mock model as unavailable for live scraping."""
    if model is None or type(model).__name__ == "MockNovelLLM":
        return False

    nested_models = [
        child for child in (getattr(model, "primary", None), getattr(model, "fallback", None))
        if child is not None
    ]
    if nested_models:
        return any(_has_configured_provider(child) for child in nested_models)
    return True


class ChatGeminiInteractions(BaseChatModel):
    """LangChain ChatModel wrapper for the Gemini Interactions API."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    model: str = Field(default=GEMINI_MODEL)
    api_key: Optional[str] = Field(default=None)
    previous_interaction_id: Optional[str] = Field(default=None)
    last_interaction_id: Optional[str] = Field(default=None)
    response_format: Optional[Dict[str, Any]] = Field(default=None)

    @property
    def _llm_type(self) -> str:
        return "gemini-interactions"

    def _get_client(self) -> Optional[genai.Client]:
        key = self.api_key or GEMINI_API_KEY
        if not key:
            return None
        return genai.Client(api_key=key)

    def _convert_messages(self, messages: List[BaseMessage]) -> tuple[Optional[str], Any]:
        """Convert LangChain messages into Gemini Interactions API system instruction and inputs."""
        system_instruction = None
        input_steps = []

        for msg in messages:
            if isinstance(msg, SystemMessage):
                system_instruction = msg.content if isinstance(msg.content, str) else str(msg.content)
            elif isinstance(msg, HumanMessage):
                text_content = msg.content if isinstance(msg.content, str) else str(msg.content)
                input_steps.append({
                    "type": "user_input",
                    "content": [{"type": "text", "text": text_content}]
                })
            elif isinstance(msg, AIMessage):
                text_content = msg.content if isinstance(msg.content, str) else str(msg.content)
                input_steps.append({
                    "type": "model_output",
                    "content": [{"type": "text", "text": text_content}]
                })
            else:
                input_steps.append({
                    "type": "user_input",
                    "content": [{"type": "text", "text": str(msg.content)}]
                })

        if len(input_steps) == 1 and input_steps[0]["type"] == "user_input":
            inputs = input_steps[0]["content"][0]["text"]
        elif input_steps:
            inputs = input_steps
        else:
            inputs = ""

        return system_instruction, inputs

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        client = self._get_client()
        if not client:
            raise RuntimeError("Gemini client unavailable (GEMINI_API_KEY missing).")

        system_instruction, inputs = self._convert_messages(messages)

        create_kwargs: Dict[str, Any] = {
            "model": self.model,
            "input": inputs,
        }
        if system_instruction:
            create_kwargs["system_instruction"] = system_instruction
        if self.previous_interaction_id:
            create_kwargs["previous_interaction_id"] = self.previous_interaction_id
        if self.response_format:
            create_kwargs["response_format"] = self.response_format
        if "response_format" in kwargs:
            create_kwargs["response_format"] = kwargs["response_format"]

        interaction = client.interactions.create(**create_kwargs)
        self.last_interaction_id = getattr(interaction, "id", None)

        output_text = getattr(interaction, "output_text", "")
        if not output_text and hasattr(interaction, "steps"):
            for step in getattr(interaction, "steps", []):
                for part in getattr(step, "content", []):
                    if getattr(part, "type", None) == "text" or hasattr(part, "text"):
                        output_text += getattr(part, "text", "")

        usage_obj = getattr(interaction, "usage", None)
        input_tokens = 0
        output_tokens = 0
        thought_tokens = 0
        total_tokens = 0

        if usage_obj:
            if isinstance(usage_obj, dict):
                input_tokens = usage_obj.get("total_input_tokens", 0)
                output_tokens = usage_obj.get("total_output_tokens", 0)
                thought_tokens = usage_obj.get("total_thought_tokens", 0)
                total_tokens = usage_obj.get("total_tokens", 0)
            else:
                input_tokens = getattr(usage_obj, "total_input_tokens", 0) or 0
                output_tokens = getattr(usage_obj, "total_output_tokens", 0) or 0
                thought_tokens = getattr(usage_obj, "total_thought_tokens", 0) or 0
                total_tokens = getattr(usage_obj, "total_tokens", 0) or 0

        token_tracker.record_usage(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            thought_tokens=thought_tokens,
            total_tokens=total_tokens,
            interaction_id=self.last_interaction_id,
            call_type=kwargs.get("call_type", "interactions_api")
        )

        ai_message = AIMessage(
            content=output_text,
            response_metadata={
                "interaction_id": self.last_interaction_id,
                "model": self.model,
                "status": getattr(interaction, "status", "completed"),
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "thought_tokens": thought_tokens,
                    "total_tokens": total_tokens,
                }
            },
            usage_metadata={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            }
        )
        return ChatResult(generations=[ChatGeneration(message=ai_message)])

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        return await asyncio.to_thread(self._generate, messages, stop, **kwargs)


class LLMClient:
    """LangChain client using NouSetsu provider routing when embedded in the desktop app."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = (
            api_key
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
            or GEMINI_API_KEY
        )
        self.last_interaction_id: Optional[str] = None
        self._provider_factory = None
        self._settings_resolver = None
        integration = _load_nousetsu_llm_integration() if api_key is None else None

        if integration:
            self._provider_factory, self._settings_resolver = integration
            settings = self._settings_resolver()
            self.model = model or settings.model
            self._chat_model = self._build_provider_model()
            if not _has_configured_provider(self._chat_model):
                self._chat_model = None
        else:
            self.model = model or GEMINI_MODEL
            self._chat_model = ChatGeminiInteractions(
                model=self.model,
                api_key=self.api_key,
            ) if self.api_key else None

        if not self._chat_model:
            logger.warning(
                "No API key is configured for scraper model %s. Operating in heuristic fallback mode.",
                self.model,
            )

    def _build_provider_model(self) -> Any:
        settings_resolver = self._settings_resolver
        provider_factory = self._provider_factory
        if settings_resolver is None or provider_factory is None:
            raise RuntimeError("NouSetsu provider routing is not initialized.")

        settings = settings_resolver()
        model = self.model or settings.model
        routed_model = provider_factory(
            model_name=model,
            fallback_model=settings.fallback_model,
        )
        primary = getattr(routed_model, "primary", None)
        fallback = getattr(routed_model, "fallback", None)
        primary_available = _has_configured_provider(primary)
        fallback_available = _has_configured_provider(fallback)
        if primary is not None and not primary_available and fallback_available:
            return fallback
        if primary_available and fallback is not None and not fallback_available:
            return primary
        return routed_model

    @property
    def is_available(self) -> bool:
        return self._chat_model is not None

    def get_chat_model(self, previous_interaction_id: Optional[str] = None) -> Any:
        """Get a configured provider model, preserving Gemini interaction chaining when available."""
        if self._provider_factory:
            chat_model = self._build_provider_model()
            if previous_interaction_id:
                primary = getattr(chat_model, "primary", chat_model)
                if hasattr(primary, "previous_interaction_id"):
                    primary.previous_interaction_id = previous_interaction_id
            return chat_model

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
        """Run the configured LangChain provider and parse its response into the requested schema."""
        if not self.is_available:
            raise RuntimeError("Scraper LLM provider is unavailable or not configured.")

        parser = PydanticOutputParser(pydantic_object=schema)

        prompt = ChatPromptTemplate.from_messages([
            ("system", "{system_instruction}\n\nStrict Output Requirement:\nReturn ONLY a valid JSON object matching this schema:\n{format_instructions}"),
            ("human", "{prompt_text}")
        ])

        chat = self.get_chat_model(previous_interaction_id=previous_interaction_id)

        chain = prompt | chat | parser

        prompt_input = {
            "system_instruction": system_instruction,
            "format_instructions": parser.get_format_instructions(),
            "prompt_text": prompt_text,
        }
        if self._provider_factory:
            from src.agent.llm.tracker import TokenCallbackHandler

            result = await chain.ainvoke(
                prompt_input,
                config={"callbacks": [TokenCallbackHandler(call_type="scraper")]},
            )
        else:
            result = await chain.ainvoke(prompt_input)

        candidates = [chat, getattr(chat, "primary", None), getattr(chat, "fallback", None)]
        self.last_interaction_id = next(
            (interaction_id for candidate in candidates if candidate is not None
             if (interaction_id := getattr(candidate, "last_interaction_id", None))),
            None,
        )
        return result
