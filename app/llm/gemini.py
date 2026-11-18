"""Gemini implementation using the google-genai SDK with manual (not automatic) function calling."""
from __future__ import annotations

import time
from typing import Any

from google import genai
from google.genai import errors, types

from app.llm.base import LLMError, T, ToolCall, ToolSpec, Turn

RETRYABLE = {429, 500, 503}


class GeminiLLM:
    name = "gemini"

    def __init__(self, api_key: str, model: str):
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set (or use LLM_PROVIDER=fake for offline mode)")
        self.client = genai.Client(api_key=api_key)
        self.model = model

    # ------------------------------------------------------------------ helpers
    def _call(self, contents: Any, config: types.GenerateContentConfig) -> types.GenerateContentResponse:
        # Free-tier keys regularly see 429 (rate limit) and 503 (model overloaded); both clear within seconds
        # to a minute, so back off exponentially: 3 + 6 + 12 + 24 s before giving up.
        delay, attempts = 3.0, 5
        for attempt in range(attempts):
            try:
                return self.client.models.generate_content(model=self.model, contents=contents, config=config)
            except errors.APIError as e:
                if e.code in RETRYABLE and attempt < attempts - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise LLMError(f"Gemini error {e.code}: {e.message}") from e
        raise LLMError("unreachable")

    # ------------------------------------------------------------------ structured / text
    def structured(self, prompt: str, schema: type[T], *, system: str | None = None) -> T:
        resp = self._call(
            prompt,
            types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=schema,
                temperature=0.1,
            ),
        )
        if isinstance(resp.parsed, schema):
            return resp.parsed
        if not resp.text:
            raise LLMError("empty structured response")
        return schema.model_validate_json(resp.text)  # raises if the model broke the schema

    def text(self, prompt: str, *, system: str | None = None) -> str:
        resp = self._call(prompt, types.GenerateContentConfig(system_instruction=system, temperature=0.4))
        return resp.text or ""

    # ------------------------------------------------------------------ tool calling
    def step(self, system: str, history: list[Turn], tools: list[ToolSpec]) -> Turn:
        declarations = [
            types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
            for t in tools
        ]
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.2,
            tools=[types.Tool(function_declarations=declarations)] if declarations else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        resp = self._call([self._to_content(t) for t in history], config)
        if not resp.candidates or resp.candidates[0].content is None:
            return Turn(role="assistant", text="Sorry, I couldn't generate a response for that.")
        content = resp.candidates[0].content
        parts = content.parts or []
        calls = [
            ToolCall(id=p.function_call.id or f"call_{i}", name=p.function_call.name, args=dict(p.function_call.args or {}))
            for i, p in enumerate(parts)
            if p.function_call is not None
        ]
        text = "".join(p.text for p in parts if p.text and not p.thought)
        return Turn(role="assistant", text=text, tool_calls=calls, raw=content)

    @staticmethod
    def _to_content(turn: Turn) -> types.Content:
        if turn.raw is not None:
            return turn.raw
        if turn.role == "user":
            return types.Content(role="user", parts=[types.Part.from_text(text=turn.text)])
        if turn.role == "assistant":
            parts = [types.Part.from_text(text=turn.text)] if turn.text else []
            parts += [types.Part(function_call=types.FunctionCall(name=c.name, args=c.args)) for c in turn.tool_calls]
            return types.Content(role="model", parts=parts)
        return types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=None if r.call_id.startswith("call_") else r.call_id, name=r.name, response=r.content
                    )
                )
                for r in turn.tool_results
            ],
        )
