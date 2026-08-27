"""Single source of truth for all per-campus configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CampusConfig:
    code: str            # "LAMC"
    name: str            # "Los Angeles Mission College"
    short_name: str      # "LA Mission College"
    nickname: str        # "Mission" (used in UI: "at Mission")
    primary_color: str   # "#00538a"
    secondary_color: str # "#004070"
    accent_color: str    # dark enough for text on white
    accent_hover: str    # darkened accent for hover states
    accent_rgb: str      # "0, 83, 138" for rgba() usage
    website: str         # "www.lamission.edu"
    subdomain: str       # "lamc-chat"
    has_voice: bool      # True when campus has a LiveKit voice agent
    phone_number: str    # "+18182145420" — LiveKit SIP number
    lk_agent_name: str   # "lamc-assistant" — LiveKit named agent

    @property
    def phone_display(self) -> str:
        """Format '+18182145420' as '(818) 214-5420'."""
        digits = self.phone_number.lstrip("+")
        if digits.startswith("1") and len(digits) == 11:
            digits = digits[1:]  # strip country code
        if len(digits) == 10:
            return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
        return self.phone_number


CAMPUSES: dict[str, CampusConfig] = {
    "ELAC": CampusConfig(
        code="ELAC",
        name="East Los Angeles College",
        short_name="East LA College",
        nickname="East LA",
        primary_color="#4A7729",
        secondary_color="#32511C",
        accent_color="#32511C",
        accent_hover="#274511",
        accent_rgb="50, 81, 28",
        website="www.elac.edu",
        subdomain="elac-chat",
        has_voice=True,
        phone_number="+18182145505",
        lk_agent_name="elac-assistant",
    ),
    "LACC": CampusConfig(
        code="LACC",
        name="Los Angeles City College",
        short_name="LA City College",
        nickname="City",
        primary_color="#305589",
        secondary_color="#122744",
        accent_color="#AE3539",
        accent_hover="#90292C",
        accent_rgb="174, 53, 57",
        website="www.lacitycollege.edu",
        subdomain="lacc-chat",
        has_voice=True,
        phone_number="+18182145505",
        lk_agent_name="lacc-assistant",
    ),
    "LAHC": CampusConfig(
        code="LAHC",
        name="Los Angeles Harbor College",
        short_name="LA Harbor College",
        nickname="Harbor",
        primary_color="#001B71",
        secondary_color="#FFC52E",
        accent_color="#0444BC",
        accent_hover="#003290",
        accent_rgb="4, 68, 188",
        website="www.lahc.edu",
        subdomain="lahc-chat",
        has_voice=True,
        phone_number="+18182145505",
        lk_agent_name="lahc-assistant",
    ),
    "LAMC": CampusConfig(
        code="LAMC",
        name="Los Angeles Mission College",
        short_name="LA Mission College",
        nickname="Mission",
        primary_color="#00538a",
        secondary_color="#004070",
        accent_color="#00538a",
        accent_hover="#004070",
        accent_rgb="0, 83, 138",
        website="www.lamission.edu",
        subdomain="lamc-chat",
        has_voice=True,
        phone_number="+18182145505",
        lk_agent_name="lamc-assistant",
    ),
    "LAPC": CampusConfig(
        code="LAPC",
        name="Los Angeles Pierce College",
        short_name="Pierce College",
        nickname="Pierce",
        primary_color="#090909",
        secondary_color="#1F1F1F",
        accent_color="#BF2117",
        accent_hover="#880F07",
        accent_rgb="191, 33, 23",
        website="www.piercecollege.edu",
        subdomain="lapc-chat",
        has_voice=True,
        phone_number="+18182145505",
        lk_agent_name="lapc-assistant",
    ),
    "LASC": CampusConfig(
        code="LASC",
        name="Los Angeles Southwest College",
        short_name="LA Southwest College",
        nickname="Southwest",
        primary_color="#002856",
        secondary_color="#00A0DD",
        accent_color="#002856",
        accent_hover="#001B3A",
        accent_rgb="0, 40, 86",
        website="www.lasc.edu",
        subdomain="lasc-chat",
        has_voice=True,
        phone_number="+18182145505",
        lk_agent_name="lasc-assistant",
    ),
    "LATTC": CampusConfig(
        code="LATTC",
        name="Los Angeles Trade-Technical College",
        short_name="LA Trade-Tech College",
        nickname="Trade-Tech",
        primary_color="#552285",
        secondary_color="#FFB800",
        accent_color="#3A3458",
        accent_hover="#32237A",
        accent_rgb="58, 52, 88",
        website="www.lattc.edu",
        subdomain="lattc-chat",
        has_voice=True,
        phone_number="+18182145505",
        lk_agent_name="lattc-assistant",
    ),
    "LAVC": CampusConfig(
        code="LAVC",
        name="Los Angeles Valley College",
        short_name="LA Valley College",
        nickname="Valley",
        primary_color="#2F3F3A",
        secondary_color="#00513F",
        accent_color="#114E3E",
        accent_hover="#2F3F3A",
        accent_rgb="17, 78, 62",
        website="www.lavc.edu",
        subdomain="lavc-chat",
        has_voice=True,
        phone_number="+18182145505",
        lk_agent_name="lavc-assistant",
    ),
    "WLAC": CampusConfig(
        code="WLAC",
        name="West Los Angeles College",
        short_name="West LA College",
        nickname="West LA",
        primary_color="#00347B",
        secondary_color="#1B3667",
        accent_color="#00347B",
        accent_hover="#1B3667",
        accent_rgb="0, 52, 123",
        website="www.wlac.edu",
        subdomain="wlac-chat",
        has_voice=True,
        phone_number="+18182145505",
        lk_agent_name="wlac-assistant",
    ),
}

# Reverse lookups for the legacy campus subdomains and the short campus domains.
_SUBDOMAIN_MAP = {c.subdomain: c.code for c in CAMPUSES.values()}
_CHAT_DOMAIN_MAP = {f"{c.code.lower()}.chat": c.code for c in CAMPUSES.values()}

DEFAULT_CAMPUS = CAMPUSES["LAMC"]


def get_campus(code: str) -> CampusConfig:
    """Lookup by abbreviation, e.g. 'LAMC'. Falls back to LAMC."""
    return CAMPUSES.get(code.upper(), DEFAULT_CAMPUS)


def get_campus_from_host(host: str) -> CampusConfig:
    """Resolve a legacy ``*-chat`` host or short ``<campus>.chat`` host."""
    # Strip port if present
    hostname = host.split(":")[0].lower()
    if hostname in _CHAT_DOMAIN_MAP:
        return CAMPUSES[_CHAT_DOMAIN_MAP[hostname]]
    # Try matching subdomain prefix (e.g. "lamc-chat" from "lamc-chat.johnnyrobot.ai")
    prefix = hostname.split(".")[0] if "." in hostname else ""
    if prefix in _SUBDOMAIN_MAP:
        return CAMPUSES[_SUBDOMAIN_MAP[prefix]]
    return DEFAULT_CAMPUS


# ── Per-campus sample questions for the demo page ──────────────────────

@dataclass(frozen=True)
class SampleQuestions:
    q11_subject: str       # schedule subject (Q11)
    q13_course: str        # specific course code (Q13)
    q16_certificate: str   # certificate name (Q16)
    q17_program: str       # popular program (Q17)
    q19_certificate: str   # another certificate (Q19)
    q20_program: str       # signature degree/program (Q20)
    q27_help_desk: str     # help desk / welcome center (Q27)
    q54_degree: str        # strong degree (Q54)
    q55_certificate: str   # another certificate (Q55)
    q56_program: str       # vocational program (Q56)
    q59_program: str       # creative/unique program (Q59)
    seq1_program: str      # sequence program (SEQ1 + XREF4)
    seq2_program: str      # sequence program (SEQ2)
    seq3_program: str      # ordering program (SEQ3)


SAMPLE_QUESTIONS: dict[str, SampleQuestions] = {
    "LAMC": SampleQuestions(
        q11_subject="culinary arts",
        q13_course="CS 119 Python",
        q16_certificate="medical billing certificate",
        q17_program="Biomanufacturing program",
        q19_certificate="Data Analytics",
        q20_program="Biomanufacturing bachelor's degree",
        q27_help_desk="Eagles Help Desk",
        q54_degree="Administration of Justice",
        q55_certificate="Biotechnology",
        q56_program="Automotive Technology",
        q59_program="Film/TV production",
        seq1_program="Cyber Security",
        seq2_program="Multimedia Animation",
        seq3_program="nursing",
    ),
    "ELAC": SampleQuestions(
        q11_subject="fire technology",
        q13_course="ENGR C110 Engineering",
        q16_certificate="Forensic Crime Scene Investigation certificate",
        q17_program="nursing program",
        q19_certificate="Animation/VFX",
        q20_program="Respiratory Therapy bachelor's degree",
        q27_help_desk="Welcome Center",
        q54_degree="Architecture",
        q55_certificate="Fire Technology",
        q56_program="Engineering",
        q59_program="Animation/VFX",
        seq1_program="Nursing",
        seq2_program="Architecture",
        seq3_program="respiratory therapy",
    ),
    "LACC": SampleQuestions(
        q11_subject="cinema",
        q13_course="CINEMA C100 Film",
        q16_certificate="Dental Lab Technician certificate",
        q17_program="nursing program",
        q19_certificate="Addiction Studies",
        q20_program="Radiologic Technology program",
        q27_help_desk="LACC Call Center",
        q54_degree="Cinema/Television",
        q55_certificate="Paralegal",
        q56_program="Radiologic Technology",
        q59_program="Cinema/TV production",
        seq1_program="Business Administration",
        seq2_program="Cinema/Television",
        seq3_program="nursing",
    ),
    "LAHC": SampleQuestions(
        q11_subject="culinary arts",
        q13_course="FIRE C100 Fire Protection",
        q16_certificate="Global Trade and Logistics certificate",
        q17_program="nursing program",
        q19_certificate="Process Technology",
        q20_program="Process Technology program",
        q27_help_desk="Welcome Center",
        q54_degree="Administration of Justice",
        q55_certificate="Fire Technology",
        q56_program="Architecture",
        q59_program="Culinary Arts",
        seq1_program="Nursing",
        seq2_program="Computer Science",
        seq3_program="nursing",
    ),
    "LAPC": SampleQuestions(
        q11_subject="agriculture",
        q13_course="VSCI C101 Veterinary",
        q16_certificate="Electric Vehicle Technology certificate",
        q17_program="nursing program",
        q19_certificate="Architecture",
        q20_program="Pre-Veterinary Science program",
        q27_help_desk="Student Services Center",
        q54_degree="Business Administration",
        q55_certificate="Agriculture",
        q56_program="Pre-Veterinary Science",
        q59_program="Horse Science/Equine",
        seq1_program="Computer Science",
        seq2_program="Architecture",
        seq3_program="nursing",
    ),
    "LASC": SampleQuestions(
        q11_subject="computer science",
        q13_course="CS 111 Python",
        q16_certificate="Phlebotomy certificate",
        q17_program="nursing program",
        q19_certificate="Cloud Computing",
        q20_program="Cyber Security program",
        q27_help_desk="Admissions and Records office",
        q54_degree="Administration of Justice",
        q55_certificate="Photovoltaics",
        q56_program="Computer Science/IT",
        q59_program="Child Development",
        seq1_program="Cyber Security",
        seq2_program="Nursing",
        seq3_program="nursing",
    ),
    "LATTC": SampleQuestions(
        q11_subject="welding",
        q13_course="ELEC C100 Electricity",
        q16_certificate="Cosmetology certificate",
        q17_program="Culinary Arts program",
        q19_certificate="HVAC",
        q20_program="Electrical Construction and Maintenance program",
        q27_help_desk="Welcome Center",
        q54_degree="Automotive Technology",
        q55_certificate="Plumbing",
        q56_program="Electrical Construction",
        q59_program="Fashion Design",
        seq1_program="Welding",
        seq2_program="Culinary Arts",
        seq3_program="electrical",
    ),
    "LAVC": SampleQuestions(
        q11_subject="media arts",
        q13_course="CINE C100 Cinema",
        q16_certificate="Fire Technology certificate",
        q17_program="nursing program",
        q19_certificate="Broadcasting",
        q20_program="Cinema/Media Arts program",
        q27_help_desk="GPS Center",
        q54_degree="Administration of Justice",
        q55_certificate="Respiratory Therapy",
        q56_program="Fire Technology",
        q59_program="Cinema/Media Arts",
        seq1_program="Cinema/Media Arts",
        seq2_program="Nursing",
        seq3_program="nursing",
    ),
    "WLAC": SampleQuestions(
        q11_subject="dental hygiene",
        q13_course="AVN C100 Aviation",
        q16_certificate="Pharmacy Technician certificate",
        q17_program="Dental Hygiene program",
        q19_certificate="Climate Change Studies",
        q20_program="Aviation Maintenance Technology bachelor's degree",
        q27_help_desk="Welcome Center",
        q54_degree="Paralegal",
        q55_certificate="Climate Change Studies",
        q56_program="Aviation Maintenance Technology",
        q59_program="Dental Hygiene",
        seq1_program="Paralegal",
        seq2_program="Dental Hygiene",
        seq3_program="dental hygiene",
    ),
}


def get_sample_questions(campus_code: str) -> SampleQuestions:
    """Return sample questions for the given campus, defaulting to LAMC."""
    return SAMPLE_QUESTIONS.get(campus_code.upper(), SAMPLE_QUESTIONS["LAMC"])
