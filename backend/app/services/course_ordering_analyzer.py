"""
Course Ordering Analyzer

Produces soft ordering constraints from multiple signals beyond hard prerequisites.
Used by CourseSequenceService to generate better course sequences when eLumen
prerequisite data is sparse.

Signals (in priority order):
1. eLumen advisories (weight 0.85) — faculty-authored recommended preparation
2. Description-based skill/tool dependencies (weight 0.7) — cross-references course
   titles (what a course teaches) against descriptions (what a course uses)
3. Title keyword level classification (weight 0.6) — intro before advanced, capstone last
4. Course number ordering (weight 0.5) — lower numbers before higher within same prefix

Every signal is a pure function of the course data. There was a fifth signal
that asked an LLM for implicit ordering; it is gone, because it sampled a
different answer every call and the plan changed with it.
"""

import logging
import re
from typing import Dict, List, Set, Tuple, Optional
from collections import defaultdict

logger = logging.getLogger(__name__)

# Course code regex for extracting prefix and number
COURSE_CODE_RE = re.compile(r'^([A-Z]+(?:\s+[A-Z]+)*)\s+(\d+)([A-Z]*)$')

# ═════════════════════════════════════════════════════════════
# Known same-hundred course sequences
# ═════════════════════════════════════════════════════════════

SAME_HUNDRED_SEQUENCES = {
    "NURSING": [("121", "122"), ("221", "222"), ("223", "224")],
    "ACCOUNTING": [("121", "122")],
    "SPANISH": [("101", "102"), ("201", "202"), ("203", "204")],
    "FRENCH": [("101", "102"), ("201", "202"), ("203", "204")],
    "GERMAN": [("101", "102"), ("201", "202"), ("203", "204")],
    "ITALIAN": [("101", "102"), ("201", "202")],
    "CHINESE": [("101", "102"), ("201", "202")],
    "JAPANESE": [("101", "102"), ("201", "202")],
    "KOREAN": [("101", "102"), ("201", "202")],
    "ARABIC": [("101", "102"), ("201", "202")],
    "AMERIC SIGN LANGUAGE": [("101", "102"), ("201", "202")],
    "PHILOSOPHY": [("101", "102"), ("201", "202")],
}

# ═══════════════════════════════════════════════════════════════
# Signal 3: Title keyword tier classification
# ═══════════════════════════════════════════════════════════════

TIER_1_KEYWORDS = [
    "introduction", "intro to", "beginning", "fundamental", "principles of",
    "survey of", "elements of", "basics of", "essentials of", "foundations",
    "human anatomy", "human physiology",
    "financial", "managerial",
    "elementary",
    "preparatory",
    "principles of",
]

TIER_3_KEYWORDS = [
    "intermediate", "advanced techniques",
]

TIER_4_KEYWORDS = [
    "laboratory", "lab", "workshop", "capstone", "portfolio",
    "experimental", "practicum", "internship", "field experience",
    "seminar",
    "clinical",
    "internship", "cooperative", "field",
    "thesis", "dissertation",
    "senior project", "senior seminar",
    "practicum", "fieldwork",
]

# ═══════════════════════════════════════════════════════════════
# Signal 4: Skill/tool keyword dictionary
# ═══════════════════════════════════════════════════════════════

