"""Tests for the external-prerequisite telemetry hook on
CourseSequenceService.generate_sequence.

This replaces the v1 ASSIST-stub tests. The v1 stub gated logging on a
truthy assist_service argument; v1.5 (post second-opinion review) emits
classified telemetry unconditionally so production data can drive any
future decision about an ASSIST resolver. See
``backend/app/services/external_prereq_telemetry.py`` for the threshold
gate that future ASSIST work is blocked behind.

The tests verify:
1. Telemetry fires whenever the sequencer surfaces external prereqs,
   independent of whether assist_service is provided.
2. Each emission carries an ``event=external_prereq.candidate`` marker
   plus a category (renumbering / assumed_prep / placeholder_mismatch /
   real_articulation_candidate).
3. The optional assist_service argument is never invoked (it remains
   reserved for a future, gated resolver).
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock

from app.services.course_sequence_service import CourseSequenceService


def _stub_program_requirements():
    """Minimal program with one course whose prereq is OUT of the program."""
    program_info = {"title": "Stub Program", "awardTitle": "AS"}
    elements = [
        {
            "name": "CIS 101",
            "shortDescription": "Intro Programming",
            "recommendedOpportunity": {"term": {"termNumber": 1}, "minUnits": 3},
            "requirement": {"requirementType": "MAJOR_REQUIRED"},
        },
    ]
    return program_info, elements


def _patched_svc():
    svc = CourseSequenceService()
    svc._get_program_requirements = AsyncMock(return_value=_stub_program_requirements())
    svc._build_full_dependency_graph = AsyncMock(return_value={
        "prerequisites": {"CIS 101": [["MATH 100"]]},
        "advisories": {},
        "corequisites": {},
        "course_details": {},
    })
    svc.analyzer.analyze = MagicMock(return_value=({}, [], {}))
    svc._fetch_course_metadata = AsyncMock(return_value={})
    return svc


def _telemetry_records(caplog):
    return [r for r in caplog.records if "external_prereq.candidate" in r.getMessage()]


def _parse_payload(record: logging.LogRecord) -> dict:
    msg = record.getMessage()
    json_start = msg.find("{")
    return json.loads(msg[json_start:])


def test_telemetry_fires_without_assist_service(caplog):
    svc = _patched_svc()
    with caplog.at_level(logging.INFO, logger="app.services.external_prereq_telemetry"):
        result = asyncio.run(svc.generate_sequence("stub-id"))
    assert result["external_prerequisites"], "Need an external prereq for the hook to fire"
    records = _telemetry_records(caplog)
    assert records, "Telemetry must fire whenever external prereqs are surfaced"
    payload = _parse_payload(records[0])
    assert payload["event"] == "external_prereq.candidate"
    assert payload["course"] == "CIS 101"
    assert payload["prereq"] == "MATH 100"
    assert payload["category"] in {
        "renumbering", "assumed_prep", "placeholder_mismatch", "real_articulation_candidate",
    }


def test_telemetry_fires_with_assist_service_but_does_not_call_it(caplog):
    svc = _patched_svc()
    fake_assist = MagicMock(name="AssistService")
    with caplog.at_level(logging.INFO, logger="app.services.external_prereq_telemetry"):
        asyncio.run(svc.generate_sequence("stub-id", assist_service=fake_assist))
    assert _telemetry_records(caplog), "Telemetry still fires when assist_service is set"
    assert not fake_assist.method_calls, (
        "Sequencer must not invoke assist_service — that path is gated behind a future "
        "data-driven decision (see external_prereq_telemetry.py)."
    )


def test_assumed_prep_courses_are_categorized_as_assumed_prep(caplog):
    svc = _patched_svc()
    svc._build_full_dependency_graph = AsyncMock(return_value={
        # MATH 125 = intermediate algebra → CAT_ASSUMED_PREP
        "prerequisites": {"CIS 101": [["MATH 125"]]},
        "advisories": {}, "corequisites": {}, "course_details": {},
    })
    with caplog.at_level(logging.INFO, logger="app.services.external_prereq_telemetry"):
        asyncio.run(svc.generate_sequence("stub-id"))
    records = _telemetry_records(caplog)
    assert records
    payload = _parse_payload(records[0])
    assert payload["category"] == "assumed_prep"


def test_ccn_form_prereqs_are_categorized_as_renumbering(caplog):
    svc = _patched_svc()
    svc._build_full_dependency_graph = AsyncMock(return_value={
        # CCN form should classify as renumbering even when alone
        "prerequisites": {"CIS 101": [["STAT C1000"]]},
        "advisories": {}, "corequisites": {}, "course_details": {},
    })
    with caplog.at_level(logging.INFO, logger="app.services.external_prereq_telemetry"):
        asyncio.run(svc.generate_sequence("stub-id"))
    records = _telemetry_records(caplog)
    payload = _parse_payload(records[0])
    assert payload["category"] == "renumbering"


def test_telemetry_does_not_change_sequence_output():
    svc1 = _patched_svc()
    svc2 = _patched_svc()
    result_off = asyncio.run(svc1.generate_sequence("stub-id"))
    result_on = asyncio.run(svc2.generate_sequence("stub-id", assist_service=MagicMock()))
    assert result_off == result_on
