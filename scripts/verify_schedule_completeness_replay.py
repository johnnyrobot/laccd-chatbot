#!/usr/bin/env python3
"""Compare CLI and chatbot schedule normalization over one replay bundle.

The checked-in chatbot fixture is a deterministic synthetic regression input.
The CLI's captured section HTML is copied into a temporary directory; the CLI
source worktree and fixture directory are never modified.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Callable


CHATBOT_FIXTURE = Path("tests/fixtures/schedule_completeness_replay.json")
DISCOVERY_NAME = "discovery-multimd.json"
MANIFEST_NAME = "replay-manifest.json"
VERIFICATION_KIND = "deterministic_synthetic_regression"
_CLASS_COMPONENT_RE = re.compile(r"^([^\s()]+)\s+\(([A-Za-z]+)\)$")


class VerificationError(RuntimeError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sorted_unique(values: list[str]) -> list[str]:
    return sorted({value.strip() for value in values if value.strip()})


def _require_string_list(document: dict[str, Any], key: str, context: str) -> list[str]:
    if key not in document:
        raise VerificationError(f"{context} is missing required key {key!r}")
    value = document[key]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise VerificationError(f"{context} key {key!r} must be an array of strings")
    return _sorted_unique(value)


def _require_string_map(document: dict[str, Any], key: str, context: str) -> dict[str, str]:
    if key not in document:
        raise VerificationError(f"{context} is missing required key {key!r}")
    value = document[key]
    if not isinstance(value, dict) or any(
        not isinstance(name, str) or not isinstance(digest, str) for name, digest in value.items()
    ):
        raise VerificationError(f"{context} key {key!r} must be an object with string values")
    return dict(value)


def _validated_replay_document(document: dict[str, Any], context: str) -> dict[str, Any]:
    validated = dict(document)
    validated["catalogs"] = _require_string_list(document, "catalogs", context)
    validated["class_numbers"] = _require_string_list(document, "class_numbers", context)
    validated["fixture_hashes"] = _require_string_map(document, "fixture_hashes", context)
    return validated


def _normalize_raw_class_number(raw: object) -> str:
    value = str(raw or "").strip()
    match = _CLASS_COMPONENT_RE.fullmatch(value)
    return match.group(1) if match else value


def recursive_raw_oracle(value: object) -> list[str]:
    """Extract schedule class numbers without using chatbot traversal."""
    numbers: list[str] = []

    if not isinstance(value, dict):
        return numbers

    for subject in value.get("subjects") or []:
        for course in subject.get("courses") or []:
            seen: set[str] = set()

            def emit_and_walk_related(section: dict[str, Any]) -> None:
                number = _normalize_raw_class_number(section.get("classNbr"))
                if number and number not in seen:
                    seen.add(number)
                    numbers.append(number)
                for child in section.get("relsections") or []:
                    emit_and_walk_related(child)

            for primary in course.get("sections") or []:
                number = _normalize_raw_class_number(primary.get("classNbr"))
                if not number:
                    continue
                if number not in seen:
                    seen.add(number)
                    numbers.append(number)
                for child in primary.get("relsections") or []:
                    emit_and_walk_related(child)

    return _sorted_unique(numbers)


def _raw_catalogs(listing: dict[str, Any]) -> list[str]:
    catalogs: list[str] = []
    for subject in listing.get("subjects") or []:
        for course in subject.get("courses") or []:
            catalogs.append(str(course.get("catalogNbr", "")))
    return _sorted_unique(catalogs)


def _load_json_bytes(path: Path) -> tuple[bytes, dict[str, Any]]:
    data = path.read_bytes()
    try:
        decoded = json.loads(data)
    except json.JSONDecodeError as exc:
        raise VerificationError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(decoded, dict):
        raise VerificationError(f"expected a JSON object in {path}")
    return data, decoded


def _format_sections_from(
    chatbot_repo: Path,
) -> tuple[Callable[[dict[str, Any]], list[dict[str, Any]]], Path]:
    backend = chatbot_repo / "backend"
    if not backend.is_dir():
        raise VerificationError(f"chatbot backend directory does not exist: {backend}")
    expected_module = _require_tracked_chatbot_file(
        chatbot_repo,
        backend / "app" / "services" / "live_schedule.py",
        "format_sections module",
    )
    previous_sys_path = list(sys.path)
    sys.path.insert(0, str(chatbot_repo))
    sys.path.insert(0, str(backend))
    previous_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        module = importlib.import_module("app.services.live_schedule")
    except ImportError as exc:
        raise VerificationError(f"cannot import chatbot format_sections from {chatbot_repo}: {exc}") from exc
    finally:
        sys.dont_write_bytecode = previous_dont_write_bytecode
        sys.path[:] = previous_sys_path
    module_file = getattr(module, "__file__", None)
    if not module_file:
        raise VerificationError("chatbot format_sections module has no source path")
    verified_module = _require_tracked_chatbot_file(
        chatbot_repo, Path(module_file), "format_sections module"
    )
    if verified_module != expected_module:
        raise VerificationError(
            f"chatbot format_sections imported from {verified_module}, expected {expected_module}"
        )
    format_sections = getattr(module, "format_sections", None)
    if not callable(format_sections):
        raise VerificationError(
            f"chatbot module does not export callable format_sections: {verified_module}"
        )
    return format_sections, verified_module


def _git_output(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise VerificationError(f"cannot inspect Git repository {repo}: {detail}")
    return completed.stdout.strip()


def _cli_repo_state(cli_repo: Path) -> tuple[str, str]:
    return _git_output(cli_repo, "rev-parse", "HEAD"), _git_output(cli_repo, "status", "--porcelain")


def _chatbot_repo_state(chatbot_repo: Path) -> tuple[str, str]:
    return _git_output(chatbot_repo, "rev-parse", "HEAD"), _git_output(
        chatbot_repo, "status", "--porcelain"
    )


def _require_clean_cli_repo(cli_repo: Path, expected_revision: str | None = None) -> str:
    revision, status = _cli_repo_state(cli_repo)
    if status:
        raise VerificationError(f"CLI repository must be clean before and after replay: {status!r}")
    if expected_revision is not None and revision != expected_revision:
        raise VerificationError(
            f"CLI repository changed during replay: expected {expected_revision}, found {revision}"
        )
    return revision


def _require_clean_chatbot_repo(
    chatbot_repo: Path, expected_revision: str | None = None
) -> str:
    revision, status = _chatbot_repo_state(chatbot_repo)
    if status:
        raise VerificationError(
            f"chatbot repository must be clean before and after replay: {status!r}"
        )
    if expected_revision is not None and revision != expected_revision:
        raise VerificationError(
            "chatbot repository changed during replay: "
            f"expected {expected_revision}, found {revision}"
        )
    return revision


def _require_tracked_chatbot_file(chatbot_repo: Path, path: Path, label: str) -> Path:
    repo = chatbot_repo.resolve()
    lexical = path.absolute()
    try:
        lexical_relative = lexical.relative_to(repo)
    except ValueError as exc:
        raise VerificationError(
            f"chatbot {label} must be inside the chatbot repository: {path}"
        ) from exc
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise VerificationError(f"chatbot {label} does not exist: {path}") from exc
    if not resolved.is_file():
        raise VerificationError(f"chatbot {label} is not a file: {path}")
    try:
        resolved_relative = resolved.relative_to(repo)
    except ValueError as exc:
        raise VerificationError(
            f"chatbot {label} must be inside the chatbot repository: {path}"
        ) from exc

    for relative in {lexical_relative, resolved_relative}:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "ls-files",
                "--error-unmatch",
                "--",
                relative.as_posix(),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise VerificationError(
                f"chatbot {label} must be tracked by Git under the chatbot repository: {relative}"
            )
    return resolved


def _build_replay_bundle(
    source_fixtures: Path,
    destination: Path,
    discovery_bytes: bytes,
    oracle_catalogs: list[str],
    oracle_numbers: list[str],
) -> tuple[dict[str, Any], dict[str, str]]:
    _manifest_bytes, decoded_manifest = _load_json_bytes(source_fixtures / MANIFEST_NAME)
    source_manifest = _validated_replay_document(decoded_manifest, "CLI replay manifest")
    source_catalogs = source_manifest["catalogs"]
    source_numbers = source_manifest["class_numbers"]
    if source_catalogs != oracle_catalogs:
        raise VerificationError(f"CLI manifest catalogs {source_catalogs} do not match raw oracle {oracle_catalogs}")
    if source_numbers != oracle_numbers:
        raise VerificationError(f"CLI manifest class numbers {source_numbers} do not match raw oracle {oracle_numbers}")

    fixture_bytes = {DISCOVERY_NAME: discovery_bytes}
    for catalog in oracle_catalogs:
        name = f"sections-{catalog}.html"
        if name not in source_manifest["fixture_hashes"]:
            raise VerificationError(f"CLI replay manifest is missing fixture hash for {name}")
        data = (source_fixtures / name).read_bytes()
        digest = _sha256(data)
        if digest != source_manifest["fixture_hashes"][name]:
            raise VerificationError(
                f"CLI source fixture {name} SHA-256 = {digest}, "
                f"want {source_manifest['fixture_hashes'][name]}"
            )
        fixture_bytes[name] = data

    fixture_hashes = {name: _sha256(data) for name, data in fixture_bytes.items()}
    manifest = {
        "college": str(source_manifest.get("college") or ""),
        "term": str(source_manifest.get("term") or ""),
        "subject": str(source_manifest.get("subject") or ""),
        "fixture_hashes": fixture_hashes,
        "catalogs": oracle_catalogs,
        "class_numbers": oracle_numbers,
    }
    if not all((manifest["college"], manifest["term"], manifest["subject"])):
        raise VerificationError("CLI replay manifest is missing college, term, or subject")

    for name, data in fixture_bytes.items():
        (destination / name).write_bytes(data)
    (destination / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest, fixture_hashes


def run_cli_replay(
    cli_repo: Path,
    fixtures: Path,
    manifest: dict[str, Any],
    expected_revision: str,
) -> dict[str, Any]:
    _require_clean_cli_repo(cli_repo, expected_revision)
    command = [
        "go",
        "run",
        "./tools/schedule-replay",
        "--fixtures",
        str(fixtures),
        "--college",
        manifest["college"],
        "--term",
        manifest["term"],
        "--subject",
        manifest["subject"],
    ]
    completed = subprocess.run(command, cwd=cli_repo, text=True, capture_output=True, check=False)
    _require_clean_cli_repo(cli_repo, expected_revision)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise VerificationError(f"CLI schedule replay failed: {detail}")
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise VerificationError(f"CLI schedule replay returned invalid JSON: {exc}") from exc
    if not isinstance(report, dict):
        raise VerificationError("CLI schedule replay report must be a JSON object")
    return _validated_replay_document(report, "CLI schedule replay report")


def verify(cli_repo: Path, chatbot_repo: Path, fixtures: Path) -> dict[str, Any]:
    cli_repo = cli_repo.resolve()
    chatbot_repo = chatbot_repo.resolve()
    fixtures = fixtures.resolve()
    try:
        fixtures.relative_to(cli_repo)
    except ValueError as exc:
        raise VerificationError(f"CLI fixtures must be inside the verified CLI repository: {fixtures}") from exc
    cli_revision = _require_clean_cli_repo(cli_repo)
    chatbot_revision = _require_clean_chatbot_repo(chatbot_repo)
    try:
        discovery_path = _require_tracked_chatbot_file(
            chatbot_repo, chatbot_repo / CHATBOT_FIXTURE, "fixture"
        )
        discovery_bytes, listing = _load_json_bytes(discovery_path)
        oracle_numbers = recursive_raw_oracle(listing)
        oracle_catalogs = _raw_catalogs(listing)
        format_sections, module_path = _format_sections_from(chatbot_repo)
        chatbot_numbers = _sorted_unique(
            [
                section.get("class_number", "")
                for section in format_sections(json.loads(discovery_bytes))
            ]
        )

        with tempfile.TemporaryDirectory(prefix="schedule-completeness-replay-") as temporary:
            replay_dir = Path(temporary)
            manifest, fixture_hashes = _build_replay_bundle(
                fixtures,
                replay_dir,
                discovery_bytes,
                oracle_catalogs,
                oracle_numbers,
            )
            cli_report = run_cli_replay(cli_repo, replay_dir, manifest, cli_revision)
    finally:
        _require_clean_chatbot_repo(chatbot_repo, chatbot_revision)

    cli_hashes = cli_report.get("fixture_hashes")
    matches = {
        "cli_to_oracle": cli_report["class_numbers"] == oracle_numbers,
        "chatbot_to_oracle": chatbot_numbers == oracle_numbers,
        "cli_catalogs_to_oracle": cli_report["catalogs"] == oracle_catalogs,
        "cli_fixture_hashes": cli_hashes == fixture_hashes,
    }
    return {
        "status": "pass" if all(matches.values()) else "mismatch",
        "verification_kind": VERIFICATION_KIND,
        "scope": {
            "college": manifest["college"],
            "term": manifest["term"],
            "subject": manifest["subject"],
        },
        "inputs": {
            "cli_repo": str(cli_repo),
            "cli_revision": cli_revision,
            "chatbot_repo": str(chatbot_repo),
            "chatbot_revision": chatbot_revision,
            "chatbot_module": str(module_path),
            "cli_fixture_source": str(fixtures),
            "chatbot_fixture": str(discovery_path),
        },
        "fixture_hashes": fixture_hashes,
        "cli": cli_report,
        "chatbot": {
            "class_numbers": chatbot_numbers,
            "discovery_sha256": _sha256(discovery_bytes),
        },
        "oracle": {
            "catalogs": oracle_catalogs,
            "class_numbers": oracle_numbers,
        },
        "matches": matches,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli-repo", required=True, type=Path)
    parser.add_argument("--chatbot-repo", required=True, type=Path)
    parser.add_argument("--fixtures", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        report = verify(args.cli_repo, args.chatbot_repo, args.fixtures)
    except (OSError, VerificationError) as exc:
        report = {
            "status": "error",
            "verification_kind": VERIFICATION_KIND,
            "error": str(exc),
            "inputs": {
                "cli_repo": str(args.cli_repo.resolve()),
                "chatbot_repo": str(args.chatbot_repo.resolve()),
                "cli_fixture_source": str(args.fixtures.resolve()),
            },
        }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
