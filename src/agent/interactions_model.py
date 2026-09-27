import json
import logging
from typing import Any, List, Optional, Mapping, Dict
from pydantic import Field, ConfigDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    BaseMessage,
    AIMessage,
    SystemMessage,
    HumanMessage,
)
from langchain_core.outputs import ChatResult, ChatGeneration
from langchain_core.callbacks import CallbackManagerForLLMRun, AsyncCallbackManagerForLLMRun

from google import genai
from src.config import GEMINI_API_KEY, GEMINI_MODEL

logger = logging.getLogger("agent.interactions")

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
                
        # If single text input from human, pass as simple string
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

        # Extract token usage from Gemini Interactions API
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
                
        # Record in central TokenTracker
        from src.agent.token_tracker import token_tracker
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
        # Client handles sync network calls, run in threadpool for async safety
        import asyncio
        return await asyncio.to_thread(self._generate, messages, stop, **kwargs)
