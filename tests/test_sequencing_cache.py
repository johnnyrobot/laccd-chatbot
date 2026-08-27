# tests/test_sequencing_cache.py
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.sequencing import cache

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _listing(term="2258", campus="LAMC", termname="Fall 2025", courses=None):
    # Real shape (mirrors the production response schema): courses nest under
    # subjects[].courses[], not a top-level "courses" key, and
    # classTypeList entries are {"name", "value"}, not {"code", "descr"}.
    return {
        "termname": termname,
        "subjects": [{"code": "MULTIMD", "courses": courses or []}],
        "classTypeList": [
            {"name": "IN-PERSON", "value": "01  In Person"},
            {"name": "C19S", "value": "03  Online Live"},
            {"name": "HYFLEX", "value": "04  HyFlex-In Person or Online"},
        ],
    }


def test_assert_real_listing_rejects_a_fabricated_term_name():
    bogus = _listing(term="9999", termname="Winter 2799")
    with pytest.raises(cache.SweepError, match="termname"):
        cache.assert_real_listing(bogus, "LAMC", "9999")


def test_assert_real_listing_rejects_an_empty_subjects_array():
    bogus = _listing()
    bogus["subjects"] = []
    with pytest.raises(cache.SweepError, match="subjects"):
        cache.assert_real_listing(bogus, "LAMC", "2258")


def test_assert_real_listing_accepts_a_real_response():
    cache.assert_real_listing(_listing(), "LAMC", "2258")


def test_rollup_marks_three_observations_high_confidence():
    records = [
        {"course_code": "MULTIMD 220", "season": "Fall", "year": y, "modality_tags": ["IN-PERSON"]}
        for y in (2022, 2023, 2024, 2025)
    ]
    out = cache.rollup(records)
    rec = out["MULTIMD 220"]
    assert rec["obs_count"] == 4
    assert rec["confidence"] == "high"
    assert rec["constrains"] == ["Fall"]
    assert rec["seasons"]["Spring"] == 0


def test_rollup_marks_one_observation_thin_and_does_not_constrain():
    records = [{"course_code": "ANIMATN 217", "season": "Fall", "year": 2026,
                "modality_tags": ["IN-PERSON"]}]
    rec = cache.rollup(records)["ANIMATN 217"]
    assert rec["obs_count"] == 1
    assert rec["confidence"] == "thin"
    assert rec["constrains"] == []


def test_rollup_never_constrains_on_winter_or_summer():
    """Winter/Summer are too sparse to read as evidence of absence."""
    records = [
        {"course_code": "MULTIMD 110", "season": s, "year": y, "modality_tags": ["ONLINE"]}
        for y in (2023, 2024, 2025) for s in ("Fall", "Spring", "Winter", "Summer")
    ]
    rec = cache.rollup(records)["MULTIMD 110"]
    assert rec["confidence"] == "high"
    assert sorted(rec["constrains"]) == ["Fall", "Spring"]


def test_rollup_counts_distinct_terms_not_section_records():
    """A single term commonly contains 3+ sections of the same course.

    obs_count/confidence must reflect how many DISTINCT (season, year)
    terms a course was seen in, never how many section records it
    produced within one of them. In representative production data, 104 of
    420 courses already reach 3+ *sections* inside that one term (one
    reaches 29) -- a record-count-based obs_count would read those as
    "confidently offered every Fall" from a single term of evidence.
    """
    records = [
        {"course_code": "ANATOMY 001", "season": "Fall", "year": 2025, "modality_tags": ["IN-PERSON"]}
        for _ in range(5)  # e.g. one lecture + four LAB relsections, all one term
    ]
    rec = cache.rollup(records)["ANATOMY 001"]
    assert rec["obs_count"] == 1
    assert rec["confidence"] == "thin"
    assert rec["constrains"] == []


def test_rollup_tallies_each_modality_tag_once_per_section():
    """A section can carry multiple classType tags on one record.

    Each tag must be tallied once per section that carries it -- not
    fused into a single joined key (which would not decode against
    modality_legend and would double-count under different key orderings)
    and not dropped.
    """
    records = [
        {"course_code": "ADM JUS 104", "season": "Fall", "year": 2025,
         "modality_tags": ["ONLINE", "OER"]},
        {"course_code": "ADM JUS 104", "season": "Spring", "year": 2025,
         "modality_tags": ["IN-PERSON"]},
    ]
    rec = cache.rollup(records)["ADM JUS 104"]
    assert rec["modalities"] == {"ONLINE": 1, "OER": 1, "IN-PERSON": 1}


def test_load_campus_refuses_a_stale_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    stale = datetime.now(timezone.utc) - timedelta(days=cache.CACHE_MAX_AGE_DAYS + 1)
    (tmp_path / "LAMC.json").write_text(json.dumps({
        "campus": "LAMC", "built_at": stale.isoformat(),
        "swept_terms": 16, "courses": {},
    }))
    assert cache.load_campus("LAMC") is None


def test_load_campus_accepts_a_fresh_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    fresh = datetime.now(timezone.utc) - timedelta(days=1)
    (tmp_path / "LAMC.json").write_text(json.dumps({
        "campus": "LAMC", "built_at": fresh.isoformat(),
        "swept_terms": 16, "courses": {"MULTIMD 220": {"confidence": "high"}},
    }))
    loaded = cache.load_campus("LAMC")
    assert loaded is not None
    assert loaded["courses"]["MULTIMD 220"]["confidence"] == "high"


