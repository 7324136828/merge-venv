import sys
import tempfile
from pathlib import Path
from unittest import TestCase

from merge_venv.merger import EnvironmentMerger, MergeError, pytorch_index_url
from merge_venv.models import EnvironmentSnapshot, MergePlan


def empty_plan() -> MergePlan:
    version = ".".join(str(part) for part in sys.version_info[:3])
    executable = Path(sys.executable).resolve()
    snapshot = EnvironmentSnapshot(
        selected_folder=executable.parent,
        environment_folder=executable.parent,
        python_executable=executable,
        base_executable=executable,
        python_version=version,
        packages=(),
    )
    return MergePlan((snapshot,), version, {})


class MergerTests(TestCase):
    def test_recognizes_official_index_for_tagged_pytorch_build(self):
        self.assertEqual(
            pytorch_index_url("torch", "2.11.0+cu128"),
            "https://download.pytorch.org/whl/cu128",
        )
        self.assertEqual(
            pytorch_index_url("torchvision", "0.24.0+rocm6.4"),
            "https://download.pytorch.org/whl/rocm6.4",
        )
        self.assertIsNone(pytorch_index_url("torch", "2.11.0"))
        self.assertIsNone(pytorch_index_url("unrelated", "1.0+cu128"))

    def test_creates_environment_at_its_final_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "unified"
            merger = EnvironmentMerger()

            def fake_run(command, _label):
                if command[1:3] == ["-m", "venv"]:
                    Path(command[3]).mkdir()

            merger._run = fake_run
            result = merger.merge(empty_plan(), target)
            self.assertEqual(result, target)
            self.assertTrue(target.is_dir())

    def test_removes_partial_destination_after_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "unified"
            merger = EnvironmentMerger()

            def fake_run(command, label):
                if command[1:3] == ["-m", "venv"]:
                    Path(command[3]).mkdir()
                elif label == "Validating dependencies":
                    raise MergeError("broken environment")

            merger._run = fake_run
            with self.assertRaises(MergeError):
                merger.merge(empty_plan(), target)
            self.assertFalse(target.exists())

    def test_installs_tagged_pytorch_build_from_its_official_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "unified"
            merger = EnvironmentMerger()
            commands = []
            plan = empty_plan()
            plan.selected_packages["torch"] = ("torch", "2.11.0+cu128")

            def fake_run(command, _label):
                commands.append(command)
                if command[1:3] == ["-m", "venv"]:
                    Path(command[3]).mkdir()

            merger._run = fake_run
            merger.merge(plan, target)

            pytorch_command = next(
                command
                for command in commands
                if "https://download.pytorch.org/whl/cu128" in command
            )
            self.assertIn("torch==2.11.0+cu128", pytorch_command)
            self.assertLess(pytorch_command.index("--index-url"), pytorch_command.index("torch==2.11.0+cu128"))
