from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_DIR / ".env", override=False)
load_dotenv(PROJECT_DIR / ".env.local", override=False)

BACKEND_DIR = PROJECT_DIR / "backend"
DEFAULT_CORPUS_DIR = PROJECT_DIR / "lamc_website_documents" / "preview-html"
DEFAULT_DATA_DIR = PROJECT_DIR / "data"
DEFAULT_NORMALIZED_DIR = DEFAULT_DATA_DIR / "normalized"
DEFAULT_CRAWL4AI_DIR = DEFAULT_DATA_DIR / "crawl4ai"
DEFAULT_CRAWL4AI_RAW_DIR = DEFAULT_CRAWL4AI_DIR / "raw"
DEFAULT_CRAWL4AI_NORMALIZED_DIR = DEFAULT_CRAWL4AI_DIR / "normalized"
DEFAULT_MANIFEST_PATH = DEFAULT_DATA_DIR / "manifest.jsonl"


@dataclass(slots=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "LAMC Chatbot")
    app_env: str = os.getenv("APP_ENV", "development")
    cors_allow_origins: str = os.getenv("CORS_ALLOW_ORIGINS", "*")
    corpus_dir: Path = Path(os.getenv("CORPUS_DIR", str(DEFAULT_CORPUS_DIR)))
    normalized_dir: Path = Path(os.getenv("NORMALIZED_DIR", str(DEFAULT_NORMALIZED_DIR)))
    crawl4ai_raw_dir: Path = Path(os.getenv("CRAWL4AI_RAW_DIR", str(DEFAULT_CRAWL4AI_RAW_DIR)))
    crawl4ai_normalized_dir: Path = Path(os.getenv("CRAWL4AI_NORMALIZED_DIR", str(DEFAULT_CRAWL4AI_NORMALIZED_DIR)))
    manifest_path: Path = Path(os.getenv("MANIFEST_PATH", str(DEFAULT_MANIFEST_PATH)))
    gemini_api_key: str | None = os.getenv("GEMINI_API_KEY")
    gemini_file_search_store: str | None = os.getenv("GEMINI_FILE_SEARCH_STORE")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
    gemini_lite_model: str = os.getenv("GEMINI_LITE_MODEL", "gemini-2.5-flash-lite")
    gemini_max_output_tokens: int = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "1024"))
    use_gemini_file_search: bool = os.getenv("USE_GEMINI_FILE_SEARCH", "true").lower() == "true"
    gemini_file_search_allow_default_fallback: bool = (
        os.getenv("GEMINI_FILE_SEARCH_ALLOW_DEFAULT_FALLBACK", "false").lower() == "true"
    )
    default_campus: str = os.getenv("DEFAULT_CAMPUS", "LAMC")
    langfuse_secret_key: str | None = os.getenv("LANGFUSE_SECRET_KEY")
    langfuse_public_key: str | None = os.getenv("LANGFUSE_PUBLIC_KEY")
    langfuse_host: str | None = os.getenv("LANGFUSE_HOST")
    openrouter_api_key: str | None = os.getenv("OPENROUTER_API_KEY")
    neo4j_uri: str | None = os.getenv("NEO4J_URI")
    neo4j_user: str | None = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password: str | None = os.getenv("NEO4J_PASSWORD")
    frontend_dir: Path = PROJECT_DIR / "frontend"
    assist_db_dir: Path = Path(os.getenv("ASSIST_DB_DIR", str(DEFAULT_DATA_DIR / "assist_db")))
    admin_api_key: str | None = os.getenv("ADMIN_API_KEY")
    voice_api_key: str | None = os.getenv("VOICE_API_KEY")
    trusted_proxy_hosts: tuple[str, ...] = tuple(
        host.strip()
        for host in os.getenv("TRUSTED_PROXY_HOSTS", "").split(",")
        if host.strip()
    )
    # Global daily budget caps (env-configurable knobs; defaults mirror the
    # constants in rate_limiter.py — ~$1.85/day of Gemini spend for the pilot).
    text_chat_daily_limit: int = int(os.getenv("TEXT_CHAT_DAILY_LIMIT", "2000"))
    voice_call_daily_limit: int = int(os.getenv("VOICE_CALL_DAILY_LIMIT", "20"))
    fallback_daily_limit: int = int(os.getenv("FALLBACK_DAILY_LIMIT", "500"))
    # Per-client caps.
    chat_rate_limit_per_minute: int = int(os.getenv("CHAT_RATE_LIMIT_PER_MINUTE", "20"))
    chat_rate_limit_per_day_per_client: int = int(os.getenv("CHAT_RATE_LIMIT_PER_DAY_PER_CLIENT", "100"))
    chat_max_body_bytes: int = int(os.getenv("CHAT_MAX_BODY_BYTES", "65536"))
    chat_max_concurrent_requests: int = int(os.getenv("CHAT_MAX_CONCURRENT_REQUESTS", "8"))
    chat_request_timeout_seconds: float = float(os.getenv("CHAT_REQUEST_TIMEOUT_SECONDS", "45"))
    voice_rate_limit_per_day_per_client: int = int(os.getenv("VOICE_RATE_LIMIT_PER_DAY_PER_CLIENT", "5"))
    mobile_tool_token_hashes: tuple[str, ...] = tuple(
        item.strip().lower()
        for item in os.getenv("MOBILE_TOOL_TOKEN_HASHES", "").split(",")
        if item.strip()
    )
    mobile_tool_rate_per_minute: int = int(os.getenv("MOBILE_TOOL_RATE_PER_MINUTE", "60"))
    elumen_cache_ttl_seconds: int = int(os.getenv("ELUMEN_CACHE_TTL_SECONDS", "86400"))
    elumen_negative_cache_ttl_seconds: int = int(os.getenv("ELUMEN_NEGATIVE_CACHE_TTL_SECONDS", "21600"))
    elumen_search_cache_ttl_seconds: int = int(os.getenv("ELUMEN_SEARCH_CACHE_TTL_SECONDS", "86400"))
    program_mapper_cache_ttl_seconds: int = int(os.getenv("PROGRAM_MAPPER_CACHE_TTL_SECONDS", "604800"))
    # How long to stop calling Program Mapper after it refuses this machine.
    # The vendor's AWS WAF 403s the VPS by IP, and only successes were cached,
    # so every program question paid a fresh ~100ms failure before falling back
    # to Neo4j. Set to 0 to disable the breaker entirely.
    program_mapper_breaker_cooldown_seconds: int = int(
        os.getenv("PROGRAM_MAPPER_BREAKER_COOLDOWN_SECONDS", "300")
    )
    # Production answers program questions from a weekly snapshot, because the
    # vendor's WAF refuses this box. 10 days is one missed weekly cycle plus
    # slack: launchd runs the refresh at the next wake if the Mac was asleep at
    # 03:00 Sunday, so a job a couple of days late is normal and must not alert.
    program_mapper_snapshot_max_age_days: int = int(
        os.getenv("PROGRAM_MAPPER_SNAPSHOT_MAX_AGE_DAYS", "10")
    )
    # How often to report snapshot age. Must not depend on user traffic, or a
    # quiet weekend looks the same as a dead refresh pipeline. 0 disables it.
    snapshot_monitor_interval_seconds: int = int(
        os.getenv("SNAPSHOT_MONITOR_INTERVAL_SECONDS", "3600")
    )
    guardrails_enabled: bool = os.getenv("GUARDRAILS_ENABLED", "false").lower() == "true"
    guardrails_fail_open: bool = os.getenv("GUARDRAILS_FAIL_OPEN", "true").lower() == "true"

    def ensure_directories(self) -> None:
        self.normalized_dir.mkdir(parents=True, exist_ok=True)
        self.crawl4ai_raw_dir.mkdir(parents=True, exist_ok=True)
        self.crawl4ai_normalized_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_directories()
