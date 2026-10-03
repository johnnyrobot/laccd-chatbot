from __future__ import annotations

import asyncio
import hmac
import logging
import mimetypes
import os
import secrets

# StaticFiles uses Python's mimetypes module; the slim base image doesn't ship
# woff/woff2 mappings, so register them so font assets serve with the correct
# Content-Type instead of text/plain.
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from langfuse import Langfuse, get_client as get_langfuse_client

from app.config import settings
from app.services.privacy import langfuse_mask
from app.models import ChatRequest, ChatResponse, HealthResponse, IngestRequest, IngestResult, RefreshRequest
from app.rate_limiter import rate_limiter
from app.request_limits import ChatBodyLimitMiddleware
from app.routes.mobile_tools import router as mobile_tools_router
from app.security import (
    client_identity_from_request,
    require_admin_api_key,
    require_trusted_origin,
    resolve_under,
)
from app.services.openrouter_fallback import openrouter_fallback
from app.services.chat import get_orchestrator
from app.services.clarification import parse_pending
from app.services.graceful_fallback import graceful_clarifying_response
try:
    from app.services.guardrails.rails import guarded_answer
except ImportError:  # guardrails deps (nemoguardrails/fastembed) are optional; feature is off by default
    guarded_answer = None
from app.services.corpus import CorpusNormalizer, is_lamission_url, load_manifest
from app.services.crawl4ai_service import Crawl4AIService
from app.services.data_health import build_data_health_report
from app.services.gemini_file_search import GeminiFileSearchService
from app.services.neo4j_service import neo4j_service
from shared.campus_config import CAMPUSES, get_campus_from_host, get_sample_questions

try:
    from voice_agent.token_endpoint import (
        create_voice_token,
        is_configured as voice_configured,
        validated_livekit_url,
    )
except ImportError:
    create_voice_token = None
    voice_configured = lambda: False
    validated_livekit_url = lambda: None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

_CLIENT_LOG_HMAC_KEY = (
    os.getenv("CLIENT_LOG_HMAC_KEY", "").encode("utf-8") or secrets.token_bytes(32)
)

CACHE_VERSION = "81"
CRAWLER_DIRECTIVES = "noindex, nofollow, noarchive, nosnippet"
PUBLIC_CAMPUS_ORDER = (
    "LAMC",
    "LACC",
    "ELAC",
    "LAHC",
    "LAPC",
    "LASC",
    "LATTC",
    "LAVC",
    "WLAC",
)

app = FastAPI(title=settings.app_name, version="0.1.0")
_CHAT_CONCURRENCY = asyncio.Semaphore(max(1, settings.chat_max_concurrent_requests))


async def _bounded_chat(awaitable):
    async def run():
        async with _CHAT_CONCURRENCY:
            return await awaitable

    return await asyncio.wait_for(
        run(),
        timeout=max(1.0, settings.chat_request_timeout_seconds),
    )


def _resolve_cors_origins() -> list[str]:
    """Resolve the CORS allowlist.

    In production, when ``CORS_ALLOW_ORIGINS`` is left unset, default to the 9
    campus domains (defense-in-depth) rather than the wide-open ``*``. Both the
    short ``<campus>.chat`` hosts and the legacy ``*-chat.johnnyrobot.ai`` hosts
    are allowed, so the allowlist stays correct throughout the DNS migration. An
    explicit ``CORS_ALLOW_ORIGINS`` — including ``*`` — always takes precedence,
    so the wildcard stays available for local dev / an intentional override.
    """
    configured = os.getenv("CORS_ALLOW_ORIGINS")
    if configured is None and settings.app_env == "production":
        origins: list[str] = []
        for campus in CAMPUSES.values():
            origins.append(f"https://{campus.code.lower()}.chat")
            origins.append(f"https://{campus.subdomain}.johnnyrobot.ai")
        return origins
    return [origin.strip() for origin in settings.cors_allow_origins.split(",") if origin.strip()]


origins = _resolve_cors_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(ChatBodyLimitMiddleware, max_bytes=settings.chat_max_body_bytes)


@app.middleware("http")
async def add_crawler_directives(request: Request, call_next):
    """Keep every app response out of indexes, archives, and snippets."""

    response = await call_next(request)
    response.headers["X-Robots-Tag"] = CRAWLER_DIRECTIVES
    livekit_url = validated_livekit_url()
    connect_src = "'self'" + (f" {livekit_url}" if livekit_url else "")
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
        f"connect-src {connect_src}; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Permissions-Policy"] = "camera=(), geolocation=()"
    return response


