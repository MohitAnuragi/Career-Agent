"""Provider-neutral types for structured output and tool calling."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for the arguments object


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: dict[str, Any]


@dataclass
class Turn:
    role: Literal["user", "assistant", "tool"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    # Provider-native message, replayed verbatim on the next call. Gemini attaches thought
    # signatures to function-call parts and rejects follow-up calls that drop them.
    raw: Any = None


class LLM(Protocol):
    name: str

    def structured(self, prompt: str, schema: type[T], *, system: str | None = None) -> T:
        """Return an instance of `schema`, generated with JSON-schema constrained decoding."""

    def text(self, prompt: str, *, system: str | None = None) -> str:
        """Plain text completion."""

    def step(self, system: str, history: list[Turn], tools: list[ToolSpec]) -> Turn:
        """One model turn: returns an assistant Turn with text and/or tool calls."""
