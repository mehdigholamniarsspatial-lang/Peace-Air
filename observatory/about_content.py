"""Load the editable About page Markdown into safe, presentation-neutral blocks."""
import re
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.utils.text import slugify

COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
STRONG_RE = re.compile(r"\*\*(.+?)\*\*")
LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def _inline(value):
    """Support the small inline Markdown subset used by the editorial file."""
    safe = escape(value)
    safe = STRONG_RE.sub(r"<strong>\1</strong>", safe)
    safe = LINK_RE.sub(r'<a href="\2">\1</a>', safe)
    safe = safe.replace("\\\n", "<br>").replace("\n", " ").replace("\\@", "@")
    return mark_safe(safe)


def _paragraph(lines):
    return {"kind": "paragraph", "html": _inline("\n".join(line.strip() for line in lines))}


def _blocks(lines):
    blocks = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or line == "---":
            index += 1
            continue
        if line.startswith("### "):
            blocks.append({"kind": "heading", "text": line[4:].strip()})
            index += 1
            continue
        if line.startswith("- "):
            items = []
            while index < len(lines) and lines[index].strip().startswith("- "):
                items.append(_inline(lines[index].strip()[2:]))
                index += 1
            blocks.append({"kind": "list", "items": items})
            continue
        if line.startswith("|"):
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                    rows.append([_inline(cell) for cell in cells])
                index += 1
            blocks.append({"kind": "table", "head": rows[0], "rows": rows[1:]})
            continue
        if line.startswith(">"):
            quote = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote.append(lines[index].strip().lstrip(">").strip())
                index += 1
            blocks.append({"kind": "blockquote", "html": _inline(" ".join(quote))})
            continue
        paragraph = []
        while index < len(lines):
            current = lines[index].strip()
            if not current or current == "---" or current.startswith(("### ", "- ", "|", ">")):
                break
            paragraph.append(current)
            index += 1
        blocks.append(_paragraph(paragraph))
    return blocks


@lru_cache(maxsize=1)
def load_about_content():
    """Read content/about.md, excluding editorial HTML comments from output."""
    source = settings.BASE_DIR / "content" / "about.md"
    text = COMMENT_RE.sub("", source.read_text(encoding="utf-8"))
    lines = text.splitlines()
    title = lines[0].removeprefix("# ").strip()
    sections = []
    current = {"title": "", "slug": "intro", "lines": []}
    for line in lines[1:]:
        if line.startswith("## "):
            sections.append(current)
            section_title = line[3:].strip()
            current = {"title": section_title, "slug": slugify(section_title), "lines": []}
        else:
            current["lines"].append(line)
    sections.append(current)
    for section in sections:
        blocks = _blocks(section.pop("lines"))
        section["lead_blocks"] = []
        section["groups"] = []
        active = section["lead_blocks"]
        for block in blocks:
            if block["kind"] == "heading":
                group = {"title": block["text"], "slug": slugify(block["text"]), "blocks": []}
                section["groups"].append(group)
                active = group["blocks"]
            else:
                active.append(block)
    return {"title": title, "intro": sections[0]["lead_blocks"], "sections": sections[1:]}
