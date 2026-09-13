from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class InstalledPackage:
    """A package observed in one virtual environment."""

    name: str
    version: str


@dataclass(frozen=True)
class EnvironmentSnapshot:
    """The information needed to reproduce a virtual environment."""

    selected_folder: Path
    environment_folder: Path
    python_executable: Path
    base_executable: Path | None
    python_version: str
    packages: tuple[InstalledPackage, ...]


@dataclass(frozen=True)
class Choice:
    """One selectable version and the environments in which it was found."""

    value: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class Conflict:
    key: str
    display_name: str
    choices: tuple[Choice, ...]
    kind: str = "package"


@dataclass
class MergePlan:
    snapshots: tuple[EnvironmentSnapshot, ...]
    selected_python: str
    selected_packages: dict[str, tuple[str, str]] = field(default_factory=dict)

    @property
    def package_count(self) -> int:
        return len(self.selected_packages)

    def requirement_lines(self) -> list[str]:
        return [
            f"{display_name}=={version}"
            for _key, (display_name, version) in sorted(
                self.selected_packages.items(), key=lambda item: item[0]
            )
        ]