if settings.langfuse_secret_key:
    # Construct the Langfuse singleton up front with a PII mask so that ALL
    # traced input/output/metadata (the @observe-captured args, set_current_trace_io,
    # Gemini generations, voice tool args) is redacted before leaving the process.
    # get_client() reuses this masked resource-manager instance thereafter.
    Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
        mask=langfuse_mask,
    )
    logger.info("Langfuse tracing enabled with PII masking (host=%s)", settings.langfuse_host)

# Mount mobile-app tool endpoints under /api/tools/*. Auth and per-client rate
# limiting are enforced by the router's own dependencies.
app.include_router(mobile_tools_router)
if settings.mobile_tool_token_hashes:
    logger.info(
        "Mobile tool API enabled at /api/tools/* (rate limit: %d req/min/client)",
        settings.mobile_tool_rate_per_minute,
    )
else:
    logger.warning(
        "Mobile tool API mounted but MOBILE_TOOL_TOKEN_HASHES is unset — endpoints will return 503"
    )

chat = get_orchestrator()
normalizer = CorpusNormalizer()
crawler = Crawl4AIService()
file_search = GeminiFileSearchService()

templates = Jinja2Templates(directory=str(settings.frontend_dir))


_snapshot_monitor_task: "asyncio.Task | None" = None


@app.on_event("startup")
async def _start_snapshot_monitor() -> None:
    """Watch how old the Program Mapper snapshot is.

    Production cannot reach the vendor, so program answers come from a snapshot
    replayed weekly from an unblocked machine. That fallback answers
    successfully, so a refresh that stops running would freeze the data while
    everything still looked healthy. The check lives here rather than in the
    refresh job because a notifier inside that job dies with it.
    """
    global _snapshot_monitor_task
    import asyncio

    from app.services.snapshot_monitor import run_periodic

    _snapshot_monitor_task = asyncio.create_task(run_periodic())


@app.on_event("shutdown")
async def shutdown_event():
    if _snapshot_monitor_task is not None:
        _snapshot_monitor_task.cancel()
    await neo4j_service.close()
    if settings.langfuse_secret_key:
        get_langfuse_client().flush()
        logger.info("Langfuse flushed on shutdown")


@app.on_event("startup")
async def _warn_if_no_trusted_proxy() -> None:
    """Loud warning when TRUSTED_PROXY_HOSTS is unset.

    Behind Caddy the app's socket peer is the proxy's internal IP, so with no
    trusted-proxy configured every request collapses to that single client
    identity — per-client rate limits then apply to *all* users at once
    (accidental self-DoS). Set TRUSTED_PROXY_HOSTS to Caddy's proxy-network
    address (see .env.example)."""
    if not settings.trusted_proxy_hosts:
        logger.warning(
            "TRUSTED_PROXY_HOSTS is empty — X-Forwarded-For will be ignored and "
            "every request behind a reverse proxy resolves to the SAME client "
            "identity, collapsing per-client rate limits onto one bucket. Set "
            "TRUSTED_PROXY_HOSTS to the proxy's address (see .env.example)."
        )


@app.on_event("startup")
async def _warm_guardrails_rails() -> None:
    """Pre-build the guardrails engine at startup (downloads the FastEmbed model
    and constructs LLMRails) off the request path, so the first user request
    doesn't stall the event loop on a synchronous cold start. Runs only when the
    feature is enabled and its deps are importable; failures are non-fatal (the
    engine falls back to lazy init on first request)."""
    if not (settings.guardrails_enabled and guarded_answer is not None):
        return
    import asyncio

    from app.services.guardrails.rails import get_rails

    try:
        await asyncio.get_running_loop().run_in_executor(None, get_rails)
        logger.info("Guardrails rails warmed at startup")
    except Exception:
        logger.exception("Guardrails warm-up failed; rails will lazy-init on first request")


@app.get("/", response_class=HTMLResponse)
async def serve_index(request: Request):
    host = request.headers.get("host", "")
    campus = get_campus_from_host(host)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "campus": campus,
        "q": get_sample_questions(campus.code),
        "cache_v": CACHE_VERSION,
    })


