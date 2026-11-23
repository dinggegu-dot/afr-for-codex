"""Start the AFR MCP server in an independent, versioned Python environment."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
import venv


RUNTIME = Path(__file__).resolve().parent
REQUIREMENTS = RUNTIME / "requirements.lock"


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError(f"目录必须由当前用户所有，权限为 0700：{path}")


def prepare_python(state_dir: Path) -> Path:
    dependency_dir = state_dir / "runtime"
    private_directory(dependency_dir)
    digest = hashlib.sha256(REQUIREMENTS.read_bytes() + os.fsencode(sys.executable)).hexdigest()[:16]
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    environment = dependency_dir / f"python-{version}-{platform.machine()}-{digest}"
    private_directory(environment)
    python = environment / "bin" / "python3"
    ready = environment / ".ready"

    lock = os.open(environment / ".bootstrap.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        if not ready.is_file() or not python.is_file():
            print("正在为 AFR for Codex 安装已锁定的 Python 依赖。", file=sys.stderr, flush=True)
            venv.EnvBuilder(with_pip=True, symlinks=True).create(environment)
            pip_env = {key: value for key, value in os.environ.items() if not key.startswith("PIP_")}
            pip_env["PIP_CONFIG_FILE"] = os.devnull
            subprocess.run(
                [
                    str(python), "-I", "-m", "pip", "install",
                    "--disable-pip-version-check", "--no-input", "--no-cache-dir",
                    "--index-url", "https://pypi.org/simple",
                    "--require-hashes", "--only-binary=:all:",
                    "--requirement", str(REQUIREMENTS),
                ],
                check=True,
                env=pip_env,
                stdout=sys.stderr,
            )
            subprocess.run(
                [str(python), "-I", "-B", "-c", "import docx, pydantic, pypdf"],
                check=True,
                stdout=sys.stderr,
            )
            ready.touch(mode=0o600)
    return python


def main() -> None:
    sys.path.insert(0, str(RUNTIME))
    from afr_share.config import owned_state_dir

    args = sys.argv[1:]
    if args not in ([], ["--doctor"]):
        raise ValueError("用法：launcher.sh [--doctor]")
    os.umask(0o077)
    configured_root = os.environ.get("AFR_FOR_CODEX_DATA_ROOT")
    default_root = Path.home() / "Library" / "Application Support" / "AFR for Codex"
    state_dir = Path(configured_root).expanduser() if configured_root else default_root
    if not state_dir.is_absolute():
        raise ValueError("AFR_FOR_CODEX_DATA_ROOT 必须是绝对路径。")
    state_dir = owned_state_dir(state_dir)
    python = prepare_python(state_dir)
    if args == ["--doctor"]:
        print(json.dumps({"status": "ready", "state_dir": str(state_dir), "python": str(python)}))
        return
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env["PYTHONPATH"] = str(RUNTIME)
    env["AFR_FOR_CODEX_DATA_ROOT"] = str(state_dir)
    os.chdir(RUNTIME)
    os.execve(
        python,
        [str(python), "-B", "-m", "afr_share.server", "--state-dir", str(state_dir)],
        env,
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"AFR for Codex 无法启动：{error}", file=sys.stderr)
        sys.exit(1)
