from __future__ import annotations

from typing import Optional

from nemoguardrails.actions import action


@action(name="answer_laccd")
async def answer_laccd(context: Optional[dict] = None) -> dict:
    """On-scope answer path. Reads the current turn + campus/history from the
    NeMo context and delegates to the existing ChatOrchestrator, returning the
    full ChatResponse as a plain dict so Colang can render it and the Python
    wrapper can reconstruct citations/route.

    Imported lazily to avoid pulling the orchestrator (and its clients) into
    the guardrails config-load path.
    """
    context = context or {}
    campus = context.get("campus_code")
    history = context.get("history") or []
    use_live_crawl = bool(context.get("use_live_crawl", False))

    from app.services.chat import RewriteResult, get_orchestrator

    # guarded_answer() resolves the standalone query before the rails classify it
    # (see rails.guarded_answer), so `last_user_message` here is the REWRITTEN
    # query. Pass the student's actual turn to the orchestrator and hand it the
    # rewrite via `resolved=` so it isn't derived a second time.
    message: str = context.get("original_message") or context.get("last_user_message", "") or ""
    resolved_query = context.get("resolved_query")
    resolved = (
        RewriteResult(
            english_query=resolved_query,
            detected_lang=context.get("detected_lang") or "en",
        )
        if resolved_query
        else None
    )

    resp = await get_orchestrator().answer(
        message,
        history=history,
        campus=campus,
        use_live_crawl=use_live_crawl,
        resolved=resolved,
    )
    return resp.model_dump()
