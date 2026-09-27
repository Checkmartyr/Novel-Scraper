import pytest
from src.agent.token_tracker import TokenTracker, TokenCallbackHandler
from src.scraper.storage import NovelStorage
from langchain_core.outputs import LLMResult, Generation
from langchain_core.messages import AIMessage

def test_token_tracker_accumulation():
    tracker = TokenTracker()
    tracker.reset()
    
    tracker.record_usage(
        prompt_tokens=100,
        completion_tokens=50,
        thought_tokens=25,
        total_tokens=175,
        call_type="classification"
    )
    
    tracker.record_usage(
        prompt_tokens=200,
        completion_tokens=80,
        thought_tokens=0,
        total_tokens=280,
        call_type="dom_analysis"
    )
    
    summary = tracker.get_summary()
    assert summary["prompt_tokens"] == 300
    assert summary["completion_tokens"] == 130
    assert summary["thought_tokens"] == 25
    assert summary["total_tokens"] == 455
    assert summary["call_count"] == 2
    assert summary["last_call"]["call_type"] == "dom_analysis"

def test_token_callback_handler():
    from langchain_core.outputs import ChatGeneration
    tracker = TokenTracker()
    tracker.reset()
    handler = TokenCallbackHandler(tracker=tracker, call_type="test_chain")
    
    # Create mock LLMResult with ChatGeneration containing usage_metadata
    message = AIMessage(
        content="Test content",
        usage_metadata={"input_tokens": 150, "output_tokens": 60, "total_tokens": 210}
    )
    llm_result = LLMResult(generations=[[ChatGeneration(text="Test content", message=message)]])
    
    handler.on_llm_end(llm_result)
    summary = tracker.get_summary()
    assert summary["prompt_tokens"] == 150
    assert summary["completion_tokens"] == 60
    assert summary["total_tokens"] == 210
    assert summary["call_count"] == 1

@pytest.mark.asyncio
async def test_token_storage_metadata(tmp_path):
    storage = NovelStorage(base_output_dir=tmp_path)
    tracker = TokenTracker()
    tracker.reset()
    tracker.record_usage(prompt_tokens=500, completion_tokens=100, total_tokens=600)
    
    meta_path = await storage.save_metadata(
        novel_title="Token Test Novel",
        source_url="https://example.com/novel",
        token_usage=tracker.get_summary()
    )
    
    assert meta_path.is_file()
    import json
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    assert "token_usage" in data
    assert data["token_usage"]["total_tokens"] == 600
    assert data["token_usage"]["prompt_tokens"] == 500
    assert data["token_usage"]["completion_tokens"] == 100
