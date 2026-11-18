from functools import lru_cache

from app.config import get_settings
from app.llm.base import LLM, LLMError, ToolCall, ToolResult, ToolSpec, Turn


@lru_cache
def get_llm() -> LLM:
    s = get_settings()
    if s.llm_provider == "fake":
        from app.llm.fake import OfflineLLM

        return OfflineLLM()
    from app.llm.gemini import GeminiLLM

    return GeminiLLM(api_key=s.gemini_api_key, model=s.gemini_model)


__all__ = ["LLM", "LLMError", "ToolCall", "ToolResult", "ToolSpec", "Turn", "get_llm"]
