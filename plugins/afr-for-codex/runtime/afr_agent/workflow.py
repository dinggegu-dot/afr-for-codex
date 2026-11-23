"""AFR-owned file intake, article processing, and batch research updates."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import contextlib
import fcntl
import json
import os
import re
import sqlite3
import tempfile
import time

from . import questions, semantic
from .intake import (SUPPORTED, check_intake_roots, digest, exclusive_json, inbox_files, inside,
                     library_originals, private_json, scan, source_bytes)
from .library import RequestError


def database(vault):
    state = inside(vault, ".afr")
    state.mkdir(exist_ok=True)
    db = sqlite3.connect(inside(vault, ".afr/workflow.sqlite"))
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS workflow (
        sha256 TEXT PRIMARY KEY, folder TEXT NOT NULL, admitted INTEGER NOT NULL,
        phase TEXT NOT NULL DEFAULT 'admitted', error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, next_at INTEGER NOT NULL DEFAULT 0,
        plan TEXT, deferred TEXT, question_count INTEGER
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS cross_article_review (
        scope TEXT PRIMARY KEY, state TEXT NOT NULL, error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, next_at INTEGER NOT NULL DEFAULT 0,
        result TEXT
    )""")
    db.execute("CREATE TABLE IF NOT EXISTS domain_navigation (sha256 TEXT PRIMARY KEY)")
    db.execute("""CREATE TABLE IF NOT EXISTS cross_article_sources (
        sha256 TEXT PRIMARY KEY, scope TEXT NOT NULL
    )""")
    return db


def status(vault):
    with contextlib.closing(database(vault)) as db:
        articles = [
            dict(row) for row in db.execute(
                "SELECT sha256,folder,admitted,phase,error,attempts,question_count,deferred "
                "FROM workflow ORDER BY admitted, rowid"
            )
        ]
        review = db.execute("SELECT state,error,attempts,result FROM cross_article_review ORDER BY rowid DESC LIMIT 1").fetchone()
    for row in articles:
        row["deferred"] = json.loads(row["deferred"]) if row["deferred"] is not None else []
    cross_article_review = None
    if review is not None:
        cross_article_review = dict(review)
        cross_article_review["result"] = json.loads(review["result"]) if review["result"] else None
    return {"articles": articles, "cross_article_review": cross_article_review}


def resume_pinned_plan(vault, db, row, approvals):
    identifier = row["sha256"]
    pinned = json.loads(row["plan"])
    actions, revisions = pinned["actions"], pinned["revisions"]
    pending = set(json.loads(row["deferred"] or "[]")) | {
        action["topic_id"] for action in actions if "new_question" in action}
    pending.difference_update(action["topic_id"] for action in actions if "question_id" in action)
    with db:
        db.execute("UPDATE workflow SET deferred=? WHERE sha256=?", (json.dumps(sorted(pending)), identifier))
    if not semantic.allowed(identifier, approvals):
        raise RequestError("local_only")
    existing = [action for action in actions if "question_id" in action and "new_question" not in action]
    applied = questions.apply(vault, identifier, existing, expected_revisions=revisions)
    with db:
        db.execute(
            "UPDATE workflow SET phase='ready', error=NULL, question_count=?, next_at=0 WHERE sha256=?",
            (len(applied["questions"]), identifier),
        )

def process_article(vault, identifier, approvals, *, preserve_original):
    result = semantic.process(vault, identifier, approvals, preserve_original=preserve_original)
    if result["state"] == "existing_publication":
        raise RequestError("legacy_publication_requires_review")
    count = result.get("topics")
    if count is None:
        count = len(semantic.reviewed_plan(vault, identifier)["groups"])
    return count


def record_failure(db, row, exc):
    error = str(exc) if isinstance(exc, RequestError) else "local_io_failed"
    attempts = 0 if error == "local_only" else row["attempts"] + 1
    with db:
        if error in {"revision_changed", "existing_question_required"}:
            db.execute("UPDATE workflow SET plan=NULL WHERE sha256=?", (row["sha256"],))
        db.execute("UPDATE workflow SET error=?,attempts=?,next_at=? WHERE sha256=?",
                   (error, attempts, int(time.time()) + 30 * attempts, row["sha256"]))