SKILL_KEYWORDS = {
    # === CREATIVE / MULTIMEDIA ===
    "photoshop": ["photoshop", "photo editing", "image editing", "digital imaging", "raster"],
    "illustrator": ["illustrator", "vector graphics", "vector illustration", "vector art"],
    "after effects": ["after effects", "motion graphics", "motion design", "compositing"],
    "maya": ["maya", "3d modeling", "3d animation", "3d design", "3d rendering"],
    "indesign": ["indesign", "page layout", "desktop publishing"],
    "premiere": ["premiere", "video editing software", "nonlinear editing"],
    "drawing": ["drawing", "sketch", "illustration technique", "figure drawing", "life drawing"],
    "photography": ["photography", "camera", "photographic", "photo composition", "lighting techniques"],
    "video production": ["video production", "filming", "videography", "cinematography", "camera operation"],
    "audio production": ["audio production", "sound design", "audio editing", "recording", "digital audio"],
    "animation principles": ["animation principles", "timing and spacing", "keyframe", "character animation"],
    "storyboarding": ["storyboard", "visual narrative", "shot composition", "scene planning"],
    "typography": ["typography", "typeface", "font", "letterform", "type design"],
    "color theory": ["color theory", "color wheel", "color palette", "color harmony"],

    # === BUSINESS / ACCOUNTING ===
    "excel": ["excel", "spreadsheet", "pivot table", "vlookup", "spreadsheet software", "microsoft excel"],
    "quickbooks": ["quickbooks", "quick books", "intuit", "accounting software"],
    "financial analysis": ["financial analysis", "financial statement analysis", "ratio analysis", "financial reporting"],
    "managerial accounting": ["managerial", "cost accounting", "budgeting", "performance evaluation", "internal accounting"],
    "auditing": ["auditing", "audit", "internal control", "compliance", "audit report"],
    "taxation": ["tax", "taxation", "irs", "tax code", "tax return", "tax preparation"],
    "marketing principles": ["marketing mix", "4p", "promotion strategy", "market research", "marketing strategy"],
    "business communication": ["business writing", "business communication", "professional communication", "business correspondence"],
    "accounting principles": ["accounting cycle", "debit credit", "financial accounting", "gaap", "accounting principles"],

    # === NURSING / ALLIED HEALTH ===
    "clinical skills": ["clinical", "patient care", "clinical practice", "bedside", "clinical skills"],
    "medical terminology": ["medical terminology", "medical language", "anatomical terminology", "medical terms"],
    "pharmacology": ["pharmacology", "medication", "drug", "dosage", "administration", "drug therapy"],
    "anatomy": ["anatomy", "anatomical", "body structure", "human body", "anatomical structure"],
    "physiology": ["physiology", "physiological", "body function", "biological process", "body systems"],
    "patient assessment": ["assessment", "patient assessment", "health assessment", "nursing assessment", "physical assessment"],
    "medical surgical": ["med surg", "medical surgical", "acute care", "med-surg"],
    "nursing fundamentals": ["nursing fundamentals", "nursing process", "nursing concepts", "basic nursing"],
    "pediatrics": ["pediatric", "pediatrics", "child health", "infant care"],
    "obstetrics": ["obstetrics", "ob", "maternal", "prenatal", "labor and delivery"],

    # === SCIENCES ===
    "lab techniques": ["lab techniques", "laboratory methods", "wet lab", "bench work", "lab skills"],
    "research methods": ["research methods", "scientific method", "experimental design", "research methodology"],
    "statistical analysis": ["statistics", "statistical analysis", "data analysis", "hypothesis testing", "statistical methods"],
    "organic chemistry": ["organic", "carbon compounds", "organic mechanisms", "organic synthesis"],
    "calculus": ["calculus", "derivative", "integral", "limit", "differentiation", "integration"],
    "algebra": ["algebra", "equation solving", "polynomial", "factoring", "algebraic expressions"],
    "microbiology": ["microbiology", "microbe", "bacteria", "virus", "fungi", "microorganism"],
    "physics": ["mechanics", "thermodynamics", "electromagnetism", "waves", "optics", "physics principles"],
    "chemistry": ["stoichiometry", "chemical bonding", "periodic table", "chemical reaction", "molecular"],
    "biology": ["cell biology", "genetics", "evolution", "ecology", "organism", "biological"],

    # === COMPUTER SCIENCE / PROGRAMMING ===
    "python": ["python", "python programming", "python code", "python script"],
    "java": ["java", "object oriented programming", "oop", "java programming"],
    "database": ["database", "sql", "query", "database design", "normalization", "relational database"],
    "data structures": ["data structures", "algorithms", "complexity", "linked list", "tree", "graph"],
    "web development": ["html", "css", "javascript", "frontend", "full stack", "web application"],
    "c++": ["c++", "cpp", "c plus plus", "memory management", "pointers"],
    "software engineering": ["software design", "software architecture", "design patterns", "software lifecycle"],
    "networking": ["network", "tcp/ip", "protocols", "network security", "routing"],
    "cybersecurity": ["cybersecurity", "information security", "network security", "encryption", "security"],
    "operating systems": ["operating system", "os", "process management", "memory management", "file systems"],

    # === SOCIAL SCIENCES ===
    "research methods social": ["research methods", "qualitative", "quantitative", "survey", "social research"],
    "statistical analysis social": ["spss", "statistical package", "data analysis social", "correlation social"],
    "psychology principles": ["psychological theory", "behavior", "cognitive", "developmental psychology"],
    "sociology concepts": ["social theory", "social structure", "social institution", "social inequality"],
    "criminal justice": ["criminal law", "criminology", "law enforcement", "corrections", "justice system"],

    # === HUMANITIES ===
    "academic writing": ["academic writing", "essay", "thesis", "research paper", "expository writing"],
    "critical reading": ["critical reading", "textual analysis", "close reading", "literary analysis"],
    "composition": ["composition", "rhetoric", "argument", "academic writing", "essay writing"],

    # === TRADES / CTE ===
    "automotive": ["engine repair", "brake systems", "automotive service", "car repair", "mechanic"],
    "welding": ["welding", "soldering", "fabrication", "metal joining", "arc welding"],
    "culinary": ["cooking", "culinary arts", "food preparation", "baking", "kitchen skills"],
    "electrical": ["electrical circuits", "wiring", "electrical code", "electrical systems", "electrical theory"],
    "plumbing": ["plumbing", "pipe fitting", "drainage", "water systems", "plumbing systems"],
    "hvAC": ["hvAC", "heating", "ventilation", "air conditioning", "climate control"],

    # === GENERAL SKILLS ===
    "html css": ["html", "css", "web design", "website", "web development", "web page"],
    "javascript": ["javascript", "js", "scripting", "interactive web"],
    "programming": ["programming", "coding", "software development", "algorithm"],
    "scriptwriting": ["script", "screenplay", "story structure", "narrative writing", "screenwriting"],
    "visual communication": ["visual communication", "graphic communication", "design principles"],
    "graphic design": ["graphic design", "layout", "composition", "branding", "visual identity"],
}


