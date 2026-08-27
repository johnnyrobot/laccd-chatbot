from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MODULE_NAME = "load_assist_data_under_test"


def _load_module():
    if _MODULE_NAME in sys.modules:
        return sys.modules[_MODULE_NAME]
    path = PROJECT_ROOT / "scripts" / "load_assist_data.py"
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves annotations via sys.modules[cls.__module__]; register
    # before exec so AssistCourse can be built.
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


# Real rows from data/assist_db/260420_uc_all.txt. Position 10 is the
# UC-Credit-Limit-Flag, NOT a "UC eligible" flag: per the ASSIST V3 spec, File #1
# "includes data for all community college courses that are UC transferable", and
# the flag only marks courses whose transfer credit is capped.
LASC_ENGLISH_101 = (
    "004409~ENGLISH~101~200010~202510~College Reading & Composition I~3.00~3.00~S~N~E"
)
LACC_LIMITED = (
    "004391~MATH~245~200510~202510~Discrete Mathematics~3.00~3.00~S~Y~"
)


def test_uc_course_without_a_credit_limit_is_uc_transferable(tmp_path):
    """Presence in uc_all.txt means UC transferable; the flag is a credit cap."""
    mod = _load_module()
    uc_file = tmp_path / "uc_all.txt"
    uc_file.write_text(LASC_ENGLISH_101 + "\n", encoding="utf-8")

    courses = mod.parse_uc_file(uc_file)

    assert len(courses) == 1
    course = next(iter(courses.values()))
    assert course.subject_code == "ENGLISH"
    assert course.number == "101"
    assert course.uc_transferable is True


def test_uc_course_with_a_credit_limit_is_still_uc_transferable(tmp_path):
    mod = _load_module()
    uc_file = tmp_path / "uc_all.txt"
    uc_file.write_text(LACC_LIMITED + "\n", encoding="utf-8")

    courses = mod.parse_uc_file(uc_file)

    course = next(iter(courses.values()))
    assert course.uc_transferable is True
