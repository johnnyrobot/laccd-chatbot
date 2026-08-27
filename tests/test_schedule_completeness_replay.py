from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.verify_schedule_completeness_replay import recursive_raw_oracle


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "verify_schedule_completeness_replay.py"
CHATBOT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "schedule_completeness_replay.json"
EXPECTED_CLASS_NUMBERS = ["17300", "17310", "17490", "17491"]
EXPECTED_DISCOVERY_BYTES = (
    b'{"termname":"Fall 2026","subjects":[{"code":"MULTIMD","courses":['
    b'{"subject":"MULTIMD","catalogNbr":"100","descr":"Multimedia 100",'
    b'"sections":[{"classNbr":"17300 (LEC)"}]},'
    b'{"subject":"MULTIMD","catalogNbr":"110","descr":"Multimedia 110",'
    b'"sections":[{"classNbr":"17310 (LEC)"}]},'
    b'{"subject":"MULTIMD","catalogNbr":"125","descr":"Multimedia 125",'
    b'"sections":[{"classNbr":"17490 (LEC)","relsections":'
    b'[{"classNbr":"17491 (LAB)","relsections":[{"classNbr":"17310 (LAB)"}]}]}]}]}]}\n'
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_source_fixtures(cli_repo: Path) -> Path:
    fixtures = cli_repo / "internal" / "laccd" / "testdata" / "completeness"
    fixtures.mkdir(parents=True)
    files = {
        "discovery-multimd.json": b'{"subjects":[{"courses":[{"catalogNbr":"100"}]}]}\n',
        "sections-100.html": b"captured sections for 100\n",
        "sections-110.html": b"captured sections for 110\n",
        "sections-125.html": b"captured sections for 125\n",
    }
    for name, data in files.items():
        (fixtures / name).write_bytes(data)
    manifest = {
        "college": "LAMC",
        "term": "2268",
        "subject": "MULTIMD",
        "fixture_hashes": {name: _sha256(data) for name, data in files.items()},
        "catalogs": ["100", "110", "125"],
        "class_numbers": EXPECTED_CLASS_NUMBERS,
    }
    (fixtures / "replay-manifest.json").write_text(json.dumps(manifest))
    return fixtures


def _git(cli_repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cli_repo), *args],
        text=True,
        capture_output=True,
        check=True,
    )


def _init_cli_repo(root: Path) -> tuple[Path, Path, str]:
    cli_repo = root / "cli-repo"
    (cli_repo / "tools" / "schedule-replay").mkdir(parents=True)
    fixtures = _write_source_fixtures(cli_repo)
    _git(cli_repo, "init", "-q")
    _git(cli_repo, "config", "user.email", "replay-test@example.invalid")
    _git(cli_repo, "config", "user.name", "Replay Test")
    _git(cli_repo, "add", ".")
    _git(cli_repo, "commit", "-q", "-m", "fixture baseline")
    revision = _git(cli_repo, "rev-parse", "HEAD").stdout.strip()
    return cli_repo, fixtures, revision


