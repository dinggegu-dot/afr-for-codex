import argparse
import json
from pathlib import Path
import sys
import sqlite3
import time

from . import library


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise library.RequestError("invalid_request")


def main():
    parser = Parser(prog="afr_agent")
    parser.add_argument("--version", action="version", version="afr_agent 1")
    actions = parser.add_subparsers(dest="action", required=True)
    search = actions.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--scope", choices=("all","article","topic","source","note"), default="all")
    read = actions.add_parser("read")
    read.add_argument("--ref", required=True)
    read.add_argument("--revision", required=True)
    scan = actions.add_parser("scan")
    scan.add_argument("--inbox", type=Path, required=True)
    scan.add_argument("--legacy-db", type=Path)
    scan.add_argument("--settle-seconds", type=float, default=0)
    evidence = actions.add_parser("evidence")
    publish = actions.add_parser("publish")
    process = actions.add_parser("process")
    process.add_argument("--approvals", type=Path)
    process.add_argument("--preserve-original", action="store_true", help="按独立审核的主题组织完整固定原文")
    process.add_argument("--draft", help="校读指定的本地草稿摘要")
    for cmd in (evidence, publish, process):
        cmd.add_argument("--sha256", required=True)
    publish.add_argument("--plan", type=Path, required=True)
    watch = actions.add_parser("watch", help="收录文件并用 Codex 处理许可版本")
    watch.add_argument("--inbox", type=Path, required=True)
    watch.add_argument("--approvals", type=Path)
    watch.add_argument("--legacy-db", type=Path)
    watch.add_argument("--settle-seconds", type=float, default=2)
    watch.add_argument("--interval", type=float, default=3)
    watch.add_argument("--once", action="store_true")
    watch.add_argument("--new-articles", action="store_true", help="按首次启用后的新投放自动授权")
    watch.add_argument("--rename-generic", action="store_true", help="用文内题名替换投放目录里的机器文件名")
    baseline = actions.add_parser("seed-baseline", help="记录已有版本，禁止新投放自动授权覆盖历史资料")
    baseline.add_argument("--inbox", type=Path, required=True)
    baseline.add_argument("--legacy-root", type=Path, action="append", default=[])
    baseline.add_argument("--legacy-db", type=Path)
    watch.add_argument("--interpret", action="store_true", help="试用经独立校读的整理论述")
    status = actions.add_parser("status", help="查看自动收录和研究问题处理状态")
    retry = actions.add_parser("retry", help="重排待处理版本")
    retry.add_argument("--sha256", required=True)
    for cmd in (read, search, scan, evidence, publish, process, watch, baseline, status, retry):
        cmd.add_argument("--vault", type=Path, required=True)
    try:
        args = parser.parse_args()
        vault = args.vault.expanduser().resolve()
        if args.action == "search":
            data = library.search(vault, args.query, args.limit, args.scope)
        elif args.action == "scan":
            from .intake import scan as collect
            data = collect(vault, args.inbox.expanduser().resolve(), args.legacy_db,args.settle_seconds)
        elif args.action == "evidence":
            from .presentation import evidence_for
            _, data = evidence_for(vault, args.sha256)
        elif args.action == "process":
            from .semantic import process as understand
            data = understand(vault, args.sha256, args.approvals, draft_sha=args.draft,
                              preserve_original=args.preserve_original)
        elif args.action == "publish":
            from .presentation import publish as save
            data = save(vault, args.sha256, json.loads(args.plan.read_text()))
        elif args.action == "seed-baseline":
            from .workflow import seed_baseline
            data = seed_baseline(vault, args.inbox.expanduser().resolve(),
                                 legacy_roots=[p.expanduser().resolve() for p in args.legacy_root],
                                 legacy_db=args.legacy_db.expanduser().resolve() if args.legacy_db else None)
        elif args.action == "watch":
            from . import workflow
            if not 0.2 <= args.interval <= 3600:
                raise library.RequestError("invalid_request")
            legacy_db = args.legacy_db.expanduser().resolve() if args.legacy_db else None
            with workflow.watcher_lock(vault):
                last = None
                while True:
                    data = workflow.cycle(vault, args.inbox.expanduser().resolve(), args.approvals,
                                          settle_seconds=args.settle_seconds,
                                          preserve_original=not args.interpret,
                                          new_articles=args.new_articles, legacy_db=legacy_db,
                                          rename_generic=args.rename_generic)
                    if args.once:
                        break
                    if data != last:
                        print(json.dumps({**data, "version": 1, "ok": True}, ensure_ascii=False), flush=True)
                        last = data
                    time.sleep(args.interval)
        elif args.action == "status":
            from .workflow import status as progress
            data = progress(vault)
        elif args.action == "retry":
            from .workflow import retry as requeue
            data = requeue(vault, args.sha256)
        else:
            data = library.read(vault, args.ref, args.revision)
        result = {**data, "version": 1, "ok": True}
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, sqlite3.Error, ImportError, KeyError, TypeError, AttributeError) as exc:
        code = str(exc) if isinstance(exc, library.RequestError) else "io_error"
        result = dict(version=1, ok=False, error=code)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