class CourseOrderingAnalyzer:
    """Analyzes courses to produce soft ordering constraints from multiple signals."""

    def analyze(
        self,
        course_codes: List[str],
        advisories: Dict[str, List[List[str]]],
        corequisites: Dict[str, List[List[str]]],
        course_details: Dict[str, dict],
        pm_titles: Optional[Dict[str, str]] = None,
    ) -> Tuple[Dict[str, Dict[str, float]], List[Set[str]], Dict[str, int]]:
        """Run all synchronous ordering signals and merge results.

        Returns:
            Tuple of:
                soft_order: {course_B: {course_A: weight}} meaning A should come before B
                coreq_groups: list of sets of course codes that should be co-scheduled
                course_levels: {code: tier} where tier 1=foundational, 4=capstone
        """
        pm_titles = pm_titles or {}
        code_set = set(course_codes)

        # Build a unified title map (eLumen preferred, PM fallback)
        titles = {}
        for code in course_codes:
            if code in course_details and course_details[code].get("title"):
                titles[code] = course_details[code]["title"]
            elif code in pm_titles:
                titles[code] = pm_titles[code]
            else:
                titles[code] = ""

        # Run each signal
        soft_order: Dict[str, Dict[str, float]] = defaultdict(dict)

        self._signal_advisories(course_codes, advisories, code_set, soft_order)
        self._signal_course_numbers(course_codes, soft_order)
        course_levels = self._signal_title_keywords(course_codes, titles, soft_order)
        self._signal_description_skills(course_codes, titles, course_details, code_set, soft_order)

        # Build corequisite groups
        coreq_groups = self._build_coreq_groups(course_codes, corequisites, code_set)

        # Filter noise: threshold at 0.3
        filtered_order: Dict[str, Dict[str, float]] = {}
        for course_b, predecessors in soft_order.items():
            filtered = {a: w for a, w in predecessors.items() if w >= 0.3}
            if filtered:
                filtered_order[course_b] = filtered

        logger.info(
            f"Course ordering analysis: {len(filtered_order)} courses with soft predecessors, "
            f"{len(coreq_groups)} corequisite groups"
        )
        return filtered_order, coreq_groups, course_levels

    def _signal_advisories(
        self,
        course_codes: List[str],
        advisories: Dict[str, List[List[str]]],
        code_set: Set[str],
        soft_order: Dict[str, Dict[str, float]],
    ):
        """Signal 1: eLumen advisories (weight 0.85)."""
        weight = 0.85
        count = 0
        for code in course_codes:
            for advisory_set in advisories.get(code, []):
                for advisory_code in advisory_set:
                    if advisory_code in code_set:
                        existing = soft_order[code].get(advisory_code, 0)
                        soft_order[code][advisory_code] = max(existing, weight)
                        count += 1
        if count:
            logger.debug(f"Signal: advisories — {count} edges at weight {weight}")

    def _signal_course_numbers(
        self,
        course_codes: List[str],
        soft_order: Dict[str, Dict[str, float]],
    ):
        """Signal 2: Course number ordering (weight 0.3-0.5)."""
        by_prefix: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
        for code in course_codes:
            m = COURSE_CODE_RE.match(code)
            if m:
                prefix = m.group(1)
                number = int(m.group(2))
                by_prefix[prefix].append((code, number))

        count = 0
        for prefix, courses in by_prefix.items():
            courses.sort(key=lambda x: x[1])
            for i, (code_a, num_a) in enumerate(courses):
                for code_b, num_b in courses[i + 1:]:
                    if num_a == num_b:
                        continue

                    num_a_str = str(num_a)
                    num_b_str = str(num_b)

                    is_known_sequence = False
                    if prefix in SAME_HUNDRED_SEQUENCES:
                        for seq_start, seq_end in SAME_HUNDRED_SEQUENCES[prefix]:
                            if num_a_str == seq_start and num_b_str == seq_end:
                                is_known_sequence = True
                                break

                    hundreds_a = num_a // 100
                    hundreds_b = num_b // 100
                    if hundreds_a != hundreds_b:
                        weight = 0.5
                    elif is_known_sequence:
                        weight = 0.4
                    else:
                        continue
                    existing = soft_order[code_b].get(code_a, 0)
                    soft_order[code_b][code_a] = max(existing, weight)
                    count += 1
        if count:
            logger.debug(f"Signal: course numbers — {count} edges")

    def _signal_title_keywords(
        self,
        course_codes: List[str],
        titles: Dict[str, str],
        soft_order: Dict[str, Dict[str, float]],
    ) -> Dict[str, int]:
        """Signal 3: Title keyword level classification (weight 0.6)."""
        weight = 0.6
        course_levels: Dict[str, int] = {}

        for code in course_codes:
            title_lower = (titles.get(code) or "").lower()
            m = COURSE_CODE_RE.match(code)
            number = int(m.group(2)) if m else 0

            tier = 2  # default: core/applied
            if any(kw in title_lower for kw in TIER_1_KEYWORDS) and number < 300:
                tier = 1
            elif any(kw in title_lower for kw in TIER_4_KEYWORDS):
                tier = 4
            elif any(kw in title_lower for kw in TIER_3_KEYWORDS):
                tier = 3
            elif "advanced" in title_lower:
                tier = 4 if number >= 400 else 3

            course_levels[code] = tier

        # Add edges: within the same prefix, lower tier before higher tier
        by_prefix: Dict[str, List[str]] = defaultdict(list)
        for code in course_codes:
            m = COURSE_CODE_RE.match(code)
            if m:
                by_prefix[m.group(1)].append(code)

        count = 0
        for prefix, codes in by_prefix.items():
            for code_a in codes:
                tier_a = course_levels.get(code_a, 2)
                for code_b in codes:
                    if code_a == code_b:
                        continue
                    tier_b = course_levels.get(code_b, 2)
                    if tier_a < tier_b:
                        existing = soft_order[code_b].get(code_a, 0)
                        soft_order[code_b][code_a] = max(existing, weight)
                        count += 1
        if count:
            logger.debug(f"Signal: title keywords — {count} edges at weight {weight}")

        return course_levels

    def _signal_description_skills(
        self,
        course_codes: List[str],
        titles: Dict[str, str],
        course_details: Dict[str, dict],
        code_set: Set[str],
        soft_order: Dict[str, Dict[str, float]],
    ):
        """Signal 4: Description-based skill/tool dependencies (weight 0.7)."""
        weight = 0.7

        # Build "teaches" index from course titles
        teaches: Dict[str, List[str]] = defaultdict(list)
        for code in course_codes:
            title_lower = (titles.get(code) or "").lower()
            for skill, keywords in SKILL_KEYWORDS.items():
                if any(kw in title_lower for kw in keywords):
                    teaches[skill].append(code)

        # Build "uses" index from descriptions/objectives
        uses: Dict[str, List[str]] = defaultdict(list)
        for code in course_codes:
            details = course_details.get(code) or {}
            desc = (details.get("description") or "").lower()
            objectives_text = " ".join(details.get("objectives") or []).lower()
            combined_text = f"{desc} {objectives_text}"

            for skill, keywords in SKILL_KEYWORDS.items():
                if any(kw in combined_text for kw in keywords):
                    uses[skill].append(code)

        # Create edges: if B uses a skill that A teaches, A -> B
        count = 0
        for skill in SKILL_KEYWORDS:
            for teacher_code in teaches.get(skill, []):
                for user_code in uses.get(skill, []):
                    if teacher_code == user_code:
                        continue
                    if teacher_code not in code_set or user_code not in code_set:
                        continue
                    if user_code in teaches.get(skill, []):
                        continue
                    existing = soft_order[user_code].get(teacher_code, 0)
                    soft_order[user_code][teacher_code] = max(existing, weight)
                    count += 1
        if count:
            logger.debug(f"Signal: description skills — {count} edges at weight {weight}")

    def _build_coreq_groups(
        self,
        course_codes: List[str],
        corequisites: Dict[str, List[List[str]]],
        code_set: Set[str],
    ) -> List[Set[str]]:
        """Build corequisite groups using union-find."""
        parent: Dict[str, str] = {}

        def find(x):
            if x not in parent:
                parent[x] = x
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for code in course_codes:
            for coreq_set in corequisites.get(code, []):
                for coreq_code in coreq_set:
                    if coreq_code in code_set:
                        union(code, coreq_code)

        groups: Dict[str, Set[str]] = defaultdict(set)
        for code in course_codes:
            if code in parent:
                root = find(code)
                groups[root].add(code)

        return [g for g in groups.values() if len(g) >= 2]
