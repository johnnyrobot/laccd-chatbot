from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ChatHistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=1_500)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4_000)
    conversation_id: str | None = Field(default=None, max_length=128)
    history: list[ChatHistoryItem] | None = Field(default=None, max_length=10)
    campus: str | None = Field(default=None, max_length=8)
    use_live_crawl: bool = False
    # A clarify-back the client is answering. Echoed back verbatim from the
    # previous ChatResponse.pending. UNTRUSTED — validated/allowlisted by
    # services.clarification.parse_pending, which drops anything unexpected.
    # Deliberately `Any`: a stale tab echoing an old shape must DEGRADE to a
    # normal turn, and typing this as a dict makes Pydantic 422 before
    # parse_pending ever runs.
    pending: Any = None


class Citation(BaseModel):
    title: str
    source_url: str | None = None
    local_path: str | None = None
    snippet: str | None = None


class ChatResponse(BaseModel):
    answer: str
    route: str
    citations: list[Citation] = Field(default_factory=list)
    tool_payload: dict[str, Any] | None = None
    detected_lang: str | None = None
    fallback_mode: bool = False
    rate_limited: bool = False
    # Set when this answer is a clarifying question. The client echoes it back on
    # the next turn so the reply can be slot-filled instead of re-derived.
    pending: dict[str, Any] | None = None


class IngestRequest(BaseModel):
    corpus_dir: str | None = None


class RefreshRequest(BaseModel):
    urls: list[str] = Field(default_factory=list, max_length=50)
    limit: int = Field(default=10, ge=1, le=100)

    @field_validator("urls")
    @classmethod
    def validate_urls(cls, urls: list[str]) -> list[str]:
        if any(len(url) > 2_048 for url in urls):
            raise ValueError("crawl URLs must be at most 2048 characters")
        return urls


class DocumentRecord(BaseModel):
    doc_id: str
    title: str
    source_url: str | None = None
    local_path: str
    source_type: Literal["preview_html", "crawl4ai"]
    content_hash: str
    last_ingested_at: datetime
    normalized_path: str
    text_path: str
    headings: list[str] = Field(default_factory=list)
    text_excerpt: str | None = None


class IngestResult(BaseModel):
    processed_count: int
    uploaded_count: int = 0
    manifest_path: str
    notes: list[str] = Field(default_factory=list)


class ScheduleCourseRequest(BaseModel):
    course_prefix: str
    course_number: str


class AvailabilityResponse(BaseModel):
    term: str
    campus_scope: str
    results: list[dict[str, Any]]


class HealthResponse(BaseModel):
    status: Literal["healthy", "degraded"]
    services: dict[str, str]
