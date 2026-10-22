"""Prepare the project-local runtime. Direct execution defaults to a dry run."""

from __future__ import annotations

import os
from pathlib import Path
import platform
import subprocess
import sys


DEFAULT_PARAMS = {
    "project_root": Path(__file__).resolve().parent.parent,
    "dryrun": True,
}

def runtime_platform(system: str | None = None, machine: str | None = None) -> str:
    """Select a self-contained runtime without consulting host Python installs."""
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    architecture = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}.get(machine)
    if system == "Darwin" and architecture:
        return f"macos-{architecture}"
    if system == "Windows" and architecture:
        # Windows on ARM can run the same x64 bundle via the OS emulator.
        return "windows-x86_64"
    raise RuntimeError("当前运行时支持 macOS 和 64 位 Windows；其他系统需要对应的独立运行时。")


def runtime_python(project_root: Path, system: str | None = None, machine: str | None = None) -> Path:
    target = runtime_platform(system, machine)
    runtime = Path(project_root) / "envs" / f"python-{target}"
    return runtime / "python.exe" if target.startswith("windows-") else runtime / "bin" / "python3"


def main(project_root=DEFAULT_PARAMS["project_root"], dryrun=DEFAULT_PARAMS["dryrun"]) -> dict:
    """Install dependencies only into the bundled, relocatable CPython runtime."""
    project_root = Path(project_root).resolve()
    interpreter = runtime_python(project_root)
    target = runtime_platform()
    requirements = project_root / "app" / ("requirements-windows.txt" if target.startswith("windows-") else "requirements.txt")
    result = {"dryrun": bool(dryrun), "python": str(interpreter), "requirements": str(requirements),
              "environment": "standalone", "platform": target}
    if dryrun:
        return result
    if not requirements.is_file():
        raise RuntimeError(f"项目文件不完整，缺少 {requirements.name}。")
    if not interpreter.is_file():
        raise RuntimeError("缺少项目独立 Python 运行时，请使用项目根目录的启动入口自动配置。")
    runtime = (project_root / "envs" / f"python-{target}").resolve()
    if Path(sys.prefix).resolve() != runtime or Path(sys.base_prefix).resolve() != runtime:
        raise RuntimeError("依赖配置必须由项目内置的独立 Python 执行，不能使用本机 Python 或虚拟环境。")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", PIP_NO_COMPILE="1")
    for name in ("PYTHONHOME", "PYTHONPATH", "PIP_TARGET", "PIP_PREFIX", "PIP_USER"):
        env.pop(name, None)
    # The copied uv runtime may carry an EXTERNALLY-MANAGED marker. The prefix
    # check above confines this override to our own standalone project copy.
    env["PIP_REQUIRE_VIRTUALENV"] = "0"
    env["PIP_CONFIG_FILE"] = os.devnull
    subprocess.run([str(interpreter), "-B", "-m", "pip", "--isolated", "install",
                    "--break-system-packages", "--disable-pip-version-check", "--only-binary=:all:",
                    "--no-user", "--no-cache-dir", "--no-compile", "-r", str(requirements)], env=env, check=True)
    subprocess.run([str(interpreter), "-c", "import pymupdf; from markitdown.converters import PdfConverter; from markitdown import StreamInfo"], env=env, check=True)
    return result


if __name__ == "__main__":
    print(main())
