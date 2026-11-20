#!/usr/bin/env python3
"""Module architecture codemap generator and validator.

Generates docs/arch/CODEMAP.md documenting modules, line counts, public symbols,
and internal dependencies. Supports --check mode for CI/linter gating.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class SymbolInfo:
    name: str
    lineno: int
    doc: str


@dataclass(slots=True)
class ModuleCodemap:
    rel_path: str
    line_count: int
    internal_imports: list[str]
    classes: list[SymbolInfo]
    functions: list[SymbolInfo]


def _first_doc_line(node: ast.AST) -> str:
    """Extract and sanitize the first line of a node's docstring."""
    doc = ast.get_docstring(node)
    if not doc:
        return ""
    first = doc.strip().splitlines()[0].strip()
    return first.replace("|", "\\|")


def _is_internal_import(mod_name: str | None, level: int) -> bool:
    """Determine whether an import statement is an internal hunter import."""
    if level > 0:
        return True
    if mod_name and (mod_name == "hunter" or mod_name.startswith("hunter.")):
        return True
    return False


def analyze_module(file_path: Path, root_dir: Path) -> ModuleCodemap | None:
    """Analyze a single module's AST for imports and public symbols."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    line_count = len(content.splitlines())
    rel_path = str(file_path.relative_to(root_dir))

    try:
        tree = ast.parse(content, filename=str(file_path))
    except SyntaxError:
        return ModuleCodemap(
            rel_path=rel_path,
            line_count=line_count,
            internal_imports=[],
            classes=[],
            functions=[],
        )

    internal_imports: list[str] = []
    classes: list[SymbolInfo] = []
    functions: list[SymbolInfo] = []

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_internal_import(alias.name, 0):
                    internal_imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if _is_internal_import(node.module, node.level):
                prefix = "." * node.level + (node.module or "")
                names = ", ".join(a.name for a in node.names)
                internal_imports.append(f"{prefix} ({names})")
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                classes.append(
                    SymbolInfo(name=node.name, lineno=node.lineno, doc=_first_doc_line(node))
                )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                functions.append(
                    SymbolInfo(name=node.name, lineno=node.lineno, doc=_first_doc_line(node))
                )

    return ModuleCodemap(
        rel_path=rel_path,
        line_count=line_count,
        internal_imports=sorted(set(internal_imports)),
        classes=classes,
        functions=functions,
    )


def generate_codemap_markdown(modules: list[ModuleCodemap]) -> str:
    """Format the list of analyzed modules into Markdown format."""
    total_files = len(modules)
    total_lines = sum(m.line_count for m in modules)

    lines: list[str] = [
        "# CODEMAP: Архитектура модулей (src/hunter)",
        "",
        "> Автоматически генерируется через `python tools/codemap.py`. Проверяется флагом `--check`.",
        "",
        f"- **Всего модулей**: {total_files}",
        f"- **Всего строк**: {total_lines}",
        "",
    ]

    for m in modules:
        lines.append(f"## `{m.rel_path}` ({m.line_count} строк)")
        lines.append("")

        if m.internal_imports:
            lines.append("**Внутренние зависимости:**")
            for imp in m.internal_imports:
                lines.append(f"- `{imp}`")
            lines.append("")

        if m.classes:
            lines.append("**Классы:**")
            for c in m.classes:
                doc_str = f" — *{c.doc}*" if c.doc else ""
                lines.append(f"- [`{c.name}`](file:///{m.rel_path}#L{c.lineno}) (L{c.lineno}){doc_str}")
            lines.append("")

        if m.functions:
            lines.append("**Публичные функции:**")
            for fn in m.functions:
                doc_str = f" — *{fn.doc}*" if fn.doc else ""
                lines.append(f"- [`{fn.name}()`](file:///{m.rel_path}#L{fn.lineno}) (L{fn.lineno}){doc_str}")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate or verify CODEMAP.md.")
    parser.add_argument("--src", type=str, default="src/hunter", help="Source directory")
    parser.add_argument("--output", type=str, default="docs/arch/CODEMAP.md", help="Output file")
    parser.add_argument("--check", action="store_true", help="Fail if CODEMAP.md is out of date")
    args = parser.parse_args()

    root_dir = Path(__file__).resolve().parent.parent
    target_dir = root_dir / args.src
    output_path = root_dir / args.output

    modules: list[ModuleCodemap] = []
    for p in sorted(target_dir.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        stat = analyze_module(p, root_dir)
        if stat is not None:
            modules.append(stat)

    generated = generate_codemap_markdown(modules)

    if args.check:
        if not output_path.exists():
            print(f"ERROR: {output_path} does not exist. Run 'python tools/codemap.py' to generate it.", file=sys.stderr)
            return 1
        existing = output_path.read_text(encoding="utf-8")
        if existing != generated:
            print("ERROR: CODEMAP.md is stale. Diff:", file=sys.stderr)
            diff = difflib.unified_diff(
                existing.splitlines(keepends=True),
                generated.splitlines(keepends=True),
                fromfile="existing CODEMAP.md",
                tofile="generated CODEMAP.md",
            )
            sys.stderr.writelines(diff)
            print("\nRun 'python tools/codemap.py' to update.", file=sys.stderr)
            return 1
        print("CODEMAP.md is up to date.")
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(generated, encoding="utf-8")
    print(f"Generated {output_path.relative_to(root_dir)} ({len(modules)} modules).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
