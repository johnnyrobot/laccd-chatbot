"""
Course Sequence Service

Generates prerequisite-aware, semester-by-semester course sequences for any program.

Uses:
- Program Mapper API for program requirements (pathwayElements with suggested term order)
- eLumen API + Neo4j for prerequisite graph (hard constraints)
- CourseOrderingAnalyzer for deterministic soft ordering signals

There is deliberately no LLM pass in here. One used to re-derive the soft-edge
graph on every call; because an LLM samples, the same student asking the same
question twice got two different plans.
"""

import logging
import re
from typing import Any, Optional, List, Dict, Set, Tuple
from collections import defaultdict

from app.services.program_mapper import get_program_mapper_client
from app.services.elumen_service import get_elumen_service
from app.services.course_ordering_analyzer import CourseOrderingAnalyzer

logger = logging.getLogger(__name__)

# Regex to detect valid course codes:
#   "CIS 101", "ADM JUS 001", "STAT C1000", "WELDG/E 111", "BIO-T 100"
# The subject prefix is one or more letter-words optionally joined by `/` or `-`,
# followed by up to two more space-separated letter-words for compound subjects.
COURSE_CODE_RE = re.compile(
    r'^([A-Z]{2,8}(?:[/-][A-Z]{1,8})?(?:\s+[A-Z]{1,8}){0,2})\s+(C?-?\d{1,4}[A-Z]{0,2})$'
)
COURSE_CODE_PREFIX_RE = re.compile(
    r'^\s*([A-Z]{2,8}(?:[/-][A-Z]{1,8})?(?:\s+[A-Z]{1,8}){0,2}\s+C?-?\d{1,4}[A-Z]{0,2})(?=\s*(?::|-|\(|$))'
)
HIGH_CONFIDENCE_SOFT_ORDER_WEIGHT = 0.7

# Gateway-course subject patterns (CCRC Guided Pathways: college-level English
# and college-level Math/Stat are the two key first-year momentum courses).
# Includes the new California Common Course Numbering forms (ENGL C1000,
# MATH C1000, STAT C1000) being adopted Fall 2025-2027.
_GATEWAY_ENGLISH_RE = re.compile(
    r"^(ENGLISH|ENGL|ENG)(\s+\S+)?\s+(C?-?\d{1,4}[A-Z]{0,2})$",
    re.IGNORECASE,
)
_GATEWAY_MATH_RE = re.compile(
    r"^(MATH|STAT|STATISTICS)(\s+\S+)?\s+(C?-?\d{1,4}[A-Z]{0,2})$",
    re.IGNORECASE,
)


def is_gateway_english(code: str) -> bool:
    """Subject is college-level English (gateway course per CCRC framework)."""
    return bool(_GATEWAY_ENGLISH_RE.match((code or "").strip()))


def is_gateway_math(code: str) -> bool:
    """Subject is college-level Math or Statistics (gateway course)."""
    return bool(_GATEWAY_MATH_RE.match((code or "").strip()))


def is_gateway_course(code: str) -> bool:
    """Combined gateway test — either English or Math/Stat."""
    return is_gateway_english(code) or is_gateway_math(code)


# Pace options use units-per-term targets aligned with CCRC research.
# "full" matches PM templates and is the default. "momentum" packs the schedule
# closer to the 18-unit ceiling for high-momentum students who can absorb it.
# "part_time" caps at 9 units per term for working students.
PACE_MAX_UNITS: Dict[str, int] = {
    "full": 15,
    "momentum": 18,
    "part_time": 9,
}
DEFAULT_PACE = "full"


def normalize_pace(pace: Optional[str]) -> str:
    """Map any input string to a valid pace key, falling back to default."""
    if not pace:
        return DEFAULT_PACE
    key = str(pace).strip().lower().replace("-", "_").replace(" ", "_")
    return key if key in PACE_MAX_UNITS else DEFAULT_PACE