def _init_chatbot_repo(root: Path) -> tuple[Path, str]:
    chatbot_repo = root / "chatbot-repo"
    services = chatbot_repo / "backend" / "app" / "services"
    fixture = chatbot_repo / "tests" / "fixtures" / "schedule_completeness_replay.json"
    services.mkdir(parents=True)
    fixture.parent.mkdir(parents=True)
    (services.parent / "__init__.py").write_text("")
    (services / "__init__.py").write_text("")
    (services / "schedule_sections.py").write_bytes(
        (REPO_ROOT / "backend" / "app" / "services" / "schedule_sections.py").read_bytes()
    )
    (services / "live_schedule.py").write_text(
        """from app.services.schedule_sections import iter_schedule_sections, normalize_class_number


def format_sections(listing):
    return [
        {"class_number": normalize_class_number(section.get("classNbr"))[0]}
        for _course, section, _primary in iter_schedule_sections(listing)
    ]
"""
    )
    fixture.write_bytes(EXPECTED_DISCOVERY_BYTES)
    (chatbot_repo / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    _git(chatbot_repo, "init", "-q")
    _git(chatbot_repo, "config", "user.email", "replay-test@example.invalid")
    _git(chatbot_repo, "config", "user.name", "Replay Test")
    _git(chatbot_repo, "add", ".")
    _git(chatbot_repo, "commit", "-q", "-m", "chatbot baseline")
    revision = _git(chatbot_repo, "rev-parse", "HEAD").stdout.strip()
    return chatbot_repo, revision


def _make_ignored_untracked(repo: Path, path: Path) -> None:
    relative = path.relative_to(repo).as_posix()
    _git(repo, "rm", "--cached", "-q", "--", relative)
    with (repo / ".gitignore").open("a") as ignore:
        ignore.write(f"/{relative}\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", f"leave {relative} untracked")


def _replace_with_external_symlink(repo: Path, path: Path, root: Path) -> None:
    data = path.read_bytes()
    outside = root / f"outside-{path.name}"
    outside.write_bytes(data)
    path.unlink()
    path.symlink_to(outside)
    _git(repo, "add", "--", str(path.relative_to(repo)))
    _git(repo, "commit", "-q", "-m", f"link {path.name} outside repository")


def _write_fake_go(root: Path) -> Path:
    bin_dir = root / "bin"
    bin_dir.mkdir()
    fake_go = bin_dir / "go"
    fake_go.write_text(
        """#!/usr/bin/env python3
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

fixtures = Path(sys.argv[sys.argv.index('--fixtures') + 1])
expected_args = [
    'run', './tools/schedule-replay', '--fixtures', str(fixtures),
    '--college', 'LAMC', '--term', '2268', '--subject', 'MULTIMD',
]
if (
    sys.argv[1:] != expected_args
    or Path.cwd() != Path(os.environ['EXPECTED_CLI_REPO'])
    or not fixtures.is_absolute()
    or not fixtures.name.startswith('schedule-completeness-replay-')
):
    print('unexpected schedule-replay subprocess contract', file=sys.stderr)
    raise SystemExit(85)
Path(os.environ['INVOCATION_CAPTURE']).write_text(json.dumps(sys.argv[1:]))
discovery = (fixtures / 'discovery-multimd.json').read_bytes()
if hashlib.sha256(discovery).hexdigest() != os.environ['EXPECTED_DISCOVERY_SHA256']:
    print('verifier did not pass the exact chatbot discovery bytes', file=sys.stderr)
    raise SystemExit(86)
Path(os.environ['CAPTURED_DISCOVERY']).write_bytes(discovery)
manifest = json.loads((fixtures / 'replay-manifest.json').read_bytes())
fixture_hashes = {
    name: hashlib.sha256((fixtures / name).read_bytes()).hexdigest()
    for name in manifest['fixture_hashes']
}
if fixture_hashes != manifest['fixture_hashes']:
    print('temporary replay manifest hashes do not match its files', file=sys.stderr)
    raise SystemExit(87)
source_section_hashes = json.loads(os.environ['EXPECTED_SOURCE_SECTION_HASHES'])
actual_section_hashes = {
    name: fixture_hashes[name] for name in source_section_hashes
}
if actual_section_hashes != source_section_hashes:
    print('temporary section HTML differs from CLI source fixture hashes', file=sys.stderr)
    raise SystemExit(88)
if os.environ.get('FAKE_CLI_MUTATION') == 'revision':
    subprocess.run(['git', 'commit', '--allow-empty', '-q', '-m', 'unexpected mutation'], check=True)
if os.environ.get('FAKE_CLI_MUTATION') == 'dirty':
    Path('unexpected-dirty-file.txt').write_text('unexpected mutation\\n')
chatbot_repo = Path(os.environ['EXPECTED_CHATBOT_REPO'])
if os.environ.get('FAKE_CHATBOT_MUTATION') == 'revision':
    subprocess.run(
        ['git', '-C', str(chatbot_repo), 'commit', '--allow-empty', '-q', '-m', 'unexpected mutation'],
        check=True,
    )
if os.environ.get('FAKE_CHATBOT_MUTATION') == 'dirty':
    (chatbot_repo / 'unexpected-dirty-file.txt').write_text('unexpected mutation\\n')
report = {
    'fixture_hashes': fixture_hashes,
    'catalogs': manifest['catalogs'],
    'class_numbers': json.loads(os.environ['FAKE_CLASS_NUMBERS']),
}
report.update(json.loads(os.environ.get('FAKE_REPORT_OVERRIDES', '{}')))
for key in json.loads(os.environ.get('FAKE_REMOVE_REPORT_KEYS', '[]')):
    report.pop(key, None)
print(json.dumps(report))
"""
    )
    fake_go.chmod(0o755)
    return bin_dir


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _run_verifier(
    tmp_path: Path,
    fake_numbers: list[str],
    *,
    dirty_cli: bool = False,
    dirty_chatbot: bool = False,
    mutate_revision: bool = False,
    mutate_dirty: bool = False,
    mutate_chatbot_revision: bool = False,
    mutate_chatbot_dirty: bool = False,
    untracked_chatbot_fixture: bool = False,
    untracked_chatbot_module: bool = False,
    outside_chatbot_fixture: bool = False,
    outside_chatbot_module: bool = False,
    report_overrides: dict[str, object] | None = None,
    remove_report_keys: list[str] | None = None,
    replace_section_and_manifest_hash: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, dict[str, bytes], str]:
    cli_repo, source_fixtures, initial_revision = _init_cli_repo(tmp_path)
    chatbot_repo, _chatbot_revision = _init_chatbot_repo(tmp_path)
    chatbot_fixture = chatbot_repo / "tests" / "fixtures" / "schedule_completeness_replay.json"
    chatbot_module = chatbot_repo / "backend" / "app" / "services" / "live_schedule.py"
    if untracked_chatbot_module or outside_chatbot_module:
        chatbot_module.write_text(
            """import os
from pathlib import Path

Path(os.environ["UNTRUSTED_MODULE_IMPORT_MARKER"]).write_text("imported\\n")
"""
            + chatbot_module.read_text()
        )
    if untracked_chatbot_fixture:
        _make_ignored_untracked(chatbot_repo, chatbot_fixture)
    if untracked_chatbot_module:
        _make_ignored_untracked(chatbot_repo, chatbot_module)
    if outside_chatbot_fixture:
        _replace_with_external_symlink(chatbot_repo, chatbot_fixture, tmp_path)
    if outside_chatbot_module:
        _replace_with_external_symlink(chatbot_repo, chatbot_module, tmp_path)
    if dirty_cli:
        (cli_repo / "dirty.txt").write_text("uncommitted\n")
    if dirty_chatbot:
        (chatbot_repo / "dirty.txt").write_text("uncommitted\n")
    if replace_section_and_manifest_hash:
        replacement = b"arbitrary replacement HTML\n"
        (source_fixtures / "sections-100.html").write_bytes(replacement)
        manifest_path = source_fixtures / "replay-manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["fixture_hashes"]["sections-100.html"] = _sha256(replacement)
        manifest_path.write_text(json.dumps(manifest))
    source_before = _snapshot(source_fixtures)
    fake_bin = _write_fake_go(tmp_path)
    capture = tmp_path / "captured-discovery.json"
    invocation_capture = tmp_path / "invocation.json"
    source_manifest = json.loads((source_fixtures / "replay-manifest.json").read_bytes())
    source_section_hashes = {
        name: value
        for name, value in source_manifest["fixture_hashes"].items()
        if name.startswith("sections-")
    }
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "PYTHONPATH": f"{chatbot_repo / 'backend'}{os.pathsep}{chatbot_repo}",
            "EXPECTED_DISCOVERY_SHA256": _sha256(EXPECTED_DISCOVERY_BYTES),
            "CAPTURED_DISCOVERY": str(capture),
            "FAKE_CLASS_NUMBERS": json.dumps(fake_numbers),
            "EXPECTED_CLI_REPO": str(cli_repo),
            "EXPECTED_CHATBOT_REPO": str(chatbot_repo),
            "UNTRUSTED_MODULE_IMPORT_MARKER": str(tmp_path / "untrusted-module-imported.txt"),
            "EXPECTED_SOURCE_SECTION_HASHES": json.dumps(source_section_hashes),
            "INVOCATION_CAPTURE": str(invocation_capture),
            "FAKE_CLI_MUTATION": "revision" if mutate_revision else "dirty" if mutate_dirty else "",
            "FAKE_CHATBOT_MUTATION": (
                "revision" if mutate_chatbot_revision else "dirty" if mutate_chatbot_dirty else ""
            ),
            "FAKE_REPORT_OVERRIDES": json.dumps(report_overrides or {}),
            "FAKE_REMOVE_REPORT_KEYS": json.dumps(remove_report_keys or []),
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--cli-repo",
            str(cli_repo),
            "--chatbot-repo",
            str(chatbot_repo),
            "--fixtures",
            str(source_fixtures),
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert _snapshot(source_fixtures) == source_before
    return completed, capture, invocation_capture, source_before, initial_revision


def test_recursive_raw_oracle_only_walks_schedule_schema() -> None:
    listing = {
        "classNbr": "outside-root",
        "subjects": [
            {
                "classNbr": "outside-subject",
                "courses": [
                    {
                        "classNbr": "outside-course",
                        "sections": [
                            {
                                "classNbr": "10001 (LEC)",
                                "meetings": [{"classNbr": "outside-meeting"}],
                                "relsections": [{"classNbr": "10002 (LAB)"}],
                            }
                        ],
                    }
                ],
            }
        ],
    }

    assert recursive_raw_oracle(listing) == ["10001", "10002"]


def test_recursive_raw_oracle_applies_blank_and_duplicate_traversal_rules() -> None:
    listing = {
        "subjects": [
            {
                "courses": [
                    {
                        "sections": [
                            {
                                "classNbr": " ",
                                "relsections": [{"classNbr": "suppressed"}],
                            },
                            {
                                "classNbr": "20001 (LEC)",
                                "relsections": [
                                    {
                                        "classNbr": "20001 (LAB)",
                                        "relsections": [{"classNbr": "20003 (LAB)"}],
                                    },
                                    {
                                        "classNbr": None,
                                        "relsections": [{"classNbr": "20004 (LAB)"}],
                                    },
                                ],
                            },
                        ]
                    }
                ]
            }
        ]
    }

    assert recursive_raw_oracle(listing) == ["20001", "20003", "20004"]


@pytest.mark.parametrize(
    "listing,expected",
    [
        ({"subjects": None}, []),
        ({"subjects": [{"courses": None}]}, []),
        ({"subjects": [{"courses": [{"sections": None}]}]}, []),
        (
            {
                "subjects": [
                    {
                        "courses": [
                            {"sections": [{"classNbr": "30001 (LEC)", "relsections": None}]}
                        ]
                    }
                ]
            },
            ["30001"],
        ),
    ],
)
def test_recursive_raw_oracle_tolerates_null_collections(
    listing: dict[str, object], expected: list[str]
) -> None:
    assert recursive_raw_oracle(listing) == expected


def test_verifier_exits_nonzero_and_reports_all_sets_on_cli_mismatch(tmp_path: Path):
    completed, _capture, _invocation, _source_before, _revision = _run_verifier(tmp_path, ["17300"])

    assert completed.returncode == 1, completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "mismatch"
    assert report["cli"]["class_numbers"] == ["17300"]
    assert report["chatbot"]["class_numbers"] == EXPECTED_CLASS_NUMBERS
    assert report["oracle"]["class_numbers"] == EXPECTED_CLASS_NUMBERS


def test_verifier_uses_exact_chatbot_json_bytes_and_preserves_cli_inputs(tmp_path: Path):
    completed, capture, invocation_capture, source_before, revision = _run_verifier(
        tmp_path, EXPECTED_CLASS_NUMBERS
    )

    assert completed.returncode == 0, completed.stderr
    expected_bytes = EXPECTED_DISCOVERY_BYTES
    assert CHATBOT_FIXTURE.read_bytes() == expected_bytes
    assert capture.read_bytes() == expected_bytes
    report = json.loads(completed.stdout)
    assert report["status"] == "pass"
    assert report["verification_kind"] == "deterministic_synthetic_regression"
    assert report["cli"]["class_numbers"] == EXPECTED_CLASS_NUMBERS
    assert report["chatbot"]["class_numbers"] == EXPECTED_CLASS_NUMBERS
    assert report["oracle"]["class_numbers"] == EXPECTED_CLASS_NUMBERS
    assert report["fixture_hashes"]["discovery-multimd.json"] == _sha256(expected_bytes)
    assert set(report["fixture_hashes"]) == {
        "discovery-multimd.json",
        "sections-100.html",
        "sections-110.html",
        "sections-125.html",
    }
    assert source_before["discovery-multimd.json"] != expected_bytes
    assert report["inputs"]["cli_revision"] == revision
    invocation = json.loads(invocation_capture.read_text())
    assert invocation[:3] == ["run", "./tools/schedule-replay", "--fixtures"]
    assert Path(invocation[3]).is_absolute()
    assert Path(invocation[3]).name.startswith("schedule-completeness-replay-")
    assert invocation[3] != str(
        tmp_path / "cli-repo" / "internal" / "laccd" / "testdata" / "completeness"
    )
    assert invocation[4:] == [
        "--college",
        "LAMC",
        "--term",
        "2268",
        "--subject",
        "MULTIMD",
    ]
    source_hashes = json.loads(source_before["replay-manifest.json"])["fixture_hashes"]
    for name in ("sections-100.html", "sections-110.html", "sections-125.html"):
        assert report["cli"]["fixture_hashes"][name] == source_hashes[name]


@pytest.mark.parametrize(
    "overrides,removed_key",
    [
        ({"catalogs": "100"}, None),
        ({"catalogs": [100]}, None),
        ({"class_numbers": {"17300": True}}, None),
        ({"class_numbers": [17300]}, None),
        ({"fixture_hashes": []}, None),
        ({"fixture_hashes": {"sections-100.html": 100}}, None),
        ({}, "fixture_hashes"),
    ],
)
def test_verifier_reports_valid_json_cli_schema_errors_without_tracebacks(
    tmp_path: Path,
    overrides: dict[str, object],
    removed_key: str | None,
):
    completed, _capture, _invocation, _source_before, _revision = _run_verifier(
        tmp_path,
        EXPECTED_CLASS_NUMBERS,
        report_overrides=overrides,
        remove_report_keys=[removed_key] if removed_key else None,
    )

    assert completed.returncode == 1
    assert "Traceback" not in completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "error"
    assert "CLI schedule replay report" in report["error"]


def test_verifier_rejects_a_dirty_cli_repo_before_invocation(tmp_path: Path):
    completed, _capture, invocation, _source_before, _revision = _run_verifier(
        tmp_path, EXPECTED_CLASS_NUMBERS, dirty_cli=True
    )

    assert completed.returncode == 1
    assert not invocation.exists()
    report = json.loads(completed.stdout)
    assert report["status"] == "error"
    assert "must be clean" in report["error"]


def test_verifier_rejects_a_cli_revision_change_during_replay(tmp_path: Path):
    completed, _capture, _invocation, _source_before, revision = _run_verifier(
        tmp_path, EXPECTED_CLASS_NUMBERS, mutate_revision=True
    )

    assert completed.returncode == 1
    report = json.loads(completed.stdout)
    assert report["status"] == "error"
    assert revision in report["error"]
    assert "changed during replay" in report["error"]


def test_verifier_rejects_a_cli_tree_made_dirty_during_replay(tmp_path: Path):
    completed, _capture, _invocation, _source_before, _revision = _run_verifier(
        tmp_path, EXPECTED_CLASS_NUMBERS, mutate_dirty=True
    )

    assert completed.returncode == 1
    report = json.loads(completed.stdout)
    assert report["status"] == "error"
    assert "must be clean" in report["error"]


def test_verifier_rejects_replaced_cli_html_even_with_a_recomputed_manifest(tmp_path: Path):
    completed, _capture, invocation, _source_before, _revision = _run_verifier(
        tmp_path,
        EXPECTED_CLASS_NUMBERS,
        replace_section_and_manifest_hash=True,
    )

    assert completed.returncode == 1
    assert not invocation.exists()
    report = json.loads(completed.stdout)
    assert report["status"] == "error"
    assert "must be clean" in report["error"]


def test_verifier_rejects_a_dirty_chatbot_repo_before_invocation(tmp_path: Path):
    completed, _capture, invocation, _source_before, _revision = _run_verifier(
        tmp_path, EXPECTED_CLASS_NUMBERS, dirty_chatbot=True
    )

    assert completed.returncode == 1
    assert not invocation.exists()
    report = json.loads(completed.stdout)
    assert report["status"] == "error"
    assert "chatbot repository must be clean" in report["error"].lower()


@pytest.mark.parametrize("mutation", ["revision", "dirty"])
def test_verifier_rejects_chatbot_mutation_during_replay(
    tmp_path: Path, mutation: str
) -> None:
    completed, _capture, _invocation, _source_before, _revision = _run_verifier(
        tmp_path,
        EXPECTED_CLASS_NUMBERS,
        mutate_chatbot_revision=mutation == "revision",
        mutate_chatbot_dirty=mutation == "dirty",
    )

    assert completed.returncode == 1
    report = json.loads(completed.stdout)
    assert report["status"] == "error"
    assert "chatbot repository" in report["error"].lower()
    assert "changed during replay" in report["error"].lower() or "must be clean" in report[
        "error"
    ].lower()


@pytest.mark.parametrize("target", ["fixture", "module"])
def test_verifier_rejects_untracked_chatbot_inputs(tmp_path: Path, target: str) -> None:
    completed, _capture, invocation, _source_before, _revision = _run_verifier(
        tmp_path,
        EXPECTED_CLASS_NUMBERS,
        untracked_chatbot_fixture=target == "fixture",
        untracked_chatbot_module=target == "module",
    )

    assert completed.returncode == 1
    assert not invocation.exists()
    if target == "module":
        assert not (tmp_path / "untrusted-module-imported.txt").exists()
    report = json.loads(completed.stdout)
    assert report["status"] == "error"
    assert target in report["error"].lower()
    assert "tracked" in report["error"].lower()


@pytest.mark.parametrize("target", ["fixture", "module"])
def test_verifier_rejects_chatbot_inputs_outside_repository(tmp_path: Path, target: str) -> None:
    completed, _capture, invocation, _source_before, _revision = _run_verifier(
        tmp_path,
        EXPECTED_CLASS_NUMBERS,
        outside_chatbot_fixture=target == "fixture",
        outside_chatbot_module=target == "module",
    )

    assert completed.returncode == 1
    assert not invocation.exists()
    if target == "module":
        assert not (tmp_path / "untrusted-module-imported.txt").exists()
    report = json.loads(completed.stdout)
    assert report["status"] == "error"
    assert target in report["error"].lower()
    assert "inside" in report["error"].lower()
