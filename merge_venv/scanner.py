from __future__ import annotations

import json
import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from .models import Choice, Conflict, EnvironmentSnapshot, InstalledPackage, MergePlan


IGNORED_PACKAGES = frozenset({"pip", "setuptools", "wheel"})
ENVIRONMENT_NAMES = (".venv", "venv", "env")
VALID_DISTRIBUTION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class ScanError(RuntimeError):
    pass


def normalize_name(name: str) -> str:
    """Apply the same normalization used for Python distribution names."""

    return re.sub(r"[-_.]+", "-", name).lower()


def _version_key(value: str):
    """Return a PEP 440 key when available, with a dependency-free fallback."""

    try:
        from packaging.version import Version

        return (2, Version(value))
    except (ImportError, ValueError):
        pass
    try:
        from pip._vendor.packaging.version import Version

        return (2, Version(value))
    except (ImportError, ValueError):
        tokens = tuple(
            (1, int(part)) if part.isdigit() else (0, part.lower())
            for part in re.findall(r"\d+|[A-Za-z]+", value)
        )
        return (1, tokens)


def versions_highest_first(versions: Iterable[str]) -> list[str]:
    return sorted(set(versions), key=_version_key, reverse=True)


def python_executable_for(folder: Path) -> Path:
    if os.name == "nt":
        return folder / "Scripts" / "python.exe"
    return folder / "bin" / "python"


def find_environment(selected: Path) -> Path:
    selected = selected.expanduser().resolve()
    if (selected / "pyvenv.cfg").is_file() and python_executable_for(selected).is_file():
        raise ScanError(
            f"{selected} is a virtual environment folder. "
            "Select its parent project folder instead."
        )

    for name in ENVIRONMENT_NAMES:
        candidate = selected / name
        if (candidate / "pyvenv.cfg").is_file() and python_executable_for(candidate).is_file():
            return candidate.resolve()

    expected = ", ".join(ENVIRONMENT_NAMES)
    raise ScanError(
        f"No usable virtual environment found in {selected}. "
        f"Select a project folder containing one of: {expected}."
    )


_PROBE = r"""
import importlib.metadata as metadata
import json
import sys

packages = []
for dist in metadata.distributions():
    name = dist.metadata.get("Name")
    if name and dist.version:
        packages.append({"name": name, "version": dist.version})

print(json.dumps({
    "version": ".".join(map(str, sys.version_info[:3])),
    "base_executable": getattr(sys, "_base_executable", None),
    "packages": packages,
}))
"""


def scan_folder(selected_folder: Path, timeout: int = 45) -> EnvironmentSnapshot:
    environment = find_environment(selected_folder)
    executable = python_executable_for(environment)
    try:
        result = subprocess.run(
            [str(executable), "-I", "-c", _PROBE],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ScanError(f"Could not inspect {environment}: {exc}") from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or "the environment's Python did not start"
        raise ScanError(f"Could not inspect {environment}: {detail}")

    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ScanError(f"Invalid response from Python in {environment}") from exc

    seen: dict[str, InstalledPackage] = {}
    for item in payload.get("packages", []):
        name = str(item.get("name", "")).strip()
        version = str(item.get("version", "")).strip()
        if name and not VALID_DISTRIBUTION_NAME.fullmatch(name):
            raise ScanError(f"Invalid package name {name!r} reported by {environment}.")
        if "\n" in version or "\r" in version:
            raise ScanError(f"Invalid version for package {name!r} reported by {environment}.")
        key = normalize_name(name)
        if name and version and key not in IGNORED_PACKAGES:
            seen[key] = InstalledPackage(name=name, version=version)

    base_value = payload.get("base_executable")
    base_executable = Path(base_value).resolve() if base_value else None
    return EnvironmentSnapshot(
        selected_folder=selected_folder.resolve(),
        environment_folder=environment,
        python_executable=executable,
        base_executable=base_executable,
        python_version=str(payload["version"]),
        packages=tuple(sorted(seen.values(), key=lambda package: normalize_name(package.name))),
    )


def scan_folders(folders: Iterable[Path]) -> tuple[EnvironmentSnapshot, ...]:
    snapshots = tuple(scan_folder(folder) for folder in folders)
    if not snapshots:
        raise ScanError("Add at least one folder before merging.")
    return snapshots


def build_conflicts(snapshots: Iterable[EnvironmentSnapshot]) -> tuple[Conflict, ...]:
    snapshots = tuple(snapshots)
    python_sources: dict[str, list[str]] = defaultdict(list)
    package_versions: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    display_names: dict[str, str] = {}

    for snapshot in snapshots:
        source = str(snapshot.selected_folder)
        python_sources[snapshot.python_version].append(source)
        for package in snapshot.packages:
            key = normalize_name(package.name)
            display_names.setdefault(key, package.name)
            package_versions[key][package.version].append(source)

    conflicts: list[Conflict] = []
    if len(python_sources) > 1:
        conflicts.append(
            Conflict(
                key="python",
                display_name="Python",
                kind="python",
                choices=tuple(
                    Choice(version, tuple(sorted(python_sources[version])))
                    for version in versions_highest_first(python_sources)
                ),
            )
        )

    for key in sorted(package_versions):
        versions = package_versions[key]
        if len(versions) > 1:
            conflicts.append(
                Conflict(
                    key=key,
                    display_name=display_names[key],
                    choices=tuple(
                        Choice(version, tuple(sorted(versions[version])))
                        for version in versions_highest_first(versions)
                    ),
                )
            )
    return tuple(conflicts)


def build_project_conflict_rows(
    snapshots: Iterable[EnvironmentSnapshot], conflicts: Iterable[Conflict]
) -> tuple[tuple[str, str, str], ...]:
    """Return project, dependency, and detected version rows for the report UI."""
    conflicts = tuple(conflicts)
    rows: list[tuple[str, str, str]] = []
    for snapshot in snapshots:
        installed = {
            normalize_name(package.name): package.version for package in snapshot.packages
        }
        for conflict in conflicts:
            version = (
                snapshot.python_version
                if conflict.kind == "python"
                else installed.get(conflict.key, "Not installed")
            )
            rows.append((str(snapshot.selected_folder), conflict.display_name, version))
    return tuple(rows)


def make_plan(
    snapshots: Iterable[EnvironmentSnapshot], selections: dict[str, str] | None = None
) -> MergePlan:
    snapshots = tuple(snapshots)
    if not snapshots:
        raise ValueError("A merge plan needs at least one environment.")
    selections = selections or {}

    python_versions = versions_highest_first(s.python_version for s in snapshots)
    selected_python = selections.get("python", python_versions[0])
    if selected_python not in python_versions:
        raise ValueError(f"Python {selected_python} was not found in the selected environments.")

    grouped: dict[str, dict[str, str]] = defaultdict(dict)
    for snapshot in snapshots:
        for package in snapshot.packages:
            grouped[normalize_name(package.name)][package.version] = package.name

    selected_packages: dict[str, tuple[str, str]] = {}
    for key, versions_to_names in grouped.items():
        available = versions_highest_first(versions_to_names)
        chosen = selections.get(key, available[0])
        if chosen not in versions_to_names:
            raise ValueError(f"{chosen} is not an available version of {key}.")
        selected_packages[key] = (versions_to_names[chosen], chosen)

    return MergePlan(
        snapshots=snapshots,
        selected_python=selected_python,
        selected_packages=selected_packages,
    )