class CourseSequenceService:
    """Service for generating course sequences."""

    def __init__(self, campus_code: str = "LAMC"):
        self.campus_code = campus_code
        self.elumen = get_elumen_service()
        self.analyzer = CourseOrderingAnalyzer()

    async def generate_sequence(
        self,
        program_id: str,
        campus_code: str = "LAMC",
        max_units_per_term: Optional[int] = None,
        pace: Optional[str] = None,
        assist_service: Optional[Any] = None,
    ) -> dict:
        """Generate a full AMP-style semester-by-semester course sequence.

        Args:
            program_id: Program Mapper masterRecordId
            campus_code: Campus code (e.g., "LAMC")
            max_units_per_term: Hard override for units cap (used by tests).
                When None, the cap is derived from `pace`.
            pace: One of "full" (15u/term, default), "momentum" (18u/term, fast
                track), or "part_time" (9u/term). Unrecognized values fall back
                to default. Ignored when max_units_per_term is set explicitly.
            assist_service: Optional ``AssistService`` instance. When provided,
                external prerequisites are logged for telemetry (v1 stub —
                actual articulation lookups land in v1.5 once we have logged
                evidence of which external prereqs benefit). When None, the
                hook is inert. The parameter exists so callers can wire it now
                without a follow-up signature change.

        Returns:
            Dict with program_name, award_type, total_units, terms_to_complete,
            pace, sequence (list of term dicts), prerequisite_chains, milestones,
            ge_progress, and notes.
        """
        # Resolve pace + max-units (max_units_per_term wins for test injection)
        resolved_pace = normalize_pace(pace)
        if max_units_per_term is None:
            max_units_per_term = PACE_MAX_UNITS[resolved_pace]

        # 1. Fetch program requirements from Program Mapper
        program_info, elements = await self._get_program_requirements(program_id, campus_code)
        program_info = program_info if isinstance(program_info, dict) else {}
        elements = elements if isinstance(elements, list) else []
        program_name = program_info.get('title', 'Unknown Program')
        award_type = program_info.get('awardTitle', program_info.get('awardShortTitle', ''))

        # 2. Extract course codes and PM titles for eLumen lookup
        course_items, skipped_elements = self._parse_pathway_elements(elements)
        course_codes = [item['code'] for item in course_items if not item['is_generic']]
        pm_titles = {
            item['code']: item['title']
            for item in course_items
            if not item['is_generic'] and item.get('title')
        }

        # 3. Build full dependency graph (Neo4j first, eLumen fallback)
        full_graph = await self._build_full_dependency_graph(course_codes, campus_code)
        prereq_graph = full_graph["prerequisites"]
        advisories = full_graph["advisories"]
        corequisites = full_graph["corequisites"]
        course_details = full_graph["course_details"]

        # 4. Run multi-signal ordering analysis
        soft_order, coreq_groups, course_levels = self.analyzer.analyze(
            course_codes=course_codes,
            advisories=advisories,
            corequisites=corequisites,
            course_details=course_details,
            pm_titles=pm_titles,
        )

        # 4b. Pre-resolve renumbering aliases for prereq codes that aren't
        # already program-internal. The walker maps CCN ↔ legacy aliases
        # (STAT C1000 ↔ STATC 1000, MATH 227 → STAT C1000) using seeded
        # RENUMBERED_TO chains in Neo4j plus a C-form alternation heuristic.
        # Anything that resolves to an in-program code is added to the
        # "covered" set inside the sort, so it doesn't surface as external.
        renumbering_resolutions = await self._resolve_renumbering_aliases(
            prereq_graph, set(course_codes), campus_code,
        )

        # 5. Build the sorted sequence with enriched ordering
        sequence, prereq_chains, diagnostics = self._topological_sort_with_terms(
            elements, prereq_graph, set(), max_units_per_term,
            soft_order=soft_order,
            coreq_groups=coreq_groups,
            course_levels=course_levels,
            renumbering_aliases=renumbering_resolutions,
            include_diagnostics=True,
            # The college's own term assignment is the default grouping;
            # unit-cap repacking is what a chosen pace track opts into.
            prefer_vendor_terms=(resolved_pace == DEFAULT_PACE),
        )

        # 5a. networkx-backed prereq-depth diagnostic. Logs a warning if any
        # course was scheduled before its depth floor — purely observability,
        # the placement is not changed.
        depth_violations = self._depth_violations(
            sequence, prereq_graph, set(course_codes),
        )
        if depth_violations:
            logger.warning(
                "Sequence depth-floor violations for program %s: %s",
                program_id, depth_violations,
            )

        # 5b. Pull GE / transferability / renumber metadata from Neo4j (best-effort,
        # additive — silently no-ops when Neo4j is unconfigured or unreachable).
        course_metadata = await self._fetch_course_metadata(course_codes, campus_code)

        # 5c. Enrich each course in the schedule with ge_area, transferable, and
        # renumbered_from. Mutates `sequence` in place; gathers term/program GE rollups.
        ge_progress = self._enrich_sequence_with_metadata(sequence, course_metadata)

        # 6. Calculate totals
        total_units = 0.0
        for term in sequence:
            total_units += term['total_units']

        notes = []
        if resolved_pace != DEFAULT_PACE:
            pace_label = {
                "momentum": f"momentum ({max_units_per_term} units/term)",
                "part_time": f"part-time ({max_units_per_term} units/term)",
            }.get(resolved_pace, f"{resolved_pace} ({max_units_per_term} units/term)")
            notes.append(f"Schedule built for {pace_label} pace.")
        if not prereq_graph:
            notes.append("Prerequisite data unavailable; sequence follows suggested program order only.")
        if diagnostics["skipped_elements"]:
            notes.append("Some non-course pathway rows were skipped while building the sequence.")
        # External-prereq telemetry: classify each surfaced prereq and emit
        # a structured log line. Always-on (no longer gated on
        # `assist_service`) so production telemetry accumulates regardless
        # of which call sites are wired. See external_prereq_telemetry.py
        # for the threshold gate that decides when an ASSIST resolver is
        # warranted.
        from app.services.external_prereq_telemetry import emit as emit_telemetry
        program_codes_for_telemetry = diagnostics.get("program_codes_snapshot") or set()
        alts_for_course = diagnostics.get("prereq_alternatives") or {}
        if diagnostics["external_prerequisites"]:
            notes.append("Some prerequisites are outside this program sequence; check with a counselor or placement record.")
            for course_code, prereq_list in diagnostics["external_prerequisites"].items():
                alts = alts_for_course.get(course_code, list(prereq_list))
                for prereq in prereq_list:
                    emit_telemetry(
                        program_name=program_name,
                        campus_code=campus_code,
                        course_code=course_code,
                        prereq=prereq,
                        alternatives=alts,
                        program_codes=program_codes_for_telemetry,
                        award_type=award_type,
                    )
        # Emit resolved=true telemetry for prereqs the renumbering walker
        # mapped to in-program codes. Counts toward observation but is
        # subtracted from the threshold gate.
        renumbering_emitted = diagnostics.get("renumbering_resolutions") or {}
        for source_code, resolved_to in renumbering_emitted.items():
            alts_for_resolved = alts_for_course.get(source_code) or [source_code]
            emit_telemetry(
                program_name=program_name,
                campus_code=campus_code,
                course_code=source_code,
                prereq=source_code,
                alternatives=alts_for_resolved,
                program_codes=program_codes_for_telemetry,
                award_type=award_type,
                resolved=True,
                resolved_to=resolved_to,
            )

        warnings = diagnostics["warnings"]
        skipped_elements = diagnostics["skipped_elements"] or skipped_elements

        # 6b. AA-T / AS-T programs: warn on courses missing CSU transferability.
        non_transferable = self._non_csu_transferable_courses(sequence, course_metadata, award_type)
        if non_transferable:
            warnings = list(warnings) + [
                f"For this transfer degree, verify CSU transferability for: "
                f"{', '.join(non_transferable[:5])}"
                + (" …" if len(non_transferable) > 5 else "")
            ]

        sequence_status = "generated_with_warnings" if warnings or skipped_elements else "generated"

        # 6c. Build milestones from PM MILESTONE rows + derived gateway markers.
        milestones = self._collect_milestones(elements, sequence, award_type)

        return {
            "program_name": program_name,
            "award_type": award_type,
            "total_units": total_units,
            "terms_to_complete": len(sequence),
            "pace": resolved_pace,
            "max_units_per_term": max_units_per_term,
            "sequence": sequence,
            "prerequisite_chains": prereq_chains,
            "milestones": milestones,
            "notes": notes,
            "sequence_status": sequence_status,
            "warnings": warnings,
            "external_prerequisites": diagnostics["external_prerequisites"],
            "term_source": diagnostics["term_source"],
            "skipped_elements": skipped_elements,
            "ge_progress": ge_progress,
        }

    async def _resolve_renumbering_aliases(
        self,
        prereq_graph: Dict[str, List[List[str]]],
        program_codes: Set[str],
        campus_code: str,
    ) -> Dict[str, str]:
        """Map prereq codes to in-program codes via the renumbering walker.

        Returns ``{source_code: canonical_code}`` for each external prereq that
        the walker mapped to a program-internal course. Empty dict when Neo4j
        is unconfigured. Skips already-program-internal prereqs.
        """
        try:
            from app.services.neo4j_service import neo4j_service
        except Exception:
            return {}
        if not neo4j_service.is_configured():
            return {}

        all_prereqs: Set[str] = set()
        for branches in (prereq_graph or {}).values():
            for branch in branches or []:
                for raw in branch or []:
                    code = self._extract_course_code(raw)
                    if code:
                        all_prereqs.add(code)
        external_prereqs = all_prereqs - program_codes
        if not external_prereqs:
            return {}

        resolutions: Dict[str, str] = {}
        for code in external_prereqs:
            parts = code.rsplit(" ", 1)
            if len(parts) != 2:
                continue
            subject, number = parts[0], parts[1]
            try:
                resolved_subject, resolved_number, via = await neo4j_service.resolve_course_code(
                    subject, number, campus_code,
                )
            except Exception as exc:
                logger.debug("renumbering resolve failed for %s: %s", code, exc)
                continue
            if not via:
                continue
            canonical = f"{resolved_subject} {resolved_number}"
            if canonical in program_codes and canonical != code:
                resolutions[code] = canonical
        return resolutions

    async def _get_program_requirements(self, program_id: str, campus_code: str) -> Tuple[dict, List[dict]]:
        """Fetch program info and pathwayElements — Neo4j first, PM API fallback."""
        import json as _json
        # Try Neo4j first (has pathway_elements stored by sync script)
        try:
            from app.services.neo4j_service import neo4j_service
            if neo4j_service.is_configured():
                rows = await neo4j_service.run_query(
                    """
                    MATCH (p:Program {master_record_id: $pid})
                    RETURN p.title AS title, p.award_type AS award_type,
                           p.pathway_elements AS pathway_elements
                    LIMIT 1
                    """,
                    {"pid": program_id},
                )
                if rows and rows[0].get("pathway_elements"):
                    row = rows[0]
                    elements = _json.loads(row["pathway_elements"])
                    program_info = {"title": row.get("title", ""), "awardTitle": row.get("award_type", "")}
                    logger.info(
                        f"Loaded {len(elements)} pathway elements from Neo4j for "
                        f"'{program_info.get('title', program_id)}'"
                    )
                    return program_info, elements
        except Exception as e:
            logger.debug(f"Neo4j pathway lookup failed, trying PM API: {e}")

        # Fall back to PM API
        client = get_program_mapper_client(campus_code)
        bundle = await client.get_program_bundle(program_id)
        program_info = bundle.get('program', {})
        requirements = bundle.get('requirements', {})
        elements = requirements.get('pathwayElements', []) if requirements else []
        logger.info(
            f"Fetched {len(elements)} pathway elements from PM API for "
            f"'{program_info.get('title', program_id)}'"
        )
        return program_info, elements

    async def _build_full_dependency_graph(self, course_codes: List[str], campus_code: str) -> dict:
        """Build full dependency graph with graceful fallback."""
        empty = {
            "prerequisites": {},
            "advisories": {},
            "corequisites": {},
            "course_details": {},
        }
        if not course_codes:
            return empty

        try:
            result = await self.elumen.build_full_dependency_graph(course_codes, campus_code)
            logger.info(
                f"Built full dependency graph with {len(result['prerequisites'])} entries"
            )
            return result
        except Exception as e:
            logger.warning(f"Full dependency graph unavailable, falling back to PM order: {e}")
            return empty

    async def _fetch_course_metadata(
        self, course_codes: List[str], campus_code: str,
    ) -> Dict[str, Dict[str, Any]]:
        """Pull GE / transferability / renumber metadata for the program's courses.

        Best-effort: returns {} when Neo4j is unconfigured or the query errors.
        Each entry is keyed by the *PM-supplied* code so downstream lookups in
        sequence enrichment match without normalization.
        """
        if not course_codes:
            return {}
        try:
            from app.services.neo4j_service import neo4j_service
            if not neo4j_service.is_configured():
                return {}
            return await neo4j_service.get_course_metadata_bulk(course_codes, campus_code)
        except Exception as e:
            logger.debug(f"Course metadata enrichment skipped: {e}")
            return {}

    @staticmethod
    def _enrich_sequence_with_metadata(
        sequence: List[dict],
        metadata: Dict[str, Dict[str, Any]],
    ) -> Dict[str, int]:
        """Annotate each course (in place) with GE / transferable / renumber data
        and return a {ge_code: count} program-wide rollup.

        Mutates each course dict to add:
          - ge_areas:        list of {code, system, name}
          - ge_summary:      short label e.g. "IGETC 2A; CSU-GE B4"
          - transferable:    list, e.g. ["UC", "CSU"]
          - renumbered_from: original PM code if a RENUMBERED_TO chain was followed

        Mutates each term dict to add:
          - ge_areas_covered: dedup list of GE codes for that term

        Returns the program-wide GE coverage count, e.g. {"2A": 1, "B4": 1, ...}.
        """
        ge_progress: Dict[str, int] = {}
        if not metadata:
            for term in sequence:
                term.setdefault("ge_areas_covered", [])
            return ge_progress

        for term in sequence:
            term_ge: List[str] = []
            for course in term.get("courses", []):
                meta = metadata.get(course["code"])
                if not meta:
                    continue
                ge_areas = meta.get("ge_areas") or []
                course["ge_areas"] = ge_areas
                if ge_areas:
                    course["ge_summary"] = "; ".join(
                        f"{g.get('system') or '?'} {g.get('code') or '?'}"
                        for g in ge_areas
                    )
                transferable: List[str] = []
                if meta.get("uc_transferable"):
                    transferable.append("UC")
                if meta.get("csu_transferable"):
                    transferable.append("CSU")
                course["transferable"] = transferable
                if meta.get("renumbered_from"):
                    course["renumbered_from"] = meta["renumbered_from"]
                # Roll up GE codes for the term and program
                for g in ge_areas:
                    code = g.get("code")
                    if not code:
                        continue
                    if code not in term_ge:
                        term_ge.append(code)
                    ge_progress[code] = ge_progress.get(code, 0) + 1
            term["ge_areas_covered"] = term_ge
        return ge_progress

    @staticmethod
    def _compute_prereq_depth_floor(
        prereq_graph: Dict[str, List[List[str]]],
        program_codes: Set[str],
    ) -> Dict[str, int]:
        """Compute the minimum term each course could possibly land in based on
        prereq depth alone (1-indexed, ignoring pm_term and unit caps).

        Uses networkx.topological_generations on the (acyclic projection of the)
        prereq DAG. A course at generation N has at least N prereq layers below
        it, so it cannot land before term N+1.

        This is used purely as a *diagnostic* signal — the real scheduler also
        respects pm_term, units, soft order, and coreq atomicity. Returns {}
        when the graph is cyclic or networkx is unavailable.
        """
        try:
            import networkx as nx  # imported lazily so unit tests don't require it
        except ImportError:
            return {}

        graph = nx.DiGraph()
        for code in sorted(program_codes):
            graph.add_node(code)
        for target, branches in (prereq_graph or {}).items():
            if target not in program_codes:
                continue
            chosen = (branches or [[]])[0]  # first branch is good enough for a depth floor
            for prereq in chosen or []:
                if prereq in program_codes and prereq != target:
                    graph.add_edge(prereq, target)

        if not nx.is_directed_acyclic_graph(graph):
            return {}

        floor: Dict[str, int] = {}
        for level, layer in enumerate(nx.topological_generations(graph), start=1):
            for code in layer:
                floor[code] = level
        return floor

    @classmethod
    def _depth_violations(
        cls,
        sequence: List[dict],
        prereq_graph: Dict[str, List[List[str]]],
        program_codes: Set[str],
    ) -> List[Dict[str, Any]]:
        """Return courses whose scheduled term is below their prereq-depth floor.

        These are unusual — the scheduler should normally never produce them —
        so any non-empty result indicates a logic gap worth investigating.
        """
        depth = cls._compute_prereq_depth_floor(prereq_graph, program_codes)
        if not depth:
            return []
        violations: List[Dict[str, Any]] = []
        for term in sequence:
            for course in term.get("courses", []):
                code = course.get("code")
                if code not in depth:
                    continue
                actual = term["term_number"]
                expected_floor = depth[code]
                if actual < expected_floor:
                    violations.append({
                        "code": code,
                        "scheduled_term": actual,
                        "depth_floor": expected_floor,
                    })
        return violations

    @staticmethod
    def _collect_milestones(
        elements: List[dict],
        sequence: List[dict],
        award_type: str,
    ) -> List[Dict[str, Any]]:
        """Build a deduplicated milestones list from two sources:

          1. PM-authored MILESTONE rows (requirementType=MILESTONE) — these are
             campus pathway team annotations and are authoritative.
          2. CCRC-derived gateway-completion markers — first-term placement of
             a college-level English course → "Complete college English" milestone;
             same for Math/Stat.

        Returns a list of {term, label, source: "pm" | "derived"} sorted by term.
        """
        milestones: List[Dict[str, Any]] = []
        seen_labels: Set[str] = set()

        # 1. PM-authored milestones
        for elem in elements or []:
            if not isinstance(elem, dict):
                continue
            req = elem.get('requirement') if isinstance(elem.get('requirement'), dict) else {}
            if str(req.get('requirementType') or '').upper() != 'MILESTONE':
                continue
            label = str(elem.get('shortDescription') or elem.get('name') or '').strip()
            if not label or label.lower() in seen_labels:
                continue
            opp = elem.get('recommendedOpportunity') if isinstance(elem.get('recommendedOpportunity'), dict) else {}
            term_info = opp.get('term') if isinstance(opp.get('term'), dict) else {}
            term_num = term_info.get('termNumber')
            try:
                term_num = max(int(term_num), 1)
            except (TypeError, ValueError):
                term_num = 1
            milestones.append({
                "term": term_num,
                "label": label,
                "source": "pm",
            })
            seen_labels.add(label.lower())

        # 2. Derived: gateway course completion markers
        first_english_term: Optional[int] = None
        first_math_term: Optional[int] = None
        for term in sequence:
            for course in term.get("courses", []):
                code = course.get("code") or ""
                if first_english_term is None and is_gateway_english(code):
                    first_english_term = term["term_number"]
                if first_math_term is None and is_gateway_math(code):
                    first_math_term = term["term_number"]
        if first_english_term is not None:
            label = "Complete college English"
            if label.lower() not in seen_labels:
                milestones.append({
                    "term": first_english_term,
                    "label": label,
                    "source": "derived",
                })
                seen_labels.add(label.lower())
        if first_math_term is not None:
            label = "Complete college Math"
            if label.lower() not in seen_labels:
                milestones.append({
                    "term": first_math_term,
                    "label": label,
                    "source": "derived",
                })
                seen_labels.add(label.lower())

        # 3. AA-T programs: surface a "Submit transfer application" reminder
        #    in the last term if PM didn't already include one.
        if "for transfer" in (award_type or "").lower() and sequence:
            last_term = sequence[-1]["term_number"]
            label = "Submit transfer application"
            if not any("transfer" in m["label"].lower() and "apply" in m["label"].lower()
                       for m in milestones) and label.lower() not in seen_labels:
                milestones.append({
                    "term": last_term,
                    "label": label,
                    "source": "derived",
                })

        milestones.sort(key=lambda m: (m["term"], m["label"]))
        return milestones

    @staticmethod
    def _non_csu_transferable_courses(
        sequence: List[dict],
        metadata: Dict[str, Dict[str, Any]],
        award_type: str,
    ) -> List[str]:
        """For transfer degrees only, list real courses that lack CSU transferability."""
        if "for transfer" not in (award_type or "").lower():
            return []
        offenders: List[str] = []
        for term in sequence:
            for course in term.get("courses", []):
                code = course.get("code")
                meta = metadata.get(code)
                if not meta:
                    continue
                if meta.get("csu_transferable") is False:
                    offenders.append(code)
        return offenders

    def _parse_pathway_elements(self, elements: List[dict]) -> Tuple[List[dict], List[dict]]:
        """Parse Program Mapper pathway elements defensively into schedulable items."""
        course_items = []
        skipped_elements = []

        for index, elem in enumerate(elements or []):
            if not isinstance(elem, dict):
                skipped_elements.append({
                    "index": index,
                    "name": "",
                    "reason": "Pathway element was not an object.",
                })
                continue

            raw_name = str(elem.get('name') or '').strip()
            code = self._extract_course_code(raw_name)
            is_valid_code = bool(code)
            title = str(elem.get('shortDescription') or '').strip()

            opp = self._safe_dict(elem.get('recommendedOpportunity'))
            term_info = self._safe_dict(opp.get('term'))
            pm_term = self._parse_pm_term(term_info.get('termNumber'))

            req = self._safe_dict(elem.get('requirement'))
            req_type = str(req.get('requirementType') or 'ELECTIVE')
            req_type_label = req_type.replace('_', ' ').title()

            units = self._parse_units(opp.get('minUnits'), default=3.0 if is_valid_code else 0.0)
            if not raw_name and not title:
                skipped_elements.append({
                    "index": index,
                    "name": "",
                    "reason": "Blank pathway element.",
                })
                continue
            if not is_valid_code and units <= 0:
                skipped_elements.append({
                    "index": index,
                    "name": raw_name or title,
                    "reason": "Non-course pathway element has no units.",
                })
                continue

            # PM occasionally encodes alternatives as "X or Y" in a single row
            # (e.g. "CIS 101 or CS 101"). The full token doesn't match
            # COURSE_CODE_RE so we'd otherwise treat it as generic and lose
            # both codes — which then makes those codes appear "external"
            # when they show up as eLumen prereqs.
            alt_codes = self._extract_alternative_codes(raw_name)

            item = {
                'code': code if is_valid_code else raw_name or title or 'GE Elective',
                'title': title,
                'units': units,
                'requirement_type': req_type_label if is_valid_code else 'Elective',
                'pm_term': pm_term,
                'is_generic': not is_valid_code,
                'source_name': raw_name,
                'alt_codes': alt_codes,
            }
            course_items.append(item)

        return course_items, skipped_elements

    @classmethod
    def _extract_alternative_codes(cls, name: str) -> List[str]:
        """Split a "X or Y" pathway row into normalized course codes.

        Returns an empty list when the row is not an alternation. The first
        code is treated as primary; downstream callers who want all codes
        treated as program-internal should consume the full list.
        """
        if not name or " or " not in name.lower():
            return []
        # Split on " or " (case-insensitive) and try to extract a code from each part
        parts = re.split(r"\s+or\s+", name, flags=re.IGNORECASE)
        codes: List[str] = []
        for part in parts:
            extracted = cls._extract_course_code(part)
            if extracted and extracted not in codes:
                codes.append(extracted)
        return codes if len(codes) >= 2 else []

    @staticmethod
    def _safe_dict(value: Any) -> dict:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _parse_units(value: Any, default: float = 3.0) -> float:
        """Parse PM unit values, including ranges such as '3-4', without throwing."""
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return max(float(value), 0.0)
        text = str(value).strip()
        if not text:
            return default
        match = re.search(r'\d+(?:\.\d+)?', text)
        if not match:
            return default
        return max(float(match.group(0)), 0.0)

    @staticmethod
    def _parse_pm_term(value: Any) -> int:
        """Parse PM term numbers. Missing/invalid values mean no minimum term."""
        if value is None:
            return 1
        if isinstance(value, (int, float)):
            term = int(value)
        else:
            match = re.search(r'\d+', str(value))
            term = int(match.group(0)) if match else 1
        return max(term, 1)

    @staticmethod
    def _format_term_label(term_number: int) -> str:
        return f"Semester {term_number} ({CourseSequenceService._format_term_season(term_number)})"

    @staticmethod
    def _format_term_season(term_number: int) -> str:
        return "Fall" if term_number % 2 == 1 else "Spring"

    @staticmethod
    def _normalize_course_code(code: str) -> str:
        if not code:
            return ""
        text = re.sub(r'\s+', ' ', str(code).strip().upper())
        prefix_match = COURSE_CODE_PREFIX_RE.match(text)
        if prefix_match:
            text = prefix_match.group(1)
        text = text.replace("C-", "C")
        return re.sub(r'\s+', ' ', text).strip()

    @staticmethod
    def _squash_code(code: str) -> str:
        """Space-insensitive key for course-code comparison.

        Program Mapper and the prerequisite graph disagree about where the
        space falls in CCN codes — "PSYC C1000" vs "PSYCC 1000" are the same
        course. Both squash to "PSYCC1000".
        """
        return re.sub(r'\s+', '', (code or '').upper())

    @classmethod
    def _subject_token(cls, value: str) -> str:
        """Subject letters of a course code, or the first word of a row name.

        Used to link a generic pathway row to the prerequisite it satisfies:
        the row is named "STATISTICS" while the prerequisite is "STAT C1000",
        so there is no code to match on — only the discipline.
        """
        squashed = cls._squash_code(value)
        match = re.match(r'^([A-Z]+)(\d+)', squashed)
        if not match:
            # A generic row name: take its first word ("English Composition").
            words = re.split(r'[^A-Za-z]+', (value or '').strip())
            return words[0].upper() if words and words[0] else ""
        letters, digits = match.group(1), match.group(2)
        # CCN codes squash their 'C' onto the subject: STATC1000 -> STAT C1000.
        # Legacy three-digit numbers (MUSC 101) keep theirs.
        if len(digits) >= 4 and letters.endswith('C'):
            letters = letters[:-1]
        return letters

    @classmethod
    def _generic_row_satisfies(cls, row_name: str, prereq_code: str) -> bool:
        """Whether a generic row plausibly covers a prerequisite.

        Deliberately generous: over-matching only constrains a generic row to
        be scheduled earlier, which cannot create a prerequisite violation,
        while under-matching leaves the row free to land on top of the course
        it unlocks.
        """
        subject = cls._subject_token(prereq_code)
        row = cls._subject_token(row_name)
        if len(subject) < 3 or len(row) < 4:
            return False
        return row.startswith(subject) or subject.startswith(row)

    @classmethod
    def _extract_course_code(cls, name: str) -> str:
        normalized = cls._normalize_course_code(name)
        return normalized if COURSE_CODE_RE.match(normalized) else ""

    @classmethod
    def _normalize_prereq_graph(
        cls,
        prereq_graph: Dict[str, List[List[str]]],
    ) -> Dict[str, List[List[str]]]:
        normalized: Dict[str, List[List[str]]] = {}
        for raw_code, prereq_sets in (prereq_graph or {}).items():
            code = cls._extract_course_code(raw_code)
            if not code:
                continue
            branches = []
            for branch in prereq_sets or []:
                cleaned = []
                for raw_prereq in branch or []:
                    prereq_code = cls._extract_course_code(raw_prereq)
                    if prereq_code:
                        cleaned.append(prereq_code)
                if cleaned:
                    branches.append(cleaned)
            normalized[code] = branches
        return normalized

    @classmethod
    def _normalize_soft_order(
        cls,
        soft_order: Dict[str, Dict[str, float]],
    ) -> Dict[str, Dict[str, float]]:
        normalized: Dict[str, Dict[str, float]] = defaultdict(dict)
        for raw_code, predecessors in (soft_order or {}).items():
            code = cls._extract_course_code(raw_code)
            if not code:
                continue
            for raw_pred, weight in (predecessors or {}).items():
                pred = cls._extract_course_code(raw_pred)
                if pred:
                    try:
                        parsed_weight = float(weight or 0)
                    except (TypeError, ValueError):
                        parsed_weight = 0.0
                    normalized[code][pred] = max(parsed_weight, normalized[code].get(pred, 0))
        return dict(normalized)

    @staticmethod
    def _reconcile_soft_order_with_college_terms(
        soft_order: Dict[str, Dict[str, float]],
        pm_term_map: Dict[str, int],
    ) -> Dict[str, Dict[str, float]]:
        """Subordinate the inferred soft edges to the college's term assignment.

        `CourseOrderingAnalyzer` reads titles and course descriptions, which
        makes its edges guesses; `recommendedOpportunity.term.termNumber` is
        what counselors published. Where they disagree, the published plan is
        the better evidence, so:

          - a predecessor the college schedules *later* than its dependent is
            dropped outright — the heuristic is pointing backwards;
          - a predecessor the college schedules in the *same* term is demoted
            just below HIGH_CONFIDENCE_SOFT_ORDER_WEIGHT, so it can still break
            ties inside a term but can no longer act as a hard gate that forces
            the two apart.

        LATTC Welding is the case this was written for: WELDG/E 113 "Applied
        Mathematics I" sits in term 1, but its description mentions welding, so
        the skill signal made every welding course a 0.7 predecessor of it and
        the packer scheduled a first-term course dead last.

        Left alone when the college published no usable assignment — a pathway
        where every row defaults to term 1 carries no ordering to defer to, and
        those are the programs the heuristics exist for.
        """
        distinct_terms = {term for code, term in pm_term_map.items() if code in pm_term_map}
        if len(distinct_terms) < 2:
            return soft_order

        demoted_weight = HIGH_CONFIDENCE_SOFT_ORDER_WEIGHT - 0.01
        reconciled: Dict[str, Dict[str, float]] = {}
        for dependent, predecessors in (soft_order or {}).items():
            dependent_term = pm_term_map.get(dependent)
            kept: Dict[str, float] = {}
            for predecessor, weight in (predecessors or {}).items():
                predecessor_term = pm_term_map.get(predecessor)
                if dependent_term is not None and predecessor_term is not None:
                    if predecessor_term > dependent_term:
                        continue
                    if predecessor_term == dependent_term:
                        weight = min(weight, demoted_weight)
                kept[predecessor] = weight
            if kept:
                reconciled[dependent] = kept
        return reconciled

    @classmethod
    def _normalize_course_levels(cls, course_levels: Dict[str, int]) -> Dict[str, int]:
        normalized = {}
        for raw_code, tier in (course_levels or {}).items():
            code = cls._extract_course_code(raw_code)
            if code:
                normalized[code] = int(tier or 2)
        return normalized

    def _topological_sort_with_terms(
        self,
        elements: List[dict],
        prereq_graph: Dict[str, List[List[str]]],
        completed: Set[str],
        max_units: int,
        soft_order: Optional[Dict[str, Dict[str, float]]] = None,
        coreq_groups: Optional[List[Set[str]]] = None,
        course_levels: Optional[Dict[str, int]] = None,
        renumbering_aliases: Optional[Dict[str, str]] = None,
        include_diagnostics: bool = False,
        prefer_vendor_terms: bool = False,
    ) -> Tuple[List[dict], List[List[str]]] | Tuple[List[dict], List[List[str]], dict]:
        """Core sorting algorithm: topological sort respecting prerequisites, soft ordering, and unit caps."""
        soft_order = self._normalize_soft_order(soft_order or {})
        coreq_groups = coreq_groups or []
        course_levels = self._normalize_course_levels(course_levels or {})
        completed = {
            self._extract_course_code(code) or self._normalize_course_code(code)
            for code in (completed or set())
            if code
        }

        course_items, skipped_elements = self._parse_pathway_elements(elements)
        generic_items = [item for item in course_items if item['is_generic']]
        real_items = [item for item in course_items if not item['is_generic']]
        program_codes = {item['code'] for item in real_items}
        pm_term_map = {item['code']: item['pm_term'] for item in real_items}
        # The analyzer's edges are inferred from titles and descriptions; the
        # college's term assignment is published. Reconcile before either the
        # hard-gate set or the candidate ranking reads soft_order.
        soft_order = self._reconcile_soft_order_with_college_terms(soft_order, pm_term_map)
        # Generic "X or Y" rows expose alternative codes that ARE covered by
        # the program (because the student picks one) but are not themselves
        # scheduled real items. Tracked separately so they can satisfy the
        # external-prereq check without polluting the scheduler's program set.
        placeholder_alt_codes: Set[str] = set()
        for item in generic_items:
            placeholder_alt_codes.update(item.get('alt_codes') or [])
        placeholder_alt_codes -= program_codes
        # Renumbering aliases: external prereqs that the walker mapped to an
        # in-program code. Treated like placeholder alt codes — covered for
        # the satisfaction check, but never scheduled as a real item.
        renumbering_aliases = {
            src: tgt for src, tgt in (renumbering_aliases or {}).items()
            if src not in program_codes and tgt in program_codes
        }
        renumbering_alias_codes: Set[str] = set(renumbering_aliases.keys())

        # Build corequisite lookup: code -> full group of corequisite codes
        coreq_lookup: Dict[str, Set[str]] = {}
        for group in coreq_groups:
            normalized_group = {
                code
                for code in (self._extract_course_code(raw_code) for raw_code in group)
                if code in program_codes
            }
            if len(normalized_group) >= 2:
                for code in normalized_group:
                    coreq_lookup[code] = normalized_group

        normalized_prereq_graph = self._normalize_prereq_graph(prereq_graph)

        # Same course, different space placement: Program Mapper writes
        # "PSYC C1000" while the prereq graph writes "PSYCC 1000". Matching
        # them literally reports an in-program course as an external prereq,
        # which excuses it from the ordering gate entirely. The renumbering
        # walker was meant to cover this, but it needs Neo4j and returns
        # nothing when that lookup is unavailable — so resolve it here too.
        # sorted(): two program codes can squash to the same key, and the last
        # one written wins. Iterating the set would let the hash seed pick the
        # winner, so the alias a student is shown would change per worker.
        squash_index = {self._squash_code(code): code for code in sorted(program_codes)}
        for prereq_sets in normalized_prereq_graph.values():
            for branch in prereq_sets or []:
                for prereq in branch or []:
                    if prereq in program_codes or prereq in renumbering_aliases:
                        continue
                    target = squash_index.get(self._squash_code(prereq))
                    if target and target != prereq:
                        renumbering_aliases[prereq] = target
        renumbering_alias_codes = set(renumbering_aliases.keys())

        # program_codes is a set, so every loop below walks it in sorted order.
        # Key order here is not cosmetic: it seeds the cycle-breaking DFS, and
        # it is the order external_prerequisites is returned and rendered in.
        effective_prereqs: Dict[str, Set[str]] = {code: set() for code in sorted(program_codes)}
        external_prerequisites: Dict[str, List[str]] = {}

        # A prereq is "covered" if it's a real program course, already
        # completed, named inside a generic "X or Y" placeholder row, or a
        # renumbering alias of a program course (e.g. STAT C1000 ↔ STATC 1000).
        covered_codes = program_codes | completed | placeholder_alt_codes | renumbering_alias_codes

        for code in sorted(program_codes):
            prereq_sets = normalized_prereq_graph.get(code, [])
            if not prereq_sets:
                continue

            def branch_score(branch: List[str]) -> Tuple[int, int, int]:
                external_count = sum(1 for prereq in branch if prereq not in covered_codes)
                program_count = sum(1 for prereq in branch if prereq in program_codes)
                return (external_count, -program_count, len(branch))

            chosen = min(prereq_sets, key=branch_score)
            # Drop placeholder-aliased and renumbering-aliased codes from
            # effective_prereqs — they have no scheduled term and would
            # otherwise look like unfulfilled "phantom" prereqs in the
            # per-course prerequisites list. The renumbering alias's
            # canonical target IS already in program_codes, which is what
            # gates ordering.
            chosen_set = set(chosen)
            for src, tgt in renumbering_aliases.items():
                if src in chosen_set:
                    chosen_set.discard(src)
                    chosen_set.add(tgt)
            effective_prereqs[code] = chosen_set - placeholder_alt_codes - renumbering_alias_codes
            external = sorted(
                prereq for prereq in chosen
                if prereq not in covered_codes
            )
            if external:
                external_prerequisites[code] = external

        # Detect cycles and break them
        effective_prereqs = self._break_cycles(effective_prereqs, pm_term_map)

        dependents_map = defaultdict(set)
        for code, prereqs in effective_prereqs.items():
            for prereq_code in prereqs:
                if prereq_code in program_codes:
                    dependents_map[prereq_code].add(code)

        high_conf_soft: Dict[str, Set[str]] = {}
        for code in sorted(program_codes):
            high_conf_soft[code] = {
                pred for pred, weight in soft_order.get(code, {}).items()
                if pred in program_codes and weight >= HIGH_CONFIDENCE_SOFT_ORDER_WEIGHT
            }

        # Track state
        scheduled = set(completed)
        remaining = {item['code']: item for item in real_items if item['code'] not in completed}

        # Reframe external-prereq warnings by classifying each prereq. CCN/
        # district-renaming aliases are noted as alternate names. Sub-transfer
        # math/English are reframed as expected pre-program prep rather than
        # alarming missing-course warnings. Anything else stays as a generic
        # outside-of-program note.
        from app.services.external_prereq_telemetry import (
            classify as _classify_external,
            CAT_RENUMBERING, CAT_ASSUMED_PREP, CAT_PLACEHOLDER_MISMATCH,
        )

        prereq_alternatives: Dict[str, List[str]] = {}
        for code in sorted(external_prerequisites.keys()):
            branches = normalized_prereq_graph.get(code) or []
            prereq_alternatives[code] = sorted({p for branch in branches for p in branch})

        warnings = []
        for code, prereqs in sorted(external_prerequisites.items()):
            alts = prereq_alternatives.get(code, list(prereqs))
            categories = {_classify_external(p, program_codes, alts) for p in prereqs}
            if categories <= {CAT_RENUMBERING, CAT_PLACEHOLDER_MISMATCH}:
                # All external prereqs here are likely an alternate-name or
                # alternation issue rather than a real gap.
                warnings.append(
                    f"{code} prerequisite(s) ({', '.join(prereqs)}) appear under different names "
                    f"in the catalog; verify with a counselor that your completed coursework matches."
                )
            elif categories == {CAT_ASSUMED_PREP}:
                warnings.append(
                    f"{code} expects {', '.join(prereqs)} completed before starting this program "
                    f"(check your placement or transcript)."
                )
            else:
                warnings.append(
                    f"{code} requires {', '.join(prereqs)}, which is outside this program sequence."
                )

        terms = []
        term_number = 0
        max_iterations = 50

        def build_course_output(item: dict, scheduled_before_term: Set[str]) -> dict:
            code = item['code']
            prereqs_for_course = sorted(effective_prereqs.get(code, set()))
            prereqs_met = all(prereq in scheduled_before_term or prereq in completed for prereq in prereqs_for_course)
            return {
                "code": code,
                "title": item['title'],
                "units": item['units'],
                "requirement_type": item['requirement_type'],
                "prerequisites": prereqs_for_course,
                "prerequisites_met": prereqs_met,
                "status": "not_started",
                "is_gateway": is_gateway_course(code) and not item.get('is_generic', False),
            }

        def build_term(term_no: int, term_courses: List[dict], term_units: float, scheduled_before_term: Set[str]) -> dict:
            return {
                "term_number": term_no,
                "term_label": self._format_term_label(term_no),
                "season": self._format_term_season(term_no),
                "courses": [build_course_output(item, scheduled_before_term) for item in term_courses],
                "total_units": term_units,
            }

        def group_for(code: str, remaining_codes: Set[str]) -> Set[str]:
            return set(coreq_lookup.get(code, {code})) & remaining_codes

        def group_is_ready(
            group_codes: Set[str],
            current_term: int,
            scheduled_before_term: Set[str],
            relax_soft: bool = False,
            ignore_hard: bool = False,
        ) -> bool:
            for code in group_codes:
                item = remaining[code]
                if item['pm_term'] > current_term:
                    return False
                hard_prereqs = effective_prereqs.get(code, set()) & program_codes
                if not ignore_hard and not hard_prereqs.issubset(scheduled_before_term):
                    return False
                soft_prereqs = high_conf_soft.get(code, set()) - group_codes
                if not relax_soft and not soft_prereqs.issubset(scheduled_before_term):
                    return False
            return True

        def build_candidates(
            current_term: int,
            scheduled_before_term: Set[str],
            relax_soft: bool = False,
            ignore_hard: bool = False,
        ) -> List[Tuple[List[dict], int]]:
            remaining_codes = set(remaining.keys())
            seen_groups = set()
            candidates = []
            for code in sorted(remaining_codes):
                group_codes = group_for(code, remaining_codes)
                key = frozenset(group_codes)
                if key in seen_groups:
                    continue
                seen_groups.add(key)
                if not group_is_ready(group_codes, current_term, scheduled_before_term, relax_soft, ignore_hard):
                    continue
                group_items = sorted((remaining[c] for c in group_codes), key=lambda item: (item['pm_term'], item['code']))
                unlock_count = sum(len(dependents_map.get(c, set()) & remaining_codes) for c in group_codes)
                candidates.append((group_items, unlock_count))
            return candidates

        def candidate_sort_key(candidate: Tuple[List[dict], int]):
            group_items, unlock_count = candidate
            group_codes = {item['code'] for item in group_items}
            remaining_codes = set(remaining.keys())
            is_capstone = 1 if max(course_levels.get(code, 2) for code in group_codes) >= 4 else 0
            is_major = 0 if any('Major' in item['requirement_type'] for item in group_items) else 1
            pm_term = min(item['pm_term'] for item in group_items)
            unmet_soft_score = 0.0
            for code in group_codes:
                for predecessor, weight in soft_order.get(code, {}).items():
                    if predecessor in remaining_codes and predecessor not in group_codes:
                        unmet_soft_score += weight
            level_tier = min(course_levels.get(code, 2) for code in group_codes)
            # pm_term leads. is_capstone and is_major are title-keyword guesses,
            # and ranking them first let those guesses override the college's
            # published order: "Multimedia Laboratory" and "Introduction To
            # Experimental Animation" both match a tier-4 keyword, so two
            # first-term courses sorted behind everything else.
            # Within one college term the heuristics still break the tie, which
            # is the job they are actually good at.
            return (pm_term, is_capstone, is_major, unmet_soft_score, level_tier, -unlock_count)

        def build_vendor_terms() -> Optional[List[dict]]:
            """Group by the college's own termNumber, or None if unusable.

            The vendor's term assignment is prerequisite-ordered by counselors
            who know the catalog, and it covers generic rows ("STATISTICS")
            that the scheduler cannot reason about by course code. Re-deriving
            the grouping from unit caps is what produced plans longer than the
            official one, and prerequisite-invalid ones.

            Returns None — falling back to unit-cap packing — whenever the
            vendor data cannot be trusted to stand on its own.
            """
            grouped: Dict[int, List[dict]] = defaultdict(list)
            for item in course_items:
                if item['code'] in completed:
                    continue
                grouped[item['pm_term']].append(item)
            if not grouped:
                return None

            ordered = sorted(grouped)
            # A single bucket is not a term assignment — it means the pathway
            # carried no usable termNumber and every row defaulted together.
            # Honouring it would drop the whole program into one term.
            if len(ordered) < 2:
                return None
            # Only honour a clean 1..n numbering. A gap is not a real
            # assignment either, and renumbering around one would silently
            # move courses earlier than the college recommends.
            if ordered != list(range(1, len(ordered) + 1)):
                return None

            placement = {
                item['code']: term_no
                for term_no, items in grouped.items()
                for item in items
                if not item.get('is_generic')
            }
            # The college's own plan must satisfy the prerequisites we know
            # about. If it does not, the data is inconsistent and packing —
            # which enforces them — is the safer answer.
            for code, term_no in sorted(placement.items()):
                for prereq in sorted(effective_prereqs.get(code, set())):
                    if placement.get(prereq, -1) >= term_no:
                        logger.warning(
                            "Vendor term assignment violates prereq %s -> %s; "
                            "falling back to unit-cap packing",
                            prereq, code,
                        )
                        return None
            for group in sorted(coreq_lookup.values(), key=sorted):
                if len({placement[c] for c in group if c in placement}) > 1:
                    logger.warning(
                        "Vendor term assignment splits corequisite group %s; "
                        "falling back to unit-cap packing", sorted(group),
                    )
                    return None

            out: List[dict] = []
            scheduled_before: Set[str] = set(completed)
            for term_no in ordered:
                items = grouped[term_no]
                out.append(build_term(
                    term_no, items,
                    sum(item['units'] for item in items),
                    set(scheduled_before),
                ))
                scheduled_before |= {item['code'] for item in items}
            return out

        vendor_terms = build_vendor_terms() if prefer_vendor_terms else None
        if vendor_terms is not None:
            # The vendor grouping already places every row, generic ones
            # included, so both the packing loop and the leftover-capacity
            # pass below have nothing left to do.
            terms = vendor_terms
            term_source = "vendor"
            remaining = {}
            generic_items = []
        else:
            term_source = "packed"

        while remaining and term_number < max_iterations:
            term_number += 1
            scheduled_before_term = set(scheduled)

            ready = build_candidates(term_number, scheduled_before_term)

            if not ready:
                ready = build_candidates(term_number, scheduled_before_term, relax_soft=True)

            if not ready:
                blocked_current = [item for item in remaining.values() if item['pm_term'] <= term_number]
                if not blocked_current and any(item['pm_term'] > term_number for item in remaining.values()):
                    continue
                logger.warning(
                    f"Term {term_number}: {len(remaining)} courses have unmet prerequisites, "
                    "scheduling ready courses with relaxed prerequisite checks"
                )
                warnings.append("Some courses were placed with unmet prerequisite data; verify with a counselor.")
                ready = build_candidates(term_number, scheduled_before_term, relax_soft=True, ignore_hard=True)
                if not ready:
                    break

            ready.sort(key=candidate_sort_key)

            # Fill term up to max_units
            term_courses = []
            term_units = 0.0
            codes_added_this_term = set()

            for group_items, _ in ready:
                group_codes = {item['code'] for item in group_items}
                if any(code in scheduled or code not in remaining for code in group_codes):
                    continue
                group_units = sum(item['units'] for item in group_items)
                if term_units + group_units > max_units and term_courses:
                    continue
                term_courses.extend(group_items)
                term_units += group_units
                codes_added_this_term |= group_codes

            if not term_courses:
                break

            scheduled |= codes_added_this_term
            for item in term_courses:
                remaining.pop(item['code'], None)

            terms.append(build_term(term_number, term_courses, term_units, scheduled_before_term))

        # Distribute generic items across terms at or after their PM-recommended
        # term, and — where the row is what satisfies a course's prerequisite —
        # strictly before that course.
        #
        # A generic row carries no course code, so the scheduler above could not
        # see that "STATISTICS" is what satisfies PSYCH 074's STAT C1000. Placed
        # purely by leftover capacity, it landed in the same term as the course
        # it unlocks, and the plan then stated the rule and broke it.
        real_placement = {
            course['code']: term['term_number']
            for term in terms for course in term['courses']
        }
        generic_deadline: Dict[str, int] = {}
        for dependent, ext_prereqs in external_prerequisites.items():
            dependent_term = real_placement.get(dependent)
            if dependent_term is None:
                continue
            for gen_item in generic_items:
                if not any(
                    self._generic_row_satisfies(gen_item['code'], prereq)
                    for prereq in ext_prereqs
                ):
                    continue
                code = gen_item['code']
                # Strictly before, and before the earliest thing it unlocks.
                deadline = dependent_term - 1
                generic_deadline[code] = min(generic_deadline.get(code, deadline), deadline)

        def make_generic_output(item: dict) -> dict:
            return {
                "code": item['code'],
                "title": item['title'],
                "units": item['units'],
                "requirement_type": "Elective",
                "prerequisites": [],
                "prerequisites_met": True,
                "status": "not_started",
                "is_gateway": False,
            }

        for gen_item in generic_items:
            if gen_item['code'] in completed:
                continue
            deadline = generic_deadline.get(gen_item['code'])
            placed = False
            for term in sorted(terms, key=lambda t: t['term_number']):
                if term['term_number'] < gen_item['pm_term']:
                    continue
                if deadline is not None and term['term_number'] > deadline:
                    break
                if term['total_units'] + gen_item['units'] <= max_units:
                    term['courses'].append(make_generic_output(gen_item))
                    term['total_units'] += gen_item['units']
                    placed = True
                    break
            if not placed and deadline is not None:
                # Nothing inside the window had room. Overfilling a term would
                # defeat the whole point of a part-time pace, and placing the
                # row on top of the course it unlocks is the bug itself — so
                # open a term for it and shift the rest of the plan back.
                insert_at = deadline + 1
                for term in terms:
                    if term['term_number'] >= insert_at:
                        term['term_number'] += 1
                        term['term_label'] = self._format_term_label(term['term_number'])
                        term['season'] = self._format_term_season(term['term_number'])
                terms.append({
                    "term_number": insert_at,
                    "term_label": self._format_term_label(insert_at),
                    "season": self._format_term_season(insert_at),
                    "courses": [make_generic_output(gen_item)],
                    "total_units": gen_item['units'],
                })
                # Deadlines and placements were computed against the old
                # numbering; move the ones after the insertion point with it.
                for other, other_deadline in list(generic_deadline.items()):
                    if other_deadline >= insert_at:
                        generic_deadline[other] = other_deadline + 1
                for other, other_term in list(real_placement.items()):
                    if other_term >= insert_at:
                        real_placement[other] = other_term + 1
                placed = True
            if not placed and gen_item['units'] > 0:
                next_term = max([term['term_number'] for term in terms] + [0]) + 1
                target_term = max(gen_item['pm_term'], next_term)
                terms.append({
                    "term_number": target_term,
                    "term_label": self._format_term_label(target_term),
                    "season": self._format_term_season(target_term),
                    "courses": [{
                        "code": gen_item['code'],
                        "title": gen_item['title'],
                        "units": gen_item['units'],
                        "requirement_type": "Elective",
                        "prerequisites": [],
                        "prerequisites_met": True,
                        "status": "not_started",
                        "is_gateway": False,
                    }],
                    "total_units": gen_item['units'],
                })

        terms.sort(key=lambda term: term['term_number'])

        # Renumber contiguously. The packing loop advances its counter while
        # waiting for a pm_term floor and appends no term for it, so the labels
        # came out as "Semester 1, 3, 4, 5" — a hole a student cannot interpret
        #. Numbering is a display concern; the ordering the packer
        # decided is preserved exactly, since this only closes gaps.
        for position, term in enumerate(terms, start=1):
            if term['term_number'] != position:
                term['term_number'] = position
                term['term_label'] = self._format_term_label(position)
                term['season'] = self._format_term_season(position)

        # Extract prerequisite chains
        prereq_chains = self._extract_prereq_chains(effective_prereqs)

        diagnostics = {
            "warnings": warnings,
            "external_prerequisites": external_prerequisites,
            "skipped_elements": skipped_elements,
            "program_codes_snapshot": program_codes,
            "prereq_alternatives": prereq_alternatives,
            "renumbering_resolutions": renumbering_aliases,
            "term_source": term_source,
        }
        if include_diagnostics:
            return terms, prereq_chains, diagnostics
        return terms, prereq_chains

    def _break_cycles(
        self,
        effective_prereqs: Dict[str, Set[str]],
        pm_term_map: Dict[str, int],
    ) -> Dict[str, Set[str]]:
        """Detect and break cycles in the prerequisite graph.

        Which edge of a cycle gets dropped decides which of the two courses a
        student is told to take first, so the walk has to be reproducible.
        NURSING 090 and 091 each list the other as a prerequisite; iterating
        the prereq sets unsorted let the hash seed pick the winner, and the
        LAMC Vocational Nursing plan swapped its first two terms between
        worker processes.
        """
        visited = set()
        rec_stack = set()
        cycle_edges = []

        def dfs(node, path):
            visited.add(node)
            rec_stack.add(node)
            for prereq in sorted(effective_prereqs.get(node, set())):
                if prereq in rec_stack:
                    cycle_edges.append((node, prereq))
                elif prereq not in visited:
                    dfs(prereq, path + [prereq])
            rec_stack.discard(node)

        for code in sorted(effective_prereqs.keys()):
            if code not in visited:
                dfs(code, [code])

        for dependent, prereq_code in cycle_edges:
            dep_term = pm_term_map.get(dependent, 99)
            prereq_term = pm_term_map.get(prereq_code, 99)
            if dep_term >= prereq_term:
                effective_prereqs.get(dependent, set()).discard(prereq_code)
                logger.warning(
                    f"Broke prerequisite cycle: removed {prereq_code} as prereq of {dependent}"
                )
            else:
                effective_prereqs.get(prereq_code, set()).discard(dependent)
                logger.warning(
                    f"Broke prerequisite cycle: removed {dependent} as prereq of {prereq_code}"
                )

        return effective_prereqs

    def _extract_prereq_chains(self, effective_prereqs: Dict[str, Set[str]]) -> List[List[str]]:
        """Extract the longest prerequisite chains from the graph."""
        all_codes = set(effective_prereqs.keys())
        has_dependent = set()
        for prereqs in effective_prereqs.values():
            has_dependent |= prereqs

        leaves = all_codes - has_dependent
        if not leaves:
            leaves = all_codes

        chains = []
        for leaf in sorted(leaves):
            chain = self._trace_chain_back(leaf, effective_prereqs)
            if len(chain) > 1:
                chains.append(chain)

        # Deduplicate
        chains.sort(key=len, reverse=True)
        unique_chains = []
        for chain in chains:
            chain_set = set(chain)
            if not any(chain_set.issubset(set(existing)) for existing in unique_chains):
                unique_chains.append(chain)

        return unique_chains

    def _trace_chain_back(
        self, code: str, effective_prereqs: Dict[str, Set[str]], visited: Optional[Set[str]] = None
    ) -> List[str]:
        """Trace the longest chain ending at this course."""
        if visited is None:
            visited = set()
        if code in visited:
            return [code]
        visited.add(code)

        prereqs = effective_prereqs.get(code, set())
        if not prereqs:
            return [code]

        # sorted(): ties on length are broken by iteration order, and these
        # chains are printed to the student. PSYCH 074 has two equal-length
        # candidates and alternated between naming STATC 1000 and PSYC C1000.
        longest = []
        for prereq in sorted(prereqs):
            sub_chain = self._trace_chain_back(prereq, effective_prereqs, visited)
            if len(sub_chain) > len(longest):
                longest = sub_chain

        return longest + [code]

    @staticmethod
    def _is_valid_course_code(code: str) -> bool:
        """Check if a string looks like a valid course code."""
        if not code:
            return False
        normalized = CourseSequenceService._normalize_course_code(code)
        return bool(COURSE_CODE_RE.match(normalized))


