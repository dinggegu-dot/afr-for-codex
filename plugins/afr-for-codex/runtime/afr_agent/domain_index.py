"""Link signed reports from the owner's existing Obsidian domain indexes."""

import fcntl
from pathlib import PurePosixPath
import re

from . import questions, semantic
from .filing import DOMAIN_TERMS, PENDING_DOMAIN
from .intake import inside, source_bytes
from .library import RequestError


_BEGIN = "<!-- AFR:DOMAIN-INDEX:BEGIN -->"
_END = "<!-- AFR:DOMAIN-INDEX:END -->"


def ensure_entry(vault, folder, sha):
    """Append a signed report's links to an existing index, once."""
    paths = semantic.publication_paths(vault, sha, folder)
    article = PurePosixPath(paths["reading"]).parent
    if len(article.parts) != 3 or article.parts[0] != "资料库":
        return False
    domain = article.parts[1]
    if domain == PENDING_DOMAIN:
        index_name = "缺少既有分类.md"
    elif domain in DOMAIN_TERMS:
        index_name = "本领域资料索引.md"
    else:
        return False
    index = inside(vault, f"资料库/{domain}/{index_name}")
    if not index.is_file():
        return False

    reading = paths["reading"]
    canvas = paths["canvas"]
    evidence = str(article / "固定原文.md")
    if not all(inside(vault, path).is_file() for path in (reading, canvas, evidence)):
        raise RequestError("domain_index_missing_article")
    title = re.sub(r" · [0-9a-f]{16}$", "", article.name)
    title = title.replace("|", "｜").replace("[", "［").replace("]", "］")
    line = f"- [[{reading}|{title}]] · [[{canvas}|文章白板]] · [[{evidence}|固定原文]]"

    inside(vault, ".afr").mkdir(exist_ok=True)
    with inside(vault, ".afr/domain-index.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = source_bytes(index)
        try:
            text = before.decode("utf-8")
        except UnicodeError as exc:
            raise RequestError("domain_index_invalid_encoding") from exc
        # 旧索引手写链接可能省略 Markdown 扩展名。
        target = re.escape(reading.removesuffix(".md"))
        if re.search(rf"\[\[{target}(?:\.md)?(?=[|\]])", text):
            return False
        start = text.count(_BEGIN)
        end = text.count(_END)
        if start == end == 0:
            separator = "" if text.endswith("\n\n") or not text else ("\n" if text.endswith("\n") else "\n\n")
            updated = f"{text}{separator}{_BEGIN}\n## 自动归档\n\n{line}\n{_END}\n"
        elif start == end == 1 and text.index(_BEGIN) < text.index(_END):
            marker = text.index(_END)
            updated = f"{text[:marker]}{line}\n{text[marker:]}"
        else:
            raise RequestError("domain_index_invalid_section")
        questions._write_projection(index, before, updated.encode("utf-8"))
    return True
