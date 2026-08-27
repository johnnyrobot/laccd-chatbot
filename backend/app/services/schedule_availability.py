from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ScheduleOffering:
    section: str
    meeting_days: str
    meeting_time: str
    instructor_name: str
    is_online: bool
    seats_available: int
    enrollment_capacity: int
    enrollment_total: int


class ScheduleAvailabilityService:
    """Mission-first placeholder for normalized seat-aware availability.

    This intentionally fails closed until a normalized availability dataset
    is supplied. Live schedule search remains available separately.
    """

    def __init__(self, dataset_path: str | None = None) -> None:
        self.dataset_path = Path(dataset_path) if dataset_path else None

    def is_configured(self) -> bool:
        return bool(self.dataset_path and self.dataset_path.exists())

    async def check_schedule_availability(self, term: str, courses: list[dict[str, str]], campus_scope: str = "LAMC") -> dict[str, Any]:
        if not self.is_configured():
            results = []
            for course in courses:
                results.append(
                    {
                        "course_prefix": course.get("course_prefix", "").upper(),
                        "course_number": course.get("course_number", "").upper(),
                        "available": False,
                        "reason": "Seat-aware availability dataset is not configured yet for this environment.",
                    }
                )
            return {
                "term": term,
                "campus_scope": campus_scope,
                "results": results,
            }
        return {
            "term": term,
            "campus_scope": campus_scope,
            "results": [],
        }