@app.get("/how-it-works", response_class=HTMLResponse)
async def serve_how_it_works(request: Request):
    host = request.headers.get("host", "")
    campus = get_campus_from_host(host)
    return templates.TemplateResponse("how-it-works.html", {
        "request": request,
        "campus": campus,
        "campuses": [CAMPUSES[code] for code in PUBLIC_CAMPUS_ORDER],
        "cache_v": CACHE_VERSION,
    })


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    services = {
        "api": "up",
        "voice_agent": "configured" if voice_configured() else "not_configured",
        "gemini_file_search": "configured" if file_search.is_configured() else "not_configured",
        "neo4j": "configured" if neo4j_service.is_configured() else "not_configured",
        "crawl4ai": (
            "available+browser_fallback"
            if crawler.is_available() and crawler.browser_fallback_available()
            else "available"
            if crawler.is_available()
            else "missing"
        ),
    }
    status = "healthy" if services["api"] == "up" else "degraded"
    return HealthResponse(status=status, services=services)


def _hash_client_id(client_id: str) -> str:
    """Keyed pseudonymous tag for a client identity.

    The rate-limit client id is (or is derived from) the raw client IP — PII we
    must never write to logs. HMAC prevents enumerating the small IP address
    space from log values; the short prefix keeps turns correlatable within a
    deployment window without exposing the raw identity.
    """
    return hmac.digest(
        _CLIENT_LOG_HMAC_KEY,
        (client_id or "unknown").encode("utf-8"),
        "sha256",
    ).hex()[:12]


def _log_chat_turn(*, client_id: str, campus: str, response: ChatResponse) -> None:
    """Emit exactly one structured INFO line per completed chat turn.

    Stable grep/Loki-parseable logfmt schema:
      event=chat_turn client_id=<hmac-sha256[:12]> campus=<CODE> tier=<...>
      route=<route> rate_limited=<bool> guardrail_refusal=<bool>

    `tier` collapses the exit path to one of gemini|guardrail_refusal|
    openrouter_fallback|graceful. NO message content is ever logged; the client
    id is hashed. A non-transient upstream error re-raises (surfaces as a 500
    with a stack trace) and is intentionally *not* logged as a chat_turn.
    """
    route = response.route
    if route == "guardrail_refusal":
        tier = "guardrail_refusal"
    elif route == "openrouter_fallback":
        tier = "openrouter_fallback"
    elif route == "clarifying_fallback":
        tier = "graceful"
    else:
        tier = "gemini"
    logger.info(
        "event=chat_turn client_id=%s campus=%s tier=%s route=%s rate_limited=%s guardrail_refusal=%s",
        _hash_client_id(client_id),
        campus,
        tier,
        route,
        str(bool(response.rate_limited)).lower(),
        str(route == "guardrail_refusal").lower(),
    )


@app.post("/api/chat", response_model=ChatResponse)
async def chat_endpoint(request: Request, body: ChatRequest) -> ChatResponse:
    history = [item.model_dump() for item in body.history] if body.history else None
    campus = body.campus
    if not campus:
        host = request.headers.get("host", "")
        campus = get_campus_from_host(host).code

    # Tier 1: Gemini (full quality with RAG, schedule, KG). Transient
    # upstream failures (Gemini 503/429, etc.) fall through to Tier 2 so
    # the student never sees a bare 500 — the chatbot stays conversational.
    client_id = client_identity_from_request(request)
    if not rate_limiter.check_text_chat(client_id):
        response = graceful_clarifying_response(body.message, campus_code=campus)
        response.rate_limited = True
        _log_chat_turn(client_id=client_id, campus=campus, response=response)
        return response

    # Client-supplied; parse_pending allowlists it and returns None for anything
    # malformed, so a stale browser tab degrades to a normal turn instead of 422ing.
    pending = parse_pending(body.pending)

    try:
        if settings.guardrails_enabled and guarded_answer is not None:
            resp = await _bounded_chat(guarded_answer(
                body.message, history=history, campus=campus,
                use_live_crawl=body.use_live_crawl, pending=pending,
            ))
            if resp.route == "guardrail_refusal":
                # An off-scope refusal reached no generation call — refund the
                # reserved slot so refusals can't exhaust the daily budget.
                rate_limiter.refund_text_chat(client_id)
            _log_chat_turn(client_id=client_id, campus=campus, response=resp)
            return resp
        if settings.guardrails_enabled and guarded_answer is None:
            logger.warning("guardrails_enabled but guardrails dependencies are unavailable; using orchestrator directly")
        resp = await _bounded_chat(chat.answer(
            body.message, history=history, campus=campus,
            use_live_crawl=body.use_live_crawl, pending=pending,
        ))
        _log_chat_turn(client_id=client_id, campus=campus, response=resp)
        return resp
    except Exception as exc:
        if not _is_transient_upstream_error(exc):
            raise
        logger.warning(
            "Gemini path failed (%s: %s); falling back",
            type(exc).__name__, str(exc)[:160],
        )

    # Tier 2: OpenRouter free fallback (general knowledge only). On
    # failure, fall through to a clarifying question so the student
    # can steer the conversation toward something the bot can answer.
    if rate_limiter.check_fallback():
        try:
            resp = await _bounded_chat(
                asyncio.to_thread(
                    openrouter_fallback.answer,
                    body.message,
                    campus_code=campus,
                    history=history,
                )
            )
            if resp.route != "openrouter_fallback_error":
                _log_chat_turn(client_id=client_id, campus=campus, response=resp)
                return resp
        except Exception:
            logger.warning("OpenRouter fallback raised", exc_info=True)

    # Tier 3: Graceful clarifying response — never bare-error to the user.
    resp = graceful_clarifying_response(body.message, campus_code=campus)
    _log_chat_turn(client_id=client_id, campus=campus, response=resp)
    return resp


