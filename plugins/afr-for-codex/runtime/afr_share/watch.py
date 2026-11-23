"""可选的 macOS 登录会话后台收录。"""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import plistlib
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

from afr_agent import codex, workflow
from afr_agent.intake import private_json
from afr_agent.library import RequestError

from .config import Store, owned_state_dir, read_policy, update_policy, write_private_json


INTERVAL_SECONDS = 3
RUNTIME = Path(__file__).resolve().parents[1]
BASELINE = "watch-baseline.json"
PROGRESS = "watch-progress.json"


@contextmanager
def _locked(state_dir):
    with os.fdopen(os.open(state_dir / "watch.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
                           | os.O_NONBLOCK, 0o600), "wb") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RequestError("unsafe_state_file")
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _label(state_dir):
    instance = hashlib.sha256(os.fsencode(state_dir)).hexdigest()[:24]
    return f"org.afr-for-codex.watch.{instance}"


def _domain():
    return f"gui/{os.getuid()}"


def _target(state_dir):
    return f"{_domain()}/{_label(state_dir)}"


def _launchctl(*args):
    try:
        return subprocess.run(["/bin/launchctl", *map(str, args)], capture_output=True,
                              text=True, errors="replace", check=False, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RequestError("watch_service_unavailable") from exc


def _service(state_dir):
    result = _launchctl("print", _target(state_dir))
    if result.returncode:
        output = f"{result.stdout}\n{result.stderr}".lower()
        if "could not find service" in output or "no such process" in output:
            return False, False
        raise RequestError("watch_service_unavailable")
    return True, bool(re.search(r"(?m)^\s*state\s*=\s*running\s*$", result.stdout))


def _write_plist(state_dir, label, new_articles):
    args = [str(Path(sys.executable).absolute()), "-B", "-E", "-m", "afr_share.watch",
            "--state-dir", str(state_dir)]
    if new_articles:
        args.append("--authorize-new")
    environment = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
    try:
        environment["AFR_FOR_CODEX_CLI"] = str(Path(codex.find_codex()).absolute())
    except RequestError:
        pass
    if os.environ.get("AFR_FOR_CODEX_MODEL"):
        environment["AFR_FOR_CODEX_MODEL"] = os.environ["AFR_FOR_CODEX_MODEL"]
    if os.path.isabs(os.environ.get("CODEX_HOME", "")):
        environment["CODEX_HOME"] = os.environ["CODEX_HOME"]
    plist = {
        "Label": label, "ProgramArguments": args, "WorkingDirectory": str(RUNTIME),
        "EnvironmentVariables": environment, "RunAtLoad": True,
        "StandardOutPath": os.devnull, "StandardErrorPath": os.devnull,
    }
    path = state_dir / "watch.plist"
    stage = None
    try:
        with tempfile.NamedTemporaryFile(dir=state_dir, prefix=".watch-", delete=False) as stream:
            stage = Path(stream.name)
            plistlib.dump(plist, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stage, path)
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)
    return path


def _set_automatic(state_dir, enabled):
    return update_policy(state_dir, lambda policy: policy.update(allow_new_articles=enabled))


def start(state_dir, inbox, vault, approval_file, *, authorize_new=False, model=None, rename_generic=False):
    if sys.platform != "darwin":
        raise RequestError("watch_requires_macos")
    if type(authorize_new) is not bool:
        raise RequestError("invalid_request")
    state_dir = owned_state_dir(state_dir)
    config = Store(state_dir).load()
    if (inbox, vault, approval_file, model, rename_generic) != (
        config.inbox, config.vault, config.approvals, config.model, config.rename_generic
    ):
        raise RequestError("invalid_settings")
    with _locked(state_dir):
        loaded, _ = _service(state_dir)
        if loaded:
            if read_policy(state_dir)["allow_new_articles"] != authorize_new:
                raise RequestError("watch_already_loaded")
            return status(state_dir)
        _set_automatic(state_dir, False)
        if authorize_new:
            workflow.seed_baseline(vault, inbox, baseline_path=state_dir / BASELINE, replace_existing=True)
        path = _write_plist(state_dir, _label(state_dir), authorize_new)
        write_private_json(state_dir / PROGRESS, {"state": "starting"})
        result = _launchctl("bootstrap", _domain(), path)
        if result.returncode:
            if _service(state_dir)[0]:
                _launchctl("bootout", _target(state_dir))
            raise RequestError("watch_start_failed")
        if authorize_new:
            try:
                _set_automatic(state_dir, True)
            except (RequestError, OSError, ValueError):
                _launchctl("bootout", _target(state_dir))
                raise
        return status(state_dir)


def stop(state_dir):
    if sys.platform != "darwin":
        raise RequestError("watch_requires_macos")
    state_dir = owned_state_dir(state_dir)
    with _locked(state_dir):
        try:
            _set_automatic(state_dir, False)
        finally:
            if _service(state_dir)[0]:
                _launchctl("bootout", _target(state_dir))
                for _ in range(10):
                    if not _service(state_dir)[0]:
                        break
                    time.sleep(0.1)
                else:
                    raise RequestError("watch_stop_failed")
        return status(state_dir)


def status(state_dir):
    if sys.platform != "darwin":
        return {"state": "unsupported", "loaded": False, "running": False, "authorize_new": False}
    state_dir = owned_state_dir(state_dir)
    loaded, running = _service(state_dir)
    try:
        progress = private_json(state_dir / PROGRESS, 16 * 1024)
    except (OSError, ValueError):
        progress = None
    return {
        "state": "running" if running else "loaded" if loaded else "stopped",
        "loaded": loaded, "running": running,
        "authorize_new": loaded and read_policy(state_dir)["allow_new_articles"],
        "last_cycle": progress,
    }


def _error_code(exc):
    if isinstance(exc, RequestError) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", str(exc)):
        return str(exc)
    return "watch_local_error"


def _cycle(store, inbox_identity, authorize_new):
    config = store.load()
    if workflow.inbox_identity(config.inbox) != inbox_identity:
        raise RequestError("inbox_changed")
    automatic = authorize_new and read_policy(config.state_dir)["allow_new_articles"]
    with workflow.watcher_lock(config.vault):
        result = workflow.cycle(
            config.vault, config.inbox, config.approvals, preserve_original=True, new_articles=automatic,
            rename_generic=config.rename_generic, baseline_path=config.state_dir / BASELINE,
        )
    articles = result["articles"]
    return {
        "state": "ready", "last_run": int(time.time()),
        "collected": result["collected"], "records": len(articles),
        "processed": sum(row["phase"] == "ready" for row in articles),
        "local_only": sum(row["error"] == "local_only" for row in articles),
        "article_failures": sum(row["error"] not in (None, "local_only") for row in articles),
        "intake_failures": len(result["errors"]),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--authorize-new", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    store = Store(args.state_dir)
    progress = store.state_dir / PROGRESS
    try:
        config = store.load()
        identity = workflow.inbox_identity(config.inbox)
        if config.model:
            os.environ["AFR_FOR_CODEX_MODEL"] = config.model
        while True:
            try:
                summary = _cycle(store, identity, args.authorize_new)
            except (RequestError, OSError, ValueError, sqlite3.Error) as exc:
                summary = {"state": "failed", "last_run": int(time.time()), "error": _error_code(exc)}
            write_private_json(progress, summary)
            time.sleep(INTERVAL_SECONDS)
    except (RequestError, OSError, ValueError, sqlite3.Error) as exc:
        write_private_json(progress, {"state": "failed", "last_run": int(time.time()), "error": _error_code(exc)})


if __name__ == "__main__":
    main()
