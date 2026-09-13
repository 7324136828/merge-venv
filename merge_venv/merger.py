from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable

from .models import MergePlan
from .scanner import python_executable_for


LogCallback = Callable[[str], None]

PYTORCH_DISTRIBUTIONS = {
    "torch",
    "torchaudio",
    "torchdata",
    "torchtext",
    "torchvision",
    "torch-tensorrt",
}
PYTORCH_BUILD_TAG = re.compile(r"^(cu\d+|rocm\d+(?:\.\d+)*|cpu|xpu)(?:[.-]|$)")


class MergeError(RuntimeError):
    pass


class MergeCancelled(MergeError):
    pass


def pytorch_index_url(package_key: str, version: str) -> str | None:
    """Return the official wheel index for a tagged PyTorch build."""
    if package_key not in PYTORCH_DISTRIBUTIONS or "+" not in version:
        return None
    local_version = version.split("+", 1)[1].lower()
    match = PYTORCH_BUILD_TAG.match(local_version)
    if not match:
        return None
    return f"https://download.pytorch.org/whl/{match.group(1)}"


class EnvironmentMerger:
    def __init__(
        self,
        log: LogCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.log = log or (lambda _message: None)
        self.cancel_event = cancel_event or threading.Event()
        self._process: subprocess.Popen[str] | None = None

    def cancel(self) -> None:
        self.cancel_event.set()
        if self._process and self._process.poll() is None:
            self._process.terminate()

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise MergeCancelled("Merge cancelled.")

    def _run(self, command: list[str], label: str) -> None:
        self._check_cancelled()
        self.log(label)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise MergeError(f"Could not start {command[0]}: {exc}") from exc

        assert self._process.stdout is not None
        for line in self._process.stdout:
            self.log(line.rstrip())
            if self.cancel_event.is_set():
                self._process.terminate()
                break
        return_code = self._process.wait()
        self._process = None
        self._check_cancelled()
        if return_code:
            raise MergeError(f"{label} failed with exit code {return_code}.")

    @staticmethod
    def _matching_source_python(plan: MergePlan) -> Path | None:
        for snapshot in plan.snapshots:
            if snapshot.python_version != plan.selected_python:
                continue
            candidates = (snapshot.base_executable, snapshot.python_executable)
            for candidate in candidates:
                if candidate and candidate.is_file():
                    return candidate
        return None

    def _find_uv(self) -> list[str] | None:
        uv = shutil.which("uv")
        if uv:
            return [uv]
        probe = subprocess.run(
            [sys.executable, "-m", "uv", "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            return [sys.executable, "-m", "uv"]
        return None

    def _bootstrap_uv(self, tools_dir: Path) -> list[str]:
        tools_env = tools_dir / "uv-tooling"
        tools_python = python_executable_for(tools_env)
        if not tools_python.is_file():
            self._run(
                [sys.executable, "-m", "venv", str(tools_env)],
                "Preparing the Python installer",
            )
        self._run(
            [str(tools_python), "-m", "pip", "install", "--disable-pip-version-check", "uv"],
            "Installing the Python installer (uv)",
        )
        return [str(tools_python), "-m", "uv"]

    def _provision_python(self, version: str, tools_dir: Path) -> Path:
        uv_command = self._find_uv() or self._bootstrap_uv(tools_dir)
        self._run(uv_command + ["python", "install", version], f"Installing Python {version}")
        command = uv_command + ["python", "find", version]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise MergeError(result.stderr.strip() or f"Could not locate Python {version}.")
        executable = Path(result.stdout.strip().splitlines()[-1])
        if not executable.is_file():
            raise MergeError(f"Python installer returned an invalid path: {executable}")
        return executable

    def merge(self, plan: MergePlan, target: Path) -> Path:
        target = target.expanduser().resolve()
        if target.exists():
            raise MergeError("The destination already exists. Choose a new environment folder.")
        if not target.parent.is_dir():
            raise MergeError("The destination's parent folder does not exist.")

        tools_dir = Path(tempfile.gettempdir()) / "merge-venv-tools"
        try:
            source_python = self._matching_source_python(plan)
            if source_python:
                self.log(f"Using Python {plan.selected_python} from {source_python}")
            else:
                self.log(f"Python {plan.selected_python} was not found locally; provisioning it now.")
                tools_dir.mkdir(parents=True, exist_ok=True)
                source_python = self._provision_python(plan.selected_python, tools_dir)

            self._run(
                [str(source_python), "-m", "venv", str(target)],
                f"Creating environment at {target}",
            )
            target_python = python_executable_for(target)

            requirements = plan.requirement_lines()
            if requirements:
                requirements_path = target / "merge-venv-requirements.txt"
                requirements_path.write_text("\n".join(requirements) + "\n", encoding="utf-8")

                pytorch_requirements: dict[str, list[str]] = {}
                for package_key, (display_name, version) in plan.selected_packages.items():
                    index_url = pytorch_index_url(package_key, version)
                    if index_url:
                        pytorch_requirements.setdefault(index_url, []).append(
                            f"{display_name}=={version}"
                        )
                for index_url, tagged_requirements in sorted(pytorch_requirements.items()):
                    self._run(
                        [
                            str(target_python),
                            "-m",
                            "pip",
                            "install",
                            "--index-url",
                            index_url,
                            *sorted(tagged_requirements),
                        ],
                        f"Installing PyTorch build from {index_url}",
                    )

                self._run(
                    [
                        str(target_python),
                        "-m",
                        "pip",
                        "install",
                        "--requirement",
                        str(requirements_path),
                    ],
                    f"Installing {len(requirements)} packages",
                )
            self._run([str(target_python), "-m", "pip", "check"], "Validating dependencies")
            self._check_cancelled()
            self.log(f"Merged environment is ready: {target}")
            return target
        except Exception:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            raise
