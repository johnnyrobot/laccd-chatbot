from __future__ import annotations

import asyncio

from app.services.data_health import build_data_health_report


def run(coro):
    return asyncio.run(coro)


class _FakeElumen:
    def cache_stats(self, campus: str) -> dict:
        return {
            "course_entries": 2 if campus == "LAMC" else 0,
            "prereq_entries": 1 if campus == "LAMC" else 0,
            "negative_course_entries": 1 if campus == "ELAC" else 0,
            "search_entries": 3 if campus == "LAMC" else 0,
        }


class _FakeProgramClient:
    def __init__(self, campus: str):
        self.campus = campus

    def cache_stats(self) -> dict:
        return {"entries": 4 if self.campus == "LAMC" else 1}


class _FakeNeo4j:
    def is_configured(self) -> bool:
        return True

    async def run_query(self, cypher: str, params: dict | None = None) -> list[dict]:
        campus = (params or {}).get("campus")
        if campus == "LAMC":
            return [{"courses": 100, "programs": 20, "instructors": 30}]
        return [{"courses": 80, "programs": 10, "instructors": 25}]


class _FakeGemini:
    def is_configured(self) -> bool:
        return True

    def get_store_for_campus(self, campus: str) -> str | None:
        return "stores/lamc" if campus == "LAMC" else None


def test_data_health_report_returns_per_campus_coverage(tmp_path):
    snapshot_dir = tmp_path / "elumen_courses"
    snapshot_dir.mkdir()
    (snapshot_dir / "lamc_courses.json").write_text("[]", encoding="utf-8")

    report = run(
        build_data_health_report(
            ["LAMC", "ELAC"],
            elumen=_FakeElumen(),
            program_client_factory=lambda campus: _FakeProgramClient(campus),
            neo4j=_FakeNeo4j(),
            gemini=_FakeGemini(),
            data_dir=tmp_path,
        )
    )

    assert report["summary"] == {"campus_count": 2, "neo4j_configured": True}
    assert report["campuses"]["LAMC"]["elumen"]["snapshot_available"] is True
    assert report["campuses"]["ELAC"]["elumen"]["negative_course_cache_entries"] == 1
    assert report["campuses"]["LAMC"]["program_mapper"]["cache_entries"] == 4
    assert report["campuses"]["ELAC"]["neo4j"]["counts"]["courses"] == 80
    assert report["campuses"]["LAMC"]["gemini_file_search"]["store_present"] is True
    assert report["campuses"]["ELAC"]["gemini_file_search"]["store_present"] is False
