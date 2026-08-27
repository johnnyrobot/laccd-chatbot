"""
eLumen Public Portal API Client

Fetches course details, prerequisites, and builds prerequisite graphs from:
https://portalapi-laccd.elumenapp.com/public

API Endpoints:
- GET /public/courses?status=approved&tenant={tenant}&query={search}&pageSize={n}&page={p}

Authentication: None required (public API).
"""

import httpx
import asyncio
import logging
import time
import json
import re
from collections import OrderedDict
from copy import deepcopy
from typing import Optional, List, Dict

from app.config import settings

logger = logging.getLogger(__name__)

# API Configuration
API_BASE = "https://portalapi-laccd.elumenapp.com/public"
API_TIMEOUT = 30.0  # seconds
DEFAULT_TENANT = "lamission.elumenapp.com"
MAX_CACHE_ENTRIES = 512
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_CACHE_BYTES = 16 * 1024 * 1024

# All 9 LACCD campus codes → eLumen tenant domains
CAMPUS_TENANTS = {
    "LAMC": "lamission.elumenapp.com",
    "LAVC": "lavc.elumenapp.com",
    "LAPC": "pierce.elumenapp.com",
    "LAHC": "lahc.elumenapp.com",
    "LATTC": "lattc.elumenapp.com",
    "LACC": "lacc.elumenapp.com",
    "ELAC": "elac.elumenapp.com",
    "LASC": "lasc.elumenapp.com",
    "WLAC": "wlac.elumenapp.com",
}

# Regex for splitting eLumen course codes (e.g., "MATH261" -> "MATH", "261")
# Handles: MATH261, MATH 261, STAT C1000, ADM JUS 001, BSICSKL001CE, WELDG/E 111
COURSE_CODE_RE = re.compile(
    r'^([A-Z]+(?:[/-][A-Z]+)?(?:\s+[A-Z]+)*)\s*(C?\d+[A-Z]*(?:CE)?)$'
)

# Regex for extracting course codes from free text (customFields fallback)
COURSE_CODE_EXTRACT_RE = re.compile(r'([A-Z]{2,6})\s*(\d{2,4}[A-Z]?)')


