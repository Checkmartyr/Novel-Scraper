import threading
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

class TokenUsageRecord(BaseModel):
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    prompt_tokens: int = 0
    completion_tokens: int = 0
    thought_tokens: int = 0
    total_tokens: int = 0
    interaction_id: Optional[str] = None
    call_type: str = "general"

class TokenTracker:
    """Thread-safe tracker for Gemini Interactions API token usage."""
    
    _instance: Optional["TokenTracker"] = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(TokenTracker, cls).__new__(cls)
                cls._instance._init_tracker()
            return cls._instance

    def _init_tracker(self) -> None:
        self._records: List[TokenUsageRecord] = []
        self._total_prompt: int = 0
        self._total_completion: int = 0
        self._total_thought: int = 0
        self._total_tokens: int = 0
        self._lock_state = threading.Lock()

    def record_usage(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        thought_tokens: int = 0,
        total_tokens: int = 0,
        interaction_id: Optional[str] = None,
        call_type: str = "general",
    ) -> TokenUsageRecord:
        """Record a single interaction's token consumption."""
        if total_tokens == 0:
            total_tokens = prompt_tokens + completion_tokens + thought_tokens
            
        record = TokenUsageRecord(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            thought_tokens=thought_tokens,
            total_tokens=total_tokens,
            interaction_id=interaction_id,
            call_type=call_type,
        )
        
        with self._lock_state:
            self._records.append(record)
            self._total_prompt += prompt_tokens
            self._total_completion += completion_tokens
            self._total_thought += thought_tokens
            self._total_tokens += total_tokens
            
        return record

    def get_summary(self) -> Dict[str, Any]:
        """Return cumulative token usage summary."""
        with self._lock_state:
            return {
                "prompt_tokens": self._total_prompt,
                "completion_tokens": self._total_completion,
                "thought_tokens": self._total_thought,
                "total_tokens": self._total_tokens,
                "call_count": len(self._records),
                "last_call": self._records[-1].model_dump() if self._records else None,
            }

    def reset(self) -> None:
        """Reset all tracking counters."""
        with self._lock_state:
            self._records.clear()
            self._total_prompt = 0
            self._total_completion = 0
            self._total_thought = 0
            self._total_tokens = 0

# Global singleton
token_tracker = TokenTracker()

class TokenCallbackHandler(BaseCallbackHandler):
    """LangChain callback handler to capture token usage from LLMResult."""

    def __init__(self, tracker: Optional[TokenTracker] = None, call_type: str = "langchain_call"):
        super().__init__()
        self.tracker = tracker or token_tracker
        self.call_type = call_type

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Capture usage_metadata or token_usage if present in LLM output."""
        recorded = False
        
        # 1. Check response.llm_output
        if response.llm_output and "token_usage" in response.llm_output:
            usage = response.llm_output["token_usage"]
            self.tracker.record_usage(
                prompt_tokens=usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0) or usage.get("output_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                call_type=self.call_type
            )
            recorded = True

        # 2. Check ChatGeneration messages
        if not recorded and response.generations:
            for generations in response.generations:
                for gen in generations:
                    message = getattr(gen, "message", None)
                    if message:
                        usage = getattr(message, "usage_metadata", None)
                        if usage:
                            self.tracker.record_usage(
                                prompt_tokens=usage.get("input_tokens", 0),
                                completion_tokens=usage.get("output_tokens", 0),
                                total_tokens=usage.get("total_tokens", 0),
                                call_type=self.call_type
                            )
