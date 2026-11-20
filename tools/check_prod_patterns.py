#!/usr/bin/env python3
"""Reject unsafe production branches and untracked test exceptions."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IDENTITY_FIELDS = {
    "chat_id",
    "owner_id",
    "recipient_id",
    "requester_id",
    "tenant_id",
    "telegram_chat_id",
    "telegram_user_id",
    "user_id",
}
MOCK_TYPES = {"AsyncMock", "MagicMock", "Mock", "NonCallableMock"}
TEST_MARKERS = {"skip", "skipif", "xfail"}
ISSUE_RE = re.compile(r"\bISSUE:[A-Z0-9][A-Z0-9_-]*\b")
EXPIRY_RE = re.compile(r"\bEXPIRES:(\d{4}-\d{2}-\d{2})\b")
REVIEW_RE = re.compile(r"\bREVIEW:[A-Za-z0-9_.-]+\b")
SECRET_PATTERNS = (
    re.compile(r"\b\d{8,}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|bot[_-]?token|client[_-]?secret|password|secret)"
        r"\b\s*[:=]\s*[\"']?(?!test(?:[-_.]|\b)|fake(?:[-_.]|\b)|dummy(?:[-_.]|\b)|"
        r"example(?:[-_.]|\b)|placeholder(?:[-_.]|\b)|redacted(?:[-_.]|\b))"
        r"[A-Za-z0-9/+=_-]{24,}"
    ),
    re.compile(r"https?://[^\s/@:]+:[^\s/@]{16,}@"),
)
TEXT_SUFFIXES = {
    ".cfg",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".service",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    message: str


def _secret_findings(path: Path, root: Path) -> list[Finding]:
    rel = str(path.relative_to(root))
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [Finding(rel, 1, f"cannot inspect text for secrets: {exc}")]
    findings: list[Finding] = []
    for line_no, line in enumerate(content.splitlines(), 1):
        if any(pattern.search(line) for pattern in SECRET_PATTERNS):
            findings.append(Finding(rel, line_no, "possible embedded secret; value redacted"))
    return findings


def _tracked_text_paths(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=False
        )
    except OSError:
        result = None
    if result is not None and result.returncode == 0:
        paths = [root / item for item in result.stdout.decode().split("\0") if item]
        return [
            path
            for path in paths
            if path.suffix in TEXT_SUFFIXES or path.name.startswith(".env")
        ]
    candidates = list((root / "src/hunter").rglob("*.py"))
    candidates.extend((root / "tests").rglob("*.py"))
    candidates.extend((root / "ops").rglob("*.sh"))
    return candidates


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _mock_names(tree: ast.Module) -> set[str]:
    names = set(MOCK_TYPES)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "unittest.mock":
            names.update(
                alias.asname or alias.name for alias in node.names if alias.name in MOCK_TYPES
            )
    return names


def _is_mock_check(node: ast.AST, names: set[str]) -> bool:
    if isinstance(node, ast.Call) and _name(node.func) == "isinstance" and len(node.args) > 1:
        return any(_name(child) in names for child in ast.walk(node.args[1]))
    return any(
        isinstance(child, ast.Attribute) and child.attr.startswith("_mock_")
        for child in ast.walk(node)
    )


def _literal_id(node: ast.AST) -> bool:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError):
        return False
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int):
        return value != 0
    return isinstance(value, str) and bool(re.fullmatch(r"-?\d{2,}", value))


def _identity_name(name: str | None) -> bool:
    if not name:
        return False
    parts = name.lower().split("_")
    return name.lower() in IDENTITY_FIELDS or (
        parts[-1] == "id"
        and bool(
            set(parts[:-1])
            & {"user", "chat", "tenant", "owner", "admin", "recipient", "requester", "principal"}
        )
    )


def _targets_identity(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and _literal_id(node)


def _identity_violations(tree: ast.Module, path: str) -> list[Finding]:
    findings: list[Finding] = []
    for node in ast.walk(tree):
        targets: set[str] = set()
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets = {name for target in node.targets if (name := _name(target))}
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = {_name(node.target)} if _name(node.target) else set()
            value = node.value
        elif isinstance(node, ast.keyword) and _identity_name(node.arg):
            targets = {node.arg}
            value = node.value
        elif isinstance(node, ast.Dict):
            for key, item in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and _identity_name(str(key.value)):
                    if _targets_identity(item):
                        findings.append(Finding(path, item.lineno, "hardcoded identity ID"))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defaults = node.args.defaults
            positional = node.args.args[-len(defaults) :] if defaults else []
            named_defaults = list(zip(positional, defaults, strict=True)) + list(
                zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True)
            )
            for arg, default in named_defaults:
                if default is not None and _identity_name(arg.arg) and _targets_identity(default):
                    findings.append(Finding(path, default.lineno, "hardcoded identity ID"))
        if (
            any(_identity_name(target) for target in targets)
            and value is not None
            and _targets_identity(value)
        ):
            findings.append(Finding(path, value.lineno, "hardcoded identity ID"))
    return findings


def _production_findings(path: Path, root: Path) -> list[Finding]:
    rel = str(path.relative_to(root))
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    except (OSError, SyntaxError) as exc:
        return [Finding(rel, 1, f"cannot inspect production source: {exc}")]
    findings = _identity_violations(tree, rel)
    findings.extend(_secret_findings(path, root))
    mock_names = _mock_names(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_mock_check(node.test, mock_names):
            findings.append(Finding(rel, node.lineno, "production branch depends on a mock"))
        if isinstance(node, ast.ExceptHandler):
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                message = "bare except: pass" if node.type is None else "typed except: pass"
                findings.append(Finding(rel, node.lineno, message))
    return findings


def _reason(call: ast.Call) -> str:
    for keyword in call.keywords:
        if keyword.arg == "reason" and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
    if _name(call.func) == "skip" and call.args and isinstance(call.args[0], ast.Constant):
        return str(call.args[0].value)
    return ""


def _pytest_marker(call: ast.Call) -> str | None:
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr == "skip" and isinstance(func.value, ast.Name) and func.value.id == "pytest":
        return "skip"
    marker = func.value
    if (
        func.attr in TEST_MARKERS
        and isinstance(marker, ast.Attribute)
        and marker.attr == "mark"
        and isinstance(marker.value, ast.Name)
        and marker.value.id == "pytest"
    ):
        return func.attr
    return None


def _metadata_error(reason: str) -> str | None:
    expiry = EXPIRY_RE.search(reason)
    if not ISSUE_RE.search(reason) or not expiry or not REVIEW_RE.search(reason):
        return "skip/xfail needs ISSUE, future EXPIRES, and REVIEW metadata"
    try:
        if date.fromisoformat(expiry.group(1)) <= date.today():
            return "skip/xfail expiry is not in the future"
    except ValueError:
        return "skip/xfail expiry is invalid"
    return None


def _test_findings(path: Path, root: Path) -> list[Finding]:
    rel = str(path.relative_to(root))
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    except (OSError, SyntaxError) as exc:
        return [Finding(rel, 1, f"cannot inspect test source: {exc}")]
    findings = _secret_findings(path, root)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        marker = _pytest_marker(node)
        if marker is None:
            continue
        reason_error = _metadata_error(_reason(node))
        if reason_error:
            findings.append(Finding(rel, node.lineno, reason_error))
        if marker == "xfail" and not any(
            keyword.arg == "strict"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        ):
            findings.append(Finding(rel, node.lineno, "xfail must set strict=True"))
    return findings


def scan_project(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    source_root = root / "src/hunter"
    tests_root = root / "tests"
    if not source_root.is_dir() or not tests_root.is_dir():
        return [Finding(".", 1, "expected src/hunter and tests directories")]
    for path in sorted(source_root.rglob("*.py")):
        findings.extend(_production_findings(path, root))
    for path in sorted(tests_root.rglob("*.py")):
        findings.extend(_test_findings(path, root))
    scanned = {str(path.resolve()) for path in source_root.rglob("*.py")}
    scanned.update(str(path.resolve()) for path in tests_root.rglob("*.py"))
    for path in _tracked_text_paths(root):
        if str(path.resolve()) not in scanned:
            findings.extend(_secret_findings(path, root))
    deploy = root / "ops/deploy.sh"
    try:
        content = deploy.read_text(encoding="utf-8")
    except OSError as exc:
        findings.append(Finding("ops/deploy.sh", 1, f"cannot inspect deploy script: {exc}"))
    else:
        for line_no, line in enumerate(content.splitlines(), 1):
            if "--skip-tests" in line or "SKIP_TESTS" in line:
                findings.append(
                    Finding("ops/deploy.sh", line_no, "deployment test bypass is forbidden")
                )
    return findings


def main() -> int:
    findings = scan_project(ROOT)
    for finding in findings:
        print(f"{finding.path}:{finding.line}: {finding.message}", file=sys.stderr)
    if findings:
        print(f"production pattern gate failed: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    print("production pattern gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
