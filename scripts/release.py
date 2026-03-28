#!/usr/bin/env python3
"""
Release script — bumps version in pyproject.toml, updates uv.lock, commits, tags, and pushes.

Usage:
    scripts/release.py              # patch bump: 1.1.1 -> 1.1.2
    scripts/release.py patch        # patch bump: 1.1.1 -> 1.1.2
    scripts/release.py minor        # minor bump: 1.1.1 -> 1.2.0
    scripts/release.py major        # major bump: 1.1.1 -> 2.0.0
    scripts/release.py 1.2.3        # explicit version
"""

import re
import subprocess
import sys
from pathlib import Path

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"
VERSION_RE = re.compile(r'^version = "(\d+)\.(\d+)\.(\d+)"', re.MULTILINE)
WARN_THRESHOLD = 10  # warn if any version component jumps by more than this


def read_current_version() -> tuple[int, int, int]:
    content = PYPROJECT.read_text()
    m = VERSION_RE.search(content)
    if not m:
        sys.exit("ERROR: could not find version in pyproject.toml")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def bump(current: tuple[int, int, int], part: str) -> tuple[int, int, int]:
    major, minor, patch = current
    if part == "major":
        return major + 1, 0, 0
    if part == "minor":
        return major, minor + 1, 0
    return major, minor, patch + 1


def parse_explicit(version: str) -> tuple[int, int, int]:
    version = version.lstrip("v")
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        sys.exit(f"ERROR: invalid version '{version}' — expected x.y.z")
    return int(parts[0]), int(parts[1]), int(parts[2])


def warn_if_large_jump(current: tuple[int, int, int], new: tuple[int, int, int]) -> None:
    for label, c, n in zip(("major", "minor", "patch"), current, new):
        diff = n - c
        if diff > WARN_THRESHOLD:
            print(f"WARNING: {label} component jumps by {diff} ({c} -> {n}) — is this intentional?")
        if n < c and not (label == "minor" and new[0] > current[0]) and not (label == "patch"):
            print(f"WARNING: {label} component decreases ({c} -> {n})")


def write_version(new: tuple[int, int, int]) -> None:
    content = PYPROJECT.read_text()
    new_str = f'version = "{new[0]}.{new[1]}.{new[2]}"'
    updated = VERSION_RE.sub(new_str, content, count=1)
    PYPROJECT.write_text(updated)


def run(cmd: str) -> None:
    print(f"$ {cmd}")
    result = subprocess.run(cmd, shell=True)
    if result.returncode != 0:
        sys.exit(f"ERROR: command failed: {cmd}")


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else "patch"
    current = read_current_version()
    current_str = ".".join(str(x) for x in current)

    if arg in ("major", "minor", "patch"):
        new = bump(current, arg)
    else:
        new = parse_explicit(arg)
        warn_if_large_jump(current, new)

    new_str = ".".join(str(x) for x in new)
    print(f"{current_str} -> {new_str}")

    try:
        input(f"Release v{new_str}? [Enter to continue, Ctrl+C to abort] ")
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)

    write_version(new)
    run("uv lock")
    run('git add pyproject.toml uv.lock')
    run(f'git commit -m "chore: release v{new_str}"')
    run(f'git tag v{new_str}')
    run(f'git push origin v{new_str}')

    print(f"\nReleased v{new_str}")


if __name__ == "__main__":
    main()
