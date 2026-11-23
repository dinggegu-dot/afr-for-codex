"""本机分享版配置、知识库归属与按版本批准。"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from uuid import uuid4

from afr_agent.intake import check_intake_roots, exclusive_json, inside, private_json
from afr_agent.library import RequestError


HEX = re.compile(r"[0-9a-f]{64}")
INSTANCE = re.compile(r"[0-9a-f]{32}")
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
MAX_PRIVATE_BYTES = 1024 * 1024
STATE_OWNER = "afr-for-codex-state.json"
STATE_IDENTITY = {"product": "AFR for Codex", "version": 1}


@dataclass(frozen=True)
class Config:
    state_dir: Path
    inbox: Path
    vault: Path
    instance_id: str
    model: str | None
    rename_generic: bool
    allow_write: bool

    @property
    def approvals(self) -> Path:
        return policy_path(self.state_dir)


def owned_state_dir(path: Path) -> Path:
    """只接管空目录，安装和运行共享同一份独立目录标记。"""
    path = path.expanduser().absolute()
    if not path.exists():
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise RequestError("unsafe_state_directory")
    owner = path / STATE_OWNER
    if not owner.exists() and not any(path.iterdir()):
        try:
            exclusive_json(owner, STATE_IDENTITY)
        except FileExistsError:
            pass
    try:
        identity = private_json(owner, 4096)
    except (OSError, ValueError) as exc:
        raise RequestError("unsafe_state_directory") from exc
    if identity != STATE_IDENTITY:
        raise RequestError("unsafe_state_directory")
    return path.resolve(strict=True)


def policy_path(state_dir: Path) -> Path:
    return state_dir / "approvals.json"


@contextmanager
def settings_lock(state_dir: Path) -> Iterator[None]:
    root = owned_state_dir(state_dir)
    path = root / "settings.lock"
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600), "wb") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RequestError("unsafe_state_directory")
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def write_private_json(path: Path, value: object) -> None:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if len(data.encode()) > MAX_PRIVATE_BYTES:
        raise RequestError("settings_limit")
    before = None
    try:
        before = path.lstat()
    except FileNotFoundError:
        pass
    if before and (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                   or before.st_mode & 0o077):
        raise RequestError("unsafe_state_file")
    stage = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".afr-share-",
                                         delete=False) as stream:
            stage = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stage, path)
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)


def read_policy(state_dir: Path) -> dict:
    path = policy_path(state_dir)
    try:
        data = private_json(path, MAX_PRIVATE_BYTES)
    except FileNotFoundError:
        return {"destination": "OpenAI Codex", "sha256": [], "denied_sha256": [], "allow_new_articles": False}
    except (OSError, ValueError) as exc:
        raise RequestError("invalid_model_policy") from exc
    _validate_policy(data)
    return data


def _validate_policy(data: object) -> None:
    if (not isinstance(data, dict) or set(data) != {"destination", "sha256", "denied_sha256", "allow_new_articles"}
            or data["destination"] != "OpenAI Codex" or type(data["allow_new_articles"]) is not bool):
        raise RequestError("invalid_model_policy")
    for name in ("sha256", "denied_sha256"):
        ids = data[name]
        if (not isinstance(ids, list) or not all(isinstance(item, str) and HEX.fullmatch(item) for item in ids)
                or len(set(ids)) != len(ids)):
            raise RequestError("invalid_model_policy")


def update_policy(state_dir: Path, mutate: Callable[[dict], None]) -> dict:
    state_dir = owned_state_dir(state_dir)
    with settings_lock(state_dir):
        policy = read_policy(state_dir)
        mutate(policy)
        _validate_policy(policy)
        write_private_json(policy_path(state_dir), policy)
    return policy


def _local_dir(value: str, *, allow_missing: bool = False) -> Path:
    if not isinstance(value, str) or not value or "\0" in value or len(value) > 1024:
        raise RequestError("invalid_directory")
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise RequestError("invalid_directory")
        path = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise RequestError("invalid_directory") from exc
    if path in (Path(path.anchor), Path.home().resolve()):
        raise RequestError("invalid_directory")
    if not path.is_dir() and not (allow_missing and not path.exists()):
        raise RequestError("invalid_directory")
    return path


def _scope(root: Path, inbox: Path, vault: Path) -> None:
    if (root.is_relative_to(inbox) or root.is_relative_to(vault) or inbox == vault
            or inbox.is_relative_to(vault) or vault.is_relative_to(inbox)):
        raise RequestError("overlapping_directories")


def _owner_file(vault: Path) -> Path:
    return inside(vault, ".afr/share-owner.json")


def _read_owner(vault: Path) -> str:
    try:
        owner = private_json(_owner_file(vault), 4096)
        if (not isinstance(owner, dict) or set(owner) != {"instance_id"}
                or not isinstance(owner["instance_id"], str) or not INSTANCE.fullmatch(owner["instance_id"])):
            raise RequestError("vault_already_managed")
    except (OSError, ValueError) as exc:
        raise RequestError("vault_already_managed") from exc
    return owner["instance_id"]


def _claim(root: Path, vault: Path) -> str:
    owner = _owner_file(vault)
    if owner.exists() or owner.is_symlink():
        # 首次配置进程中断时，只允许同一个安装恢复归属。
        candidate = root / "setup-instance.json"
        try:
            pending = private_json(candidate, 4096)
        except (OSError, ValueError) as exc:
            raise RequestError("vault_already_managed") from exc
        existing = _read_owner(vault)
        if pending != {"instance_id": existing, "vault": str(vault)}:
            raise RequestError("vault_already_managed")
        return existing
    if owner.parent.exists() and any(owner.parent.iterdir()):
        raise RequestError("vault_already_managed")
    owner.parent.mkdir(mode=0o700, exist_ok=True)
    identifier = uuid4().hex
    write_private_json(root / "setup-instance.json", {"instance_id": identifier, "vault": str(vault)})
    try:
        exclusive_json(owner, {"instance_id": identifier})
    except FileExistsError as exc:
        raise RequestError("vault_already_managed") from exc
    return identifier


class Store:
    def __init__(self, state_dir: Path):
        self.state_dir = owned_state_dir(state_dir)
        self.path = self.state_dir / "settings.json"

    def _value(self) -> dict:
        try:
            data = private_json(self.path, MAX_PRIVATE_BYTES)
        except FileNotFoundError as exc:
            raise RequestError("setup_required") from exc
        except (OSError, ValueError) as exc:
            raise RequestError("invalid_settings") from exc
        expected = {"version", "instance_id", "inbox", "vault", "model", "rename_generic", "allow_write"}
        if (not isinstance(data, dict) or set(data) != expected or type(data["version"]) is not int
                or data["version"] != 1 or not isinstance(data["instance_id"], str)
                or not INSTANCE.fullmatch(data["instance_id"]) or type(data["rename_generic"]) is not bool
                or type(data["allow_write"]) is not bool or not _valid_model(data["model"])):
            raise RequestError("invalid_settings")
        return data

    def load(self) -> Config:
        data = self._value()
        inbox = _local_dir(data["inbox"])
        vault = _local_dir(data["vault"])
        _scope(self.state_dir, inbox, vault)
        if str(inbox) != data["inbox"] or str(vault) != data["vault"] or _read_owner(vault) != data["instance_id"]:
            raise RequestError("vault_already_managed")
        return Config(self.state_dir, inbox, vault, data["instance_id"], data["model"],
                      data["rename_generic"], data["allow_write"])

    def configure(self, *, inbox: str | None = None, vault: str | None = None, model: str | None = None,
                  rename_generic: bool | None = None) -> Config:
        if not _valid_model(model) or (rename_generic is not None and type(rename_generic) is not bool):
            raise RequestError("invalid_settings")
        with settings_lock(self.state_dir):
            try:
                current = self.load()
            except RequestError as exc:
                if str(exc) != "setup_required":
                    raise
            else:
                if ((inbox is not None and _local_dir(inbox) != current.inbox)
                        or (vault is not None and _local_dir(vault) != current.vault)
                        or (model is not None and model != current.model)
                        or (rename_generic is not None and rename_generic != current.rename_generic)):
                    raise RequestError("already_configured")
                return current
            inbox_path = _local_dir(str(self.state_dir / "Inbox") if inbox is None else inbox, allow_missing=True)
            vault_path = _local_dir(str(self.state_dir / "Library") if vault is None else vault, allow_missing=True)
            _scope(self.state_dir, inbox_path, vault_path)
            vault_path.mkdir(parents=True, exist_ok=True, mode=0o700)
            identifier = _claim(self.state_dir, vault_path)
            inbox_path.mkdir(parents=True, exist_ok=True, mode=0o700)
            check_intake_roots(vault_path, inbox_path)
            if not policy_path(self.state_dir).exists():
                write_private_json(policy_path(self.state_dir), read_policy(self.state_dir))
            write_private_json(self.path, {"version": 1, "instance_id": identifier, "inbox": str(inbox_path),
                                           "vault": str(vault_path), "model": model,
                                           "rename_generic": bool(rename_generic), "allow_write": False})
            (self.state_dir / "setup-instance.json").unlink(missing_ok=True)
        return self.load()

    def discussion_write(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise RequestError("invalid_request")
        with settings_lock(self.state_dir):
            self.load()
            data = self._value()
            data["allow_write"] = enabled
            write_private_json(self.path, data)


def _valid_model(model: object) -> bool:
    return model is None or (isinstance(model, str) and MODEL.fullmatch(model) is not None)