def _is_transient_upstream_error(exc: Exception) -> bool:
    """True for upstream API errors worth falling back on (Gemini 5xx /
    rate-limit / quota). Anything else (programmer errors, KeyError, etc.)
    re-raises so we get a real stack trace in the logs."""
    try:
        from google.genai.errors import APIError as GenaiAPIError  # type: ignore
    except Exception:
        GenaiAPIError = ()  # type: ignore
    if GenaiAPIError and isinstance(exc, GenaiAPIError):
        return True
    # httpx / network blips that bubble up uncaught.
    name = type(exc).__name__
    if name in {"TimeoutError", "ConnectError", "ReadTimeout", "ConnectTimeout", "RemoteProtocolError"}:
        return True
    return False


@app.get("/api/rate-limit", dependencies=[Depends(require_admin_api_key)])
async def rate_limit_status():
    """Check current daily usage status."""
    return rate_limiter.get_status()


@app.get("/api/data-health", dependencies=[Depends(require_admin_api_key)])
async def data_health():
    """Per-campus data-coverage diagnostics (eLumen / Program Mapper / Neo4j /
    Gemini File Search). Admin-guarded because it reveals internal store names
    and seed status."""
    return await build_data_health_report()


@app.post(
    "/api/ingest/bootstrap",
    response_model=IngestResult,
    dependencies=[Depends(require_admin_api_key)],
)
async def bootstrap_ingest(request: IngestRequest) -> IngestResult:
    corpus_dir = settings.corpus_dir
    if request.corpus_dir:
        corpus_dir = resolve_under(settings.corpus_dir, request.corpus_dir, field_name="corpus_dir")
    local_normalizer = CorpusNormalizer(
        corpus_dir=corpus_dir,
    )
    records = local_normalizer.normalize()
    notes = [f"Normalized {len(records)} HTML files from {local_normalizer.corpus_dir}."]
    synced = 0
    if file_search.is_configured():
        preview_paths = [record.text_path for record in records if record.source_type == "preview_html"]
        synced = file_search.upload_paths(preview_paths, display_prefix="preview-")
        notes.append(f"Uploaded {synced} preview corpus documents to Gemini File Search.")
    return IngestResult(
        processed_count=len(records),
        uploaded_count=synced,
        manifest_path=str(settings.manifest_path),
        notes=notes,
    )


@app.post(
    "/api/ingest/refresh",
    response_model=IngestResult,
    dependencies=[Depends(require_admin_api_key)],
)
async def refresh_ingest(request: RefreshRequest) -> IngestResult:
    if not crawler.is_available():
        raise HTTPException(status_code=503, detail="crawl4ai is unavailable in this environment")
    if request.urls:
        # SSRF guard: only crawl campus/LACCD domains, even for an admin caller
        # (a leaked ADMIN_API_KEY otherwise pivots into the internal network).
        rejected = [url for url in request.urls if not is_lamission_url(url)]
        if rejected:
            raise HTTPException(
                status_code=400,
                detail=f"urls must be http(s) LACCD campus domains; rejected: {rejected[:5]}",
            )
        urls = request.urls
    else:
        urls = await crawler.discover_seed_urls(limit=request.limit)
    notes = []
    refreshed = await crawler.crawl_and_persist(urls, limit=request.limit)
    notes.append(f"Crawled and persisted {len(refreshed)} live page(s) with Crawl4AI.")
    synced = 0
    if file_search.is_configured():
        synced = file_search.upload_paths([item["text_path"] for item in refreshed], display_prefix="crawl4ai-")
        notes.append(f"Uploaded {synced} Crawl4AI documents to Gemini File Search.")
    return IngestResult(
        processed_count=len(refreshed),
        uploaded_count=synced,
        manifest_path=str(settings.manifest_path),
        notes=notes,
    )


