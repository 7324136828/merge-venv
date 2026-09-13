#!/usr/bin/env python3
"""Report declared Python and dependency versions for projects below a directory.

The script looks for common project metadata files in every directory and
prints one row per directory that declares a Python version.  It also reports
declared dependencies and writes dependency version disagreements to
``conflict.json`` in the directory being scanned.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Iterator


SKIP_DIRECTORIES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}


def read_text(path: Path) -> str:
    """Return UTF-8 text, tolerating files with an invalid byte."""
    return path.read_text(encoding="utf-8", errors="replace")


def from_python_version(path: Path) -> str | None:
    value = read_text(path).strip().splitlines()
    return value[0].strip() if value else None


def from_pyvenv_cfg(path: Path) -> str | None:
    """Read the interpreter version recorded when a virtual environment was made."""
    match = re.search(r"^\s*version\s*=\s*(.+?)\s*$", read_text(path), re.M | re.I)
    return match.group(1) if match else None


def from_pyproject(path: Path) -> str | None:
    text = read_text(path)
    try:
        import tomllib  # Python 3.11+

        data = tomllib.loads(text)
        project = data.get("project", {})
        if project.get("requires-python"):
            return str(project["requires-python"])
        poetry = data.get("tool", {}).get("poetry", {})
        dependencies = poetry.get("dependencies", {})
        if dependencies.get("python"):
            return str(dependencies["python"])
    except (ModuleNotFoundError, ValueError):
        pass

    # Works on older Python versions and malformed TOML as a best effort.
    match = re.search(r"^\s*requires-python\s*=\s*[\"']([^\"']+)[\"']", text, re.M)
    if not match:
        match = re.search(r"^\s*python\s*=\s*[\"']([^\"']+)[\"']", text, re.M)
    return match.group(1) if match else None


def from_setup_cfg(path: Path) -> str | None:
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    return parser.get("options", "python_requires", fallback=None)


def from_setup_py(path: Path) -> str | None:
    match = re.search(
        r"python_requires\s*=\s*[\"']([^\"']+)[\"']", read_text(path)
    )
    return match.group(1) if match else None


def from_pipfile(path: Path) -> str | None:
    match = re.search(
        r"^\s*python_(?:full_)?version\s*=\s*[\"']([^\"']+)[\"']",
        read_text(path),
        re.M,
    )
    return match.group(1) if match else None


def from_runtime_json(path: Path) -> str | None:
    try:
        value = json.loads(read_text(path)).get("python")
        return str(value) if value else None
    except json.JSONDecodeError:
        return None


READERS = {
    ".python-version": from_python_version,
    "pyproject.toml": from_pyproject,
    "setup.cfg": from_setup_cfg,
    "setup.py": from_setup_py,
    "Pipfile": from_pipfile,
    "runtime.json": from_runtime_json,
}

REQUIREMENTS_FILE = re.compile(r"^requirements(?:[-_.].*)?\.txt$", re.I)
DEPENDENCY_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[^]]*\])?\s*(.*)$")


def normalize_dependency_name(name: str) -> str:
    """Return a PEP 503-style name so spelling variants compare equally."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirement(requirement: str) -> tuple[str, str] | None:
    """Return the dependency name and declared version constraint, if present."""
    requirement = requirement.split(";", 1)[0].strip()
    if not requirement or requirement.startswith(("#", "-", ".", "/")):
        return None
    match = DEPENDENCY_NAME.match(requirement)
    if not match:
        return None
    name, version = match.groups()
    # A bare name is still important: it conflicts with a pinned/ranged
    # declaration because it permits a different installed version.
    return name, version.strip() or "(unversioned)"


def requirements_dependencies(path: Path) -> Iterator[tuple[str, str]]:
    """Yield direct dependency declarations from a requirements file."""
    for line in read_text(path).splitlines():
        # Preserve URL fragments such as ``#egg=package`` while dropping normal
        # comments.  URL-only requirements without an egg name are skipped.
        line = line.strip()
        if " #" in line:
            line = line.split(" #", 1)[0]
        parsed = parse_requirement(line)
        if parsed:
            yield parsed


def toml_data(path: Path) -> dict:
    """Load TOML when the standard-library parser is available."""
    try:
        import tomllib  # Python 3.11+

        return tomllib.loads(read_text(path))
    except (ModuleNotFoundError, ValueError):
        return {}


def dependency_from_value(name: str, value: object) -> tuple[str, str] | None:
    """Convert Poetry/Pipfile dependency values to the common representation."""
    if isinstance(value, str):
        return name, value.strip() or "(unversioned)"
    if isinstance(value, dict):
        version = value.get("version")
        if version is not None:
            return name, str(version).strip() or "(unversioned)"
        if "path" in value:
            return name, f"path:{value['path']}"
        if "git" in value:
            return name, f"git:{value['git']}"
    return name, "(unversioned)"


def pyproject_dependencies(path: Path) -> Iterator[tuple[str, str]]:
    """Yield PEP 621 and Poetry dependencies from a pyproject.toml file."""
    data = toml_data(path)
    project = data.get("project", {})
    for requirement in project.get("dependencies", []):
        if isinstance(requirement, str):
            parsed = parse_requirement(requirement)
            if parsed:
                yield parsed
    for requirements in project.get("optional-dependencies", {}).values():
        for requirement in requirements:
            if isinstance(requirement, str):
                parsed = parse_requirement(requirement)
                if parsed:
                    yield parsed

    poetry = data.get("tool", {}).get("poetry", {})
    for name, value in poetry.get("dependencies", {}).items():
        if normalize_dependency_name(str(name)) != "python":
            parsed = dependency_from_value(str(name), value)
            if parsed:
                yield parsed
    for group in poetry.get("group", {}).values():
        for name, value in group.get("dependencies", {}).items():
            parsed = dependency_from_value(str(name), value)
            if parsed:
                yield parsed