def _migrate_completed_scope(db, ordered_versions, contract):
    """沿用可识别的最近一份旧回执，切换调度时不重新复核旧批次。"""
    if db.execute("SELECT 1 FROM cross_article_sources LIMIT 1").fetchone():
        return
    historical = {row[0] for row in db.execute(
        "SELECT scope FROM cross_article_review WHERE state='ready'")}
    if not historical:
        return
    for count in range(len(ordered_versions), max(1, len(ordered_versions) - 5), -1):
        versions = ordered_versions[:count]
        key = semantic.fingerprint({"contract": contract, "versions": sorted(versions)})
        if key in historical:
            with db:
                db.executemany("INSERT OR IGNORE INTO cross_article_sources VALUES (?,?)",
                               ((sha, key) for sha in versions))
            break


def _cross_article_batch(db, versions, contract):
    authorized = set(versions)
    now = time.time()
    pending = db.execute("""SELECT * FROM cross_article_review
        WHERE state IN ('working','pending') AND attempts < 3 AND next_at <= ? ORDER BY rowid""",
                         (now,)).fetchall()
    for row in pending:
        manifest = json.loads(row["result"] or "{}")
        if "new_versions" not in manifest:  # 旧格式没有固定批次。
            continue
        if set(manifest["versions"]).issubset(authorized):
            return row, manifest
        # 许可变了，撤销旧批次并只允许仍获准的版本重新成组。
        with db:
            db.execute("UPDATE cross_article_review SET state='superseded',error='local_only' WHERE scope=?",
                       (row["scope"],))
            db.execute("DELETE FROM cross_article_sources WHERE scope=?", (row["scope"],))

    observed = dict(db.execute("SELECT sha256,scope FROM cross_article_sources"))
    new = [sha for sha in versions if sha not in observed][:5]
    if not new:
        return None, None
    ready_scopes = {row[0] for row in db.execute("SELECT scope FROM cross_article_review WHERE state='ready'")}
    peers = [sha for sha in reversed(versions) if observed.get(sha) in ready_scopes][:5 - len(new)]
    if len(new) + len(peers) < 2:
        return None, None
    manifest = {"versions": sorted(new + peers), "new_versions": sorted(new)}
    scope = semantic.fingerprint({"contract": contract, **manifest})
    with db:
        db.execute("""INSERT INTO cross_article_review (scope,state,result) VALUES (?,'working',?)
                   ON CONFLICT(scope) DO UPDATE SET state='working',attempts=0,next_at=0,
                   error=NULL,result=excluded.result""", (scope, json.dumps(manifest)))
        db.executemany("INSERT INTO cross_article_sources VALUES (?,?)", ((sha, scope) for sha in new))
    state = db.execute("SELECT * FROM cross_article_review WHERE scope=?", (scope,)).fetchone()
    return state, manifest


def supplement_questions(vault, db, approvals):
    from . import reconcile

    rows = db.execute("SELECT sha256,deferred FROM workflow WHERE phase='ready' ORDER BY admitted,rowid").fetchall()
    approved = [row for row in rows if semantic.allowed(row["sha256"], approvals)]
    versions = [row["sha256"] for row in approved]
    _migrate_completed_scope(db, versions, reconcile.CONTRACT)
    state, manifest = _cross_article_batch(db, versions, reconcile.CONTRACT)
    if state is None:
        return
    scope = state["scope"]
    versions = manifest["versions"]
    try:
        result = reconcile.run(vault, versions, approvals, new_version_ids=manifest["new_versions"])
    except (RequestError, OSError, sqlite3.Error) as exc:
        error = str(exc) if isinstance(exc, RequestError) else "local_io_failed"
        attempts = state["attempts"] + 1
        with db:
            db.execute("UPDATE cross_article_review SET state='pending',error=?,attempts=?,next_at=? WHERE scope=?",
                       (error, attempts, int(time.time()) + 30 * attempts, scope))
        return
    approved = [row for row in approved if row["sha256"] in set(versions)]
    covered = {identifier: set() for identifier in versions}
    counts = {identifier: 0 for identifier in versions}
    for question in questions.catalog(vault):
        sources = {link["sha256"] for link in question["links"]}
        for sha in sources.intersection(covered):
            counts[sha] += 1
        for link in question["links"]:
            if link["sha256"] in covered:
                covered[link["sha256"]].add(link["topic_id"])
    with db:
        db.executemany("UPDATE workflow SET deferred=?,question_count=? WHERE sha256=?", [
            (json.dumps(sorted(set(json.loads(row["deferred"] or "[]")) - covered[row["sha256"]])),
             counts[row["sha256"]], row["sha256"])
            for row in approved
        ])
        db.execute("UPDATE cross_article_review SET state='ready',error=NULL,next_at=0,result=? WHERE scope=?",
                   (json.dumps({**manifest, **result}, ensure_ascii=False), scope))