def format_sequence_for_context(data: dict) -> str:
    """Format sequence data for Gemini context injection."""
    lines = []
    lines.append(f"COURSE SEQUENCE PLAN: {data.get('program_name', 'Unknown')}")
    award = data.get('award_type', '')
    if award:
        lines.append(f"Award: {award}")
    lines.append(f"Total Units: {data.get('total_units', 0)}")
    lines.append(f"Terms to Complete: {data.get('terms_to_complete', 0)}")
    lines.append("")

    for term in data.get('sequence', []):
        lines.append(f"{term['term_label']}:")
        for course in term.get('courses', []):
            prereq_info = ""
            if course.get('prerequisites'):
                prereq_info = f" (prereqs: {', '.join(course['prerequisites'])})"
            lines.append(
                f"  - {course['code']}: {course['title']} "
                f"({course['units']} units) - {course['requirement_type']}"
                f"{prereq_info}"
            )
        lines.append(f"  Term units: {term['total_units']}")
        lines.append("")

    chains = data.get('prerequisite_chains', [])
    if chains:
        lines.append("PREREQUISITE CHAINS:")
        for chain in chains:
            lines.append(f"  {' -> '.join(chain)}")
        lines.append("")

    notes = data.get('notes', [])
    if notes:
        lines.append("NOTES:")
        for note in notes:
            lines.append(f"  - {note}")
        lines.append("")

    warnings = data.get('warnings', [])
    if warnings:
        lines.append("WARNINGS:")
        for warning in warnings:
            lines.append(f"  - {warning}")

    return "\n".join(lines)


# Per-campus singleton instances
_services: Dict[str, CourseSequenceService] = {}


def get_course_sequence_service(campus_code: str = "LAMC") -> CourseSequenceService:
    """Get CourseSequenceService singleton for a specific campus."""
    campus_code = campus_code.upper()
    if campus_code not in _services:
        _services[campus_code] = CourseSequenceService(campus_code)
    return _services[campus_code]
