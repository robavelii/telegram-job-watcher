"""Model providers. Each one turns (system prompt, user message) into JSON text that
matches RESULT_SCHEMA, or raises ScoreError. Neither gets tools.

Provider SDKs are imported inside the factories, so only the one in use has to load.
"""

from __future__ import annotations

import logging

from .scorer import RESULT_SCHEMA, ScoreError

log = logging.getLogger(__name__)

class AnthropicBackend:
    name = "anthropic"

    def __init__(self, client, model: str, effort: str = "medium", max_tokens: int = 8000):
        self.client = client
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens

    def request(self, system: str, user: str) -> dict:
        return {
            "model": self.model,
            # Thinking is on by default on current Haiku and counts toward max_tokens,
            # so leave room well above the size of the JSON answer.
            "max_tokens": self.max_tokens,
            # Same text for every post, so it is cached; only the user turn changes.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": RESULT_SCHEMA},
            },
        }

    async def complete(self, system: str, user: str) -> str:
        import anthropic

        try:
            response = await self.client.messages.create(**self.request(system, user))
        except anthropic.APIStatusError as exc:
            raise ScoreError(f"Claude API {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ScoreError(f"Claude API unreachable: {exc}") from exc

        usage = response.usage
        log.debug(
            "claude usage: in=%s cache_read=%s out=%s",
            usage.input_tokens,
            getattr(usage, "cache_read_input_tokens", None),
            usage.output_tokens,
        )
        if response.stop_reason == "refusal":
            raise ScoreError("Claude declined to assess this post")
        if response.stop_reason == "max_tokens":
            raise ScoreError("Claude hit max_tokens before finishing")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise ScoreError(f"Claude returned no text (stop_reason={response.stop_reason})")
        return text


class GeminiBackend:
    name = "gemini"

    def __init__(self, client, model: str, max_output_tokens: int = 8000):
        self.client = client
        self.model = model
        self.max_output_tokens = max_output_tokens

    def config(self, system: str):
        from google.genai import types

        return types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=RESULT_SCHEMA,
            max_output_tokens=self.max_output_tokens,
        )

    async def complete(self, system: str, user: str) -> str:
        from google.genai import errors

        try:
            response = await self.client.aio.models.generate_content(
                model=self.model, contents=user, config=self.config(system)
            )
        except errors.ClientError as exc:
            # 429 here means retries ran out: the free tier's per-minute or daily quota.
            raise ScoreError(f"Gemini API {exc.code}: {exc.message}") from exc
        except errors.ServerError as exc:
            raise ScoreError(f"Gemini API {exc.code}: {exc.message}") from exc
        except errors.APIError as exc:
            raise ScoreError(f"Gemini API error: {exc}") from exc
        except Exception as exc:  # network failures surface as httpx errors
            raise ScoreError(f"Gemini API unreachable: {type(exc).__name__}: {exc}") from exc

        feedback = getattr(response, "prompt_feedback", None)
        if feedback is not None and getattr(feedback, "block_reason", None):
            raise ScoreError(f"Gemini blocked the prompt: {feedback.block_reason}")
        candidates = response.candidates or []
        if not candidates:
            raise ScoreError("Gemini returned no candidates")
        reason = getattr(candidates[0].finish_reason, "name", str(candidates[0].finish_reason))
        if reason != "STOP":
            raise ScoreError(f"Gemini stopped early: {reason}")
        usage = response.usage_metadata
        if usage is not None:
            log.debug(
                "gemini usage: in=%s cached=%s out=%s",
                usage.prompt_token_count,
                usage.cached_content_token_count,
                usage.candidates_token_count,
            )
        text = response.text
        if not text:
            raise ScoreError("Gemini returned no text")
        return text


def make_backend(provider: str, model: str, effort: str = "medium"):
    if provider == "anthropic":
        import anthropic

        return AnthropicBackend(anthropic.AsyncAnthropic(max_retries=4, timeout=120), model, effort)
    if provider == "gemini":
        from google import genai
        from google.genai import types

        # Reads GEMINI_API_KEY. The retries cover the free tier's per-minute limit (429)
        # and transient 5xx; a spent daily quota still fails after the last attempt.
        client = genai.Client(
            http_options=types.HttpOptions(
                timeout=120_000,  # milliseconds
                retry_options=types.HttpRetryOptions(attempts=5, initial_delay=10, max_delay=120),
            )
        )
        return GeminiBackend(client, model)
    raise ValueError(f"unknown LLM_PROVIDER {provider!r}")
