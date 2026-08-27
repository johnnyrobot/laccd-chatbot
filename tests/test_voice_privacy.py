from __future__ import annotations

from voice_agent.safety import room_log_id, transcript_log_metadata


def test_voice_transcript_log_metadata_is_content_free() -> None:
    class Event:
        is_final = True
        transcript = "student id 881234567 and private details"

    metadata = transcript_log_metadata(Event())

    assert metadata == {"final": True, "characters": len(Event.transcript)}
    assert "transcript" not in metadata
    assert Event.transcript not in repr(metadata)


def test_room_log_id_does_not_expose_phone_number() -> None:
    room_name = "district-call_+18185551212_random"
    identifier = room_log_id(room_name)

    assert "+18185551212" not in identifier
    assert len(identifier) == 12
