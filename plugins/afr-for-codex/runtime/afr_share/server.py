"""AFR for Codex 的本机 MCP 入口和按需任务执行。"""

import argparse
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import time
from urllib.parse import quote
from uuid import uuid4

from afr_agent import domain_index, intake, mcp, questions, reconcile, semantic, workflow
from afr_agent.library import RequestError
from afr_agent.presentation import evidence_for

from .config import (HEX, INSTANCE, MAX_PRIVATE_BYTES, Store, read_policy, settings_lock,
                     update_policy, write_private_json)


BOOL = {"type": "boolean"}
CONFIRM = {"type": "boolean", "const": True}
PATH = {"type": "string", "minLength": 1, "maxLength": 1024}
CANDIDATE_ID = {"type": "string", "pattern": r"^c-[0-9a-f]{32}$"}
TOOLS = {tool["name"]: tool for tool in (
    mcp.tool("afr_setup",
             "首次使用时选择专用收件箱和 Obsidian 库。用户可确认使用独立默认目录。"
             "可选启用通用文件名重命名。此操作只创建本机配置。",
             {"inbox": PATH, "vault": PATH, "model": {"type": "string", "minLength": 1, "maxLength": 80},
              "rename_generic": BOOL, "confirm": CONFIRM}, ["confirm"], read_only=False),
    mcp.tool("afr_status", "查看本机收录、授权、任务和已签发白板状态。article_sha 可定位固定版本。",
             {"article_sha": mcp.SHA}, []),
    mcp.tool("afr_scan", "从已配置收件箱收录文件，返回状态和固定版本编号。"
             "指定收件箱内已知的相对文件名时，仅返回该文件的版本。",
             {"filename": PATH}, [], read_only=False),
    mcp.tool("afr_authorize", "仅在用户同意由 OpenAI Codex 处理这个固定版本后调用。",
             {"article_sha": mcp.SHA, "confirm": CONFIRM}, ["article_sha", "confirm"], read_only=False),
    mcp.tool("afr_revoke", "撤回这个固定版本的 Codex 处理和读取权限。保留本机资料及已经生成的成果。",
             {"article_sha": mcp.SHA, "confirm": CONFIRM}, ["article_sha", "confirm"], read_only=False),
    mcp.tool("afr_process", "用户确认后，排队处理一个已授权的固定版本，完成后用 afr_status 获取白板和完整阅读。"
             "返回 queued 表示任务尚未完成。",
             {"article_sha": mcp.SHA, "confirm": CONFIRM}, ["article_sha", "confirm"], read_only=False),
    mcp.tool("afr_review_questions", "用户确认后，从已经签发并获准的多篇来源更新研究问题。",
             {"confirm": CONFIRM}, ["confirm"], read_only=False),
    mcp.tool("afr_list_question_candidates", "查看待确认的跨篇研究问题与固定出处。只有批次中所有来源均明确逐篇授权且已签发时，才会显示。",
             {"batch_id": mcp.SHA, "limit": mcp.LIMIT, "offset": mcp.OFFSET}, []),
    mcp.tool("afr_promote_question_candidate", "用户确认一个具名候选问题后，将其和全部固定来源一起写入 Obsidian 研究问题索引。",
             {"batch_id": mcp.SHA, "candidate_id": CANDIDATE_ID, "confirm": CONFIRM},
             ["batch_id", "candidate_id", "confirm"], read_only=False),
    mcp.tool("afr_discussion_write", "用户确认后授予或撤销 Codex 研究问题讨论的读写能力。",
             {"enabled": BOOL, "confirm": CONFIRM}, ["enabled", "confirm"], read_only=False),
    mcp.tool("afr_watch_start", "用户确认后启用 macOS 收件箱监听。默认仅自动收录。"
             "authorize_new 为真时，须经用户明确同意其未来放入的材料可发送到 OpenAI Codex，"
             "启动时已经存在的版本继续按单篇授权判断。",
             {"authorize_new": BOOL, "confirm": CONFIRM}, ["confirm"], read_only=False),
    mcp.tool("afr_watch_stop", "用户确认后关闭 macOS 监听和未来新文章的自动授权。",
             {"confirm": CONFIRM}, ["confirm"], read_only=False),
)}
for name in ("afr_authorize", "afr_process", "afr_review_questions", "afr_watch_start"):
    TOOLS[name]["annotations"]["openWorldHint"] = True

