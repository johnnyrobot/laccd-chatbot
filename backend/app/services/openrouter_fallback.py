"""OpenRouter free model fallback for when Gemini daily limits are hit.

Uses the openrouter/free model router which auto-selects from available
free models (Llama, Gemma, Nemotron, etc.). No tool calling — just
simple question/answer with general knowledge.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.models import ChatResponse
from shared.campus_config import get_campus

logger = logging.getLogger(__name__)

try:
    from openrouter import OpenRouter
except ImportError:
    OpenRouter = None


class OpenRouterFallback:
    def __init__(self) -> None:
        self._client = None

    def is_configured(self) -> bool:
        return bool(OpenRouter is not None and settings.openrouter_api_key)

    @property
    def client(self):
        if self._client is None and self.is_configured():
            self._client = OpenRouter(api_key=settings.openrouter_api_key)
        return self._client

    def answer(self, question: str, campus_code: str = "LAMC", history: list | None = None) -> ChatResponse:
        if not self.is_configured():
            return ChatResponse(
                answer="The assistant is temporarily unavailable. Please try again later.",
                route="openrouter_fallback_unconfigured",
                rate_limited=True,
            )

        campus = get_campus(campus_code)
        system_prompt = (
            f"You are a helpful assistant for {campus.name}, part of the "
            f"Los Angeles Community College District (LACCD). "
            f"Answer the student's question using your general knowledge about community colleges. "
            f"You do NOT have access to the college's live schedule, specific course catalog, "
            f"or campus documents right now. If you're unsure about campus-specific details "
            f"(like exact course offerings, office hours, or phone numbers), say so and "
            f"suggest the student check {campus.name}'s website or call the campus directly."
        )

        messages = [{"role": "system", "content": system_prompt}]
        if history:
            for h in history[-6:]:  # Keep last 6 messages for context
                role = h.get("role", "user")
                content = h.get("content", "")
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": question})

        try:
            response = self.client.chat.send(
                model="openrouter/free",
                messages=messages,
                max_tokens=512,
                timeout_ms=30_000,
            )
            answer_text = response.choices[0].message.content
            logger.info("OpenRouter fallback response for campus=%s, length=%d", campus_code, len(answer_text))

            return ChatResponse(
                answer=answer_text,
                route="openrouter_fallback",
                fallback_mode=True,
            )
        except Exception:
            logger.warning("OpenRouter fallback failed", exc_info=True)
            return ChatResponse(
                answer="The assistant is temporarily unavailable. Please try again later.",
                route="openrouter_fallback_error",
                rate_limited=True,
            )


# Module-level singleton
openrouter_fallback = OpenRouterFallback()
