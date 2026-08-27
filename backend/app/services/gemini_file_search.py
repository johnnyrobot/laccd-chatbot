from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Iterable

from langfuse import get_client, observe

from app.config import settings
from app.models import Citation
from shared.content_filters import classify, get_purge_reason, should_purge
from shared.district_facts import district_facts_for

logger = logging.getLogger(__name__)

try:
    from google import genai
    from google.genai import types
except Exception:  # pragma: no cover
    genai = None
    types = None


class GeminiFileSearchService:
    def __init__(self) -> None:
        self.api_key = settings.gemini_api_key or os.getenv("GOOGLE_API_KEY")
        self.store_name = settings.gemini_file_search_store
        self._client = None
        self._campus_store_cache: dict[str, str | None] = {}

    def is_configured(self) -> bool:
        return bool(settings.use_gemini_file_search and self.api_key and genai and types)

    @property
    def client(self):
        if self._client is None and self.is_configured():
            os.environ["GOOGLE_API_KEY"] = self.api_key
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def ensure_store(self, display_name: str = "laccd-chatbot-file-search") -> str | None:
        if not self.is_configured():
            return None
        if self.store_name:
            return self.store_name
        for store in self.client.file_search_stores.list():
            if getattr(store, "display_name", None) == display_name:
                self.store_name = store.name
                return self.store_name
        store = self.client.file_search_stores.create(config={"display_name": display_name})
        self.store_name = store.name
        return self.store_name

    def upload_paths(self, paths: Iterable[str], display_prefix: str = "") -> int:
        store_name = self.ensure_store()
        if not store_name:
            return 0
        existing_display_names = set()
        for document in self.client.file_search_stores.documents.list(parent=store_name):
            display_name = getattr(document, "display_name", None)
            if display_name:
                existing_display_names.add(display_name)
        uploaded = 0
        for path in paths:
            file_path = Path(path)
            if not file_path.exists():
                continue
            display_name = f"{display_prefix}{file_path.name}" if display_prefix else file_path.name
            if display_name in existing_display_names:
                continue
            operation = self.client.file_search_stores.upload_to_file_search_store(
                file=str(file_path),
                file_search_store_name=store_name,
                config={
                    "display_name": display_name,
                    "chunking_config": {
                        "white_space_config": {
                            "max_tokens_per_chunk": 350,
                            "max_overlap_tokens": 50,
                        }
                    },
                },
            )
            while not operation.done:
                time.sleep(2)
                operation = self.client.operations.get(operation=operation)
            uploaded += 1
            existing_display_names.add(display_name)
        return uploaded

    @observe(as_type="generation", name="gemini.generate")
    def generate(self, prompt: str) -> str | None:
        """Plain Gemini generation via API key (no file search tools)."""
        if not self.is_configured():
            return None
        try:
            response = self.client.models.generate_content(
                model=settings.gemini_lite_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=max(1, settings.gemini_max_output_tokens),
                ),
            )
            get_client().update_current_generation(
                input=prompt,
                output=response.text,
                model=settings.gemini_lite_model,
                usage_details=self._extract_usage(response),
            )
            return response.text
        except Exception:
            logger.warning("Gemini lite generation failed", exc_info=True)
            return None

    def get_store_for_campus(self, campus_code: str) -> str | None:
        """Resolve the file search store for a given campus."""
        campus = (campus_code or settings.default_campus or "LAMC").upper()
        if campus in self._campus_store_cache:
            return self._campus_store_cache[campus]
        if not self.is_configured():
            self._campus_store_cache[campus] = None
            return None
        # Check campus-specific env var first
        campus_store = os.getenv(f"GEMINI_FILE_SEARCH_STORE_{campus}")
        if campus_store:
            self._campus_store_cache[campus] = campus_store
            return campus_store
        # Auto-discover by display name
        target = f"{campus.lower()}-chatbot-file-search"
        try:
            for store in self.client.file_search_stores.list():
                if getattr(store, "display_name", None) == target:
                    self._campus_store_cache[campus] = store.name
                    return store.name
        except Exception:
            logger.debug("Failed to list file search stores for campus %s", campus, exc_info=True)

        default_campus = (settings.default_campus or "LAMC").upper()
        if campus == default_campus or settings.gemini_file_search_allow_default_fallback:
            fallback = self.ensure_store()
        else:
            fallback = None
        self._campus_store_cache[campus] = fallback
        return fallback

    @observe(as_type="generation", name="gemini.file_search")
    def answer(
        self,
        question: str,
        system_prompt: str = "",
        campus_code: str = "LAMC",
        *,
        thinking_budget: int | None = None,
    ) -> tuple[str | None, list[Citation], str]:
        store_name = self.get_store_for_campus(campus_code)
        if not store_name:
            route = "gemini_file_search_unconfigured"
            if self.is_configured():
                route = "gemini_file_search_no_campus_store"
            return None, [], route
        # Some rules are set in Title 5 and are identical district-wide, but the
        # corpus is per-campus and the nine campus sites disagree. State those
        # rules in the prompt so retrieval cannot decide them. Injected here
        # rather than at the call sites so every caller gets it.
        facts = district_facts_for(question)
        preamble = f"{system_prompt}{facts}" if system_prompt else facts.lstrip()
        prompt = f"{preamble}\n\nQuestion:\n{question}" if preamble else question
        config_kwargs = {
            "max_output_tokens": max(1, settings.gemini_max_output_tokens),
            "tools": [
                types.Tool(
                    file_search=types.FileSearch(
                        file_search_store_names=[store_name],
                    )
                )
            ]
        }
        if thinking_budget is not None:
            config_kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_budget=thinking_budget,
                include_thoughts=False,
            )
        response = self.client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(**config_kwargs),
        )
        citations = self._extract_citations(response)
        grounding = self._extract_grounding_metadata(response)
        get_client().update_current_generation(
            input=prompt,
            output=response.text,
            model=settings.gemini_model,
            usage_details=self._extract_usage(response),
            metadata={"grounding_sources": grounding, "citation_count": len(citations)},
        )
        return response.text, citations, "gemini_file_search"

    def delete_old_documents(self, min_year: int = 2020) -> int:
        """Delete documents with pre-min_year years in their display names."""
        store_name = self.ensure_store()
        if not store_name:
            return 0
        old_year_re = re.compile(r"(?:19\d{2}|200\d|201\d)")
        deleted = 0
        for doc in self.client.file_search_stores.documents.list(parent=store_name):
            display_name = getattr(doc, "display_name", "") or ""
            # Strip hash suffix to avoid false positives from hex hashes
            base = re.sub(r"-[0-9a-f]{8}\.\w+$", "", display_name)
            years = old_year_re.findall(base)
            if years and all(int(y) < min_year for y in years):
                try:
                    doc_name = getattr(doc, "name", None)
                    if doc_name:
                        self.client.file_search_stores.documents.delete(
                            name=doc_name,
                            config=types.DeleteDocumentConfig(force=True),
                        )
                        deleted += 1
                        logger.info("Deleted old document: %s", display_name)
                except Exception:
                    logger.warning("Failed to delete document: %s", display_name, exc_info=True)
        return deleted

    def purge_documents_by_pattern(
        self, store_name: str, *, dry_run: bool = False
    ) -> dict:
        """Delete documents matching PURGE patterns from a given store.

        Uses ``shared.content_filters`` for classification.  Returns a
        summary dict: ``{"deleted": N, "kept": N, "breakdown": {...}}``.
        """
        if not self.is_configured():
            return {"deleted": 0, "kept": 0, "breakdown": {}}

        docs = list(
            self.client.file_search_stores.documents.list(parent=store_name)
        )
        deleted = 0
        kept = 0
        breakdown: dict[str, int] = {}

        for doc in docs:
            display_name = getattr(doc, "display_name", "") or ""
            doc_name = getattr(doc, "name", None)
            reason = get_purge_reason(display_name)

            if reason:
                breakdown[reason] = breakdown.get(reason, 0) + 1
                if not dry_run and doc_name:
                    try:
                        self.client.file_search_stores.documents.delete(
                            name=doc_name,
                            config=types.DeleteDocumentConfig(force=True),
                        )
                        deleted += 1
                    except Exception:
                        logger.warning(
                            "Failed to delete %s", display_name, exc_info=True
                        )
                elif dry_run:
                    deleted += 1  # count as "would delete" in dry-run
            else:
                kept += 1

        return {"deleted": deleted, "kept": kept, "breakdown": breakdown}

    def list_documents(self) -> list[dict]:
        store_name = self.ensure_store()
        if not store_name:
            return []
        documents = []
        for doc in self.client.file_search_stores.documents.list(parent=store_name):
            data = doc.model_dump() if hasattr(doc, "model_dump") else {}
            documents.append(data)
        return documents

    @staticmethod
    def _extract_usage(response) -> dict:
        usage = getattr(response, "usage_metadata", None)
        if not usage:
            return {}
        return {
            "input": getattr(usage, "prompt_token_count", 0),
            "output": getattr(usage, "candidates_token_count", 0),
            "total": getattr(usage, "total_token_count", 0),
        }

    @staticmethod
    def _extract_grounding_metadata(response) -> list[dict]:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return []
        grounding = getattr(candidates[0], "grounding_metadata", None)
        if not grounding:
            return []
        chunks = getattr(grounding, "grounding_chunks", None) or []
        metadata = []
        for chunk in chunks:
            source = getattr(chunk, "retrieved_context", None) or chunk
            data = source.model_dump() if hasattr(source, "model_dump") else {}
            metadata.append({
                "title": data.get("title") or data.get("display_name", "unknown"),
                "text_preview": (data.get("text") or data.get("chunk") or "")[:200],
            })
        return metadata

    def _extract_citations(self, response) -> list[Citation]:
        """Extract citations from Gemini response.

        Note: The Gemini File Search API (as of google-genai 1.66) does not
        populate grounding_metadata for file search responses.  Grounding
        chunks are only returned for Google Search grounding.  We still
        attempt extraction so that if the API changes in future SDK versions,
        citations will start flowing automatically.
        """
        citations: list[Citation] = []
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return citations
        candidate = candidates[0]
        grounding = getattr(candidate, "grounding_metadata", None)
        if not grounding:
            return citations
        # Try grounding_chunks first (standard path)
        chunks = getattr(grounding, "grounding_chunks", None) or []
        # Fallback to grounding_supports if chunks are empty
        if not chunks:
            chunks = getattr(grounding, "grounding_supports", None) or []
        for chunk in chunks:
            source = getattr(chunk, "retrieved_context", None) or chunk
            data = source.model_dump() if hasattr(source, "model_dump") else {}
            text = data.get("text") or data.get("chunk") or ""
            title = data.get("title") or data.get("display_name") or "Gemini File Search"
            source_url = data.get("uri") or data.get("source_url") or data.get("url")
            citations.append(Citation(
                title=title,
                snippet=text[:220] if text else None,
                source_url=source_url,
            ))
        return citations