HTML_MARKUP = re.compile(r"<(?:/?[A-Za-z][A-Za-z0-9-]*(?=[\s/>])|!|\?)")


SAFE_ERRORS = mcp.SAFE_ERRORS | {
    "already_configured", "baseline_required", "file_in_progress", "inbox_changed", "invalid_baseline",
    "invalid_codex_model", "invalid_directory", "invalid_model_policy", "invalid_path", "invalid_reconciliation",
    "invalid_review_receipt",
    "invalid_settings", "job_busy", "local_io_failed", "overlapping_directories", "setup_required",
    "settings_limit", "unsafe_state_directory", "unsafe_state_file", "vault_already_managed",
    "watcher_running", "watch_unsupported", "watcher_unsupported", "watch_start_failed", "watch_stop_failed",
    "watch_not_running", "watch_unavailable", "watch_requires_macos", "watch_service_unavailable",
    "watch_already_loaded", "worker_launch_failed", "worker_interrupted", "unsupported_file",
    "codex_unavailable", "codex_authentication", "codex_timeout", "codex_model_unavailable", "codex_failed",
    "invalid_codex_output", "legacy_publication_requires_review",
    "unsafe_discussion_markup",
}


def safe_error(exc):
    return (str(exc) if isinstance(exc, RequestError)
            and re.fullmatch(r"[a-z][a-z0-9_]{2,79}", str(exc)) else "local_io_failed")


def _watch():
    from . import watch
    return watch


def watch_status(cfg):
    try:
        return _watch().status(cfg.state_dir)
    except (ImportError, RequestError, OSError, ValueError) as exc:
        return {"running": False, "error": safe_error(exc)}


def _auto_enabled(watch):
    return bool(watch.get("loaded") and watch.get("running") and watch.get("authorize_new"))


def effective_policy(cfg, db=None, *, permit_automatic=None):
    policy = read_policy(cfg.state_dir)
    current = cfg.approvals
    if policy["allow_new_articles"] and (
        permit_automatic if permit_automatic is not None else _auto_enabled(watch_status(cfg))
    ):
        historical = workflow.load_baseline(cfg.vault, cfg.inbox, current,
                                            baseline_path=cfg.state_dir / _watch().BASELINE)
        if db is None:
            with closing(workflow.database(cfg.vault)) as handle:
                admitted = frozenset(row[0] for row in handle.execute("SELECT sha256 FROM workflow"))
        else:
            admitted = frozenset(row[0] for row in db.execute("SELECT sha256 FROM workflow"))
        return semantic.WatchApprovals(current, admitted, historical)
    return current


def _job_path(cfg):
    return cfg.state_dir / "manual-job.json"


