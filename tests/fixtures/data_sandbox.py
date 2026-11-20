"""Create a disposable test data directory before pytest collects tests."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory


def create_test_data_sandbox(root: Path) -> TemporaryDirectory[str]:
    """Copy only tracked product fixtures, never a database or untracked user data."""
    sandbox = TemporaryDirectory(prefix="avito-hunter-pytest-")
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "-z", "--", "data/products"],
            cwd=root, check=True, capture_output=True,
        ).stdout.split(b"\0")
        product_root = root / "data/products"
        files = [Path(os.fsdecode(item)) for item in tracked if item]
        if not files or product_root.is_symlink():
            raise RuntimeError("Tracked product fixtures are unavailable")
        for relative in files:
            source = root / relative
            product_path = source.relative_to(product_root)
            components = (
                product_root.joinpath(*product_path.parts[:index])
                for index in range(1, len(product_path.parts) + 1)
            )
            if any(path.is_symlink() for path in components) or not source.resolve().is_relative_to(
                product_root.resolve()
            ):
                raise RuntimeError("Unsafe product fixture path")
            destination = Path(sandbox.name) / "products" / product_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    except BaseException:
        sandbox.cleanup()
        raise
    return sandbox