def test_load_campus_returns_none_on_corrupt_json(tmp_path, monkeypatch):
    """A damaged cache file disables the campus; it must not crash the caller."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    (tmp_path / "LAMC.json").write_text("{not valid json")
    assert cache.load_campus("LAMC") is None


def test_load_campus_returns_none_on_unparseable_built_at(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    (tmp_path / "LAMC.json").write_text(json.dumps({
        "campus": "LAMC", "built_at": "not-a-timestamp",
        "swept_terms": 16, "courses": {},
    }))
    assert cache.load_campus("LAMC") is None


def test_load_campus_accepts_a_naive_built_at_timestamp(tmp_path, monkeypatch):
    """A hand-written or legacy cache file might have a naive built_at.

    Comparing a naive stamp against a tz-aware `now` (or vice versa) raises
    TypeError; load_campus must normalize instead of propagating that.
    """
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    fresh_naive = (datetime.now(timezone.utc) - timedelta(days=1)).replace(tzinfo=None)
    (tmp_path / "LAMC.json").write_text(json.dumps({
        "campus": "LAMC", "built_at": fresh_naive.isoformat(),
        "swept_terms": 16, "courses": {},
    }))
    assert cache.load_campus("LAMC") is not None


def test_iter_sections_walks_subjects_courses_and_relsections():
    """Ground-truth check against a deliberately synthetic listing with the production response shape.

    Synthetic courses live two levels down (subjects[].courses[]), not in a
    top-level "courses" key -- a walk that reads the wrong level yields
    zero (code, section) pairs and every course reads "never offered".
    This also confirms relsections (LAB components) are not dropped: the
    fixture's 14 records include 8 relsections, and the test adds a nested
    related component to exercise recursive traversal.
    """
    fixture = json.loads((FIXTURES_DIR / "lamc_2258_sample.json").read_text())
    nested = fixture["subjects"][1]["courses"][0]["sections"][0]["relsections"][0]
    nested["relsections"] = [{"classNbr": "11339 (LAB)"}]
    pairs = list(cache._iter_sections(fixture))
    assert len(pairs) == 15  # 6 top-level sections + 9 relsections
    assert {code for code, _ in pairs} == {"ADM JUS 104", "ANATOMY 001"}
    lab_class_numbers = {
        section["classNbr"] for _, section in pairs if "(LAB)" in section.get("classNbr", "")
    }
    assert len(lab_class_numbers) == 9
    assert ("ANATOMY 001", {"classNbr": "11339 (LAB)"}) in pairs


def test_iter_sections_delegates_to_shared_schedule_iterator(monkeypatch):
    listing = {"sentinel": True}
    course = {"subject": "BIO", "catalogNbr": "001"}
    section = {"classNbr": "90001 (LEC)"}
    calls = []

    def fake_iter_schedule_sections(received_listing):
        calls.append(received_listing)
        yield course, section, "90001"

    monkeypatch.setattr(cache, "iter_schedule_sections", fake_iter_schedule_sections, raising=False)

    assert list(cache._iter_sections(listing)) == [("BIO 001", section)]
    assert calls == [listing]


def test_build_campus_counts_distinct_terms_and_credits_every_modality_tag():
    """End-to-end regression pinned to a deliberately synthetic listing with the production response shape.

    recent_term_codes("2258", 16) sweeps 16 distinct (season, year) terms
    -- 4 full Winter/Spring/Summer/Fall cycles -- and this fixture's two
    courses appear in every one of them. Locks down two things a naive
    per-record count would get wrong:

    - obs_count/confidence reflect distinct terms, not the number of
      section records within them: ADM JUS 104 has 2 sections per term and
      ANATOMY 001 has 12 (4 lecture + 8 lab relsections), but both must
      land on obs_count == 16 (the number of terms), not 2*16 or 12*16.
    - a section carrying multiple classType tags at once (ADM JUS 104's
      first section: ONLINE + OER) credits each tag once per section it
      appears in -- not zero, and not fused into one key.
    """
    import asyncio

    from app.services.sequencing.terms import parse_term_code

    fixture = json.loads((FIXTURES_DIR / "lamc_2258_sample.json").read_text())

    async def fetch(campus, term):
        season, year = parse_term_code(term)
        listing = dict(fixture)
        listing["termname"] = f"{season} {year}"
        return listing

    payload = asyncio.run(cache.build_campus("LAMC", "2258", fetch=fetch))
    # 14 raw records/term (6 sections + 8 relsections) swept across 16 terms.
    assert payload["observation_count"] == 14 * 16

    admjus = payload["courses"]["ADM JUS 104"]
    assert admjus["obs_count"] == 16
    assert admjus["confidence"] == "high"
    assert sorted(admjus["constrains"]) == ["Fall", "Spring"]
    assert admjus["modalities"] == {"ONLINE": 16, "OER": 16, "IN-PERSON": 16}

    anatomy = payload["courses"]["ANATOMY 001"]
    assert anatomy["obs_count"] == 16
    assert anatomy["confidence"] == "high"
    assert sorted(anatomy["constrains"]) == ["Fall", "Spring"]


def test_build_campus_fails_loudly_on_zero_observations():
    """Every term returns a real-looking but empty listing.

    The stub must derive termname from the requested term, or
    assert_real_listing raises on the mismatch first and this test passes for
    entirely the wrong reason.
    """
    import asyncio

    from app.services.sequencing.terms import parse_term_code

    async def fetch(campus, term):
        season, year = parse_term_code(term)
        return _listing(term=term, termname=f"{season} {year}", courses=[])

    with pytest.raises(cache.SweepError, match="zero observations"):
        asyncio.run(cache.build_campus("LAMC", "2258", fetch=fetch))
