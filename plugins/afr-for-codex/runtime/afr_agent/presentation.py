"""发表可回源的白板，保留初读页与用户编辑。"""
from collections import defaultdict
import colorsys
import contextlib
import fcntl
import math
import os
import sqlite3
import tempfile
import json
from pathlib import Path
import re
import unicodedata

from . import locations
from .intake import digest, inside, plain, exclusive_json, source_bytes
from .library import RequestError

ARCHIVE_BOARD_LAYOUT = "archive_columns_v1"
CHAPTER_READING_LAYOUT = "chapters_v1"
CHAPTER_READING_MARKER = "<!-- AFR:READING:chapters_v1 -->"
MAX_READING_CHAPTERS = 7
ORIGINAL_ARGUMENT = "该组保留完整固定原文。"

COLORS = ["#97b0c0","#a1b99e","#d0b397","#b7a5c5","#9cbdb6","#c3acba","#b6bd8e","#b1accc"]


def text_width(value):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in value)


def branch_color(index):
    if index < len(COLORS):
        return COLORS[index]
    hue = ((index - len(COLORS)) * 0.61803398875 + 0.12) % 1
    rgb = colorsys.hls_to_rgb(hue, 0.70, 0.27)
    return "#" + "".join(f"{round(component * 255):02x}" for component in rgb)


