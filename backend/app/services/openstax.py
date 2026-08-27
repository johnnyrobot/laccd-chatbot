"""OpenStax free textbook suggestions for ZTC responses.

Static mapping of LACCD subject codes to OpenStax books.
Run `scripts/refresh_openstax.py` to update from the live catalog.
"""

from __future__ import annotations

OPENSTAX_BASE_URL = "https://openstax.org/details/books"

# Subject code → list of {title, slug} for latest editions only.
OPENSTAX_BOOKS: dict[str, list[dict[str, str]]] = {
    "MATH": [
        {"title": "College Algebra 2e", "slug": "college-algebra-2e"},
        {"title": "Precalculus 2e", "slug": "precalculus-2e"},
        {"title": "Calculus Volume 1", "slug": "calculus-volume-1"},
        {"title": "Calculus Volume 2", "slug": "calculus-volume-2"},
        {"title": "Calculus Volume 3", "slug": "calculus-volume-3"},
        {"title": "Elementary Algebra 2e", "slug": "elementary-algebra-2e"},
        {"title": "Intermediate Algebra 2e", "slug": "intermediate-algebra-2e"},
        {"title": "Prealgebra 2e", "slug": "prealgebra-2e"},
    ],
    "STAT": [
        {"title": "Introductory Statistics 2e", "slug": "introductory-statistics-2e"},
    ],
    "ENGLISH": [
        {"title": "Writing Guide with Handbook", "slug": "writing-guide"},
    ],
    "BIOLOGY": [
        {"title": "Biology 2e", "slug": "biology-2e"},
        {"title": "Concepts of Biology", "slug": "concepts-biology"},
        {"title": "Microbiology", "slug": "microbiology"},
    ],
    "ANATOMY": [
        {"title": "Anatomy and Physiology 2e", "slug": "anatomy-and-physiology-2e"},
    ],
    "CHEM": [
        {"title": "Chemistry 2e", "slug": "chemistry-2e"},
        {"title": "Chemistry: Atoms First 2e", "slug": "chemistry-atoms-first-2e"},
    ],
    "PHYSICS": [
        {"title": "College Physics 2e", "slug": "college-physics-2e"},
        {"title": "University Physics Volume 1", "slug": "university-physics-volume-1"},
    ],
    "PSYCH": [
        {"title": "Psychology 2e", "slug": "psychology-2e"},
    ],
    "SOC": [
        {"title": "Introduction to Sociology 3e", "slug": "introduction-sociology-3e"},
    ],
    "ECON": [
        {"title": "Principles of Economics 3e", "slug": "principles-economics-3e"},
        {"title": "Principles of Macroeconomics 3e", "slug": "principles-macroeconomics-3e"},
        {"title": "Principles of Microeconomics 3e", "slug": "principles-microeconomics-3e"},
    ],
    "ACCTG": [
        {"title": "Principles of Financial Accounting", "slug": "principles-financial-accounting"},
        {"title": "Principles of Managerial Accounting", "slug": "principles-managerial-accounting"},
    ],
    "BUS": [
        {"title": "Introduction to Business", "slug": "introduction-business"},
        {"title": "Business Ethics", "slug": "business-ethics"},
        {"title": "Business Law I Essentials", "slug": "business-law-i-essentials"},
    ],
    "POLSCI": [
        {"title": "American Government 4e", "slug": "american-government-4e"},
        {"title": "Introduction to Political Science", "slug": "introduction-political-science"},
    ],
    "ANTHRO": [
        {"title": "Introduction to Anthropology", "slug": "introduction-anthropology"},
    ],
    "PHILOS": [
        {"title": "Introduction to Philosophy", "slug": "introduction-philosophy"},
    ],
    "ASTRON": [
        {"title": "Astronomy 2e", "slug": "astronomy-2e"},
    ],
    "NURSE": [
        {"title": "Fundamentals of Nursing", "slug": "fundamentals-nursing"},
        {"title": "Pharmacology for Nurses", "slug": "pharmacology"},
    ],
    "CS": [
        {"title": "Introduction to Computer Science", "slug": "introduction-computer-science"},
        {"title": "Introduction to Python Programming", "slug": "introduction-python-programming"},
    ],
    "NUTR": [
        {"title": "Nutrition for Nurses", "slug": "nutrition"},
    ],
    "HIST": [
        {"title": "U.S. History", "slug": "us-history"},
        {"title": "World History Volume 1", "slug": "world-history-volume-1"},
        {"title": "World History Volume 2", "slug": "world-history-volume-2"},
    ],
}


def get_openstax_suggestions(subject_code: str) -> list[dict[str, str]]:
    """Return OpenStax books for a subject, with full URLs."""
    books = OPENSTAX_BOOKS.get(subject_code.upper(), [])
    return [{"title": b["title"], "url": f"{OPENSTAX_BASE_URL}/{b['slug']}"} for b in books]


def get_openstax_suggestions_multi(subject_codes: list[str]) -> dict[str, list[dict[str, str]]]:
    """Return OpenStax books for multiple subjects."""
    result: dict[str, list[dict[str, str]]] = {}
    for code in subject_codes:
        books = get_openstax_suggestions(code)
        if books:
            result[code] = books
    return result
