"""
Thin Claude (Anthropic) client wrapper.

Deliberately minimal and generic — the actual bounded tool-use logic and
structured-output extraction live in app/agent/tools/, not here. This module
exists so every call site depends on a small, swappable interface
(`LLMClient` Protocol) rather than the Anthropic SDK directly, matching the
same pattern as Phase 1's WhatsAppSender: tests inject a fake, nothing in the
test suite makes a real Anthropic API call (no key exists in this sandbox).

Own dataclasses (not the SDK's) are used for responses so fakes in tests
don't need the real `anthropic` package's types at all.
"""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class LLMTextBlock:
    text: str


@dataclass(frozen=True)
class LLMToolUseBlock:
    tool_use_id: str
    tool_name: str
    tool_input: dict[str, Any]


LLMContentBlock = LLMTextBlock | LLMToolUseBlock


@dataclass(frozen=True)
class LLMResponse:
    content: list[LLMContentBlock]
    stop_reason: str  # "end_turn" | "tool_use" | "max_tokens" | ...

    @property
    def tool_use_blocks(self) -> list[LLMToolUseBlock]:
        return [b for b in self.content if isinstance(b, LLMToolUseBlock)]

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, LLMTextBlock))


class LLMProviderError(Exception):
    """Any recoverable failure calling the LLM provider (maps to
    incomplete_reason='provider_unavailable', decisions.md §1A)."""


class LLMProviderTimeout(LLMProviderError):
    """Maps to incomplete_reason='timeout' (decisions.md §1A)."""


class LLMClient(Protocol):
    async def create_message(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        max_tokens: int = 1024,
    ) -> LLMResponse: ...


class AnthropicLLMClient:
    """Real implementation, backed by the `anthropic` SDK. Never imported by
    tests — see tests/conftest.py's FakeLLMClient."""

    def __init__(self, api_key: str, model: str, timeout_seconds: float = 30.0) -> None:
        import anthropic  # imported lazily so the SDK is only required when this class is used

        # The SDK requires a non-empty api_key at construction time. Like the
        # lazy Redis/Postgres clients, we don't want a missing credential to
        # fail app *startup* — only an actual call without a valid key should
        # fail, as an LLMProviderError the orchestrator already handles.
        self._client = anthropic.AsyncAnthropic(api_key=api_key or "not-configured", timeout=timeout_seconds)
        self._model = model

    async def create_message(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        import anthropic

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice

        try:
            response = await self._client.messages.create(**kwargs)
        except anthropic.APITimeoutError as exc:
            raise LLMProviderTimeout(str(exc)) from exc
        except anthropic.APIError as exc:
            raise LLMProviderError(str(exc)) from exc

        content: list[LLMContentBlock] = []
        for block in response.content:
            if block.type == "text":
                content.append(LLMTextBlock(text=block.text))
            elif block.type == "tool_use":
                content.append(
                    LLMToolUseBlock(tool_use_id=block.id, tool_name=block.name, tool_input=block.input)
                )
        return LLMResponse(content=content, stop_reason=response.stop_reason or "end_turn")
