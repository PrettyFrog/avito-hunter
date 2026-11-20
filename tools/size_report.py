#!/usr/bin/env python3
"""Codebase size baseline generator.

Reports files > 400 lines and functions > 60 lines, and top-20 largest files and functions.
"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(slots=True)
class FuncStat:
    name: str
    file_path: str
    start_line: int
    end_line: int
    line_count: int


@dataclass(slots=True)
class FileStat:
    file_path: str
    line_count: int
    functions: list[FuncStat]


def analyze_functions(tree: ast.AST, rel_path: str) -> list[FuncStat]:
    """Extract functions and methods with their line spans from AST."""
    funcs: list[FuncStat] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._record_func(node)
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._record_func(node)
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def _record_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            end_line = getattr(node, "end_lineno", node.lineno)
            line_count = end_line - node.lineno + 1
            qualified = ".".join([*self.scope, node.name]) if self.scope else node.name
            funcs.append(
                FuncStat(
                    name=qualified,
                    file_path=rel_path,
                    start_line=node.lineno,
                    end_line=end_line,
                    line_count=line_count,
                )
            )

    Visitor().visit(tree)
    return funcs


def analyze_file(file_path: Path, root_dir: Path) -> FileStat | None:
    """Analyze a single Python file for line counts and function spans."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    line_count = len(content.splitlines())
    rel_path = str(file_path.relative_to(root_dir))

    try:
        tree = ast.parse(content, filename=str(file_path))
        funcs = analyze_functions(tree, rel_path)
    except SyntaxError:
        funcs = []

    return FileStat(file_path=rel_path, line_count=line_count, functions=funcs)


def scan_directory(target_dir: Path, root_dir: Path) -> list[FileStat]:
    """Scan directory recursively for all .py files."""
    results: list[FileStat] = []
    for p in sorted(target_dir.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        stat = analyze_file(p, root_dir)
        if stat is not None:
            results.append(stat)
    return results


def _format_files_tables(sorted_files: list[FileStat], max_file_lines: int) -> list[str]:
    """Format top files and overflow files tables."""
    lines: list[str] = [
        "## Топ-20 файлов по размеру",
        "",
        "| № | Файл | Строк | Статус |",
        "|---|------|-------|--------|",
    ]
    for i, f in enumerate(sorted_files[:20], 1):
        status = "⚠️ Превышен" if f.line_count > max_file_lines else "✅ В норме"
        lines.append(f"| {i} | `{f.file_path}` | {f.line_count} | {status} |")
    lines.append("")

    overflow_files = [f for f in sorted_files if f.line_count > max_file_lines]
    lines.extend([
        f"## Файлы > {max_file_lines} строк ({len(overflow_files)} шт.)",
        "",
        "| № | Файл | Строк | Превышение |",
        "|---|------|-------|------------|",
    ])
    for i, f in enumerate(overflow_files, 1):
        excess = f.line_count - max_file_lines
        lines.append(f"| {i} | `{f.file_path}` | {f.line_count} | +{excess} |")
    lines.append("")
    return lines


def _format_funcs_tables(sorted_funcs: list[FuncStat], max_func_lines: int) -> list[str]:
    """Format top functions and overflow functions tables."""
    lines: list[str] = [
        "## Топ-20 функций по размеру",
        "",
        "| № | Функция / Метод | Файл | Строки | Размер |",
        "|---|-----------------|------|--------|--------|",
    ]
    for i, fn in enumerate(sorted_funcs[:20], 1):
        lines.append(
            f"| {i} | `{fn.name}` | `{fn.file_path}` | {fn.start_line}–{fn.end_line} | {fn.line_count} |"
        )
    lines.append("")

    overflow_funcs = [fn for fn in sorted_funcs if fn.line_count > max_func_lines]
    lines.extend([
        f"## Функции > {max_func_lines} строк ({len(overflow_funcs)} шт.)",
        "",
        "| № | Функция / Метод | Файл | Строки | Размер | Превышение |",
        "|---|-----------------|------|--------|--------|------------|",
    ])
    for i, fn in enumerate(overflow_funcs, 1):
        excess = fn.line_count - max_func_lines
        lines.append(
            f"| {i} | `{fn.name}` | `{fn.file_path}` | {fn.start_line}–{fn.end_line} | {fn.line_count} | +{excess} |"
        )
    lines.append("")
    return lines


def format_markdown(
    file_stats: list[FileStat],
    all_funcs: list[FuncStat],
    max_file_lines: int = 400,
    max_func_lines: int = 60,
) -> str:
    """Generate Markdown report."""
    total_files = len(file_stats)
    total_lines = sum(f.line_count for f in file_stats)
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines: list[str] = [
        "# Baseline размера кодовой базы (src/hunter)",
        "",
        f"- Сгенерировано: {now}",
        f"- Всего файлов: {total_files}",
        f"- Всего строк: {total_lines}",
        f"- Лимит на файл: {max_file_lines} строк",
        f"- Лимит на функцию: {max_func_lines} строк",
        "",
    ]
    sorted_files = sorted(file_stats, key=lambda f: f.line_count, reverse=True)
    lines.extend(_format_files_tables(sorted_files, max_file_lines))

    sorted_funcs = sorted(all_funcs, key=lambda fn: fn.line_count, reverse=True)
    lines.extend(_format_funcs_tables(sorted_funcs, max_func_lines))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate codebase size baseline report.")
    parser.add_argument("--src", type=str, default="src/hunter", help="Path to source directory")
    parser.add_argument("--output", type=str, default="docs/arch/SIZE_BASELINE.md", help="Output file")
    parser.add_argument("--json", type=str, default=None, help="Output JSON baseline file")
    args = parser.parse_args()

    root_dir = Path(__file__).resolve().parent.parent
    target_dir = root_dir / args.src
    output_path = root_dir / args.output

    file_stats = scan_directory(target_dir, root_dir)
    all_funcs: list[FuncStat] = []
    for f in file_stats:
        all_funcs.extend(f.functions)

    report_md = format_markdown(file_stats, all_funcs)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report_md, encoding="utf-8")

    overflow_files = [f for f in file_stats if f.line_count > 400]
    overflow_funcs = [fn for fn in all_funcs if fn.line_count > 60]

    if args.json:
        json_path = root_dir / args.json
        data = {
            "max_file_lines": 400,
            "max_func_lines": 60,
            "baseline_files": {f.file_path: f.line_count for f in overflow_files},
            "baseline_functions": {f"{fn.file_path}::{fn.name}": fn.line_count for fn in overflow_funcs},
        }
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"JSON baseline written to: {json_path.relative_to(root_dir)}")

    print(f"Scanned {len(file_stats)} files ({sum(f.line_count for f in file_stats)} lines)")
    print(f"Files > 400 lines: {len(overflow_files)}")
    print(f"Functions > 60 lines: {len(overflow_funcs)}")
    print(f"Report written to: {output_path.relative_to(root_dir)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
