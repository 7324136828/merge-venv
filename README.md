# Merge Venv

Merge Venv is a small desktop application that inventories multiple Python virtual environments and recreates their combined package set in one clean environment.

It does **not** copy `.venv` files. Virtual environments contain machine-specific paths and launchers, so copying them produces fragile results. Instead, the app:

1. detects the Python version and installed distributions in every selected environment;
2. shows a dropdown for every Python or package version conflict, newest first;
3. creates a new virtual environment with the selected Python;
4. installs the selected package versions and runs `pip check`.

Fresh virtual environments keep the pip version bundled with the chosen Python; the app does not upgrade pip as an unrelated side effect.

## Run

Python 3.10+ is recommended. Tkinter is included with standard Windows and macOS Python installers.

```powershell
python run.py
```

or:

```powershell
python -m merge_venv
```

No third-party runtime packages are required. If the chosen Python version is not already available through one of the source environments, the app installs `uv` into a temporary tooling environment and uses it to provision that Python version. This operation requires internet access.

## Use

- Click **Add folders** once for each top-level project folder. Select the parent project folder, not its `.venv`, `venv`, or `env` folder; the app detects the environment inside it automatically.
- Click **Scan & merge**.
- Review the conflict report. The dependency view asks you to choose the version to keep, while the project view lists every selected project and all conflicting dependencies detected in it. Use **Copy table** to copy that report in a spreadsheet-friendly table format. The newest detected version is preselected.
- Choose a parent directory and a new environment folder name.
- Keep the progress window open while Python and packages are installed.

The app writes `merge-venv-requirements.txt` into the resulting environment as a record of the exact selected versions. If creation fails or is cancelled, the newly created destination is removed so no partial environment is left behind.

Package metadata cannot retain credentials, private index URLs, editable source checkouts, or platform-specific installation flags from the original environment. Such packages may need to be installed manually afterward. For PyTorch packages, recognized CUDA, ROCm, CPU, and XPU build tags are installed from the corresponding official PyTorch wheel index. Other packages with private or custom build indexes may still need to be installed manually afterward.

## Test

```powershell
python -m unittest discover -s tests -v
```
