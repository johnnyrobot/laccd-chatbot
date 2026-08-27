from __future__ import annotations

from types import SimpleNamespace

from app.config import settings
from app.services.gemini_file_search import GeminiFileSearchService
from google.genai import types


class _FakeStores:
    def __init__(self, stores):
        self._stores = stores

    def list(self):
        return list(self._stores)


class _FakeClient:
    def __init__(self, stores):
        self.file_search_stores = _FakeStores(stores)


def configured_service(stores):
    service = GeminiFileSearchService()
    service._client = _FakeClient(stores)
    service.is_configured = lambda: True
    return service


def test_gemini_file_search_resolves_campus_store_and_does_not_fallback(monkeypatch):
    monkeypatch.setattr(settings, "gemini_file_search_allow_default_fallback", False)
    monkeypatch.delenv("GEMINI_FILE_SEARCH_STORE_LAVC", raising=False)
    service = configured_service(
        [
            SimpleNamespace(display_name="elac-chatbot-file-search", name="stores/elac"),
        ]
    )

    assert service.get_store_for_campus("ELAC") == "stores/elac"
    assert service.get_store_for_campus("LAVC") is None

    answer, citations, route = service.answer("What programs are offered?", campus_code="LAVC")
    assert answer is None
    assert citations == []
    assert route == "gemini_file_search_no_campus_store"


def test_gemini_file_search_uses_env_campus_store(monkeypatch):
    monkeypatch.setenv("GEMINI_FILE_SEARCH_STORE_LAVC", "stores/env-lavc")
    monkeypatch.setattr(settings, "gemini_file_search_allow_default_fallback", False)
    service = configured_service([])

    assert service.get_store_for_campus("lavc") == "stores/env-lavc"


def test_gemini_file_search_default_fallback_requires_explicit_setting(monkeypatch):
    monkeypatch.setattr(settings, "gemini_file_search_allow_default_fallback", True)
    monkeypatch.setattr(settings, "default_campus", "LAMC")
    service = configured_service([])
    service.store_name = "stores/default-lamc"

    assert service.get_store_for_campus("ELAC") == "stores/default-lamc"


def test_gemini_file_search_accepts_low_latency_thinking_budget(monkeypatch):
    captured = {}

    class _FakeModels:
        def generate_content(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                text="Answer",
                candidates=[],
                usage_metadata=None,
            )

    service = GeminiFileSearchService()
    service._client = SimpleNamespace(models=_FakeModels())
    service.is_configured = lambda: True
    service.get_store_for_campus = lambda _campus: "stores/lamc"
    monkeypatch.setattr(
        "app.services.gemini_file_search.get_client",
        lambda: SimpleNamespace(update_current_generation=lambda **kwargs: None),
    )

    answer, _citations, route = service.answer(
        "Question?",
        campus_code="LAMC",
        thinking_budget=0,
    )

    assert answer == "Answer"
    assert route == "gemini_file_search"
    assert isinstance(captured["config"], types.GenerateContentConfig)
    assert captured["config"].thinking_config.thinking_budget == 0
    assert captured["config"].thinking_config.include_thoughts is False
