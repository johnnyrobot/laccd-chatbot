"""Unit tests for external_prereq_telemetry.classify().

Covers the four categorization buckets the synthetic measurement and
second-opinion review identified. The threshold gate referenced in the
helper docstring is policy, not code-enforced — tests here only verify
that emissions land in the expected buckets so future log analysis is
trustworthy.
"""

from __future__ import annotations

import json
import logging

from app.services.external_prereq_telemetry import (
    classify,
    emit,
    CAT_RENUMBERING,
    CAT_ASSUMED_PREP,
    CAT_PLACEHOLDER_MISMATCH,
    CAT_REAL_CANDIDATE,
)


def test_ccn_form_classifies_as_renumbering():
    # Direct CCN form: SUBJ Cnnn
    assert classify("STAT C1000", program_codes={"PSYCH 074"}, all_alternatives=["STAT C1000"]) == CAT_RENUMBERING
    # CCN form without internal space (legacy display variant)
    assert classify("STATC 1000", program_codes={"PSYCH 074"}, all_alternatives=["STATC 1000"]) == CAT_RENUMBERING
    # Non-CCN prereq alongside a CCN alternative — still renumbering
    assert classify(
        "STAT 101",
        program_codes={"PSYCH 074"},
        all_alternatives=["STAT 101", "STAT C1000"],
    ) == CAT_RENUMBERING


def test_district_renaming_cis_cs_classifies_as_renumbering():
    # Program contains the new name CIS 101 — prereq references the alias CS 101
    assert classify(
        "CS 101",
        program_codes={"CIS 101", "CIS 165"},
        all_alternatives=["CS 101"],
    ) == CAT_RENUMBERING


def test_intermediate_algebra_classifies_as_assumed_prep():
    assert classify("MATH 125", program_codes={"MATH 238"}, all_alternatives=["MATH 125"]) == CAT_ASSUMED_PREP


def test_sub_transfer_english_classifies_as_assumed_prep():
    assert classify("ENGLISH 28", program_codes={"ENGLISH 101"}, all_alternatives=["ENGLISH 28"]) == CAT_ASSUMED_PREP


def test_alternative_present_in_program_classifies_as_placeholder_mismatch():
    # The prereq itself isn't in program_codes, but one of the alternatives is.
    assert classify(
        "CS 101",
        program_codes={"CIS 101"},  # district renaming match — caught earlier as renumbering
        all_alternatives=["CS 101", "CIS 101"],
    ) == CAT_RENUMBERING

    # A neutral case: alt-in-program without a known renaming relationship
    assert classify(
        "BIOL 200",
        program_codes={"BIOL 220"},
        all_alternatives=["BIOL 200", "BIOL 220"],
    ) == CAT_PLACEHOLDER_MISMATCH


def test_unrelated_lacc_course_classifies_as_real_candidate():
    # Genuine missing-prereq case where only ASSIST articulation could help.
    assert classify(
        "PHYSICS 6",
        program_codes={"CHEM 101", "CHEM 102"},
        all_alternatives=["PHYSICS 6"],
    ) == CAT_REAL_CANDIDATE


def _last_emitted_payload(caplog) -> dict:
    """Pull the most recent external_prereq.candidate JSON line from caplog."""
    for record in reversed(caplog.records):
        if "external_prereq.candidate" in record.getMessage():
            blob = record.getMessage().split("external_prereq.candidate ", 1)[1]
            return json.loads(blob)
    raise AssertionError("no external_prereq.candidate log line captured")


def test_emit_default_resolved_false(caplog):
    caplog.set_level(logging.INFO, logger="app.services.external_prereq_telemetry")
    emit(
        program_name="Psychology",
        campus_code="LAMC",
        course_code="PSYCH 074",
        prereq="STAT C1000",
        alternatives=["STAT C1000", "STAT 101"],
        program_codes={"STATC 1000"},
    )
    payload = _last_emitted_payload(caplog)
    assert payload["category"] == CAT_RENUMBERING
    assert payload["resolved"] is False
    assert "resolved_to" not in payload


def test_emit_resolved_true_includes_target(caplog):
    caplog.set_level(logging.INFO, logger="app.services.external_prereq_telemetry")
    emit(
        program_name="Psychology",
        campus_code="LAMC",
        course_code="PSYCH 074",
        prereq="STAT C1000",
        alternatives=["STAT C1000"],
        program_codes={"STATC 1000"},
        resolved=True,
        resolved_to="STATC 1000",
    )
    payload = _last_emitted_payload(caplog)
    assert payload["resolved"] is True
    assert payload["resolved_to"] == "STATC 1000"
    # Threshold-gate analysis subtracts resolved=true from raw counts.
    assert payload["category"] == CAT_RENUMBERING
