"""本地投放、证据冻结与可重试的首次发布。"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
import zlib
from io import BytesIO

from .library import RequestError
from .filing import classify_article, is_generic_title
from . import locations

SUPPORTED = {".docx", ".pdf", ".md", ".txt"}
MAX_BYTES = 100 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def source_bytes(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
            raise RequestError("unsupported_file")
        data = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
        if len(data) != before.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise RequestError("file_in_progress")
        return data


def private_json(path, limit):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise ValueError("private_file_required")
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("file_too_large")
        return json.loads(raw)


def frozen(db, sha):
    if not db:
        return None
    # 只接受指向同一字节版本原件的旧解析记录。
    with contextlib.closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        row = conn.execute("""
            SELECT r.payload_json FROM document_representations r
            JOIN evidence_snapshots s ON s.id=r.snapshot_id AND s.source_id=r.source_id
            WHERE EXISTS (SELECT 1 FROM json_each(s.artifact_manifest_json) a
              WHERE json_extract(a.value,'$.object_sha256')=?)
            ORDER BY r.created_at DESC LIMIT 1""", (sha,)).fetchone()
    return json.loads(row[0]) if row else None


def units(path, data, sha, db):
    rep = frozen(db, sha)
    if not rep and path.suffix.lower() == ".docx":
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
        from app.document_representation.adapters.native_docx import NativeDocxAdapter
        from app.document_representation.domain import ParserInput, ParseOptions
        source = ParserInput(sha, sha, "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", path.name, "", data)
        rep = NativeDocxAdapter().parse(source, ParseOptions()).model_dump()
        # 新文档定位由固定原件和原序确定，崩溃后的重新提取应产生相同引用。
        for number, element in enumerate(rep["elements"], 1):
            element["id"] = f"{sha}:e{number}"
    if rep:
        parts = [dict(kind=e["type"], text=e["content"], locator=e["locator"], element=e["id"])
                 for e in rep["elements"] if e["content"].strip()]
        return parts, rep["warnings"], rep["source_id"], rep["snapshot_id"]
    parts, gaps = [], []
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        from pypdf.errors import PyPdfError

        try:
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted:
                raise RequestError("encrypted_pdf")
            for number, page in enumerate(reader.pages, 1):
                text = page.extract_text().strip()
                if text:
                    parts.append(dict(kind="paragraph", text=text, locator={"page": number}))
                else:
                    gaps.append(f"第 {number} 页需要 OCR")
        except PyPdfError as exc:
            raise RequestError("invalid_pdf") from exc
        gaps.append("仅提取 PDF 文字，图表与版面仍需核对")
    else:
        text = data.decode("utf-8-sig")
        offset = 0
        paragraph = None
        for line in text.splitlines(keepends=True):
            raw = line.rstrip("\r\n")
            heading = re.match(r"^ {0,3}#{1,6}[ \t]+", raw)
            if paragraph is not None and (heading or not raw.strip()):
                parts.append(dict(kind="paragraph", text=text[paragraph:offset].rstrip("\r\n"), locator={"offset": paragraph}))
                paragraph = None
            if heading:
                parts.append(dict(kind="heading", text=raw, locator={"offset": offset}))
            elif raw.strip() and paragraph is None:
                paragraph = offset
            offset += len(line)
        if paragraph is not None:
            parts.append(dict(kind="paragraph", text=text[paragraph:].rstrip("\r\n"), locator={"offset": paragraph}))
    return parts, gaps, sha, sha


def plain(value):
    # 原文另存原件。阅读页使用文字显示，禁用材料自带的嵌入、HTML 和外链。
    import html
    return re.sub(r'([\\`*_{}\[\]()#+!|^~=\->$])', r'\\\1', html.escape(value))


def safe_name(title):
    cleaned = re.sub(r'[\x00-\x1f\[\]#^|/\\:*?"<>]', " ", title).strip(" .")
    # 同时为版本后缀与文件扩展名留出空间。
    return cleaned[:65].encode()[:180].decode(errors="ignore").strip() or "研报"


def inside(root, relative):
    target = root / relative
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError:
        raise RequestError("invalid_path") from None
    if any(p.is_symlink() for p in [target, *target.parents] if p != root and root in p.parents):
        raise RequestError("invalid_path")
    return target


def exclusive_json(path, obj):
    # 同一文件系统中原子发布；已存在的版本与用户修改永不被覆盖。
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf-8") as f:
        tmp = Path(f.name)
        try:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.link(tmp, path)
    finally:
        tmp.unlink()


def initial_files(evidence, original):
    relative = evidence["folder"]
    suffix = evidence["suffix"]
    parts = evidence["parts"]
    gaps = evidence["gaps"]
    text = [f"# {plain(evidence['title'])}", f"[[{relative}/原件{suffix}|打开原件]]",
            "读取范围　" + "、".join(map(plain, gaps)) if gaps else "已提取可读文字"]
    for i, unit in enumerate(parts, 1):
        page = unit["locator"].get("page")
        line = f"[[{relative}/原件.pdf#page={page}|原件第 {page} 页]]" if page and suffix == ".pdf" else ""
        text.extend([f"## 第 {i} 段", plain(unit["text"]), f"^e{i}", line])
    return {
        "原件" + suffix: original,
        "固定原文.md": "\n\n".join(text).encode(),
        "证据索引.json": json.dumps(evidence, ensure_ascii=False).encode(),
    }


def publish_first(conn, vault, path, data, sha, legacy, receipt, moved):
    if receipt:
        evidence = json.loads(zlib.decompress(receipt[0]))
        relative = locations.resolve(evidence["folder"], moved)
    else:
        parts, gaps, source_id, snapshot_id = units(path, data, sha, legacy)
        if not parts:
            gaps = list(gaps) + ["没有可读文字，原件可打开"]
        title, domain = classify_article(path.stem, parts)
        relative = f"资料库/{domain}/{safe_name(title)} · {sha[:16]}"
        evidence = dict(version=1, sha256=sha, title=title, source_id=source_id, snapshot_id=snapshot_id,
                        gaps=gaps, parts=parts, folder=relative, suffix=path.suffix.lower())
    target = inside(vault, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    outputs = initial_files(evidence, data)
    reading = outputs["固定原文.md"]
    if relative != evidence["folder"]:
        outputs["固定原文.md"] = locations.rewrite(reading.decode(), moved).encode()
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".afr-") as stage:
        out = Path(stage)
        for name, content in outputs.items():
            (out / name).write_bytes(content)
        if not target.exists():
            out.rename(target)
        else:
            for name, content in outputs.items():
                saved = inside(vault, relative + "/" + name)
                if not saved.exists():
                    os.link(out / name, saved)
                elif digest(source_bytes(saved)) != digest(content):
                    raise RequestError("evidence_changed")
    encoded = outputs["证据索引.json"]
    conn.execute("INSERT OR IGNORE INTO receipts VALUES(?,?,?,?,?)",
                 (sha, zlib.compress(encoded), digest(encoded), digest(reading), bool(evidence["parts"])))
    return relative


def initial_state(conn, vault, sha, folder, receipt, moved):
    if not receipt:
        return "missing"
    for name, expected in (("证据索引.json", receipt[1]), ("固定原文.md", receipt[2])):
        path = inside(vault, folder + "/" + name)
        if not path.is_file():
            return "missing"
        original_folder = locations.resolve(folder, moved, reverse=True)
        if name == "固定原文.md" and original_folder != folder:
            text = source_bytes(path).decode()
            mapping = {original_folder: folder}
            normalized = locations.rewrite(text, mapping, reverse=True)
            if locations.rewrite(normalized, mapping) != text:
                raise RequestError("evidence_changed")
            actual = digest(normalized.encode())
        else:
            actual = remember(conn, path)
        if actual != expected:
            raise RequestError("evidence_changed")
    files = list(inside(vault, folder).glob("原件.*"))
    if not files:
        return "missing"
    if len(files) != 1 or remember(conn, inside(vault, str(files[0].relative_to(vault)))) != sha:
        raise RequestError("evidence_changed")
    return "ready"


def library_originals(vault):
    manifest = inside(vault, "资料导入清单.json")
    if not manifest.exists():
        return
    rows = json.loads(manifest.read_text())
    if not isinstance(rows, list) or not all(isinstance(row, dict) and isinstance(row.get("originals"), list)
            and all(isinstance(name, str) for name in row["originals"]) for row in rows):
        raise RequestError("invalid_manifest")
    for row in rows:
        for original in row["originals"]:
            yield inside(vault, original)


def raise_walk_error(error):
    raise error


def inbox_files(inbox, *, strict=False):
    for parent, dirs, names in os.walk(inbox, onerror=raise_walk_error if strict else None):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not Path(parent, d).is_symlink())
        for name in sorted(names):
            path = Path(parent, name)
            if name.startswith((".", "~$")) or path.is_symlink():
                continue
            if strict:
                regular = stat.S_ISREG(path.lstat().st_mode)
            else:
                regular = path.is_file()
            if regular:
                yield path


def check_intake_roots(vault, inbox):
    if not vault.is_dir() or not inbox.is_dir() or vault == inbox or vault in inbox.parents or inbox in vault.parents:
        raise RequestError("invalid_path")


def _readable_input_name(path, folder, sha):
    if not is_generic_title(path.stem):
        return None
    title = Path(folder).name.rsplit(" · ", 1)[0]
    if title == "待命名研报" or is_generic_title(title):
        return None
    # 验证新名指向同一份原件后移除旧名。重名时保留已有文件。
    before = path.stat(follow_symlinks=False)
    for label in (title, f"{title} · {sha[:8]}"):
        target = path.with_name(label + path.suffix)
        try:
            os.link(path, target, follow_symlinks=False)
        except FileExistsError:
            if not target.is_file() or target.is_symlink() or not target.samefile(path):
                continue
        if (target.is_symlink() or not target.samefile(path)
                or path.stat(follow_symlinks=False).st_mtime_ns != before.st_mtime_ns
                or digest(source_bytes(target)) != sha):
            raise RequestError("file_in_progress")
        path.unlink()
        return target
    return None


def scan(vault,inbox,legacy=None,settle_seconds=0,*,rename_generic=False):
    import fcntl
    import time
    if not 0 <= settle_seconds <= 3600:
        raise RequestError("invalid_request")
    check_intake_roots(vault, inbox)
    state = inside(vault,".afr")
    state.mkdir(exist_ok=True)
    with inside(vault,".afr/scan.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        moved = locations.load(vault)
        with contextlib.closing(sqlite3.connect(inside(vault,".afr/intake.sqlite"))) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS seen (path TEXT PRIMARY KEY, stamp TEXT, sha TEXT, folder TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS articles (sha TEXT PRIMARY KEY, folder TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS receipts (sha TEXT PRIMARY KEY, payload BLOB NOT NULL, evidence TEXT, reading TEXT, readable INTEGER)")
            known = {remember(conn, original) for original in library_originals(vault) if original.is_file()}
            added=[]; errors=[]; skipped=0
            renamed = 0
            present = set()
            settling = 0
            unsupported = []
            for p in inbox_files(inbox):
                name = p.name
                if p.suffix.lower() not in SUPPORTED:
                    unsupported.append(str(p.relative_to(inbox)))
                    continue
                try:
                    if time.time() - max(p.stat().st_mtime,p.stat().st_ctime) < settle_seconds:
                        settling += 1
                        continue
                    sha=remember(conn,p)
                    if sha in known:
                        skipped+=1
                        continue
                    row=conn.execute("SELECT folder FROM articles WHERE sha=?",(sha,)).fetchone()
                    receipt = conn.execute("SELECT payload,evidence,reading FROM receipts WHERE sha=?", (sha,)).fetchone()
                    if row and initial_state(conn,vault,sha,row[0],receipt,moved) == "ready":
                        present.add(sha)
                        if rename_generic and _readable_input_name(p, row[0], sha):
                            renamed += 1
                        continue
                    data=source_bytes(p)
                    if digest(data)!=sha:
                        raise RequestError("file_in_progress")
                    t=time.monotonic()
                    folder=publish_first(conn,vault,p,data,sha,legacy,receipt,moved)
                    conn.execute("INSERT OR IGNORE INTO articles VALUES(?,?)",(sha,folder))
                    conn.execute("UPDATE seen SET folder=? WHERE path=? AND sha=?",(folder,str(p),sha))
                    conn.commit()
                    present.add(sha)
                    added.append(dict(sha256=sha,folder=folder,publish_seconds=round(time.monotonic()-t,3)))
                    if rename_generic and _readable_input_name(p, folder, sha):
                        renamed += 1
                except ImportError:
                    errors.append(dict(file=name,error="parser_unavailable"))
                except (RequestError, ValueError, OSError, RuntimeError) as exc:
                    errors.append(dict(file=name,error=str(exc) if isinstance(exc,RequestError) else type(exc).__name__))
            conn.commit()
            versions = [
                dict(sha256=sha, folder=folder, readable=bool(readable))
                for sha, folder, readable in conn.execute(
                    "SELECT a.sha,a.folder,r.readable FROM articles a JOIN receipts r ON r.sha=a.sha"
                )
                if sha in present and inside(vault, folder).is_dir()
            ]
            pending = [
                dict(sha256=row["sha256"], folder=row["folder"]) for row in versions
                if row["readable"] and not all(inside(vault, row["folder"]+"/"+name).is_file()
                                               for name in ("文章白板.canvas", "完整阅读.md"))
            ]
            return dict(added=added,existing=skipped,errors=errors,pending=pending,settling=settling,renamed=renamed,
                        unsupported=unsupported,versions=versions)


def remember(conn,path):
    s=path.stat()
    stamp=f"{s.st_dev}:{s.st_ino}:{s.st_size}:{s.st_mtime_ns}:{s.st_ctime_ns}"
    row=conn.execute("SELECT sha FROM seen WHERE path=? AND stamp=?",(str(path),stamp)).fetchone()
    if row:
        return row[0]
    sha=digest(source_bytes(path))
    conn.execute("INSERT INTO seen(path,stamp,sha) VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET stamp=excluded.stamp,sha=excluded.sha,folder=CASE WHEN seen.sha=excluded.sha THEN seen.folder END",
                (str(path),stamp,sha))
    return sha