@app.post("/api/voice/token", dependencies=[Depends(require_trusted_origin)])
async def voice_token(request: Request, campus: str = ""):
    if create_voice_token is None or not voice_configured():
        raise HTTPException(status_code=503, detail="Voice agent is not configured")
    # Per-client cap (so one actor can't drain every campus's LiveKit budget)
    # plus the global daily cap. The origin guard blocks browser CSRF but a
    # scripted client can forge Origin, so the per-client limit is the real
    # anti-abuse control here.
    client_id = client_identity_from_request(request)
    if not rate_limiter.check_voice_call(client_id):
        raise HTTPException(
            status_code=429,
            detail="The voice assistant has reached its daily usage limit. Please come back tomorrow when the limit resets.",
        )
    campus_code = campus.upper() if campus else get_campus_from_host(
        request.headers.get("host", "")
    ).code
    result = await create_voice_token(campus_code)
    if result is None:
        raise HTTPException(status_code=503, detail="Voice agent is not configured")
    return result


@app.post("/api/ingest/purge-old", dependencies=[Depends(require_admin_api_key)])
async def purge_old_documents():
    deleted = file_search.delete_old_documents(min_year=2020)
    return {"deleted": deleted, "note": f"Removed {deleted} pre-2020 documents from Gemini File Search store."}


@app.get("/api/neo4j/status", dependencies=[Depends(require_admin_api_key)])
async def neo4j_status():
    if not neo4j_service.is_configured():
        return {"status": "not_configured"}
    connected = await neo4j_service.verify_connectivity()
    if not connected:
        # Don't leak the internal Bolt URI in a public response.
        return {"status": "unreachable"}
    nodes = await neo4j_service.get_node_counts()
    rels = await neo4j_service.get_relationship_counts()
    return {
        "status": "connected",
        "node_counts": nodes,
        "relationship_counts": rels,
    }


@app.get("/api/neo4j/renumberings", dependencies=[Depends(require_admin_api_key)])
async def neo4j_renumberings(campus: str = "LAMC"):
    if not neo4j_service.is_configured():
        raise HTTPException(status_code=503, detail="Neo4j not configured")
    renumberings = await neo4j_service.get_all_renumberings(campus.upper())
    return {"campus": campus.upper(), "renumberings": renumberings, "count": len(renumberings)}


@app.get("/api/neo4j/ge-courses", dependencies=[Depends(require_admin_api_key)])
async def neo4j_ge_courses(ge_code: str, system: str = "IGETC", campus: str = "LAMC"):
    if not neo4j_service.is_configured():
        raise HTTPException(status_code=503, detail="Neo4j not configured")
    courses = await neo4j_service.get_ge_courses(ge_code, system, campus.upper())
    return {"ge_code": ge_code, "system": system, "campus": campus.upper(), "courses": courses, "count": len(courses)}


@app.get("/api/neo4j/course-info", dependencies=[Depends(require_admin_api_key)])
async def neo4j_course_info(subject: str, number: str, campus: str = "LAMC"):
    if not neo4j_service.is_configured():
        raise HTTPException(status_code=503, detail="Neo4j not configured")
    transfer = await neo4j_service.get_course_transfer_status(subject.upper(), number, campus.upper())
    ge_areas = await neo4j_service.get_course_ge_areas(subject.upper(), number, campus.upper())
    cross_refs = await neo4j_service.find_course_cross_refs(subject.upper(), number, campus.upper())
    prereqs = await neo4j_service.get_prerequisites(subject.upper(), number, campus.upper())
    return {
        "subject": subject.upper(),
        "number": number,
        "campus": campus.upper(),
        "transfer": transfer,
        "ge_areas": ge_areas,
        "cross_references": cross_refs,
        "prerequisites": prereqs,
    }


if settings.frontend_dir.exists():
    app.mount("/", StaticFiles(directory=settings.frontend_dir), name="frontend")