def board(evidence, groups, preview=False, *, layout=None):
    if layout == ARCHIVE_BOARD_LAYOUT:
        return _archive_columns_board(evidence, groups, preview)

    relative = evidence["folder"]
    caption = "原文结构 · 可先阅读" if preview else "文章思维导图"
    root_text = f"### {plain(evidence['title'])}\n\n{caption}"
    root_text += f"\n\n[[{relative}/固定原文.md|固定原文]] · [[{relative}/原件{evidence['suffix']}|原件]]"
    if evidence["gaps"]:
        root_text += "\n\n保留读取缺口，详见固定原文。"

    nodes, edges = [], []

    def card(key, text, x, y, width, height, color):
        nodes.append(dict(id=key, type="text", text=text, x=x, y=y, width=width, height=height, color=color))

    def connect(parent, child, color):
        edges.append(dict(id=parent + "-" + child, fromNode=parent, fromSide="right",
                          toNode=child, toSide="left", toEnd="none", color=color))

    branches = defaultdict(list)
    for number, group in enumerate(groups, 1):
        branches[group["branch"]].append((number, group))
    y = 0
    for index, (branch, children) in enumerate(branches.items()):
        color = branch_color(index)
        key = f"b{index}"
        start_y = y
        for number, group in children:
            cite = next((i for i in range(group["start"], group["end"] + 1)
                         if evidence["parts"][i - 1]["kind"] not in {"heading", "title"}), group["start"])
            source = f"{relative}/固定原文.md#^e{cite}"
            reading = source if preview else f"{relative}/完整阅读.md#^topic-{number}"
            label = group["title"].replace("|", "｜").replace("[", "［").replace("]", "］")
            text = f"### [[{reading}|{plain(label)}]]\n\n{plain(group['summary'])}\n\n[[{source}|查看原文]]"
            lines = math.ceil(text_width(label) / 30) + math.ceil(text_width(group["summary"]) / 37)
            height = max(215, 145 + (lines + group["summary"].count("\n")) * 38)
            child = f"t{number}"
            card(child, text, 920, y, 520, height, color)
            connect(key, child, color)
            y += height + 50
        branch_height = max(125, 70 + math.ceil(text_width(branch) / 15) * 36)
        card(key, "### " + plain(branch), 530, (start_y + y - branch_height - 50) // 2, 280, branch_height, color)
        connect("article", key, color)
        y += 60
    root_height = max(285, 200 + math.ceil(text_width(evidence["title"]) / 24) * 38)
    card("article", root_text, 0, max(0, (y - root_height) // 2), 425, root_height, COLORS[0])
    return {"nodes": nodes, "edges": edges}


def _wrapped_lines(value, width):
    return sum(max(1, math.ceil(text_width(line) / width)) for line in value.split("\n"))


def _archive_columns_board(evidence, groups, preview):
    # 章节按初次出现排列，同一章节的卡片保持原稿顺序。
    topics_by_branch = defaultdict(list)
    for number, group in enumerate(groups, 1):
        topics_by_branch[" ".join(group["branch"].split())].append((number, group))
    # 单章有三个以上论述时也展开为并列的续列，保留章内阅读顺序。
    lanes = []
    for index, (branch, topics) in enumerate(topics_by_branch.items()):
        cards_per_lane = 2 if len(topics_by_branch) == 1 and len(topics) >= 3 else 4
        for start in range(0, len(topics), cards_per_lane):
            lanes.append((branch, topics[start:start + cards_per_lane], index, bool(start)))

    relative = evidence["folder"]
    fixed_source = f"{relative}/固定原文.md"
    full_reading = f"{relative}/完整阅读.md"
    card_width, column_gap, card_gap, row_gap, columns = 520, 70, 55, 100, 4
    nodes, edges = [], []
    rows = [lanes[start:start + columns] for start in range(0, len(lanes), columns)]

    def card(key, text, x, y, width, height, color):
        nodes.append(dict(id=key, type="text", text=text, x=x, y=y, width=width, height=height, color=color))

    def connect(parent, child, color):
        edges.append(dict(id=parent + "-" + child, fromNode=parent, fromSide="bottom",
                          toNode=child, toSide="top", toEnd="none", color=color))

    def route(row):
        return " · ".join(dict.fromkeys(plain(branch) for branch, _, _, _ in row))

    first = evidence["parts"][0] if evidence["parts"] else None
    title = evidence["title"]
    if first and (first["kind"] == "title" or (first["kind"] == "heading" and first["text"].startswith("# "))):
        title = " ".join(first["text"].lstrip("# ").split()) or title
    title = plain(title)
    caption = "原文结构 · 可先阅读" if preview else "文章思维导图"
    links = f"[[{fixed_source}|固定原文]] · [[{relative}/原件{evidence['suffix']}|原件]]"
    if not preview:
        links += f" · [[{full_reading}|完整阅读]]"
    root_text = f"### {title}\n\n{caption}\n\n{links}"
    root_height = 154 + 34 * _wrapped_lines(title, 47)
    if rows:
        first_route = route(rows[0])
        if len(rows) > 1:
            first_route = "第 1 行：" + first_route
        root_text += f"\n\n**阅读地图** · 从左向右选主题\n\n{first_route}"
        root_height += 47 + 28 * _wrapped_lines(first_route, 58)
        if len(rows) > 1:
            root_text += f"\n\n共 {len(rows)} 行章节，向下查看后续章节。每列从上往下读。"
            root_height += 58
    if evidence["gaps"]:
        root_text += "\n\n保留读取缺口，详见固定原文。"
        root_height += 44
    card("article", root_text, 0, 0, 560, root_height, COLORS[0])

    row_top = 0
    for row_number, row in enumerate(rows, 1):
        guide = "article"
        guide_height = root_height
        if row_number > 1:
            guide = f"row{row_number}"
            row_route = route(row)
            guide_height = 82 + 28 * _wrapped_lines(row_route, 54)
            card(guide, f"### 阅读地图 · 第 {row_number} 行\n\n{row_route}", 0, row_top, card_width,
                 guide_height, COLORS[0])
        first_top = row_top + guide_height + row_gap
        row_bottom = first_top
        for column, (branch, topics, branch_index, continued) in enumerate(row):
            x = column * (card_width + column_gap)
            color = branch_color(branch_index)
            parent, y = guide, first_top
            for number, group in topics:
                cite = next((i for i in range(group["start"], group["end"] + 1)
                             if evidence["parts"][i - 1]["kind"] not in {"heading", "title"}), group["start"])
                source = f"{fixed_source}#^e{cite}"
                reading = source if preview else f"{full_reading}#^topic-{number}"
                label = plain(group["title"].replace("|", "｜").replace("[", "［").replace("]", "］"))
                summary = group["summary"]
                # 较长摘要把第一句单独成段，保留其余原句与模型已有的段落。
                if len(summary) > 105 and "\n" not in summary:
                    summary = re.sub(r"^(.+?[。！？]+[”’」』）】]*)(?=\S)", r"\1\n\n", summary, count=1)
                summary = plain(summary)
                topic_text = f"### [[{reading}|{label}]]\n\n{summary}\n\n[[{source}|查看原文]]"
                height = 110 + 34 * _wrapped_lines(label, 43) + 28 * _wrapped_lines(summary, 53)
                if parent == guide and (continued or plain(branch).strip() != label.strip()):
                    chapter = plain(branch) + ("（续）" if continued else "")
                    topic_text = f"**{chapter}**\n\n{topic_text}"
                    height += 18 + 28 * _wrapped_lines(chapter, 53)
                key = f"t{number}"
                height = max(220, height)
                card(key, topic_text, x, y, card_width, height, color)
                connect(parent, key, color)
                parent = key
                row_bottom = max(row_bottom, y + height)
                y += height + card_gap
        row_top = row_bottom + row_gap
    return {"nodes": nodes, "edges": edges}


def evidence_for(vault, identifier):
    if not re.fullmatch(r"[0-9a-f]{64}",identifier):
        raise RequestError("invalid_request")
    db=inside(vault,".afr/intake.sqlite")
    if not db.exists():
        raise RequestError("not_found")
    with contextlib.closing(sqlite3.connect(db.as_uri()+"?mode=ro",uri=True)) as conn:
        row=conn.execute("SELECT a.folder,r.evidence,r.reading FROM articles a JOIN receipts r ON r.sha=a.sha WHERE a.sha=?",(identifier,)).fetchone()
    if not row:
        raise RequestError("not_found")
    path=inside(vault,row[0])
    raw = source_bytes(inside(vault, row[0]+"/证据索引.json"))
    if digest(raw) != row[1]:
        raise RequestError("evidence_changed")
    evidence = json.loads(raw)
    original = evidence["folder"]
    if evidence["sha256"] != identifier or (original != row[0] and locations.load(vault).get(original) != row[0]):
        raise RequestError("version_conflict")
    fixed = source_bytes(inside(vault, row[0]+"/固定原文.md"))
    if original != row[0]:
        mapping = {original: row[0]}
        try:
            normalized = locations.rewrite(fixed.decode("utf-8"), mapping, reverse=True)
        except UnicodeDecodeError:
            raise RequestError("evidence_changed") from None
        if locations.rewrite(normalized, mapping).encode() != fixed:
            raise RequestError("evidence_changed")
        fixed = normalized.encode()
    if digest(fixed) != row[2]:
        raise RequestError("evidence_changed")
    if digest(source_bytes(inside(vault, row[0]+"/原件"+evidence["suffix"]))) != identifier:
        raise RequestError("evidence_changed")
    return path,evidence


def publish(vault, identifier, plan):
    """外部计划作为本地草稿留存，正式页由独立校读流程签发。"""
    _, evidence = evidence_for(vault, identifier)
    from .semantic import fingerprint, receipt, validate

    if not isinstance(plan, dict) or plan.get("sha256") != identifier:
        raise RequestError("invalid_plan")
    groups = plan.get("groups")
    validate(groups, 1, len(evidence["parts"]))
    draft = {"sha256": identifier, "groups": groups}
    draft_sha = fingerprint(draft)
    path = receipt(vault, identifier, f"draft-{draft_sha}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        exclusive_json(path, draft)
    except FileExistsError:
        if json.loads(source_bytes(path)) != draft:
            raise RequestError("invalid_plan") from None
    return {"sha256": identifier, "state": "draft", "draft_sha256": draft_sha, "topics": len(groups)}


def topic_section(evidence, number, group, *, include_source_text=False):
    lines = [f"## {plain(group['title'])}", plain(group["argument"]), f"^topic-{number}", "### 原文依据"]
    for index in range(group["start"], group["end"] + 1):
        lines.append(f"[[{evidence['folder']}/固定原文.md#^e{index}|原文第 {index} 段]]")
        if include_source_text:
            original = plain(evidence["parts"][index - 1]["text"])
            lines.append("\n".join("> " + line for line in original.splitlines()))
    return "\n\n".join(lines)


def _source_heading(text):
    return " ".join(re.sub(r"^#{1,6}\s+", "", text.strip()).split())


def _reading_title(evidence):
    title = evidence["title"]
    first = next(iter(evidence["parts"]), None)
    if first and (first["kind"] == "title" or (first["kind"] == "heading" and (
            first["text"].startswith("# ") or re.fullmatch(r"export[_-]\d+", title, re.I)))):
        return _source_heading(first["text"]) or title
    return title


def reading_chapters(evidence, groups):
    """用白板主线组织章节，保留原文与各张卡片的顺序。"""
    branches = {}
    previous = None
    for number, group in enumerate(groups, 1):
        branch = " ".join(group["branch"].split())
        if branch != previous:
            branches[number] = branch
            previous = branch
    if len(branches) <= MAX_READING_CHAPTERS:
        return branches

    # 卡片主题过密时，沿用作者的章节边界。段内的小标题仍随正文阅读。
    outline = {}
    article_title = _source_heading(_reading_title(evidence))
    for number, group in enumerate(groups, 1):
        for part in evidence["parts"][group["start"] - 1:group["end"]]:
            if part["kind"] != "heading":
                continue
            title = _source_heading(part["text"])
            if title and title != article_title:
                outline[number] = title
                break
    if len(outline) < 2:
        outline = branches
    if 1 not in outline:
        outline = {1: branches[1], **outline}
    if len(outline) <= MAX_READING_CHAPTERS:
        return outline

    starts = list(outline)
    return {number: outline[number] for number in
            (starts[index * len(starts) // MAX_READING_CHAPTERS] for index in range(MAX_READING_CHAPTERS))}


def chapter_topic_section(evidence, number, group, *, chapter=None):
    if group["argument"] == ORIGINAL_ARGUMENT:
        parts = evidence["parts"][group["start"] - 1:group["end"]]
        body = []
        article_title = _source_heading(_reading_title(evidence))
        for part in parts:
            text = part["text"].strip()
            if part["kind"] in {"heading", "title"}:
                text = _source_heading(text)
                if not body and text in {chapter, article_title}:
                    continue
            if text:
                body.append(text)
        prose = "\n\n".join(body) or group["title"]
    else:
        prose = group["argument"].strip()
    prose = prose.replace("\r\n", "\n").replace("\r", "\n")
    first, separation, remaining = plain(prose).partition("\n\n")
    anchored = f"{first.rstrip()} ^topic-{number}{separation}{remaining}"
    return f"<!-- AFR:TOPIC:{number} -->\n\n{anchored}\n\n<!-- /AFR:TOPIC:{number} -->"


def reading_markdown(evidence, groups, *, canvas_path=None, include_source_text=False, layout=None):
    folder = evidence["folder"]
    canvas_path = canvas_path or f"{folder}/文章白板.canvas"
    title = _reading_title(evidence) if layout == CHAPTER_READING_LAYOUT else evidence["title"]
    body = [f"# {plain(title)}", f"[[{canvas_path}|文章白板]]"]
    if layout != CHAPTER_READING_LAYOUT:
        body.extend(topic_section(evidence, number, group, include_source_text=include_source_text)
                    for number, group in enumerate(groups, 1))
        return "\n\n".join(body) + "\n"

    body.insert(0, CHAPTER_READING_MARKER)
    body[2] += f" · [[{folder}/固定原文.md|固定原文]]"
    chapters = reading_chapters(evidence, groups)
    for number, group in enumerate(groups, 1):
        chapter = chapters.get(number)
        if chapter:
            body.append(f"## {plain(chapter)}")
        body.append(chapter_topic_section(evidence, number, group, chapter=chapter))
        if number == len(groups) or number + 1 in chapters:
            first = max(start for start in chapters if start <= number)
            source = groups[first - 1]["start"]
            body.append(f"[[{folder}/固定原文.md#^e{source}|本节原文]]")
    return "\n\n".join(body) + "\n"


def _publish_reviewed(vault, identifier):
    from .semantic import receipt, signed_plan

    folder, evidence = evidence_for(vault, identifier)
    plan = signed_plan(vault, identifier, evidence)
    groups = plan["groups"]
    display_evidence = {**evidence, "folder": folder.relative_to(vault).as_posix()}
    text = reading_markdown(display_evidence, groups, include_source_text=plan.get("includes_source_text", False),
                            layout=plan.get("reading_layout"))
    # 只新建；部分发布可用同一计划安全重试。人工改动通过冲突回执保留。
    with inside(vault,".afr/publish.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        reading=inside(vault,display_evidence["folder"]+"/完整阅读.md")
        canvas=inside(vault,display_evidence["folder"]+"/文章白板.canvas")
        content=board(display_evidence, groups, layout=plan.get("board_layout"))
        if reading.exists() and reading.read_text()!=text:
            raise RequestError("user_edit_conflict")
        if canvas.exists():
            try:
                saved_canvas = json.loads(canvas.read_text())
            except (ValueError, UnicodeError):
                raise RequestError("user_edit_conflict") from None
            if saved_canvas != content:
                raise RequestError("user_edit_conflict")
        if not reading.exists():
            with tempfile.NamedTemporaryFile(dir=folder,delete=False) as tmp:
                tmp.write(text.encode()); tmp.flush(); os.fsync(tmp.fileno())
            try:
                os.link(tmp.name,reading)
            finally:
                Path(tmp.name).unlink()
        if not canvas.exists():
            exclusive_json(canvas,content)
        exclusive_json(receipt(vault, identifier, "publication"),
                       {"sha256": identifier, "plan_sha256": plan["plan_sha256"], "state": "published"})
    return {"sha256": identifier, "state": "published", "review": "passed",
            "board": canvas.relative_to(vault).as_posix(), "topics": len(groups)}