def inbox_identity(inbox):
    st = inbox.stat()
    return [str(inbox.resolve()), st.st_dev, st.st_ino]


def seed_baseline(vault, inbox, *, legacy_roots=(), legacy_db=None, baseline_path=None, replace_existing=False):
    check_intake_roots(vault, inbox)
    with watcher_lock(vault):
        path = baseline_path if baseline_path is not None else inside(vault, ".afr/automatic-baseline.json")
        existing = path.exists() or path.is_symlink()
        if existing and (not replace_existing or baseline_path is None):
            raise RequestError("baseline_exists")
        identity = inbox_identity(inbox)
        historical = {digest(source_bytes(file)) for file in library_originals(vault)}
        if existing:
            historical.update(_read_baseline(inbox, path))
        for root in (inbox, *legacy_roots):
            if not root.is_dir():
                raise RequestError("invalid_path")
            historical.update(digest(source_bytes(file)) for file in inbox_files(root, strict=True)
                              if file.suffix.lower() in SUPPORTED)
        for filename, queries in (
            ("intake.sqlite", {"articles": "SELECT sha FROM articles",
                               "seen": "SELECT sha FROM seen"}),
            ("workflow.sqlite", {"workflow": "SELECT sha256 FROM workflow"}),
        ):
            db_path = inside(vault, f".afr/{filename}")
            if db_path.is_file():
                with contextlib.closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as db:
                    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    for table, query in queries.items():
                        if table in tables:
                            historical.update(sha for (sha,) in db.execute(query))
        if legacy_db is not None:
            with contextlib.closing(sqlite3.connect(legacy_db.as_uri() + "?mode=ro", uri=True)) as db:
                historical.update(sha for (sha,) in db.execute("""
                    SELECT json_extract(a.value, '$.object_sha256')
                    FROM evidence_snapshots s, json_each(s.artifact_manifest_json) a
                """))
        if any(not isinstance(sha, str) or not re.fullmatch("[0-9a-f]{64}", sha) for sha in historical):
            raise RequestError("invalid_baseline")
        if inbox_identity(inbox) != identity:
            raise RequestError("inbox_changed")
        data = {"version": 1, "inbox": identity, "sha256": sorted(historical)}
        if existing:
            stage = None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf-8") as stream:
                    stage = stream.name
                    json.dump(data, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(stage, path)
            finally:
                if stage is not None and os.path.exists(stage):
                    os.unlink(stage)
        else:
            exclusive_json(path, data)
    return {"historical_versions": len(historical)}


def load_baseline(vault, inbox, approvals, *, baseline_path=None):
    check_intake_roots(vault, inbox)
    if approvals is None:
        raise RequestError("approval_path_required")
    for root in (vault, inbox):
        if approvals.resolve().is_relative_to(root.resolve()) or approvals.absolute().is_relative_to(root.absolute()):
            raise RequestError("invalid_approval_path")
    path = baseline_path if baseline_path is not None else inside(vault, ".afr/automatic-baseline.json")
    return _read_baseline(inbox, path)


def _read_baseline(inbox, path):
    if not path.is_file():
        raise RequestError("baseline_required")
    try:
        state = private_json(path, 100 * 1024 * 1024)
    except (OSError, ValueError) as exc:
        raise RequestError("invalid_baseline") from exc
    versions = semantic.approved_hashes(state, "sha256") if isinstance(state, dict) else frozenset()
    if (not isinstance(state, dict) or state.get("version") != 1 or state.get("inbox") != inbox_identity(inbox)
            or not isinstance(state.get("sha256"), list) or len(versions) != len(state["sha256"])):
        raise RequestError("invalid_baseline")
    return versions


def cycle(vault, inbox, approvals=None, *, settle_seconds=2, preserve_original=False, new_articles=False,
          legacy_db=None, rename_generic=False, baseline_path=None):
    from .domain_index import ensure_entry

    historical = load_baseline(vault, inbox, approvals, baseline_path=baseline_path) if new_articles else frozenset()
    collected = scan(vault, inbox, legacy=legacy_db, settle_seconds=settle_seconds, rename_generic=rename_generic)
    admitted = {row["sha256"]: row["folder"] for row in collected["versions"]}
    now = int(time.time())
    with contextlib.closing(database(vault)) as db:
        with db:
            db.executemany("INSERT OR IGNORE INTO workflow (sha256,folder,admitted) VALUES (?,?,?)",
                           ((sha, folder, now) for sha, folder in admitted.items()))
        if new_articles:
            admitted_hashes = frozenset(sha for (sha,) in db.execute("SELECT sha256 FROM workflow"))
            approvals = semantic.WatchApprovals(approvals, admitted_hashes, historical)
        jobs = db.execute("SELECT * FROM workflow WHERE phase != 'ready' ORDER BY admitted,rowid").fetchall()
        pending = []
        for row in jobs:
            identifier = row["sha256"]
            if not semantic.allowed(identifier, approvals):
                if row["error"] != "local_only":
                    with db:
                        db.execute("UPDATE workflow SET error='local_only' WHERE sha256=?", (identifier,))
                continue
            if row["error"] != "local_only" and (row["next_at"] > now or row["attempts"] >= 3):
                continue
            if row["plan"] is None:
                pending.append(row)
                continue
            try:
                resume_pinned_plan(vault, db, row, approvals)
            except (RequestError, OSError, sqlite3.Error) as exc:
                record_failure(db, row, exc)

        # 文章各有自己的签发锁。流程数据库只由本线程更新。
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = {
                workers.submit(process_article, vault, row["sha256"], approvals,
                               preserve_original=preserve_original): row for row in pending
            }
            for future in as_completed(futures):
                row = futures[future]
                try:
                    count = future.result()
                except (RequestError, OSError, sqlite3.Error) as exc:
                    record_failure(db, row, exc)
                    continue
                with db:
                    db.execute("UPDATE workflow SET phase='ready',error=NULL,next_at=0,deferred=?,question_count=0"
                               " WHERE sha256=?", (json.dumps(list(range(1, count + 1))), row["sha256"]))
        for sha, folder in db.execute("SELECT sha256,folder FROM workflow WHERE phase='ready' AND NOT EXISTS "
                                      "(SELECT 1 FROM domain_navigation WHERE sha256=workflow.sha256)").fetchall():
            try:
                ensure_entry(vault, folder, sha)
            except (RequestError, OSError) as exc:
                collected["errors"].append({"file": folder, "error": str(exc) if isinstance(exc, RequestError)
                                             else type(exc).__name__})
                continue
            with db:
                db.execute("INSERT OR IGNORE INTO domain_navigation VALUES (?)", (sha,))
        supplement_questions(vault, db, approvals)
    return {
        "collected": len(collected["added"]), "settling": collected["settling"], "renamed": collected["renamed"],
        "errors": collected["errors"], "unsupported": collected["unsupported"],
        **status(vault),
    }


@contextlib.contextmanager
def watcher_lock(vault):
    if not vault.is_dir():
        raise RequestError("invalid_vault")
    state = inside(vault, ".afr")
    state.mkdir(exist_ok=True)
    with inside(vault, ".afr/workflow.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RequestError("watcher_running") from exc
        yield


def retry(vault, identifier):
    with watcher_lock(vault), contextlib.closing(database(vault)) as db, db:
        changed = db.execute(
            "UPDATE workflow SET error=NULL,attempts=0,next_at=0 WHERE sha256=? AND phase!='ready'",
            (identifier,),
        ).rowcount
        if not changed:
            changed = db.execute("""UPDATE cross_article_review SET error=NULL,attempts=0,next_at=0
                WHERE state='pending' AND scope=(
                    SELECT scope FROM cross_article_sources WHERE sha256=?
                )""", (identifier,)).rowcount
    if not changed:
        raise RequestError("not_found")
    return {"sha256": identifier, "state": "queued"}
