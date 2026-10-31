"""Tests for the public-repo hygiene gate.

Derived strictly from ``specs/public-hygiene-gate-brief.md`` (AC-1, AC-2, AC-3).
Nothing here is derived from the scanner implementation.

Surface used: the CLI (``python scripts/check_public_hygiene.py`` run with the
scan root as cwd). The brief names no Python function, so driving the exit code
+ stdout keeps the tests free of an invented API. A missing script raises
``ImportError`` *inside* each test, so RED is a per-test runtime failure, never a
collection error (same lazy-module convention as ``tests/test_sandbox.py``).

This file is itself git-tracked and therefore scanned by the gate. Every
planted private marker is assembled at runtime by ``frag()`` so that no marker
appears literally in this source.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCANNER = REPO / "scripts" / "check_public_hygiene.py"
WORKFLOW = REPO / ".github" / "workflows" / "hygiene.yml"

_CRASH = "Traceback (most recent call last)"


def frag(*parts: str) -> str:
    """Join marker fragments at runtime (keeps marker text out of this file)."""
    return "".join(parts)


# --- marker fixtures (all assembled at runtime) ----------------------------
HOME_LEAK = frag("/home/z", "eta/machine", "/data/timeline.db")
LAN_192 = frag("192", ".168.9.9")
LAN_10 = frag("10", ".20.30.40")
LAN_172 = frag("172", ".19.4.5")
EMAIL_LEAK = frag("af", "details@example.invalid")
USERNAME = frag("sp", "aro")
KEY_SK = frag("sk", "-", "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789")
KEY_OC = frag("oc", "aB3dE5fG7hJ9kL1mN3pQ5rS7tU")
KEY_ST = frag("st", "_9sT3dE5fG7hJ9kL")

# allowed placeholders / innocuous numbers the brief says must NOT be flagged
ALLOWED = [
    "/home/x/data/timeline.db",
    "/home/user/work",
    "/home/username/work",
    "/home/you/work",
    "version 10.5",
    "python 3.12.3",
    "/usr/local/bin",
    "/etc/passwd",
]


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _require_scanner() -> None:
    if not SCANNER.is_file():
        raise ImportError(
            f"MISSING_BEHAVIOR: {SCANNER.relative_to(REPO).as_posix()} does not exist"
        )


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    _require_scanner()
    return subprocess.run(
        [sys.executable, str(SCANNER), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=180,
    )


def _mixed(proc: subprocess.CompletedProcess) -> str:
    return (proc.stdout or "") + (proc.stderr or "")


def _git(cwd: Path, *args: str) -> None:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr.strip()}"


def _tracked_pairs(out: str) -> set[tuple[str, int]]:
    """(basename, line) pairs parsed out of ``path:line`` occurrences."""
    pairs = set()
    for match in re.finditer(r"([A-Za-z0-9_.@+\-/\\]+):(\d+)", out):
        base = match.group(1).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        pairs.add((base, int(match.group(2))))
    return pairs


def _stray_paths(out: str, tree: Path) -> list[str]:
    """Reported files that are not inside ``tree``: the scanner scanned elsewhere."""
    names = set()
    proc = subprocess.run(
        ["git", "ls-files", "-z"], cwd=str(tree), capture_output=True, text=True
    )
    if proc.returncode == 0:
        names = {Path(n).name for n in proc.stdout.split("\0") if n}
    stray = []
    for match in re.finditer(r"([A-Za-z0-9_.@+\-/\\]+):(\d+)", out):
        base = match.group(1).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if base not in names:
            stray.append(match.group(1))
    return stray


def _check_not_crashed(proc: subprocess.CompletedProcess) -> None:
    if _CRASH in _mixed(proc):
        pytest.fail(f"MISSING_BEHAVIOR: scanner crashed:\n{_mixed(proc)[:800]}")


@pytest.fixture
def scan(tmp_path):
    """Run the scanner over a temp git tree and return the completed process."""
    default = tmp_path / "tree"

    def _scan(tree: Path | None = None) -> subprocess.CompletedProcess:
        root = default if tree is None else tree
        proc = _run(root)
        _check_not_crashed(proc)
        if proc.returncode in (0, 1) and not _stray_paths(_mixed(proc), root):
            return proc
        # Implementation defaulted to its own checkout: hand it the tree explicitly.
        alt = _run(root, str(root))
        if alt.returncode in (0, 1) and not _stray_paths(_mixed(alt), root):
            _check_not_crashed(alt)
            return alt
        return proc

    return _scan


def write_tree(tree: Path, files: dict[str, str | bytes]) -> Path:
    """Create ``tree`` with ``files`` and stage them all (state: tracked)."""
    tree.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            path.write_bytes(body)
        else:
            path.write_text(body, encoding="utf-8")
    if not (tree / ".git").exists():
        _git(tree, "init", "-q")
    _git(tree, "add", "-A", "-f")
    return tree


def make_tree(tmp_path: Path, files: dict[str, str | bytes], name: str = "tree") -> Path:
    return write_tree(tmp_path / name, files)


def rc(proc: subprocess.CompletedProcess) -> int:
    return proc.returncode


def output(proc: subprocess.CompletedProcess) -> str:
    return _mixed(proc)


# ===========================================================================
# AC-1: planted leak of each marker class -> detected, with the right line
# ===========================================================================

LEAK_CASES = {
    "home_path": f"open {HOME_LEAK} for writing",
    "username_word": f"chown -R {USERNAME}:{USERNAME} /srv/out",
    "lan_192": f"lan endpoint {LAN_192} is the box",
    "lan_10": f"lan endpoint {LAN_10} is the box",
    "lan_172": f"lan endpoint {LAN_172} is the box",
    "email": f"mail {EMAIL_LEAK} for access",
    "key_sk": f"export TOKEN={KEY_SK}",
    "key_oc": f"export TOKEN={KEY_OC}",
    "key_st": f"export TOKEN={KEY_ST}",
}


@pytest.mark.parametrize("marker_id", sorted(LEAK_CASES))
def test_ac1_detects_each_marker_class_on_its_line(marker_id, scan, tmp_path):
    body = "line one is innocuous\nline two is innocuous\n\n" + LEAK_CASES[marker_id] + "\n"
    name = f"{marker_id}.txt"
    make_tree(tmp_path, {name: body})
    proc = scan()
    out = output(proc)
    assert rc(proc) == 1, f"{marker_id} not detected (rc={rc(proc)}): {out[:400]}"
    assert (name, 4) in _tracked_pairs(out), f"wrong file:line for {marker_id}: {out[:400]}"


def test_ac1_reports_every_leak_with_correct_lines(scan, tmp_path):
    lines = [
        f"first {HOME_LEAK}",
        "innocuous middle one",
        "innocuous middle two",
        f"third {LAN_192}",
        f"fourth {EMAIL_LEAK}",
        "innocuous tail",
    ]
    make_tree(tmp_path, {"many.md": "\n".join(lines) + "\n"})
    proc = scan()
    out = output(proc)
    assert rc(proc) == 1, out[:400]
    reported = {line for base, line in _tracked_pairs(out) if base == "many.md"}
    assert reported == {1, 4, 5}, f"expected one match line per leak, got {reported}: {out[:400]}"


def test_ac1_prints_marker_type_and_bounded_reveal(scan, tmp_path):
    cases = {"home.txt": HOME_LEAK, "ip.txt": LAN_192, "key.txt": KEY_SK}
    make_tree(tmp_path, {name: f"see {leak} here\n" for name, leak in cases.items()})
    proc = scan()
    out = output(proc)
    assert rc(proc) == 1, out[:400]
    for name, planted in cases.items():
        assert name in out, f"no match line for {name}: {out[:400]}"
        assert planted not in out, f"full {name} secret printed: {out[:400]}"
        if len(planted) > 8:
            assert planted[:8] not in out, f"too many leading chars of {name}: {out[:400]}"
        tail = "\n".join(
            line.split(":", 2)[-1]
            for line in out.splitlines()
            if name in line
        )
        words = [
            word
            for word in re.findall(r"[A-Za-z]{3,}", tail)
            if word.lower() not in planted.lower()
        ]
        assert words, f"no marker-type label on the match line for {name}: {tail[:200]}"


def test_ac1_never_prints_the_whole_line(scan, tmp_path):
    before, after = "benign-left-context", "benign-right-context"
    make_tree(tmp_path, {"line.txt": f"{before} {HOME_LEAK} {after}\n"})
    proc = scan()
    out = output(proc)
    assert rc(proc) == 1, out[:400]
    assert HOME_LEAK not in out
    assert after not in out, "the full line was printed instead of the redacted match"


def test_ac1_allowed_placeholders_and_version_numbers_are_not_flagged(scan, tmp_path):
    for marker in ALLOWED:
        assert not re.search(r"[/.]" + USERNAME, marker)
    body = "\n".join(f"example: {text}" for text in ALLOWED) + "\n"
    make_tree(tmp_path, {"allowed.md": body})
    proc = scan()
    out = output(proc)
    assert rc(proc) == 0, f"allowed text was flagged: {out[:400]}"


def test_ac1_markers_inside_larger_tokens_are_not_flagged(scan, tmp_path):
    # "token matches" in the brief: a key prefix must start a token, a dotted
    # quad must be a whole address -- substrings of longer words are not keys.
    embedded = [
        f"file {frag('ba', 'sk', '-', 'et')} in the pantry",
        f"column {frag('fir', 'st', '_', 'name')} in the schema",
        f"{frag('block', 'c', 'hain')}{KEY_OC[2:]}",  # 'oc' + base62, not at a token start
        f"subnet {frag('11', '92.168.9.9')}",  # quad embedded in a longer token
    ]
    make_tree(tmp_path, {"words.md": "\n".join(embedded) + "\n"})
    proc = scan()
    out = output(proc)
    assert rc(proc) == 0, f"embedded substrings were flagged: {out[:400]}"


def test_ac1_binary_files_are_skipped(scan, tmp_path):
    blob = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00"
    blob += (HOME_LEAK + "\n").encode()
    blob += b"\x00\xff\xfe binary tail"
    make_tree(tmp_path, {"image.bin": blob, "text.md": f"see {EMAIL_LEAK}\n"})
    proc = scan()
    out = output(proc)
    assert rc(proc) == 1, f"text leak in the same tree was not detected: {out[:400]}"
    assert ("text.md", 1) in _tracked_pairs(out), out[:400]
    assert "image.bin" not in out, f"binary file was scanned: {out[:400]}"


def test_ac1_untracked_local_only_files_are_never_scanned(scan, tmp_path):
    tree = make_tree(tmp_path, {"clean.md": "nothing private here\n"})
    (tree / ".env").write_text(f"TOKEN={KEY_SK}\nPATH={HOME_LEAK}\n", encoding="utf-8")
    ignore = tree / ".gitignore"
    ignore.write_text(".env\n", encoding="utf-8")
    _git(tree, "add", "-f", ".gitignore")
    proc = scan()
    out = output(proc)
    assert rc(proc) == 0, f"an untracked/local-only file was scanned: {out[:400]}"


def test_ac1_clean_tree_exits_zero(scan, tmp_path):
    make_tree(
        tmp_path,
        {"README.md": "generic prose\n", "src/app.py": "print('hi')\n"},
    )
    proc = scan()
    assert rc(proc) == 0, f"clean tree was flagged: {output(proc)[:400]}"


def test_ac1_empty_scan_is_an_error_never_a_pass(scan, tmp_path):
    # A git repo root with zero tracked files: the guard must fail loudly.
    make_tree(tmp_path, {})
    proc = scan()
    out = output(proc)
    assert rc(proc) == 1, (
        "a scan of zero tracked files returned rc="
        f"{rc(proc)} (the brief requires exit 1 with an error): {out[:400]}"
    )
    assert out.strip(), "empty-scan guard produced no error output"
    assert re.search(r"(?i)error|fail|empty|no tracked|zero", out), out[:400]


# ===========================================================================
# AC-2: the real repo is scrubbed of every marker class
# ===========================================================================


def _scanned_count(out: str) -> int | None:
    """The number of files the scanner reports having scanned (if any)."""
    patterns = (
        r"(?i)(\d+)\s+(?:git[- ]?tracked\s+)?files?\b",
        r"(?i)(?:scanned|checked|tracked)\D{0,24}(\d+)",
        r"(?i)(\d+)\D{0,24}(?:files?|paths?|tracked|scanned)",
    )
    for pattern in patterns:
        found = re.search(pattern, out)
        if found:
            return int(found.group(1))
    numbers = [int(n) for n in re.findall(r"\b\d+\b", out)]
    return max(numbers) if numbers else None


def test_ac2_repo_scan_is_clean_and_reports_a_non_empty_count():
    _require_scanner()
    proc = _run(REPO)
    out = _mixed(proc)
    _check_not_crashed(proc)
    assert rc(proc) == 0, f"tracked files still contain private markers:\n{out[:1200]}"
    pairs = _tracked_pairs(out)
    assert not pairs, f"clean run still reported match lines: {sorted(pairs)[:10]}"
    count = _scanned_count(out)
    assert count is not None, f"scanner does not report a scanned-file count: {out[:400]}"
    assert count > 0, f"scanned-file count is empty ({count}): {out[:400]}"


# ===========================================================================
# AC-3: CI gate -- .github/workflows/hygiene.yml
# ===========================================================================

# PyYAML is not installed in .venv, so AC-3's "the YAML parses (PyYAML)" check
# runs through PyYAML when importable and otherwise through the strict
# block-YAML subset reader below (mappings, sequences, scalars, flow seq/map,
# quotes, comments). Both paths produce the same assertions.


def _split_key(text: str) -> tuple[str, str] | None:
    """Split ``key: value`` at the first colon that ends the key."""
    for index, char in enumerate(text):
        if char != ":":
            continue
        if index + 1 == len(text) or text[index + 1] in " \t":
            return text[:index].strip(), text[index + 1 :].strip()
    return None


def _scalar(text: str) -> object:
    text = text.strip()
    if not text:
        return {}
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [_scalar(part) for part in inner.split(",")] if inner else []
    if text.startswith("{") and text.endswith("}"):
        inner = text[1:-1].strip()
        out: dict[str, object] = {}
        if inner:
            for part in inner.split(","):
                pair = _split_key(part.strip())
                assert pair is not None, f"unparsable flow mapping entry: {part!r}"
                out[pair[0]] = _scalar(pair[1])
        return out
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    if text in ("null", "~"):
        return None
    return text


def _mini_yaml(text: str) -> object:
    rows: list[tuple[int, str]] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        rows.append((len(raw) - len(raw.lstrip(" ")), stripped))

    def parse(index: int, indent: int) -> tuple[object, int]:
        if index >= len(rows):
            return {}, index
        if rows[index][1].startswith("- "):
            return parse_sequence(index, indent)
        return parse_mapping(index, indent)

    def parse_sequence(index: int, indent: int) -> tuple[list, int]:
        items: list = []
        while index < len(rows):
            level, text_ = rows[index]
            if level != indent or not text_.startswith("- "):
                break
            body = text_[2:].strip()
            pair = None if body.startswith(("'", '"')) else _split_key(body)
            if pair is None:
                items.append(_scalar(body))
                index += 1
                continue
            # `- key: value` opens an inline mapping whose keys align at indent+2
            rows[index] = (indent + 2, body)
            value, index = parse_mapping(index, indent + 2)
            items.append(value)
        return items, index

    def parse_mapping(index: int, indent: int) -> tuple[dict, int]:
        mapping: dict = {}
        while index < len(rows):
            level, text_ = rows[index]
            if level != indent or text_.startswith("- "):
                break
            pair = _split_key(text_)
            assert pair is not None, f"unparsable YAML line: {text_!r}"
            key, rest = pair
            index += 1
            if rest:
                mapping[key] = _scalar(rest)
                continue
            if index < len(rows) and rows[index][0] > indent:
                mapping[key], index = parse(index, rows[index][0])
            elif index < len(rows) and rows[index][0] == indent and rows[index][1].startswith("- "):
                mapping[key], index = parse_sequence(index, indent)
            else:
                mapping[key] = {}
        return mapping, index

    value, _ = parse(0, rows[0][0] if rows else 0)
    return value


def _normalise_on_key(doc: dict) -> dict:
    # PyYAML resolves bare `on:` as boolean True (YAML 1.1)
    if True in doc and "on" not in doc:
        doc["on"] = doc.pop(True)
    return doc


def _workflow() -> dict:
    if not WORKFLOW.is_file():
        pytest.fail(f"MISSING_BEHAVIOR: {WORKFLOW.relative_to(REPO).as_posix()} does not exist")
    text = WORKFLOW.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        doc = _mini_yaml(text)
    else:
        doc = yaml.safe_load(text)
    assert isinstance(doc, dict), f"workflow did not parse to a mapping: {doc!r}"
    return _normalise_on_key(doc)


def _triggers(doc: dict) -> list[str]:
    on = doc.get("on")
    if isinstance(on, str):
        return [on]
    if isinstance(on, list):
        return [str(item) for item in on]
    if isinstance(on, dict):
        return [str(key).strip("'\"") for key in on]
    return []


def _steps(doc: dict) -> list[dict]:
    steps: list[dict] = []
    jobs = doc.get("jobs") or {}
    assert isinstance(jobs, dict) and jobs, f"workflow declares no jobs: {list(doc)}"
    for job in jobs.values():
        if isinstance(job, dict):
            for step in job.get("steps") or []:
                if isinstance(step, dict):
                    steps.append(step)
    return steps


def test_ac3_workflow_exists_and_parses():
    doc = _workflow()
    assert "jobs" in doc, f"workflow has no jobs key: {sorted(doc)}"
    assert _steps(doc), "workflow declares no steps"


def test_ac3_workflow_triggers_on_push_and_pull_request():
    triggers = _triggers(_workflow())
    assert "push" in triggers, f"missing push trigger: {triggers}"
    assert "pull_request" in triggers, f"missing pull_request trigger: {triggers}"


def test_ac3_workflow_runs_the_hygiene_scanner():
    script = SCANNER.relative_to(REPO).as_posix()
    runs = [step.get("run") for step in _steps(_workflow())]
    commands = [str(run) for run in runs if isinstance(run, str)]
    matching = [cmd for cmd in commands if script in cmd]
    assert matching, f"no step runs {script}: {commands}"
    assert any(re.search(r"(?i)\bpython3?\b", cmd) for cmd in matching), (
        f"the scanner step does not run it through python: {matching}"
    )


def test_ac3_workflow_fails_the_job_on_any_match():
    doc = _workflow()
    text = WORKFLOW.read_text(encoding="utf-8")
    for step in _steps(doc):
        assert not step.get("continue-on-error"), f"step tolerates failure: {step}"
    assert "continue-on-error" not in text
    assert "|| true" not in text, "a failing scanner command is masked with `|| true`"


def test_ac3_workflow_sets_up_python_312():
    versions = []
    for step in _steps(_workflow()):
        if str(step.get("uses", "")).startswith("actions/setup-python"):
            with_ = step.get("with") or {}
            assert isinstance(with_, dict), f"setup-python has no `with`: {step}"
            value = with_.get("python-version")
            versions.append(str(value) if value is not None else "")
    assert versions, "workflow does not use actions/setup-python"
    assert any("3.12" in value for value in versions), f"python-version not 3.12: {versions}"
