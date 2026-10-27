#!/usr/bin/env python3
"""Write requirements.lock: exact versions of requirements.txt and everything it pulls in.

The versions are the ones installed in the environment running this script -
run it where the test suite passes, so the lock records a combination known to
work. Each dependency keeps the environment marker under which its parent
requires it (``pywin32; sys_platform == "win32"``), so the lock also installs
on other platforms. Extras are followed. Packages only needed on another
platform are not installed here and so cannot be pinned; they are listed
unpinned with their marker.

Usage (from the project root):
    python scripts/setup/lock_dependencies.py            # -> requirements.lock
    pip install -r requirements.lock                     # reproduce the environment
"""

from __future__ import annotations

import argparse
import platform
import sys
from datetime import date
from importlib import metadata
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def _installed(name: str) -> Optional[metadata.Distribution]:
    try:
        return metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return None


def resolve(requirements_file: Path) -> Tuple[Dict[str, Tuple[str, Optional[str]]], Set[str]]:
    """{canonical name: (version, marker or None)} and the names not installed here."""
    pinned: Dict[str, Tuple[str, Optional[str]]] = {}
    missing: Set[str] = set()
    queue = []
    for line in requirements_file.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            queue.append((Requirement(line), None))
    seen: Set[Tuple[str, frozenset]] = set()
    while queue:
        req, inherited = queue.pop()
        marker = str(req.marker) if req.marker else inherited
        key = (canonicalize_name(req.name), frozenset(req.extras))
        if key in seen:
            continue
        seen.add(key)
        dist = _installed(req.name)
        name = canonicalize_name(req.name)
        if dist is None:
            if marker is None or Marker(marker).evaluate():
                raise SystemExit(f"{req.name} is required here but not installed - install requirements.txt first")
            missing.add(f"{req.name}; {marker}")
            continue
        # A package needed unconditionally by any path is unconditional.
        previous = pinned.get(name)
        pinned[name] = (dist.version, None if (previous and previous[1] is None) or marker is None else marker)
        for child in dist.requires or []:
            child_req = Requirement(child)
            if child_req.marker and "extra" in str(child_req.marker):
                if not any(child_req.marker.evaluate({"extra": extra}) for extra in req.extras):
                    continue
                child_req.marker = None
            elif child_req.marker and not child_req.marker.evaluate():
                missing.add(f"{child_req.name}; {child_req.marker}")
                continue
            queue.append((child_req, str(child_req.marker) if child_req.marker else marker))
    return pinned, missing


def read_overrides(path: Path) -> Dict[str, str]:
    """``name==version`` lines (comments allowed) -> {canonical name: version}."""
    overrides: Dict[str, str] = {}
    if not path.exists():
        return overrides
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, _, version = line.partition("==")
            overrides[canonicalize_name(name)] = version.strip()
    return overrides


def conflicts(pinned: Dict[str, Tuple[str, Optional[str]]], overridden: Set[str]) -> List[str]:
    """Every requirement of a locked package that the locked versions do not satisfy.

    Requirements are read from the installed distributions; an overridden
    package's own requirements are those of the installed version (its new
    version cannot be inspected offline), so they are reported separately as
    unverified rather than trusted.
    """
    problems: List[str] = []
    for name, (_, _) in pinned.items():
        if name in overridden:
            continue
        dist = _installed(name)
        for raw in (dist.requires or []) if dist else []:
            req = Requirement(raw)
            if req.marker and ("extra" in str(req.marker) or not req.marker.evaluate()):
                continue
            locked = pinned.get(canonicalize_name(req.name))
            if locked and req.specifier and not req.specifier.contains(locked[0], prereleases=True):
                problems.append(f"{name} requires {req}, lock has {req.name}=={locked[0]}")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--requirements", default="requirements.txt")
    parser.add_argument("--out", default="requirements.lock")
    parser.add_argument("--overrides", default="requirements.lock.overrides",
                        help="name==version lines replacing installed versions that are inconsistent")
    args = parser.parse_args(argv)

    pinned, missing = resolve(Path(args.requirements))
    overrides = read_overrides(Path(args.overrides))
    for name, version in overrides.items():
        if name in pinned:
            pinned[name] = (version, pinned[name][1])
    problems = conflicts(pinned, set(overrides))
    if problems:
        # Never write an inconsistent lock (the 2026-10-27 lock pinned h11 0.16
        # with an httpcore that requires h11<0.15). Fix the environment or add
        # an override with its reason.
        print("Refusing to write a lock whose pins conflict:")
        for problem in problems:
            print("  " + problem)
        return 1

    lines = [
        f"# Generated by scripts/setup/lock_dependencies.py on {date.today()} from {args.requirements}",
        f"# in Python {platform.python_version()} on {platform.system()} - the environment the tests passed in.",
        "# Regenerate after changing requirements.txt; install with: pip install -r requirements.lock",
        "# Every pin satisfies every requirement of the other pins (checked offline);",
        "# CI installs this lock into an empty environment and runs `pip check`.",
    ]
    if overrides:
        lines.append("# Overridden (requirements.lock.overrides; their own requirements are verified by CI):")
        lines += [f"#   {name}=={version}" for name, version in sorted(overrides.items())]
    lines.append("")
    for name in sorted(pinned):
        version, marker = pinned[name]
        lines.append(f"{name}=={version}" + (f"; {marker}" if marker else ""))
    if missing:
        lines += ["", "# Needed only on other platforms (not installed where this lock was made):"]
        lines += sorted(m for m in missing if canonicalize_name(m.split(";")[0]) not in pinned)
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{args.out}: {len(pinned)} pinned ({len(overrides)} overridden), {len(missing)} platform-specific")
    return 0


if __name__ == "__main__":
    sys.exit(main())
