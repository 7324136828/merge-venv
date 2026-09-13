import tempfile
from pathlib import Path
from unittest import TestCase

from merge_venv.models import EnvironmentSnapshot, InstalledPackage
from merge_venv.scanner import (
    ScanError,
    build_conflicts,
    build_project_conflict_rows,
    find_environment,
    make_plan,
    normalize_name,
    python_executable_for,
    versions_highest_first,
)


def snapshot(label: str, python: str, packages: list[tuple[str, str]]) -> EnvironmentSnapshot:
    root = Path("C:/projects") / label
    return EnvironmentSnapshot(
        selected_folder=root,
        environment_folder=root / ".venv",
        python_executable=root / ".venv/Scripts/python.exe",
        base_executable=None,
        python_version=python,
        packages=tuple(InstalledPackage(*package) for package in packages),
    )


class ScannerTests(TestCase):
    def test_finds_environment_inside_selected_project_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            environment = project / ".venv"
            environment.mkdir(parents=True)
            (environment / "pyvenv.cfg").touch()
            executable = python_executable_for(environment)
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.touch()

            self.assertEqual(find_environment(project), environment.resolve())

    def test_rejects_selecting_virtual_environment_folder_directly(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = Path(temporary) / ".venv"
            environment.mkdir()
            (environment / "pyvenv.cfg").touch()
            executable = python_executable_for(environment)
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.touch()

            with self.assertRaisesRegex(ScanError, "parent project folder"):
                find_environment(environment)

    def test_normalizes_distribution_names(self):
        self.assertEqual(normalize_name("My_Package.Name"), "my-package-name")

    def test_versions_are_newest_first(self):
        self.assertEqual(
            versions_highest_first(["1.9", "2.0rc1", "1.10", "2.0"]),
            ["2.0", "2.0rc1", "1.10", "1.9"],
        )

    def test_detects_python_and_package_conflicts(self):
        snapshots = (
            snapshot("one", "3.11.8", [("NumPy", "1.26.4"), ("requests", "2.31.0")]),
            snapshot("two", "3.12.2", [("numpy", "2.0.0"), ("requests", "2.31.0")]),
        )
        conflicts = build_conflicts(snapshots)
        self.assertEqual([conflict.key for conflict in conflicts], ["python", "numpy"])
        self.assertEqual(conflicts[0].choices[0].value, "3.12.2")
        self.assertEqual(conflicts[1].choices[0].value, "2.0.0")
        self.assertEqual(conflicts[1].choices[0].sources, (str(Path("C:/projects/two")),))

    def test_builds_project_first_view_of_every_conflict(self):
        snapshots = (
            snapshot("one", "3.11.8", [("NumPy", "1.26.4")]),
            snapshot("two", "3.12.2", [("numpy", "2.0.0")]),
            snapshot("three", "3.12.2", [("Flask", "3.0.1")]),
        )
        rows = build_project_conflict_rows(snapshots, build_conflicts(snapshots))

        self.assertEqual(
            rows,
            (
                (str(Path("C:/projects/one")), "Python", "3.11.8"),
                (str(Path("C:/projects/one")), "NumPy", "1.26.4"),
                (str(Path("C:/projects/two")), "Python", "3.12.2"),
                (str(Path("C:/projects/two")), "NumPy", "2.0.0"),
                (str(Path("C:/projects/three")), "Python", "3.12.2"),
                (str(Path("C:/projects/three")), "NumPy", "Not installed"),
            ),
        )

    def test_plan_defaults_to_highest_versions(self):
        snapshots = (
            snapshot("one", "3.11.8", [("NumPy", "1.26.4")]),
            snapshot("two", "3.12.2", [("numpy", "2.0.0"), ("Flask", "3.0.1")]),
        )
        plan = make_plan(snapshots)
        self.assertEqual(plan.selected_python, "3.12.2")
        self.assertEqual(plan.selected_packages["numpy"][1], "2.0.0")
        self.assertEqual(plan.requirement_lines(), ["Flask==3.0.1", "numpy==2.0.0"])

    def test_plan_applies_user_choices(self):
        snapshots = (
            snapshot("one", "3.11.8", [("numpy", "1.26.4")]),
            snapshot("two", "3.12.2", [("numpy", "2.0.0")]),
        )
        plan = make_plan(snapshots, {"python": "3.11.8", "numpy": "1.26.4"})
        self.assertEqual(plan.selected_python, "3.11.8")
        self.assertEqual(plan.selected_packages["numpy"][1], "1.26.4")