class ElumenService:
    """Service for fetching course details and prerequisites from eLumen Public Portal API"""

    def __init__(self):
        self.base_url = API_BASE
        self.tenant = DEFAULT_TENANT
        self._course_cache: OrderedDict[str, dict] = OrderedDict()
        self._prereq_cache: OrderedDict[str, List] = OrderedDict()
        self._search_cache: OrderedDict[str, List[dict]] = OrderedDict()
        self._negative_cache: OrderedDict[str, None] = OrderedDict()
        self._cache_timestamps: Dict[str, float] = {}   # key -> time.time()
        self._cache_sizes: dict[str, int] = {}
        self._cache_bytes = 0
        self.cache_ttl = settings.elumen_cache_ttl_seconds
        self.negative_cache_ttl = settings.elumen_negative_cache_ttl_seconds
        self.search_cache_ttl = settings.elumen_search_cache_ttl_seconds

    def _is_cache_valid(self, key: str, ttl: Optional[int] = None) -> bool:
        """Check if a cache entry is still valid."""
        ttl_seconds = self.cache_ttl if ttl is None else ttl
        valid = (
            ttl_seconds > 0
            and key in self._cache_timestamps
            and (time.time() - self._cache_timestamps[key]) < ttl_seconds
        )
        if not valid:
            self._drop_cache_key(key)
        return valid

    def _drop_cache_key(self, key: str) -> None:
        self._cache_timestamps.pop(key, None)
        self._course_cache.pop(key, None)
        self._prereq_cache.pop(key, None)
        self._search_cache.pop(key, None)
        self._negative_cache.pop(key, None)
        self._cache_bytes -= self._cache_sizes.pop(key, 0)

    def _cache_put(self, cache: OrderedDict, key: str, value) -> None:
        encoded_size = len(json.dumps(value, default=str).encode("utf-8"))
        if encoded_size > MAX_CACHE_BYTES:
            return
        self._cache_bytes -= self._cache_sizes.pop(key, 0)
        cache[key] = deepcopy(value)
        cache.move_to_end(key)
        self._cache_timestamps[key] = time.time()
        self._cache_sizes[key] = encoded_size
        self._cache_bytes += encoded_size
        while len(cache) > MAX_CACHE_ENTRIES or self._cache_bytes > MAX_CACHE_BYTES:
            evicted, _ = cache.popitem(last=False)
            self._cache_timestamps.pop(evicted, None)
            self._cache_bytes -= self._cache_sizes.pop(evicted, 0)

    def _campus_code(self, campus_code: str = "LAMC") -> str:
        return (campus_code or "LAMC").upper()

    def _campus_cache_key(self, kind: str, campus_code: str, app_code: str) -> str:
        return f"{kind}:{self._campus_code(campus_code)}:{app_code}"

    def _search_cache_key(self, tenant: str, query: str, page: int, page_size: int) -> str:
        return f"search:{tenant}:{query.strip().upper()}:{page}:{page_size}"

    def _negative_key(self, cache_key: str) -> str:
        return f"miss:{cache_key}"

    def _is_negative_cached(self, cache_key: str) -> bool:
        miss_key = self._negative_key(cache_key)
        if miss_key in self._negative_cache and self._is_cache_valid(miss_key, self.negative_cache_ttl):
            return True
        self._negative_cache.pop(miss_key, None)
        return False

    def _mark_negative(self, cache_key: str) -> None:
        miss_key = self._negative_key(cache_key)
        self._cache_put(self._negative_cache, miss_key, None)

    def _get_tenant(self, campus_code: str = "LAMC") -> str:
        """Get the eLumen tenant domain for a campus."""
        return CAMPUS_TENANTS.get(self._campus_code(campus_code), DEFAULT_TENANT)

    def _normalize_course_code(self, code: str, to_app_format: bool = True) -> str:
        """Convert between eLumen format ('MATH261') and app format ('MATH 261')."""
        code = code.strip().upper()
        match = COURSE_CODE_RE.match(code)
        if not match:
            return code
        prefix, number = match.group(1), match.group(2)
        if to_app_format:
            return f"{prefix} {number}"
        else:
            return f"{prefix}{number}"

    async def search_courses(self, query: str, tenant: Optional[str] = None, page: int = 1, page_size: int = 100) -> List[dict]:
        """Search courses via the eLumen public API."""
        tenant = tenant or self.tenant
        cache_key = self._search_cache_key(tenant, query, page, page_size)
        if self._is_cache_valid(cache_key, self.search_cache_ttl) and cache_key in self._search_cache:
            return deepcopy(self._search_cache[cache_key])

        results = await self._search_courses_live(query=query, tenant=tenant, page=page, page_size=page_size)
        self._cache_put(self._search_cache, cache_key, results)
        return results

    async def _search_courses_live(self, query: str, tenant: str, page: int = 1, page_size: int = 100) -> List[dict]:
        """Fetch and parse one eLumen course search page from the live API."""
        params = {
            "status": "approved",
            "tenant": tenant,
            "query": query,
            "pageSize": page_size,
            "page": page,
        }

        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
                async with client.stream(
                    "GET", f"{self.base_url}/courses", params=params
                ) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise ValueError("eLumen response exceeds byte limit")
                data = json.loads(body)

            results = []
            items = data.get("_embedded", {}).get("courses", [])
            for item in items:
                raw_json = item.get("fullCourseInfo")
                if raw_json:
                    top_subject = item.get("subject", "")
                    top_number = item.get("number", "")
                    parsed = self._parse_full_course_info(raw_json, top_subject, top_number)
                    if parsed:
                        results.append(parsed)

            logger.info(f"eLumen search '{query}': found {len(results)} courses (page {page})")
            return results

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error searching eLumen courses for '{query}': {e.response.status_code}")
            return []
        except httpx.RequestError as e:
            logger.error(f"Request error searching eLumen courses for '{query}': {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error searching eLumen courses for '{query}': {e}")
            return []

    async def get_course_details(self, course_code: str, campus_code: str = "LAMC") -> Optional[dict]:
        """Get parsed course details for a specific course."""
        app_code = self._normalize_course_code(course_code, to_app_format=True)
        cache_key = self._campus_cache_key("course", campus_code, app_code)

        if self._is_cache_valid(cache_key) and cache_key in self._course_cache:
            return deepcopy(self._course_cache[cache_key])
        if self._is_negative_cached(cache_key):
            return None

        tenant = self._get_tenant(campus_code)
        elumen_code = self._normalize_course_code(course_code, to_app_format=False)
        courses = await self.search_courses(elumen_code, tenant=tenant)

        # Find exact match
        for course in courses:
            if course.get("code") == app_code:
                self._cache_put(self._course_cache, cache_key, course)
                self._negative_cache.pop(self._negative_key(cache_key), None)
                return deepcopy(course)

        # Try matching by prefix + number components
        match = COURSE_CODE_RE.match(app_code)
        if match:
            target_prefix, target_number = match.group(1), match.group(2)
            for course in courses:
                c_match = COURSE_CODE_RE.match(course.get("code", ""))
                if c_match and c_match.group(1) == target_prefix and c_match.group(2) == target_number:
                    self._cache_put(self._course_cache, cache_key, course)
                    self._negative_cache.pop(self._negative_key(cache_key), None)
                    return deepcopy(course)

        logger.warning(f"Course {app_code} not found in eLumen search results")
        self._mark_negative(cache_key)
        return None

    async def get_prerequisites(self, course_code: str, campus_code: str = "LAMC") -> List[List[str]]:
        """Get structured prerequisites for a course.

        Returns list of 'prereq sets' in OR-of-ANDs form:
        Each inner list contains course codes that must ALL be taken (AND).
        Any one inner list satisfies the requirement (OR).
        """
        app_code = self._normalize_course_code(course_code, to_app_format=True)
        cache_key = self._campus_cache_key("prereq", campus_code, app_code)

        if self._is_cache_valid(cache_key) and cache_key in self._prereq_cache:
            return deepcopy(self._prereq_cache[cache_key])

        course = await self.get_course_details(app_code, campus_code)
        if not course:
            return []

        prereqs = course.get("prerequisites", [])
        self._cache_put(self._prereq_cache, cache_key, prereqs)
        return deepcopy(prereqs)

    def cache_stats(self, campus_code: Optional[str] = None) -> dict:
        """Return lightweight cache counts for diagnostics."""
        campus = self._campus_code(campus_code) if campus_code else None
        tenant = self._get_tenant(campus) if campus else None

        def count_valid(keys, prefix: str, ttl: Optional[int] = None) -> int:
            return sum(
                1
                for key in keys
                if key.startswith(prefix) and self._is_cache_valid(key, ttl)
            )

        if campus:
            return {
                "campus": campus,
                "course_entries": count_valid(self._course_cache, f"course:{campus}:"),
                "prereq_entries": count_valid(self._prereq_cache, f"prereq:{campus}:"),
                "negative_course_entries": count_valid(
                    self._negative_cache,
                    f"miss:course:{campus}:",
                    self.negative_cache_ttl,
                ),
                "search_entries": count_valid(self._search_cache, f"search:{tenant}:", self.search_cache_ttl) if tenant else 0,
            }

        return {
            "course_entries": count_valid(self._course_cache, "course:"),
            "prereq_entries": count_valid(self._prereq_cache, "prereq:"),
            "negative_entries": count_valid(self._negative_cache, "miss:", self.negative_cache_ttl),
            "search_entries": count_valid(self._search_cache, "search:", self.search_cache_ttl),
        }

    async def build_full_dependency_graph(self, course_codes: List[str], campus_code: str = "LAMC") -> dict:
        """Build complete dependency graph including advisories, corequisites, and course details.

        Tries Neo4j first for prerequisite data, falls back to eLumen API.
        """
        normalized = [self._normalize_course_code(c, to_app_format=True) for c in course_codes]

        # Try Neo4j first
        neo4j_data = await self._get_from_neo4j(normalized, campus_code)
        if neo4j_data and neo4j_data["prerequisites"]:
            logger.info(f"Using Neo4j prerequisite data for {len(normalized)} courses")
            return neo4j_data

        # Fall back to eLumen API
        return await self._get_from_elumen_api(normalized, campus_code)

    async def _get_from_neo4j(self, course_codes: List[str], campus_code: str) -> Optional[dict]:
        """Query Neo4j for prerequisite/advisory/corequisite data."""
        try:
            from app.services.neo4j_service import neo4j_service
            if not neo4j_service.is_configured():
                return None

            prerequisites: Dict[str, List[List[str]]] = {}
            advisories: Dict[str, List[List[str]]] = {}
            corequisites: Dict[str, List[List[str]]] = {}
            course_details: Dict[str, dict] = {}

            for code in course_codes:
                match = COURSE_CODE_RE.match(code)
                if not match:
                    prerequisites[code] = []
                    advisories[code] = []
                    corequisites[code] = []
                    continue

                subject = match.group(1)
                number = match.group(2)

                # Get prerequisites
                prereq_rows = await neo4j_service.run_query(
                    """
                    MATCH (prereq:Course)-[r:PREREQUISITE_OF]->(c:Course {subject_code: $subject, number: $number, campus_code: $campus})
                    RETURN prereq.code AS prereq_code, r.type AS req_type, r.group AS req_group
                    ORDER BY r.group, prereq.code
                    """,
                    {"subject": subject, "number": number, "campus": campus_code},
                )
                # Group prereqs by group (OR groups)
                prereq_groups: Dict[int, List[str]] = {}
                for row in prereq_rows:
                    g = row.get("req_group") or 0
                    prereq_groups.setdefault(g, []).append(row["prereq_code"])
                prerequisites[code] = list(prereq_groups.values()) if prereq_groups else []

                # Get advisories
                advisory_rows = await neo4j_service.run_query(
                    """
                    MATCH (adv:Course)-[r:ADVISORY_FOR]->(c:Course {subject_code: $subject, number: $number, campus_code: $campus})
                    RETURN adv.code AS advisory_code
                    """,
                    {"subject": subject, "number": number, "campus": campus_code},
                )
                advisories[code] = [[r["advisory_code"]] for r in advisory_rows] if advisory_rows else []

                # Get corequisites
                coreq_rows = await neo4j_service.run_query(
                    """
                    MATCH (coreq:Course)-[r:COREQUISITE_OF]->(c:Course {subject_code: $subject, number: $number, campus_code: $campus})
                    RETURN coreq.code AS coreq_code
                    """,
                    {"subject": subject, "number": number, "campus": campus_code},
                )
                corequisites[code] = [[r["coreq_code"]] for r in coreq_rows] if coreq_rows else []

                # Get course details
                detail_rows = await neo4j_service.run_query(
                    """
                    MATCH (c:Course {subject_code: $subject, number: $number, campus_code: $campus})
                    RETURN c.title AS title, c.description AS description,
                           c.objectives AS objectives, c.slos AS slos,
                           c.units_min AS units_min, c.units_max AS units_max
                    LIMIT 1
                    """,
                    {"subject": subject, "number": number, "campus": campus_code},
                )
                if detail_rows:
                    d = detail_rows[0]
                    course_details[code] = {
                        "title": d.get("title", ""),
                        "description": d.get("description", ""),
                        "objectives": d.get("objectives") or [],
                        "slos": d.get("slos") or [],
                        "units_min": d.get("units_min", 0.0) or 0.0,
                        "units_max": d.get("units_max", 0.0) or 0.0,
                    }

            return {
                "prerequisites": prerequisites,
                "advisories": advisories,
                "corequisites": corequisites,
                "course_details": course_details,
            }
        except Exception as e:
            logger.warning(f"Neo4j prerequisite lookup failed, will try eLumen: {e}")
            return None

    async def _get_from_elumen_api(self, course_codes: List[str], campus_code: str) -> dict:
        """Fetch full dependency graph from eLumen API."""
        prerequisites: Dict[str, List[List[str]]] = {}
        advisories: Dict[str, List[List[str]]] = {}
        corequisites: Dict[str, List[List[str]]] = {}
        course_details: Dict[str, dict] = {}

        to_process = list(set(course_codes))
        processed = set()

        while to_process:
            batch_codes = [c for c in to_process if c not in processed]
            if not batch_codes:
                break

            new_prereq_codes = set()
            for i in range(0, len(batch_codes), 10):
                batch = batch_codes[i:i + 10]
                results = await asyncio.gather(
                    *[self.get_course_details(code, campus_code) for code in batch],
                    return_exceptions=True,
                )
                for code, result in zip(batch, results):
                    processed.add(code)
                    if isinstance(result, Exception):
                        logger.error(f"Error fetching course details for {code}: {result}")
                        prerequisites[code] = []
                        advisories[code] = []
                        corequisites[code] = []
                    elif result is None:
                        prerequisites[code] = []
                        advisories[code] = []
                        corequisites[code] = []
                    else:
                        prerequisites[code] = result.get("prerequisites", [])
                        advisories[code] = result.get("advisories", [])
                        corequisites[code] = result.get("corequisites", [])
                        course_details[code] = {
                            "title": result.get("title", ""),
                            "description": result.get("description", ""),
                            "objectives": result.get("objectives", []),
                            "slos": result.get("slos", []),
                            "topics": result.get("topics", []),
                            "units_min": result.get("units_min", 0.0),
                            "units_max": result.get("units_max", 0.0),
                        }
                        # Collect transitive prereq codes
                        for prereq_set in prerequisites[code]:
                            for prereq_code in prereq_set:
                                if prereq_code not in processed:
                                    new_prereq_codes.add(prereq_code)

            to_process = list(new_prereq_codes)

        logger.info(
            f"Built full dependency graph from eLumen: {len(prerequisites)} courses, "
            f"{sum(1 for v in advisories.values() if v)} with advisories, "
            f"{sum(1 for v in corequisites.values() if v)} with corequisites"
        )
        return {
            "prerequisites": prerequisites,
            "advisories": advisories,
            "corequisites": corequisites,
            "course_details": course_details,
        }

    def _parse_full_course_info(self, raw_json_str: str, top_subject: str = "", top_number: str = "") -> Optional[dict]:
        """Parse the fullCourseInfo JSON string into a clean dict."""
        try:
            data = json.loads(raw_json_str) if isinstance(raw_json_str, str) else raw_json_str
        except (json.JSONDecodeError, TypeError) as e:
            logger.error(f"Failed to parse fullCourseInfo JSON: {e}")
            return None

        code = self._extract_course_code(data, top_subject, top_number)
        if not code:
            return None

        units_min, units_max = self._extract_units(data)
        requisites = self._parse_requisites(data.get("requisites"))

        topics = []
        for topic in data.get("courseTopics", []):
            if isinstance(topic, dict) and topic.get("name"):
                topics.append(topic["name"])

        slos = []
        for outcome in data.get("outcomes", []):
            if isinstance(outcome, dict) and outcome.get("text"):
                slos.append(outcome["text"])

        objectives = []
        for obj in data.get("objectives", []):
            if isinstance(obj, dict) and obj.get("text"):
                objectives.append(obj["text"])

        transfer_status = None
        cb05 = data.get("cb05")
        if cb05:
            transfer_status = cb05

        return {
            "code": code,
            "title": data.get("courseTitle", "").strip(),
            "description": data.get("courseDescription", "").strip(),
            "units_min": units_min,
            "units_max": units_max,
            "prerequisites": requisites.get("prerequisites", []),
            "corequisites": requisites.get("corequisites", []),
            "advisories": requisites.get("advisories", []),
            "topics": topics,
            "slos": slos,
            "objectives": objectives,
            "transfer_status": transfer_status,
            "academic_year": data.get("academicYear"),
            "status": data.get("courseStatus"),
        }

    def _extract_course_code(self, data: dict, top_subject: str = "", top_number: str = "") -> Optional[str]:
        """Extract and normalize course code from eLumen course data."""
        # Strategy 1: Use top-level subject + number from wrapper (most reliable)
        if top_subject and top_number:
            raw_code = f"{top_subject}{top_number}".strip()
            return self._normalize_course_code(raw_code, to_app_format=True)

        # Strategy 2: Try offerings
        offerings = data.get("offerings", [])
        if offerings and isinstance(offerings, list):
            offering = offerings[0]
            if isinstance(offering, dict):
                dept = offering.get("department", "")
                catalog_num = offering.get("catalogNumber", "")
                suffix = offering.get("suffix", "") or ""
                if dept and catalog_num:
                    raw_code = f"{dept}{catalog_num}{suffix}".strip()
                    return self._normalize_course_code(raw_code, to_app_format=True)

        # Strategy 3: subject (dict or str) + number from fullCourseInfo
        subject = data.get("subject", "")
        if isinstance(subject, dict):
            subject = subject.get("code", "") or subject.get("value", "")
        number = data.get("number", "")
        if subject and number:
            raw_code = f"{subject}{number}".strip()
            return self._normalize_course_code(raw_code, to_app_format=True)

        # Strategy 4: department (dict or str) + number
        dept = data.get("department", "")
        if isinstance(dept, dict):
            dept = dept.get("code", "")
        if dept and number:
            raw_code = f"{dept}{number}".strip()
            return self._normalize_course_code(raw_code, to_app_format=True)

        return None

    def _extract_units(self, data: dict) -> tuple:
        """Extract min and max units from creditsAndHours."""
        units_min = 0.0
        units_max = 0.0
        credits_hours = data.get("creditsAndHours", [])
        if isinstance(credits_hours, list):
            for entry in credits_hours:
                if isinstance(entry, dict):
                    min_c = entry.get("minimumCredit")
                    max_c = entry.get("maximumCredit")
                    if min_c is not None:
                        try:
                            units_min = max(units_min, float(min_c))
                        except (ValueError, TypeError):
                            pass
                    if max_c is not None:
                        try:
                            units_max = max(units_max, float(max_c))
                        except (ValueError, TypeError):
                            pass
        if units_max == 0.0 and units_min > 0.0:
            units_max = units_min
        return units_min, units_max

    def _parse_requisites(self, requisites_data) -> Dict[str, List[List[str]]]:
        """Parse nested OR/AND requisite structure from eLumen."""
        result = {
            "prerequisites": [],
            "corequisites": [],
            "advisories": [],
        }

        if not requisites_data:
            return result

        if isinstance(requisites_data, str):
            try:
                requisites_data = json.loads(requisites_data)
            except (json.JSONDecodeError, TypeError):
                return result

        if not isinstance(requisites_data, dict):
            return result

        items_by_type: Dict[str, List[List[str]]] = {
            "prerequisites": [],
            "corequisites": [],
            "advisories": [],
        }

        block_list = requisites_data.get("blockList", [])
        if not isinstance(block_list, list):
            return result

        for or_block in block_list:
            if not isinstance(or_block, dict):
                continue

            inner_blocks = or_block.get("blockList", [])
            if not isinstance(inner_blocks, list):
                continue

            prereq_and_group = []
            coreq_and_group = []
            advisory_and_group = []

            for inner_item in inner_blocks:
                if not isinstance(inner_item, dict):
                    continue

                item = inner_item.get("item", {})
                if not isinstance(item, dict):
                    continue

                if not item.get("isCourse", False):
                    continue

                code = item.get("code", "")
                if not code:
                    continue

                normalized = self._normalize_course_code(code, to_app_format=True)
                item_type = (item.get("itemType") or "").lower()

                if item_type == "corequisite":
                    coreq_and_group.append(normalized)
                elif item_type == "advisory":
                    advisory_and_group.append(normalized)
                else:
                    prereq_and_group.append(normalized)

            if prereq_and_group:
                items_by_type["prerequisites"].append(prereq_and_group)
            if coreq_and_group:
                items_by_type["corequisites"].append(coreq_and_group)
            if advisory_and_group:
                items_by_type["advisories"].append(advisory_and_group)

        result.update(items_by_type)

        # Fallback: if no prerequisites found, try extracting from customFields text
        if not result["prerequisites"]:
            custom_prereqs = self._extract_prereqs_from_text(requisites_data)
            if custom_prereqs:
                result["prerequisites"] = custom_prereqs

        return result

    def _extract_prereqs_from_text(self, requisites_data: dict) -> List[List[str]]:
        """Fallback: extract prerequisite course codes from customFields text."""
        custom_fields = requisites_data.get("customFields")
        if not custom_fields:
            return []

        text = ""
        if isinstance(custom_fields, str):
            text = custom_fields
        elif isinstance(custom_fields, dict):
            text = custom_fields.get("text", "") or custom_fields.get("prerequisite", "") or str(custom_fields)
        elif isinstance(custom_fields, list):
            text = " ".join(str(f) for f in custom_fields)

        if not text:
            return []

        matches = COURSE_CODE_EXTRACT_RE.findall(text)
        prereqs = []
        seen = set()
        for prefix, number in matches:
            code = self._normalize_course_code(f"{prefix}{number}", to_app_format=True)
            if code not in seen:
                seen.add(code)
                prereqs.append([code])

        return prereqs


# Singleton
_service: Optional[ElumenService] = None


def get_elumen_service() -> ElumenService:
    """Get the singleton ElumenService instance."""
    global _service
    if _service is None:
        _service = ElumenService()
    return _service