def _lease(cfg):
    fd = os.open(cfg.state_dir / "manual-job.lock",
                 os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CREAT, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RequestError("unsafe_state_file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_job(cfg):
    try:
        job = intake.private_json(_job_path(cfg), MAX_PRIVATE_BYTES)
    except FileNotFoundError:
        return None
    if (not isinstance(job, dict) or not isinstance(job.get("id"), str) or not INSTANCE.fullmatch(job["id"])
            or job.get("action") not in ("process", "review")
            or job.get("state") not in ("queued", "running", "ready", "no_new_batch", "failed")
            or ("article_sha" in job and (not isinstance(job["article_sha"], str)
                                         or not HEX.fullmatch(job["article_sha"])))):
        raise RequestError("invalid_settings")
    return job


def job_status(cfg):
    with settings_lock(cfg.state_dir):
        job = _read_job(cfg)
        if not job or job["state"] not in ("queued", "running"):
            return job
        try:
            fd = _lease(cfg)
        except BlockingIOError:
            return job
        try:
            job = _read_job(cfg)
            if job and job["state"] in ("queued", "running"):
                job.update(state="failed", error="worker_interrupted", finished_at=int(time.time()))
                write_private_json(_job_path(cfg), job)
            return job
        finally:
            os.close(fd)


def _article(cfg, sha, row=None, *, approved=None):
    if row is None:
        with closing(workflow.database(cfg.vault)) as db:
            row = db.execute("SELECT * FROM workflow WHERE sha256=?", (sha,)).fetchone()
        if row is None:
            raise RequestError("not_found")
    if approved is None:
        approved = semantic.approved_versions(effective_policy(cfg))
    state = row["phase"]
    result = {"sha256": sha, "authorized": sha in approved, "state": state,
              "error": row["error"], "attempts": row["attempts"]}
    if sha in approved and result["error"] == "local_only":
        result["error"] = None
    if state == "ready" and sha in approved:
        # 只返回已签发成果的定位。正文读取受按版本批准保护。
        paths = {
            key: intake.inside(cfg.vault, value)
            for key, value in semantic.publication_paths(cfg.vault, sha, row["folder"]).items()
        }
        result["files"] = {
            key: {"path": str(path), "obsidian": "obsidian://open?path=" + quote(str(path), safe="")}
            for key, path in paths.items() if path.is_file()
        }
    return result


def _explicit_versions(cfg):
    policy = read_policy(cfg.state_dir)
    return frozenset(policy["sha256"]) - frozenset(policy["denied_sha256"])


def _review_batches(cfg, batch_id=None):
    sql = "SELECT scope,result FROM cross_article_review WHERE state='ready' AND result IS NOT NULL"
    with closing(workflow.database(cfg.vault)) as db:
        if batch_id is None:
            return db.execute(sql + " ORDER BY rowid DESC").fetchall()
        return db.execute(sql + " AND scope=?", (batch_id,)).fetchall()


def _candidate_id(batch_id, cluster):
    return "c-" + semantic.fingerprint([batch_id, cluster])[:32]


def _candidate_batch(cfg, row, explicit):
    try:
        result = json.loads(row["result"])
    except ValueError as exc:
        raise RequestError("invalid_reconciliation") from exc
    if not isinstance(result, dict):
        raise RequestError("invalid_reconciliation")
    if not result.get("candidate_path"):
        return None, []
    return reconcile.read_candidates(cfg.vault, result, explicit)


def _pending_candidates(cfg, explicit, batch_id=None):
    known = {row["id"]: {(link["sha256"], link["topic_id"]) for link in row["links"]}
             for row in questions.catalog(cfg.vault)}
    batches = _review_batches(cfg, batch_id)
    if batch_id and not batches:
        raise RequestError("not_found")
    for row in batches:
        try:
            path, clusters = _candidate_batch(cfg, row, explicit)
        except RequestError as exc:
            if batch_id or str(exc) not in {"local_only", "review_required", "invalid_reconciliation",
                                            "invalid_review_receipt", "not_found"}:
                raise
            continue
        if path is None:
            continue
        pending = []
        for cluster in clusters:
            sources = {(link["sha256"], link["topic_id"]) for link in cluster["links"]}
            question_id = questions.id_for_title(cluster["new_question"])
            if not sources.issubset(known.get(question_id, set())):
                pending.append(cluster)
        yield row["scope"], path, pending


def _candidate_status(cfg):
    with settings_lock(cfg.state_dir):
        recent = next(_pending_candidates(cfg, _explicit_versions(cfg)), None)
        if recent is None:
            return {}
        batch, relative, pending = recent
        path = intake.inside(cfg.vault, relative)
        result = {"candidate_batch_id": batch, "candidate_count": len(pending)}
        if path.is_file():
            result["candidate_artifact"] = {"path": str(path), "obsidian": "obsidian://open?path=" + quote(str(path), safe="")}
        return result


def _list_candidates(cfg, batch_id=None, limit=20, offset=0):
    with settings_lock(cfg.state_dir):
        selected = []
        for batch, _, clusters in _pending_candidates(cfg, _explicit_versions(cfg), batch_id):
            selected.extend((batch, cluster) for cluster in clusters)
            if len(selected) > offset + limit:
                break
        candidates = []
        for batch, cluster in selected[offset:offset + limit]:
            sources = []
            for link in cluster["links"]:
                topic = questions.read_topic(cfg.vault, link["sha256"], link["topic_id"])
                sources.append({**link, "topic_revision": topic["revision"], "path": topic["path"],
                                "article_title": topic["article_title"], "topic_title": topic["title"]})
            candidates.append({"batch_id": batch, "candidate_id": _candidate_id(batch, cluster),
                               "title": cluster["new_question"], "sources": sources})
        return {"candidates": candidates, "next_offset": offset + limit if len(selected) > offset + limit else None}


def _promote_candidate(cfg, batch_id, candidate_id):
    with settings_lock(cfg.state_dir):
        explicit = _explicit_versions(cfg)
        rows = _review_batches(cfg, batch_id)
        if not rows:
            raise RequestError("not_found")
        _, clusters = _candidate_batch(cfg, rows[0], explicit)
        for cluster in clusters:
            if candidate_id == _candidate_id(batch_id, cluster):
                result = questions.promote_candidate(cfg.vault, cluster["new_question"], cluster["links"], explicit)
                relative = result["path"]
                path = intake.inside(cfg.vault, relative)
                return {"id": result["id"], "title": result["title"], "revision": result["revision"],
                        "added_links": result["added_links"], "path": str(path),
                        "obsidian": "obsidian://open?path=" + quote(str(path), safe="")}
        raise RequestError("not_found")


def _summary(cfg, sha=None):
    policy = read_policy(cfg.state_dir)
    watcher = watch_status(cfg)
    automatic = policy["allow_new_articles"] and _auto_enabled(watcher)
    result = {"configured": True, "inbox": str(cfg.inbox), "vault": str(cfg.vault),
              "model": cfg.model or os.environ.get("AFR_FOR_CODEX_MODEL") or "account_default",
              "rename_generic": cfg.rename_generic, "discussion_write": cfg.allow_write,
              "model_destination": policy["destination"], "automatically_authorize_new": automatic,
              "watch": watcher, "job": job_status(cfg)}
    state = workflow.status(cfg.vault)
    result["article_count"] = len(state["articles"])
    result["article_counts_by_state"] = {
        phase: sum(row["phase"] == phase for row in state["articles"])
        for phase in sorted({row["phase"] for row in state["articles"]})
    }
    review = state["cross_article_review"]
    result["research"] = ({**{k: review[k] for k in ("state", "error", "attempts")}, **_candidate_status(cfg)}
                          if review else None)
    approved = semantic.approved_versions(effective_policy(cfg, permit_automatic=automatic))
    if sha:
        result["article"] = _article(cfg, sha, approved=approved)
    else:
        result["recent_articles"] = [_article(cfg, row["sha256"], row, approved=approved)
                                     for row in reversed(state["articles"][-25:])]
    return result


def _scan(cfg, filename=None):
    selected = None
    if filename is not None:
        relative = Path(filename)
        if (relative.is_absolute() or not relative.parts or ".." in relative.parts
                or any(part.startswith(".") for part in relative.parts) or relative.name.startswith("~$")):
            raise RequestError("invalid_path")
        path = intake.inside(cfg.inbox, relative)
        if path.suffix.lower() not in intake.SUPPORTED:
            raise RequestError("unsupported_file")
        if not path.is_file():
            raise RequestError("not_found")
        selected = intake.digest(intake.source_bytes(path))
    scan = intake.scan(cfg.vault, cfg.inbox, rename_generic=cfg.rename_generic)
    with closing(workflow.database(cfg.vault)) as db, db:
        now = int(time.time())
        db.executemany("INSERT OR IGNORE INTO workflow (sha256,folder,admitted) VALUES (?,?,?)",
                       ((row["sha256"], row["folder"], now) for row in scan["versions"]))
    # 初读路径和标题可能来自正文。留在本机，由已批准的读取工具按需返回。
    return {"collected": len(scan["added"]), "settling": scan["settling"], "renamed": scan["renamed"],
            "versions": [{k: row[k] for k in ("sha256", "readable")} for row in scan["versions"]
                         if selected is None or row["sha256"] == selected],
            "errors": [x["error"] if re.fullmatch(r"[A-Za-z_]{1,80}", x["error"]) else "local_io_failed"
                       for x in scan["errors"]], "unsupported_count": len(scan["unsupported"])}


def _authorize(cfg, sha, grant):
    with closing(workflow.database(cfg.vault)) as db:
        if db.execute("SELECT 1 FROM workflow WHERE sha256=?", (sha,)).fetchone() is None:
            raise RequestError("not_found")
    if grant:
        _, evidence = evidence_for(cfg.vault, sha)
        if not evidence["parts"]:
            raise RequestError("requires_ocr")

    def apply(policy):
        granted, denied = set(policy["sha256"]), set(policy["denied_sha256"])
        if grant:
            granted.add(sha)
            denied.discard(sha)
        else:
            granted.discard(sha)
            denied.add(sha)
        policy.update(sha256=sorted(granted), denied_sha256=sorted(denied))

    update_policy(cfg.state_dir, apply)
    return {"sha256": sha, "authorized": grant, "destination": "OpenAI Codex"}


def _queue(cfg, action, sha=None):
    if sha:
        article = _article(cfg, sha)
        if not article["authorized"]:
            raise RequestError("local_only")
        if article["state"] == "ready":
            return article
    if watch_status(cfg).get("running"):
        return {"state": "queued_by_watcher", "action": action, **({"article_sha": sha} if sha else {})}
    with settings_lock(cfg.state_dir):
        try:
            fd = _lease(cfg)
        except BlockingIOError as exc:
            current = _read_job(cfg)
            if current and current["action"] == action and current.get("article_sha") == sha:
                return current
            raise RequestError("job_busy") from exc
        try:
            job = {"id": uuid4().hex, "action": action, "state": "queued", "created_at": int(time.time())}
            if sha:
                job["article_sha"] = sha
            write_private_json(_job_path(cfg), job)
            argv = [sys.executable, "-B", "-m", "afr_share.server", "--state-dir", str(cfg.state_dir),
                    "--run-once", "--lease-fd", str(fd), "--job-id", job["id"]]
            try:
                subprocess.Popen(argv, cwd=str(Path(__file__).resolve().parent.parent), pass_fds=(fd,),
                                 start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except OSError as exc:
                job.update(state="failed", error="worker_launch_failed", finished_at=int(time.time()))
                write_private_json(_job_path(cfg), job)
                raise RequestError("worker_launch_failed") from exc
            return job
        finally:
            os.close(fd)


def _run_article(cfg, db, job):
    sha = job["article_sha"]
    policy = effective_policy(cfg, db)
    if not semantic.allowed(sha, policy):
        raise RequestError("local_only")
    row = db.execute("SELECT * FROM workflow WHERE sha256=?", (sha,)).fetchone()
    if row is None:
        raise RequestError("not_found")
    if row["phase"] != "ready":
        try:
            count = workflow.process_article(cfg.vault, sha, policy, preserve_original=True)
        except (RequestError, OSError, TypeError, ValueError, sqlite3.Error) as exc:
            workflow.record_failure(db, row, RequestError(safe_error(exc)))
            raise
        with db:
            db.execute("UPDATE workflow SET phase='ready',error=NULL,next_at=0,deferred=?,question_count=0 "
                       "WHERE sha256=?", (json.dumps(list(range(1, count + 1))), sha))
    domain_index.ensure_entry(cfg.vault, row["folder"], sha)
    with db:
        db.execute("INSERT OR IGNORE INTO domain_navigation VALUES (?)", (sha,))
    return "ready"


def _run_review(cfg, db):
    approvals = effective_policy(cfg, db)
    before = {row["scope"]: tuple(row) for row in db.execute("SELECT * FROM cross_article_review")}
    workflow.supplement_questions(cfg.vault, db, approvals)
    row = next((row for row in db.execute("SELECT * FROM cross_article_review ORDER BY rowid DESC")
                if row["state"] != "superseded" and before.get(row["scope"]) != tuple(row)), None)
    if row is None:
        return "no_new_batch"
    if row["state"] in ("pending", "working"):
        raise RequestError(row["error"] or "review_required")
    return "ready" if row["state"] == "ready" else "no_new_batch"


def run_once(store, fd, job_id):
    cfg = store.load()
    if fd is None or job_id is None or not INSTANCE.fullmatch(job_id):
        raise RequestError("invalid_request")
    saved = os.stat(cfg.state_dir / "manual-job.lock", follow_symlinks=False)
    inherited = os.fstat(fd)
    if (not stat.S_ISREG(saved.st_mode) or (saved.st_dev, saved.st_ino) != (inherited.st_dev, inherited.st_ino)):
        raise RequestError("invalid_request")
    os.set_inheritable(fd, False)
    try:
        job = _read_job(cfg)
        if not job or job["id"] != job_id or job["state"] != "queued":
            raise RequestError("invalid_request")
        job.update(state="running", started_at=int(time.time()))
        write_private_json(_job_path(cfg), job)
        if cfg.model:
            os.environ["AFR_FOR_CODEX_MODEL"] = cfg.model
        try:
            with workflow.watcher_lock(cfg.vault), closing(workflow.database(cfg.vault)) as db:
                state = _run_article(cfg, db, job) if job["action"] == "process" else _run_review(cfg, db)
        except (RequestError, OSError, TypeError, ValueError, KeyError, sqlite3.Error) as exc:
            job.update(state="failed", error=safe_error(exc))
        else:
            job["state"] = state
        job["finished_at"] = int(time.time())
        write_private_json(_job_path(cfg), job)
    finally:
        os.close(fd)


def _validate(spec, args):
    schema = spec["inputSchema"]
    if not isinstance(args, dict) or set(args) - schema["properties"].keys() or set(schema["required"]) - args.keys():
        raise RequestError("invalid_request")
    for key, value in args.items():
        field = schema["properties"][key]
        if field["type"] == "boolean":
            if type(value) is not bool or ("const" in field and value != field["const"]):
                raise RequestError("invalid_request")
        elif field["type"] == "integer":
            if type(value) is not int or not field["minimum"] <= value <= field.get("maximum", value):
                raise RequestError("invalid_request")
        elif (not isinstance(value, str) or not field.get("minLength", 0) <= len(value) <= field.get("maxLength", 1024)
              or ("pattern" in field and re.fullmatch(field["pattern"], value) is None)):
            raise RequestError("invalid_request")


def _validate_discussion(body):
    if not isinstance(body, str):
        raise RequestError("invalid_request")
    # 普通链接和 Obsidian 双链仍可保存。阻止投影渲染时自动加载的图片、嵌入和原始 HTML。
    if "![" in body or HTML_MARKUP.search(body):
        raise RequestError("unsafe_discussion_markup")


class ShareTools:
    def __init__(self, store):
        self.store = store

    def _research(self, cfg):
        return mcp.ResearchTools(cfg.vault, effective_policy(cfg), cfg.allow_write,
                                 allow_discussion_read=cfg.allow_write)

    def available_tools(self):
        # 客户端可缓存工具清单。权限在每次调用时读取，开启讨论后可继续使用同一会话。
        research_specs = []
        for name, spec in mcp.TOOLS.items():
            if name in ("read_discussion", "update_question", "revise_discussion"):
                spec = {**spec, "description": "用户须先显式开启讨论读写。" + spec["description"]}
            if not spec["annotations"]["readOnlyHint"]:
                schema = spec["inputSchema"]
                spec["inputSchema"] = {**schema, "properties": {**schema["properties"], "confirm": CONFIRM},
                                       "required": [*schema["required"], "confirm"]}
            research_specs.append(spec)
        return [*TOOLS.values(), *research_specs]

    def call(self, name, args):
        if name not in TOOLS and name not in mcp.TOOLS:
            raise RequestError("unknown_tool")
        if name in mcp.TOOLS:
            cfg = self.store.load()
            if not mcp.TOOLS[name]["annotations"]["readOnlyHint"]:
                if not isinstance(args, dict) or args.get("confirm") is not True:
                    raise RequestError("invalid_request")
                if not cfg.allow_write:
                    raise RequestError("write_disabled")
                _validate_discussion(args.get("body" if name == "update_question" else "replacement"))
                args = {key: value for key, value in args.items() if key != "confirm"}
            return self._research(cfg).call(name, args)
        _validate(TOOLS[name], args)
        if name == "afr_setup":
            cfg = self.store.configure(**{k: v for k, v in args.items() if k != "confirm"})
            return _summary(cfg)
        try:
            cfg = self.store.load()
        except RequestError as exc:
            if name == "afr_status" and str(exc) == "setup_required" and not args:
                return {"configured": False, "setup_required": True}
            raise
        if name == "afr_status":
            return _summary(cfg, args.get("article_sha"))
        if name == "afr_scan":
            return _scan(cfg, args.get("filename"))
        if name in ("afr_authorize", "afr_revoke"):
            return _authorize(cfg, args["article_sha"], name == "afr_authorize")
        if name == "afr_process":
            return _queue(cfg, "process", args["article_sha"])
        if name == "afr_review_questions":
            return _queue(cfg, "review")
        if name == "afr_list_question_candidates":
            return _list_candidates(cfg, **args)
        if name == "afr_promote_question_candidate":
            return _promote_candidate(cfg, args["batch_id"], args["candidate_id"])
        if name == "afr_discussion_write":
            self.store.discussion_write(args["enabled"])
            return {"discussion_write": args["enabled"]}
        if name == "afr_watch_start":
            current = job_status(cfg)
            if current and current["state"] in ("queued", "running"):
                raise RequestError("job_busy")
            return _watch().start(cfg.state_dir, cfg.inbox, cfg.vault, cfg.approvals,
                                  authorize_new=args.get("authorize_new", False),
                                  model=cfg.model,
                                  rename_generic=cfg.rename_generic)
        return _watch().stop(cfg.state_dir)


class Session(mcp.Session):
    def dispatch(self, request):
        if (isinstance(request, dict) and request.get("jsonrpc") == "2.0" and self.ready
                and type(request.get("id")) in (str, int) and request.get("method") == "tools/call"
                and isinstance(request.get("params", {}), dict)):
            params = request.get("params", {})
            try:
                if not isinstance(params.get("name"), str):
                    raise RequestError("invalid_request")
                payload = self.research.call(params["name"], params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}
            except RequestError as exc:
                result = {"content": [{"type": "text", "text": str(exc) if str(exc) in SAFE_ERRORS
                                       else "research_data_unavailable"}], "isError": True}
            except (ImportError, OSError, TypeError, ValueError, KeyError, sqlite3.Error):
                result = {"content": [{"type": "text", "text": "research_data_unavailable"}], "isError": True}
            return {"jsonrpc": "2.0", "id": request["id"], "result": result}
        response = super().dispatch(request)
        if isinstance(response, dict) and "serverInfo" in response.get("result", {}):
            response["result"]["serverInfo"] = {"name": "afr-for-codex", "version": "0.1.0"}
        return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--run-once", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--lease-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--job-id", help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        store = Store(args.state_dir)
        if args.run_once:
            run_once(store, args.lease_fd, args.job_id)
            return 0
        if args.lease_fd is not None or args.job_id is not None:
            raise RequestError("invalid_request")
    except (RequestError, OSError, ValueError) as exc:
        print(safe_error(exc), file=sys.stderr)
        return 1

    session = Session(ShareTools(store))
    for line in sys.stdin.buffer:
        try:
            request = json.loads(line.decode("utf-8"))
        except (UnicodeError, ValueError):
            response = mcp.rpc_error(None, -32700, "Parse error")
        else:
            response = session.receive(request)
        if response is not None:
            sys.stdout.buffer.write(json.dumps(response, ensure_ascii=True).encode("ascii") + b"\n")
            sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
