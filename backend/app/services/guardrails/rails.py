from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nemoguardrails import LLMRails, RailsConfig
from nemoguardrails.types import LLMResponse, LLMResponseChunk

from app.config import settings
from app.models import ChatResponse
from app.services.guardrails.actions import answer_laccd
from shared.campus_config import get_campus

if TYPE_CHECKING:  # avoid importing the orchestrator at guardrails config-load time
    from app.services.chat import RewriteResult
    from app.services.clarification import Clarification

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parent / "config"
_rails: LLMRails | None = None


def _prompt_to_text(prompt: Any) -> str:
    """Flatten the NeMo LLMModel `prompt` (str or list[ChatMessage]) to plain text."""
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        parts: list[str] = []
        for msg in prompt:
            content = getattr(msg, "content", None)
            if content is None and isinstance(msg, dict):
                content = msg.get("content")
            if isinstance(content, str):
                parts.append(content)
        return "\n".join(parts)
    return str(prompt)


class GeminiRailsLLM:
    """'Main' LLM implementing the NeMo Guardrails 0.23 ``LLMModel`` protocol.

    Delegates to the existing Gemini client. Effectively unused on the hot path
    (embeddings-only intent matching + deterministic flows + predefined bot
    messages), but NeMo requires a main LLM to be present. Implemented directly
    against ``nemoguardrails.types.LLMModel`` because 0.23 dropped the hard
    LangChain dependency (``langchain_core`` is not installed) and the legacy
    ``llm=<LangChain LLM>`` path now requires LangChain."""

    @property
    def model_name(self) -> str:
        return "gemini_file_search"

    @property
    def provider_name(self) -> str | None:
        return "gemini_file_search"

    @property
    def provider_url(self) -> str | None:
        return None

    async def generate_async(self, prompt: Any, *, stop: Any = None, **kwargs: Any) -> LLMResponse:
        from app.services.chat import get_orchestrator

        text = _prompt_to_text(prompt)
        generate = get_orchestrator().tools.gemini_file_search.generate
        content = await asyncio.to_thread(generate, text)
        return LLMResponse(content=content or "")

    async def stream_async(self, prompt: Any, *, stop: Any = None, **kwargs: Any):
        resp = await self.generate_async(prompt, stop=stop, **kwargs)
        yield LLMResponseChunk(delta_content=resp.content, finish_reason="stop")


def get_rails() -> LLMRails:
    """Lazily build the rails engine. First call downloads the FastEmbed model."""
    global _rails
    if _rails is None:
        config = RailsConfig.from_path(str(_CONFIG_DIR))
        rails = LLMRails(config, llm=GeminiRailsLLM())
        rails.register_action(answer_laccd, name="answer_laccd")
        _rails = rails
    return _rails


async def guarded_answer(
    message: str,
    history: list[dict] | None = None,
    campus: str | None = None,
    use_live_crawl: bool = False,
    pending: "Clarification | None" = None,
) -> ChatResponse:
    """Public entry point. Runs the dialog rails; on-scope turns delegate to the
    orchestrator (full ChatResponse preserved), off-scope turns return a refusal
    without any generation call."""
    campus_code = (campus or settings.default_campus).upper()

    # A reply to one of our own clarify-backs continues a conversation the rails
    # already admitted, so it is in scope by construction. Classifying it on its
    # own ("UCLA", "MATH 31A") is exactly what matched `express greeting` and
    # swallowed the answer.
    #
    # But `pending` is echoed by the browser and therefore FORGEABLE: attaching one
    # to an off-topic message must not buy a free pass around the rails. Only a
    # genuine continuation skips classification; a topic change is just a new turn,
    # and is classified like one.
    if pending is not None:
        from app.services.chat import get_orchestrator

        orchestrator = get_orchestrator()
        if orchestrator.is_clarification_continuation(pending, message):
            return await orchestrator.answer(
                message, history=history, campus=campus,
                use_live_crawl=use_live_crawl, pending=pending,
            )
        pending = None  # topic change — fall through to the rails

    try:
        rails = get_rails()
        campus_name = get_campus(campus_code).name

        # Resolve the standalone query BEFORE the rails see it. NeMo's
        # embeddings-only matching classifies the latest user message in
        # isolation, so a terse reply to one of our own clarify-backs ("UCLA",
        # "MATH 31A") carries no scope signal and lands on `express greeting`.
        # Classifying the rewrite ("Is STAT C1000 transferable to UCLA?")
        # gives the rails the context they need to see it's on-scope.
        #
        # Only when there IS history: a first turn has nothing to resolve against
        # (resolve_query would no-op), and skipping it keeps the opening-turn
        # refusal path free of any generation call — refusals must not spend the
        # Gemini budget they get refunded for.
        resolved: RewriteResult | None = None
        if history:
            from app.services.chat import get_orchestrator

            resolved = get_orchestrator().resolve_query(message, history)
        classified = resolved.english_query if resolved else message

        messages: list[dict] = [
            {
                "role": "context",
                "content": {
                    "campus_code": campus_code,
                    "campus_name": campus_name,
                    "history": history or [],
                    "use_live_crawl": use_live_crawl,
                    "original_message": message,
                    "resolved_query": resolved.english_query if resolved else None,
                    "detected_lang": resolved.detected_lang if resolved else None,
                },
            }
        ]
        messages.extend(history or [])
        messages.append({"role": "user", "content": classified})

        res = await rails.generate_async(
            messages=messages,
            options={"output_vars": ["laccd"], "rails": ["dialog"]},
        )

        # With options set, generate_async returns a GenerationResponse-like object
        # exposing .response (list of messages) and .output_data (dict). Fall back
        # defensively to the plain-dict shape.
        out = getattr(res, "response", res)
        assistant = out[-1] if isinstance(out, list) else out
        content = assistant.get("content", "") if isinstance(assistant, dict) else str(assistant)
        output_data = getattr(res, "output_data", None) or {}

        payload = output_data.get("laccd")
        if payload:
            # On-scope: reconstruct the orchestrator's full response (route + citations).
            return ChatResponse(**payload)

        # Off-scope: the assistant content is the refusal message.
        return ChatResponse(answer=content, route="guardrail_refusal")

    except Exception:
        logger.exception("guardrails error for campus=%s", campus_code)
        if settings.guardrails_fail_open:
            from app.services.chat import get_orchestrator

            return await get_orchestrator().answer(
                message, history=history, campus=campus, use_live_crawl=use_live_crawl
            )
        raise
