from __future__ import annotations

import pytest

from shared.campus_config import CAMPUSES, get_campus_from_host


@pytest.mark.parametrize("campus_code", sorted(CAMPUSES))
def test_get_campus_from_host_accepts_short_chat_domains(campus_code: str) -> None:
    campus = get_campus_from_host(f"{campus_code.lower()}.chat")

    assert campus.code == campus_code


@pytest.mark.parametrize("campus_code", sorted(CAMPUSES))
def test_get_campus_from_host_preserves_legacy_domains(campus_code: str) -> None:
    campus = CAMPUSES[campus_code]

    assert get_campus_from_host(f"{campus.subdomain}.johnnyrobot.ai").code == campus_code


def test_get_campus_from_host_accepts_port_on_short_domain() -> None:
    assert get_campus_from_host("lavc.chat:8000").code == "LAVC"