def setup_cfg_dependencies(path: Path) -> Iterator[tuple[str, str]]:
    """Yield install and extra dependencies from setup.cfg."""
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    for section, option in (("options", "install_requires"),):
        for requirement in parser.get(section, option, fallback="").splitlines():
            parsed = parse_requirement(requirement)
            if parsed:
                yield parsed
    if parser.has_section("options.extras_require"):
        for _, requirements in parser.items("options.extras_require"):
            for requirement in requirements.splitlines():
                parsed = parse_requirement(requirement)
                if parsed:
                    yield parsed


def setup_py_dependencies(path: Path) -> Iterator[tuple[str, str]]:
    """Best-effort extraction of quoted requirements in setup.py dependency lists."""
    text = read_text(path)
    for match in re.finditer(
        r"(?:install_requires|extras_require)\s*=\s*\[([^]]*)\]", text, re.S
    ):
        for requirement in re.findall(r"[\"']([^\"']+)[\"']", match.group(1)):
            parsed = parse_requirement(requirement)
            if parsed:
                yield parsed


def pipfile_dependencies(path: Path) -> Iterator[tuple[str, str]]:
    """Yield regular and development dependencies from a Pipfile."""
    data = toml_data(path)
    for section in ("packages", "dev-packages"):
        for name, value in data.get(section, {}).items():
            parsed = dependency_from_value(str(name), value)
            if parsed:
                yield parsed


DEPENDENCY_READERS = {
    "Pipfile": pipfile_dependencies,
    "pyproject.toml": pyproject_dependencies,
    "setup.cfg": setup_cfg_dependencies,
    "setup.py": setup_py_dependencies,
}


def find_dependencies(root: Path) -> Iterable[tuple[Path, str, str, str]]:
    """Yield (folder, source file, dependency name, version constraint)."""
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = sorted(d for d in subdirs if d not in SKIP_DIRECTORIES)
        folder = Path(directory)
        for filename in sorted(files):
            reader = DEPENDENCY_READERS.get(filename)
            if reader is None and REQUIREMENTS_FILE.match(filename):
                reader = requirements_dependencies
            if reader is None:
                continue
            path = folder / filename
            try:
                yield from (
                    (folder, filename, name, version) for name, version in reader(path)
                )
            except (OSError, ValueError, configparser.Error) as exc:
                print(f"warning: could not read {path}: {exc}", file=sys.stderr)


def write_conflicts(
    root: Path, dependencies: Iterable[tuple[Path, str, str, str]]
) -> tuple[Path, int]:
    """Write every dependency declared at different versions by scanned folders."""
    grouped: dict[str, dict[str, list[dict[str, str]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    display_names: dict[str, str] = {}
    for folder, source, name, version in dependencies:
        normalized_name = normalize_dependency_name(name)
        display_names.setdefault(normalized_name, name)
        relative = "." if folder == root else str(folder.relative_to(root))
        occurrence = {"folder": relative, "source": source}
        if occurrence not in grouped[normalized_name][version]:
            grouped[normalized_name][version].append(occurrence)

    conflicts = []
    for name in sorted(grouped):
        versions = grouped[name]
        if len(versions) > 1:
            conflicts.append(
                {
                    "dependency": display_names[name],
                    "versions": [
                        {"version": version, "declared_in": occurrences}
                        for version, occurrences in sorted(versions.items())
                    ],
                }
            )

    output = root / "conflict.json"
    output.write_text(
        json.dumps(
            {
                "root": str(root),
                "conflict_count": len(conflicts),
                "conflicts": conflicts,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return output, len(conflicts)


def find_versions(root: Path) -> Iterable[tuple[Path, str, str]]:
    """Yield (folder, source file, declared version) triples."""
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = sorted(d for d in subdirs if d not in SKIP_DIRECTORIES)
        folder = Path(directory)

        venv_config = folder / ".venv" / "pyvenv.cfg"
        if venv_config.is_file():
            try:
                version = from_pyvenv_cfg(venv_config)
            except OSError as exc:
                print(f"warning: could not read {venv_config}: {exc}", file=sys.stderr)
            else:
                if version:
                    yield folder, ".venv/pyvenv.cfg", version

        for filename in sorted(files):
            reader = READERS.get(filename)
            if not reader:
                continue
            path = folder / filename
            try:
                version = reader(path)
            except (OSError, configparser.Error) as exc:
                print(f"warning: could not read {path}: {exc}", file=sys.stderr)
                continue
            if version:
                yield folder, filename, version


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find declared Python versions and dependency constraints in projects."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        type=Path,
        help="directory to scan (default: current directory)",
    )
    args = parser.parse_args()
    root = args.directory.resolve()

    if not root.is_dir():
        parser.error(f"not a directory: {root}")

    print(f"{'Folder':<50} {'Source':<18} Python version")
    print("-" * 95)
    found = False
    for folder, source, version in find_versions(root):
        found = True
        relative = "." if folder == root else str(folder.relative_to(root))
        print(f"{relative:<50} {source:<18} {version}")

    if not found:
        print("No declared Python versions found.")

    dependencies = list(find_dependencies(root))
    print()
    print(f"{'Folder':<50} {'Source':<24} {'Dependency':<30} Version")
    print("-" * 125)
    if dependencies:
        for folder, source, name, version in dependencies:
            relative = "." if folder == root else str(folder.relative_to(root))
            print(f"{relative:<50} {source:<24} {name:<30} {version}")
    else:
        print("No declared dependencies found.")

    conflict_path, conflict_count = write_conflicts(root, dependencies)
    print(
        f"\nWrote {conflict_count} dependency conflict(s) to "
        f"{conflict_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
