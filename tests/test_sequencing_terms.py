from __future__ import annotations

import pytest

from app.services.sequencing.terms import (
    build_term_code,
    parse_term_code,
    recent_term_codes,
)


@pytest.mark.parametrize(
    "code,expected",
    [
        ("2168", ("Fall", 2016)),      # archive floor, 201x decade
        ("2258", ("Fall", 2025)),
        ("2272", ("Winter", 2027)),
        ("2244", ("Spring", 2024)),
        ("2216", ("Summer", 2021)),
    ],
)
def test_parse_term_code(code, expected):
    assert parse_term_code(code) == expected


@pytest.mark.parametrize("bad", ["", "22", "abcd", "2259", "9999", "0", "-1"])
def test_parse_term_code_rejects_garbage(bad):
    assert parse_term_code(bad) is None


def test_build_term_code_round_trips():
    for code in ("2168", "2258", "2272", "2244"):
        season, year = parse_term_code(code)
        assert build_term_code(season, year) == code


def test_recent_term_codes_walks_backwards_across_the_decade_boundary():
    # Winter 2022 back ten terms reaches Fall 2019, encoded with the "21"
    # decade prefix. This is the case get_term_name() cannot handle.
    codes = recent_term_codes("2222", count=10)
    assert codes == ["2198", "2202", "2204", "2206", "2208",
                     "2212", "2214", "2216", "2218", "2222"]
    assert parse_term_code(codes[0]) == ("Fall", 2019)


def test_recent_term_codes_walks_one_year_at_a_time():
    codes = recent_term_codes("2224", count=6)
    assert codes == ["2212", "2214", "2216", "2218", "2222", "2224"]
    assert all(parse_term_code(c) for c in codes)


def test_recent_term_codes_includes_the_anchor_last():
    codes = recent_term_codes("2258", count=16)
    assert len(codes) == 16
    assert codes[-1] == "2258"
    # 16 terms is four full years of four seasons, so the window opens at
    # Winter 2022.
    assert parse_term_code(codes[0]) == ("Winter", 2022)
