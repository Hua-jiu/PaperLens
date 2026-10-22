"""In-memory PDF extraction and rendering for the paper reader.

Public page numbers are one based. No function writes files or makes network
requests. ``extract_document`` reports integer completion percentages to its
optional callback. Image crops remain PDF coordinates and are rendered on demand.
"""

from __future__ import annotations

from collections import Counter
from functools import wraps
from math import ceil, isfinite
import re
from statistics import median
from threading import RLock
from typing import Any, Callable
import unicodedata

import fitz

from .pdf_converter import convert_pdf_pages


# All default parameters live here so this module can also be imported directly.
DEFAULT_RENDER_SCALE = 1.3
DEFAULT_THUMBNAIL_WIDTH = 360
DEFAULT_FIGURE_SCALE = 1.8
# Formulas are small crops shown at reading size, so they need more pixels.
DEFAULT_FORMULA_SCALE = 4.0
DEFAULT_MAX_RENDER_PIXELS = 18_000_000
DEFAULT_MAX_PDF_PAGES = 2000
DEFAULT_DRYRUN = True
# Some typesetters emit every word as its own text block. A page is rebuilt
# from baselines when at least this share of its lines is a single word.
# PDFMiner splits such widely justified lines at stretched spaces and drops
# or interleaves the pieces, so these pages use the rebuilt PDF lines.
DEFAULT_FRAGMENTED_WORD_SHARE = .6
DEFAULT_FRAGMENTED_MIN_LINES = 60
EXTRACTION_VERSION = 7

# PyMuPDF does not support concurrent use from different threads, including
# calls operating on separate documents. Every public entry point shares this
# reentrant lock so threaded HTTP handlers and background workers stay safe.
_PDF_LOCK = RLock()

_LIGATURES = str.maketrans({
    "ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl",
    "ﬅ": "st", "ﬆ": "st", "\u00ad": "", "\ufeff": "", "\u00a0": " ",
})
_CAPTION = re.compile(r"^\s*(?P<kind>fig(?:ure)?s?\.?|table|图|表)\s*(?:[A-Z]?\d+[A-Za-z]?(?:[.\-–]\d+)?|[IVX]+|[一二三四五六七八九十]+)(?=\s|[.:：)（(\-–—，、]|[\u3400-\u9fff]|$)[\s.:：)\-–]*", re.I)
_SECTION = re.compile(r"^(?:\d+(?:\.\d+){0,4}\.?\s+|[IVX]+[.)]\s+|[A-Z][.)]\s+)(?=\S)")
_NAMED_SECTION = re.compile(
    r"^(?:abstract|摘要|introduction|background|related work|methods?|methodology|"
    r"materials? and methods?|results?|discussion|conclusions?|limitations?|"
    r"references|bibliography|acknowledg(?:e)?ments?|appendix(?:\s+[A-Z])?|"
    r"supplementary (?:information|materials?)|keywords?|关键词|引言|背景|方法|结果|讨论|结论|参考文献)\s*[:：.]?$", re.I,
)
_ABSTRACT = re.compile(r"^\s*(?:abstract|摘要)\s*[:：.—–-]?\s*", re.I)
_STOP_ABSTRACT = re.compile(r"^\s*(?:key\s*words?|index terms|关键词|\d+\.?\s+introduction|introduction|引言)\b", re.I)
_PAGE_MARKER = re.compile(r"^\s*(?:page\s*)?\d{1,4}\s*(?:[/|]\s*\d{1,4})?\s*$", re.I)
_INSTITUTION = re.compile(r"university|institute|department|laborator|school of|college|hospital|academy|@|https?://|doi\s*:|arxiv", re.I)
_PUBLISHER_NAVIGATION = re.compile(r"^(?:check\s+for\b|article\s*$|open\s+access\s*$|(?:download|view)\s+pdf\s*$)", re.I)
_MATH = re.compile(r"[=∑∏∫√±∂∇≈≠≤≥∞∈∉⊂⊆⊕⊗∀∃→↦×÷α-ωΑ-Ω]")
_EQUATION_NUMBER = re.compile(r"\(\s*[A-Z]?\d{1,3}[a-z]?\s*\)")
_LIST_MARKER = re.compile(r"^(?:\[\d{1,3}\]|\d{1,3}[.)]|[•▪◦●■∙·*]|[-–—])\s+")


def _serialized_pdf(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    def locked(*args: Any, **kwargs: Any) -> Any:
        with _PDF_LOCK:
            return function(*args, **kwargs)
    return locked


def _open_pdf(pdf_bytes: bytes) -> fitz.Document:
    if not isinstance(pdf_bytes, (bytes, bytearray, memoryview)) or not pdf_bytes:
        raise ValueError("PDF 文件内容为空或格式无效。")
    try:
        document = fitz.open(stream=bytes(pdf_bytes), filetype="pdf")
    except (RuntimeError, ValueError, TypeError) as exc:
        raise ValueError("无法打开 PDF，请确认文件完整且为有效的 PDF 文档。") from exc
    if document.needs_pass or document.is_encrypted:
        document.close()
        raise ValueError("暂不支持加密 PDF，请先移除密码保护后重新上传。")
    if document.page_count < 1:
        document.close()
        raise ValueError("PDF 中没有可以读取的页面。")
    if document.page_count > DEFAULT_MAX_PDF_PAGES:
        document.close()
        raise ValueError(f"PDF 页数超过 {DEFAULT_MAX_PDF_PAGES} 页，请拆分后上传。")
    return document


def _clean_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text.translate(_LIGATURES))
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) != "Cc")
    return re.sub(r"[ \t]+", " ", text).strip()


def _bbox(rect: Any) -> list[float]:
    return [round(float(value), 2) for value in rect]


def _join_lines(lines: list[dict[str, Any]], equation: bool = False) -> str:
    if equation:
        return "\n".join(line["text"] for line in lines if line["text"])
    result = ""
    for line in lines:
        text = line["text"]
        if not text:
            continue
        if not result:
            result = text
        elif result.endswith("-") and re.search(r"[A-Za-z]-$", result) and re.match(r"[a-z]", text):
            result = result[:-1] + text
        elif re.search(r"[\u3400-\u9fff]$", result) and re.match(r"[\u3400-\u9fff，。；：、！？]", text):
            result += text
        else:
            result += " " + text
    return _clean_text(result)


def _line_from_spans(line: dict[str, Any]) -> dict[str, Any] | None:
    spans = line.get("spans", [])
    if not spans:
        return None
    # Span boundaries can denote either font changes within a word or separate
    # words. Only insert a space when their geometric gap actually contains one.
    fragments: list[str] = []
    previous = None
    weighted_sizes: list[tuple[float, int]] = []
    for span in spans:
        text = span.get("text", "")
        if previous and fragments and text and not fragments[-1].endswith(" ") and not text.startswith(" "):
            gap = float(span["bbox"][0]) - float(previous["bbox"][2])
            if gap > max(1.3, min(float(span.get("size", 10)), float(previous.get("size", 10))) * 0.18):
                fragments.append(" ")
        fragments.append(text)
        weighted_sizes.append((float(span.get("size", 10)), max(1, len(text.strip()))))
        previous = span
    text = _clean_text("".join(fragments))
    if not text:
        return None
    size = sum(value * weight for value, weight in weighted_sizes) / sum(weight for _, weight in weighted_sizes)
    return {
        "text": text, "bbox": tuple(line["bbox"]), "size": size,
        "bold": any(bool(span.get("flags", 0) & 16) or "bold" in span.get("font", "").lower() for span in spans),
        "math_font": any(any(token in span.get("font", "").lower() for token in ("math", "symbol", "cmsy", "cmmi")) for span in spans),
    }


def _is_equation(text: str, lines: list[dict[str, Any]], body_size: float) -> bool:
    if _CAPTION.match(text) or len(text) > 450:
        return False
    math_count = len(_MATH.findall(text))
    word_count = len(re.findall(r"[A-Za-z]{3,}", text))
    math_font = sum(len(line["text"]) for line in lines if line["math_font"])
    total = max(1, sum(len(line["text"]) for line in lines))
    return (math_count >= 2 and word_count <= 8 and math_count / max(len(text), 1) >= .035) or (math_count >= 1 and word_count <= 3 and len(text) < 120 and math_count / max(len(text), 1) >= .06) or (math_font / total > .34 and math_count > 0 and word_count < 10)


def _classify(text: str, lines: list[dict[str, Any]], body_size: float) -> str:
    if _CAPTION.match(text):
        return "caption"
    if _is_equation(text, lines, body_size):
        return "equation"
    size = median(line["size"] for line in lines)
    short = len(text) < 180 and len(lines) <= 3
    terminal_sentence = bool(re.search(r"[.!?。！？]\s*$", text))
    # A genus initial followed by a sampled species count is prose, even
    # though "E. longirostris (12), ..." resembles a lettered section.
    taxon_counts = bool(re.match(r"^[A-Z]\.\s+[a-z][A-Za-z-]+\s*\(\s*(?:n\s*=\s*)?\d+", text))
    if short and (_NAMED_SECTION.match(text) or (_SECTION.match(text) and not taxon_counts and len(text) < 130 and not terminal_sentence)):
        return "heading"
    if short and size >= body_size * 1.18 and not _INSTITUTION.search(text):
        return "heading"
    if short and all(line["bold"] for line in lines) and size >= body_size * .97 and len(text) < 120 and not terminal_sentence:
        return "heading"
    return "paragraph"


def _margin_key(text: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", text.casefold())).strip()


def _vertical_overlap(a: tuple, b: tuple) -> float:
    return max(0, min(a[3], b[3]) - max(a[1], b[1])) / max(.1, min(a[3] - a[1], b[3] - b[1]))


def _join_fragments(fragments: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine word-level runs of one baseline into a single line record."""
    text, previous = "", None
    for fragment in fragments:
        if previous is not None:
            gap = fragment["bbox"][0] - previous["bbox"][2]
            if gap > max(1.0, min(fragment["size"], previous["size"]) * .15):
                text += " "
        text += fragment["text"]
        previous = fragment
    weights = [max(1, len(fragment["text"])) for fragment in fragments]
    total = sum(weights)
    rect = fitz.Rect(fragments[0]["bbox"])
    for fragment in fragments[1:]:
        rect |= fitz.Rect(fragment["bbox"])
    # The dominant size ignores superscripts, so lines of one paragraph match
    # each other exactly while two adjacent heading levels remain distinct.
    sizes = Counter()
    for fragment, weight in zip(fragments, weights):
        sizes[round(fragment["size"] * 4) / 4] += weight
    return {
        "text": _clean_text(text), "bbox": tuple(rect),
        "size": sizes.most_common(1)[0][0],
        "bold": sum(weight for fragment, weight in zip(fragments, weights) if fragment["bold"]) >= total * .6,
        "math_font": any(fragment["math_font"] for fragment in fragments),
    }


def _same_line(a: dict[str, Any], b: dict[str, Any], adjacent: bool) -> bool:
    """Compare two word runs; only touching runs may be sub/superscripts."""
    center_a, center_b = (a["bbox"][1] + a["bbox"][3]) / 2, (b["bbox"][1] + b["bbox"][3]) / 2
    if abs(a["size"] - b["size"]) < .5:
        # Neighbouring columns are often offset by a fraction of a line.
        return abs(center_a - center_b) <= max(a["size"], b["size"]) * .25
    larger = max(a["size"], b["size"])
    return adjacent and abs(center_a - center_b) <= larger * .6 and _vertical_overlap(a["bbox"], b["bbox"]) >= .5


def _rebuild_lines(fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join same-baseline words, but never across a column gutter."""
    rows: list[dict[str, Any]] = []
    # Larger text first: every line exists before its sub/superscripts are
    # attached, so a leading superscript cannot claim a line of its own.
    order = sorted(fragments, key=lambda item: (-round(item["size"] * 2) / 2,
                                                (item["bbox"][1] + item["bbox"][3]) / 2, item["bbox"][0]))
    for fragment in order:
        # Compare with the horizontally nearest word of each row, so a
        # superscript joins its own word rather than a heading in another
        # column. Touching runs come first, then a same-size baseline.
        best = None
        for row in rows:
            if abs(row["center"] - (fragment["bbox"][1] + fragment["bbox"][3]) / 2) > max(row["size"], fragment["size"]) * 1.5:
                continue
            nearest = min(row["items"], key=lambda item: max(0, item["bbox"][0] - fragment["bbox"][2],
                                                             fragment["bbox"][0] - item["bbox"][2]))
            distance = max(0, nearest["bbox"][0] - fragment["bbox"][2], fragment["bbox"][0] - nearest["bbox"][2])
            adjacent = distance <= max(fragment["size"], nearest["size"]) * 1.5
            if _same_line(fragment, nearest, adjacent):
                rank = (not adjacent, abs(fragment["size"] - nearest["size"]) >= .5, distance)
                if best is None or rank < best[0]:
                    best = (rank, row)
        if best is None:
            rows.append({"items": [fragment], "center": (fragment["bbox"][1] + fragment["bbox"][3]) / 2,
                         "size": fragment["size"]})
        else:
            best[1]["items"].append(fragment)
    rows.sort(key=lambda row: (row["center"], min(item["bbox"][0] for item in row["items"])))
    # Stretched justified spaces stay below one em, except in a rare line with
    # a long unbreakable word. A column gutter or table column recurs at the
    # same position on several rows, so only those gaps split a row.
    gaps: list[tuple[int, float, float]] = []
    for index, row in enumerate(rows):
        row["items"].sort(key=lambda item: item["bbox"][0])
        row["limit"] = median(item["size"] for item in row["items"]) * 1.1
        gaps.extend((index, previous["bbox"][2], item["bbox"][0])
                    for previous, item in zip(row["items"], row["items"][1:])
                    if item["bbox"][0] - previous["bbox"][2] > row["limit"])
    recurring = {(index, start) for index, start, end in gaps
                 if len({other for other, left, right in gaps
                         if other != index and min(end, right) - max(start, left) > 0}) >= 2}
    lines: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        items = row["items"]
        segment = [items[0]]
        for item in items[1:]:
            gap = item["bbox"][0] - segment[-1]["bbox"][2]
            # A bullet or item number is followed by a recurring gap as well.
            marker = len(segment) == 1 and bool(_LIST_MARKER.match(segment[0]["text"] + " "))
            if gap > row["limit"] and not marker and (gap > row["limit"] * 4 or (index, segment[-1]["bbox"][2]) in recurring):
                lines.append(_join_fragments(segment))
                segment = []
            segment.append(item)
        lines.append(_join_fragments(segment))
    return lines


def _starts_paragraph(line: dict[str, Any], group: list[dict[str, Any]]) -> bool:
    """Recognize first-line indents, hanging list items and run-in headings."""
    size, first, last = line["size"], group[0], group[-1]
    left = min(item["bbox"][0] for item in group)
    right = max(item["bbox"][2] for item in group)
    if _LIST_MARKER.match(first["text"]):
        # Reference lists and bullets indent their continuation lines. A
        # line back at the marker position begins the next item.
        if len(group) == 1:
            return line["bbox"][0] <= first["bbox"][0] + size * .3 and bool(_LIST_MARKER.match(line["text"]))
        return line["bbox"][0] < group[1]["bbox"][0] - size * .6
    if (_LIST_MARKER.match(last["text"]) and last["bbox"][0] <= left + size * .3
            and line["bbox"][0] > last["bbox"][0] + size * .3):
        # The hanging continuation of an item that follows text carried over
        # from the previous column; _split_list_items separates that item.
        return False
    if last["bold"] and not line["bold"] and len(last["text"]) < 80 and len(group) <= 2:
        return True
    # A full-width line starting one em inside the column opens a new
    # paragraph. Centred title lines do not reach the right edge, and an
    # inline graphic at a line start does not follow a finished sentence.
    finished = bool(re.search(r"[.!?:。！？：][\s\d,\-–\[\]()\"'’”»]*$", last["text"])) or last["bbox"][2] < right - size * 2
    return finished and line["bbox"][0] - left >= size * .6 and line["bbox"][2] >= right - size


def _merge_display_math(groups: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    """Join the pieces of a displayed formula and its number into one group.

    Fractions, sums and sub/superscripts are separate runs on different
    baselines. They become one block, rendered from the PDF as an image.
    """
    text_of = lambda group: " ".join(line["text"] for line in group)
    relation = re.compile(r"[=<>≤≥≈≠∝]")

    def candidate(group: list[dict[str, Any]]) -> bool:
        text = text_of(group)
        return (len(group) <= 4 and all(len(line["text"]) <= 70 for line in group)
                and bool(_MATH.search(text) or re.search(r"[−+]", text) or _EQUATION_NUMBER.fullmatch(text)
                         or all(len(line["text"]) <= 12 for line in group)))

    def compatible(a: dict[str, Any], b: dict[str, Any]) -> bool:
        # Two numbered formulas, or two relations on different baselines,
        # are separate displayed equations even when closely stacked.
        if a["number"] and b["number"]:
            return False
        size = max(line["size"] for group in a["groups"] + b["groups"] for line in group)
        # Limits such as "i=1" beneath a sum are smaller and do not count.
        rows = [[(line["bbox"][1] + line["bbox"][3]) / 2 for group in cluster["groups"] for line in group
                 if relation.search(line["text"]) and line["size"] >= size * .9] for cluster in (a, b)]
        return not rows[0] or not rows[1] or any(abs(y - z) <= size * .5 for y in rows[0] for z in rows[1])

    clusters = []
    for group in filter(candidate, groups):
        rect = fitz.Rect(group[0]["bbox"])
        for line in group[1:]:
            rect |= fitz.Rect(line["bbox"])
        number = bool(_EQUATION_NUMBER.fullmatch(text_of(group)))
        clusters.append({"groups": [group], "rect": rect, "number": number, "lone_number": number})
    # Join the closest pieces first, so a fraction attaches to its own left
    # side before it could reach a neighbouring formula above or below.
    while True:
        best = None
        for a in clusters:
            for b in clusters:
                if a is b or not compatible(a, b):
                    continue
                size = max(line["size"] for group in a["groups"] + b["groups"] for line in group)
                horizontal = max(0, max(a["rect"].x0, b["rect"].x0) - min(a["rect"].x1, b["rect"].x1))
                vertical = max(0, max(a["rect"].y0, b["rect"].y0) - min(a["rect"].y1, b["rect"].y1))
                # An equation number sits at the right margin of its column.
                numbered = (b["lone_number"] and b["rect"].x0 >= a["rect"].x1 - 1 and horizontal <= size * 25
                            and _vertical_overlap(tuple(a["rect"]), tuple(b["rect"])) >= .5)
                if ((vertical <= size * 1.1 and horizontal <= size * 3) or numbered) and (
                        best is None or horizontal + vertical < best[0]):
                    best = (horizontal + vertical, a, b)
        if best is None:
            break
        _, a, b = best
        a["groups"] += b["groups"]
        a["rect"] |= b["rect"]
        a["number"] = a["number"] or b["number"]
        a["lone_number"] = False
        clusters.remove(b)
    merged: dict[int, list[dict[str, Any]]] = {}
    for cluster in clusters:
        lines = [line for group in cluster["groups"] for line in group]
        text = " ".join(line["text"] for line in lines)
        # Only a relation makes a formula; lone symbols remain ordinary text.
        if not re.search(r"[=<>≤≥≈≠∝]", text):
            continue
        lines.sort(key=lambda line: ((line["bbox"][1] + line["bbox"][3]) / 2, line["bbox"][0]))
        lines[0] = {**lines[0], "display_math": True}
        for group in cluster["groups"]:
            merged[id(group)] = lines
    result, emitted = [], set()
    for group in groups:
        lines = merged.get(id(group))
        if lines is None:
            result.append(group)
        elif id(lines) not in emitted:
            emitted.add(id(lines))
            result.append(lines)
    return result


def _group_lines(lines: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group rebuilt lines into column paragraphs, as PDF blocks would be."""
    groups: list[list[dict[str, Any]]] = []
    for line in sorted(lines, key=lambda item: (item["bbox"][1], item["bbox"][0])):
        best, best_overlap = None, 0.0
        for group in groups:
            last = group[-1]
            size = max(line["size"], last["size"])
            gap = line["bbox"][1] - last["bbox"][3]
            overlap = max(0, min(line["bbox"][2], last["bbox"][2]) - max(line["bbox"][0], last["bbox"][0]))
            overlap /= max(1, min(line["bbox"][2] - line["bbox"][0], last["bbox"][2] - last["bbox"][0]))
            if (-size * .3 <= gap <= size * .75 and overlap >= .5 and overlap > best_overlap
                    and abs(line["size"] - last["size"]) <= max(.3, size * .03)):
                best, best_overlap = group, overlap
        if best is not None and not _starts_paragraph(line, best):
            best.append(line)
        else:
            groups.append([line])
    return _merge_display_math([item for group in groups for item in _split_list_items(group)])


def _split_list_items(group: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Separate hanging-indent list items, as in a reference list."""
    left = min(line["bbox"][0] for line in group)
    result = [[group[0]]]
    for index, line in enumerate(group[1:], 1):
        following = group[index + 1] if index + 1 < len(group) else None
        if (_LIST_MARKER.match(line["text"]) and line["bbox"][0] <= left + line["size"] * .3
                and following is not None and following["bbox"][0] > line["bbox"][0] + line["size"] * .3):
            result.append([])
        result[-1].append(line)
    return result


def _word_runs(lines: list[dict[str, Any]]) -> bool:
    """Whether a page stores its prose as one text line per word.

    Such words sit one space apart from a neighbour on the same baseline.
    Table cells are single tokens as well, but separated by wide gaps.
    """
    singles = [line for line in lines if " " not in line["text"]]
    if len(lines) < DEFAULT_FRAGMENTED_MIN_LINES or len(singles) < len(lines) * DEFAULT_FRAGMENTED_WORD_SHARE:
        return False
    by_row: dict[int, list[dict[str, Any]]] = {}
    for line in lines:
        by_row.setdefault(round((line["bbox"][1] + line["bbox"][3]) / 2), []).append(line)
    spaced = 0
    for line in singles:
        center, size = (line["bbox"][1] + line["bbox"][3]) / 2, line["size"]
        spaced += any(other is not line and abs((other["bbox"][1] + other["bbox"][3]) / 2 - center) <= size * .25
                      and (0 <= other["bbox"][0] - line["bbox"][2] <= size * .6 or 0 <= line["bbox"][0] - other["bbox"][2] <= size * .6)
                      for key in range(round(center) - 2, round(center) + 3) for other in by_row.get(key, []))
    return spaced >= len(singles) * DEFAULT_FRAGMENTED_WORD_SHARE


def _read_page(page: fitz.Page, number: int) -> dict[str, Any]:
    # TEXTFLAGS_TEXT deliberately excludes raster image payloads from the dict.
    raw = page.get_text("dict", flags=fitz.TEXTFLAGS_TEXT, sort=False)
    text_lines = [line for block in raw.get("blocks", []) if block.get("type") == 0
                  for line in block.get("lines", [])]
    horizontal = sum(len(span.get("text", "")) for line in text_lines if abs(line.get("dir", (1, 0))[1]) < .2
                     for span in line.get("spans", []))
    total = sum(len(span.get("text", "")) for line in text_lines for span in line.get("spans", []))
    # Download stamps and arXiv identifiers run vertically along a margin.
    # Rotated text is retained when it is the page's main text direction.
    upright = total > 0 and horizontal >= total * .6
    groups: list[list[dict[str, Any]]] = []
    rotated: list[str] = []
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        group = []
        for line in block.get("lines", []):
            value = _line_from_spans(line)
            if value is None:
                continue
            if upright and abs(line.get("dir", (1, 0))[1]) >= .2:
                rotated.append(value["text"])
                continue
            group.append(value)
        if group:
            groups.append(group)
    lines = [line for group in groups for line in group]
    fragmented = _word_runs(lines)
    if fragmented:
        groups = _group_lines(_rebuild_lines(lines))
    # Dotted rules and leader lines carry no readable text. Some typesetters
    # draw table rules this way, so their extents still bound tables.
    dotted = lambda line: len(line["text"]) >= 6 and not re.search(r"\w", line["text"])
    dotted_rules = [_bbox(line["bbox"]) for group in groups for line in group
                    if dotted(line) and line["bbox"][2] - line["bbox"][0] >= 45]
    groups = [kept for group in groups if (kept := [line for line in group if not dotted(line)])]
    weighted = Counter()
    for group in groups:
        for line in group:
            weighted[round(line["size"] * 2) / 2] += len(line["text"])
    # The dominant character-weighted font is more stable than the median of
    # spans when a paper contains many small labels or footnotes.
    body_size = float(weighted.most_common(1)[0][0]) if weighted else 10.0
    return {"number": number, "width": float(page.rect.width), "height": float(page.rect.height), "groups": groups,
            "body_size": body_size, "fragmented": fragmented, "rotated_key": _alignment_key(" ".join(rotated)),
            "dotted_rules": dotted_rules}


def _remove_margins(pages: list[dict[str, Any]]) -> None:
    occurrences: dict[str, set[int]] = {}
    for page in pages:
        for group in page["groups"]:
            for line in group:
                if line["bbox"][1] < page["height"] * .075 or line["bbox"][3] > page["height"] * .93:
                    occurrences.setdefault(_margin_key(line["text"]), set()).add(page["number"])
    # Odd and even pages often carry different running headers, so each
    # variant appears on only about half of the pages.
    required = max(2, ceil(len(pages) * .3))
    repeated = {key for key, numbers in occurrences.items() if len(numbers) >= required}
    for page in pages:
        kept = []
        for group in page["groups"]:
            filtered = []
            for line in group:
                in_margin = line["bbox"][1] < page["height"] * .075 or line["bbox"][3] > page["height"] * .93
                # Keep large title text on page one even when it is repeated as
                # a small running header on subsequent pages.
                is_title = page["number"] == 1 and line["size"] > page["body_size"] * 1.3
                is_page_number = _PAGE_MARKER.match(line["text"]) and (line["bbox"][1] < page["height"] * .045 or line["bbox"][3] > page["height"] * .93)
                if not is_title and in_margin and (_margin_key(line["text"]) in repeated or is_page_number):
                    continue
                filtered.append(line)
            if filtered:
                kept.append(filtered)
        page["groups"] = kept


def _split_group(group: list[dict[str, Any]], body_size: float) -> list[list[dict[str, Any]]]:
    result: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for line in group:
        split = False
        if current:
            previous = current[-1]
            gap = line["bbox"][1] - previous["bbox"][3]
            size_change = abs(line["size"] - previous["size"]) > max(1.4, body_size * .16)
            starts_heading = bool(_NAMED_SECTION.match(line["text"])) or bool(_SECTION.match(line["text"]) and line["bold"])
            previous_heading = bool(_NAMED_SECTION.match(previous["text"])) or (previous["size"] > body_size * 1.18 and len(previous["text"]) < 100)
            same_baseline = abs(line["bbox"][1] - previous["bbox"][1]) < body_size * .5
            split = not same_baseline and (gap > body_size * .9 or (size_change and not (line["math_font"] or previous["math_font"])) or starts_heading or previous_heading and size_change)
        if split:
            result.append(current)
            current = []
        current.append(line)
    if current:
        result.append(current)
    return result


def _order_blocks(blocks: list[dict[str, Any]], width: float) -> list[dict[str, Any]]:
    if len(blocks) < 4:
        return sorted(blocks, key=lambda block: (round(block["bbox"][1] / 4), block["bbox"][0]))
    left = [block for block in blocks if block["bbox"][0] < width * .36 and block["bbox"][2] < width * .61 and len(block["text"]) > 20]
    right = [block for block in blocks if block["bbox"][0] > width * .43 and block["bbox"][2] > width * .64 and len(block["text"]) > 20]
    # A first page may hold only one prose block per column beneath a wide
    # abstract. Two side-by-side blocks are sufficient evidence of columns.
    side_by_side = any(min(a["bbox"][3], b["bbox"][3]) > max(a["bbox"][1], b["bbox"][1]) for a in left for b in right)
    if not side_by_side:
        return sorted(blocks, key=lambda block: (round(block["bbox"][1] / 4), block["bbox"][0]))
    divider = (median(block["bbox"][2] for block in left) + median(block["bbox"][0] for block in right)) / 2
    spanning = [block for block in blocks if block["bbox"][0] < divider - width * .10 and block["bbox"][2] > divider + width * .10]
    remaining = [block for block in blocks if block not in spanning]
    ordered: list[dict[str, Any]] = []

    def append_columns(region: list[dict[str, Any]]) -> None:
        for column in (0, 1):
            subset = [block for block in region if int((block["bbox"][0] + block["bbox"][2]) / 2 >= divider) == column]
            ordered.extend(sorted(subset, key=lambda block: (block["bbox"][1], block["bbox"][0])))

    for anchor in sorted(spanning, key=lambda block: (block["bbox"][1], block["bbox"][0])):
        before = [block for block in remaining if (block["bbox"][1] + block["bbox"][3]) / 2 < (anchor["bbox"][1] + anchor["bbox"][3]) / 2]
        append_columns(before)
        remaining = [block for block in remaining if block not in before]
        ordered.append(anchor)
    append_columns(remaining)
    return ordered


def _make_blocks(page: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = []
    for group in page["groups"]:
        display_math = bool(group[0].get("display_math"))
        for lines in [group] if display_math else _split_group(group, page["body_size"]):
            text = _join_lines(lines)
            if not text:
                continue
            kind = "equation" if display_math else _classify(text, lines, page["body_size"])
            if kind == "equation":
                text = _join_lines(lines, equation=True)
            rect = fitz.Rect(lines[0]["bbox"])
            for line in lines[1:]:
                rect |= fitz.Rect(line["bbox"])
            block = {"page": page["number"], "type": kind, "text": text, "bbox": _bbox(rect),
                     "first_line_bbox": _bbox(lines[0]["bbox"]), "last_line_bbox": _bbox(lines[-1]["bbox"]),
                     "font_size": round(median(line["size"] for line in lines), 2)}
            if display_math:
                # The reader shows the PDF crop; its pieces are not prose.
                block["display_math"] = True
            blocks.append(block)
    blocks = _order_blocks(blocks, page["width"])
    for index, block in enumerate(blocks):
        block["id"] = f"p{page['number']}-b{index + 1}"
    return blocks


def _alignment_key(text: str) -> str:
    """Match converter text to original lines despite ligatures and spacing."""
    return re.sub(r"[\W_]", "", _clean_text(text).casefold())


def _markdown_blocks(markdown: str, page: dict[str, Any],
                     figures: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Use MarkItDown paragraphs/order; original lines supply geometry only.

    PDF text extraction engines disagree about span boundaries, whitespace,
    ligatures and hyphenated words. Alphanumeric alignment preserves the actual
    converter text while retaining page navigation, caption crops and font
    evidence for section headings. It never reorders the Markdown paragraphs.
    """
    original_lines = [(line, _alignment_key(line["text"]))
                      for group in page["groups"] for line in group]
    page_figures = [figure for figure in figures or [] if figure["page"] == page["number"]]
    regions = [fitz.Rect(region) for figure in page_figures for region in figure.get("visual_regions", [])]
    table_regions = [fitz.Rect(figure["bbox"]) for figure in page_figures if figure["kind"] == "table"]
    active_table = table_regions[0] if table_regions else None
    blocks: list[dict[str, Any]] = []
    for paragraph in re.split(r"\n[ \t]*\n+", markdown.replace("\r\n", "\n")):
        parts: list[dict[str, Any]] = []
        for line_index, original_text in enumerate(paragraph.splitlines()):
            text = _clean_text(original_text)
            heading_marker = re.match(r"^#{1,6}\s+", text)
            if heading_marker:
                text = text[heading_marker.end():]
            comparable = _alignment_key(text)
            if not comparable or _PAGE_MARKER.fullmatch(text):
                continue
            if len(comparable) >= 8 and comparable in page.get("rotated_key", ""):
                continue
            caption_match = _CAPTION.match(text)
            if caption_match:
                known_label = any((source_label := _CAPTION.match(figure.get("caption", "")))
                                  and _alignment_key(source_label.group(0)) == _alignment_key(caption_match.group(0))
                                  for figure in page_figures)
                if ")" in caption_match.group(0) or (line_index > 0 and not known_label):
                    caption_match = None
            if caption_match and caption_match.group("kind").lower() in {"table", "表"}:
                label = _alignment_key(caption_match.group(0))
                match = next((figure for figure in page_figures if figure["kind"] == "table"
                              and (source_label := _CAPTION.match(figure.get("caption", "")))
                              and _alignment_key(source_label.group(0)) == label), None)
                if match:
                    active_table = fitz.Rect(match["bbox"])
            table_markup = "|" in text and active_table is not None and not caption_match
            aligned = [line for line, key in original_lines if key == comparable]
            if not aligned:
                aligned = [line for line, key in original_lines if len(key) >= 4 and (
                    key in comparable and len(key) >= len(comparable) * .65 or
                    comparable in key and len(comparable) >= max(4, len(key) * .65)
                )]
            if not aligned:
                # A converted table row combines cells from several geometric
                # lines. Only use those smaller matches within a known table.
                aligned = [line for line, key in original_lines if len(key) >= 4 and key in comparable
                           and active_table is not None and active_table.contains(fitz.Rect(line["bbox"]))]
            if len(aligned) > 1 and active_table is not None and len(comparable) < 70:
                inside_table = [line for line in aligned if active_table.contains(fitz.Rect(line["bbox"]))]
                if inside_table:
                    aligned = inside_table
            if not aligned and not table_markup and (len(comparable) < 25 or re.match(r"^https?://\S+$", text)):
                continue
            rect = fitz.Rect(aligned[0]["bbox"]) if aligned else fitz.Rect(active_table) if table_markup else fitz.Rect(0, 0, page["width"], page["height"])
            for line in aligned[1:]:
                rect |= fitz.Rect(line["bbox"])
            if table_markup and aligned and not any(active_table.contains(fitz.Rect(line["bbox"])) for line in aligned):
                # Form detection can wrap an ordinary prose line in a one-row
                # Markdown table. Keep it as prose when its actual PDF location
                # is outside the detected source table.
                table_markup = False
                text = _clean_text(text.replace("|", " "))
            if (rect.x1 < page["width"] * .055 or rect.x0 > page["width"] * .955) and len(re.findall(r"[A-Za-z\u3400-\u9fff]", text)) < 4:
                continue
            # PdfConverter's form path occasionally removes spaces from a
            # prose line. Restore whitespace only when the original characters
            # match exactly; content and order still come from MarkItDown.
            if len(aligned) == 1 and _alignment_key(aligned[0]["text"]) == comparable and ("|" in original_text or len(text.split()) < len(aligned[0]["text"].split()) * .55):
                text = aligned[0]["text"]
            kind = _classify(text, aligned, page["body_size"]) if aligned else "paragraph"
            if kind == "caption" and caption_match is None:
                kind = "paragraph"
            caption_start = bool(caption_match)
            if table_markup:
                rect = fitz.Rect(active_table)
            visual = not caption_start and (table_markup or any(region.contains(rect) for region in regions))
            scope = "figure_content" if visual else "caption" if caption_start else "body"
            if parts and parts[-1]["scope"] == "caption" and not visual and not caption_start and not heading_marker:
                # Keep the complete multi-line caption together, including its
                # non-bold continuation. A following table/figure ends it.
                kind, scope = "caption", "caption"
            if heading_marker:
                kind = "heading"
            if visual:
                kind = "table" if table_markup else "paragraph"
            previous = parts[-1] if parts else None
            separate = (previous is None or scope != previous["scope"] or
                        (kind == "heading") != (previous["type"] == "heading") or
                        caption_start or kind == "heading" and bool(_NAMED_SECTION.match(text)))
            if separate:
                parts.append({"type": kind, "scope": scope, "lines": [], "aligned": [], "rect": rect})
            part = parts[-1]
            part["lines"].append({"text": text})
            part["aligned"].extend(aligned)
            part["rect"] |= rect
        for part in parts:
            text = _join_lines(part["lines"], equation=part["type"] == "equation")
            aligned = part["aligned"]
            blocks.append({"id": f"p{page['number']}-b{len(blocks) + 1}",
                           "page": page["number"], "type": part["type"], "text": text,
                           "bbox": _bbox(part["rect"]),
                           "first_line_bbox": _bbox(aligned[0]["bbox"]) if aligned else None,
                           "last_line_bbox": _bbox(aligned[-1]["bbox"]) if aligned else None,
                           "font_size": round(median(line["size"] for line in aligned), 2) if aligned else page["body_size"],
                           "text_source": "markitdown", "geometry_matched": bool(aligned),
                           "translation_scope": part["scope"]})
    return blocks


def _prose_continues(previous: dict[str, Any], current: dict[str, Any],
                     page: dict[str, Any], column_starts: list[float]) -> bool:
    """Recognize a wrapped prose line without treating an empty line as proof."""
    if any(block.get("type") != "paragraph" or block.get("translation_scope") != "body"
           or block.get("geometry_matched") is False for block in (previous, current)):
        return False
    left, right = previous["text"].rstrip(), current["text"].lstrip()
    # Lists and bibliography entries have independent semantic boundaries.
    item_start = r"^(?:\[\d+\]\s*|\((?:\d+|[A-Za-z]|[ivxlcdmIVXLCDM]+)\)\s+|(?:\d+|[A-Za-z]|[ivxlcdmIVXLCDM]+)[.)]\s+|[•▪◦*]\s*|[-–—]\s+)"
    if not left or not right or any(re.match(item_start, text) for text in (left, right)):
        return False
    if _CAPTION.match(right) or _NAMED_SECTION.match(right):
        return False
    # Superscript references can follow the full stop in extracted plain text.
    if re.search(r"[.!?。！？][\s\d,\-–\[\]()\"'’”»]*$", left):
        return False
    lower_start = bool(re.match(r"[a-z\u3400-\u9fff]", right))
    proper_name_continuation = (bool(re.match(r"[A-Z][A-Za-z]", right)) and
                                bool(re.search(r"\b(?:a|an|the|of|to|in|on|for|with|and|or|by|from|as|between|among|than|at)$", left, re.I)))
    if not (lower_start or proper_name_continuation):
        return False
    size = max(float(previous.get("font_size", 0)), float(current.get("font_size", 0)))
    if size <= 0 or abs(float(previous.get("font_size", 0)) - float(current.get("font_size", 0))) > max(.6, size * .08):
        return False
    tail = previous.get("source_fragments", [previous])[-1]
    tail_rect, head_rect = tail["bbox"], current["bbox"]
    last, first = tail.get("last_line_bbox"), current.get("first_line_bbox")
    if not last or not first:
        return False
    # A normal new paragraph is often indented, even when Markdown drops that
    # distinction. Compare the actual first line with its column's left edge.
    nearby_starts = [start for start in column_starts if abs(start - head_rect[0]) <= size * 2.5]
    column_left = min(nearby_starts, default=head_rect[0])
    if first[0] - column_left > max(2, size * .35):
        return False
    # Wrapped text reaches the column edge; a short final line is evidence for
    # a real paragraph ending, even when its punctuation could not be read.
    if tail_rect[2] - last[2] > max(size * 3, (tail_rect[2] - tail_rect[0]) * .1):
        return False
    column_change = (head_rect[0] - tail_rect[0] > page["width"] * .18 and
                     head_rect[0] >= tail_rect[2] - size * .5 and first[1] < last[1] - size)
    overlap = max(0, min(tail_rect[2], head_rect[2]) - max(tail_rect[0], head_rect[0]))
    same_column = (overlap >= min(tail_rect[2] - tail_rect[0], head_rect[2] - head_rect[0]) * .75 and
                   -.3 * size <= first[1] - last[3] <= size * 1.1)
    return column_change or same_column


def _merge_prose_continuations(document: dict[str, Any]) -> None:
    """Repair same-page prose after visual scopes and headings are resolved.

    Keep each original region instead of creating one large rectangle across
    columns and intervening figures. IDs and the leading region remain stable
    for saved highlights; source fragments retain the other original anchors.
    Physical PDF page boundaries remain available for page navigation.
    """
    protected = {item.get("block_id") for item in document.get("toc", [])}
    protected.update(figure.get("caption_block_id") for figure in document.get("figures", []))
    metadata_text = {document.get("title"), document.get("authors")}
    in_references = False
    for page in document.get("pages", []):
        blocks = page.get("blocks", [])
        starts = [block["bbox"][0] for block in blocks
                  if block.get("type") == "paragraph" and block.get("translation_scope") == "body"]
        merged: list[dict[str, Any]] = []
        for block in blocks:
            text = block.get("text", "").strip()
            if block.get("type") == "heading":
                if re.fullmatch(r"(?:\d+(?:\.\d+)*\.?\s+)?(?:references|bibliography|参考文献)\s*[:：.]?", text, re.I):
                    in_references = True
                elif _NAMED_SECTION.match(text):
                    in_references = False
            # A paragraph can begin with an inline section title. Retaining
            # its ID lets its continuation join without moving the TOC anchor.
            if (merged and not in_references and block.get("id") not in protected
                    and text not in metadata_text and merged[-1].get("text") not in metadata_text
                    and _prose_continues(merged[-1], block, page, starts)):
                previous = merged[-1]
                def fragments(source):
                    return source.get("source_fragments") or [
                        {key: source[key] for key in ("id", "page", "text", "bbox", "first_line_bbox", "last_line_bbox") if key in source}
                    ]
                previous["source_fragments"] = fragments(previous) + fragments(block)
                previous["text"] = _join_lines([{"text": previous["text"]}, {"text": block["text"]}])
                previous["last_line_bbox"] = block.get("last_line_bbox")
                previous.pop("text_zh", None)
            else:
                merged.append(block)
        page["blocks"] = merged


def _paragraph_pair_matches(previous: dict[str, Any], current: dict[str, Any],
                            column_starts: list[float]) -> bool:
    """Require prose, a full trailing line and an unindented continuation."""
    if any(block.get("type") != "paragraph" or block.get("translation_scope") != "body"
           or block.get("geometry_matched") is False for block in (previous, current)):
        return False
    left, right = previous.get("text", "").rstrip(), current.get("text", "").lstrip()
    item_start = r"^(?:\[\d+\]\s*|\((?:\d+|[A-Za-z]|[ivxlcdmIVXLCDM]+)\)\s+|(?:\d+|[A-Za-z]|[ivxlcdmIVXLCDM]+)[.)]\s+|[•▪◦*]\s*|[-–—]\s+)"
    if not left or not right or any(re.match(item_start, text) for text in (left, right)):
        return False
    if _CAPTION.match(right) or _NAMED_SECTION.match(right):
        return False
    if re.search(r"[.!?。！？][\s\d,\-–\[\]()\"'’”»]*$", left):
        return False
    # A wrapped sentence can resume with an opening parenthesis or a proper
    # noun. The geometry requirements below still distinguish a new paragraph.
    head = right.lstrip("(（[‘’\"“")
    lower_start = bool(re.match(r"[a-z\u3400-\u9fff]", head))
    proper_start = bool(re.match(r"[A-Z][A-Za-z]", head)) and (
        head != right or bool(re.search(r"\b(?:a|an|the|of|to|in|on|for|with|and|or|by|from|as|between|among|than|at)$", left, re.I)))
    if not (lower_start or proper_start):
        return False
    size = max(float(previous.get("font_size", 0)), float(current.get("font_size", 0)))
    if size <= 0 or abs(float(previous.get("font_size", 0)) - float(current.get("font_size", 0))) > max(.6, size * .08):
        return False
    tail = (previous.get("source_fragments") or [previous])[-1]
    tail_rect, head_rect = tail.get("bbox"), current.get("bbox")
    last, first = tail.get("last_line_bbox"), current.get("first_line_bbox")
    if not tail_rect or not head_rect or not last or not first:
        return False
    nearby_starts = [start for start in column_starts if abs(start - head_rect[0]) <= size * 2.5]
    column_left = min(nearby_starts, default=head_rect[0])
    if first[0] - column_left > max(2, size * .35):
        return False
    if tail_rect[2] - last[2] > max(size * 3, (tail_rect[2] - tail_rect[0]) * .1):
        return False
    return True


def _bottom_graphic_follows(tail: dict[str, Any], page: dict[str, Any],
                           figures: list[dict[str, Any]], size: float) -> bool:
    """Allow an early page break only when an actual large graphic follows."""
    last, column = tail["last_line_bbox"], tail["bbox"]
    column_width = column[2] - column[0]
    for figure in figures:
        if figure.get("page") != page["number"]:
            continue
        # Inferred figure crops can include whitespace and neighbouring prose.
        # Use the confirmed source components rather than that broad crop.
        regions = figure.get("visual_regions", [])
        if not regions:
            continue
        graphic = fitz.Rect(regions[0])
        for region in regions[1:]:
            graphic |= fitz.Rect(region)
        overlap = max(0, min(column[2], graphic.x1) - max(column[0], graphic.x0))
        gap = graphic.y0 - last[3]
        if (column_width > 0 and overlap >= column_width * .65
                and 0 <= gap <= max(size * 4, page["height"] * .055)
                and graphic.y1 >= page["height"] * .7
                and graphic.height >= page["height"] * .2):
            return True
    return False


def annotate_paragraph_groups(document: dict[str, Any]) -> None:
    """Link complete prose across pages/graphics without moving PDF regions.

    Group order is the converter's canonical order, independent of a later AI
    page permutation. Original IDs, physical pages and graphic positions remain
    intact so translation and navigation can refer to the same source blocks.
    """
    old_groups = {(tuple(group.get("block_ids", [])), group.get("text")): group
                  for group in document.get("paragraph_groups", [])}
    protected = {item.get("block_id") for item in document.get("toc", [])}
    metadata = {document.get("title"), document.get("authors")}
    pages = {page["number"]: page for page in document.get("pages", [])}
    starts = {number: [block["bbox"][0] for block in page.get("blocks", [])
                       if block.get("type") == "paragraph" and block.get("translation_scope") == "body"
                       and block.get("geometry_matched") is not False and block.get("bbox")
                       and block["bbox"][2] - block["bbox"][0] < page["width"] * .85]
              for number, page in pages.items()}
    all_blocks = [block for page in pages.values() for block in page.get("blocks", [])]
    prose_pages = {number for number, page in pages.items() for block in page.get("blocks", [])
                   if block.get("type") == "paragraph" and block.get("translation_scope") == "body"
                   and len(block.get("text", "")) >= 200}
    for block in all_blocks:
        for key in ("paragraph_id", "continuation_of", "continues_to"):
            block.pop(key, None)
    groups: dict[str, list[dict[str, Any]]] = {}
    previous = None
    in_references = False
    for page in pages.values():
        text_counts = Counter(block.get("text", "").strip() for block in page.get("blocks", []))
        body_sizes = [float(block.get("font_size", 0)) for block in page.get("blocks", [])
                      if block.get("type") == "paragraph" and block.get("translation_scope") == "body"
                      and len(block.get("text", "")) >= 200]
        body_size = median(body_sizes) if body_sizes else 0
        for block in page.get("blocks", []):
            text = block.get("text", "").strip()
            if block.get("type") == "heading":
                if re.fullmatch(r"(?:\d+(?:\.\d+)*\.?\s+)?(?:references|bibliography|参考文献)\s*[:：.]?", text, re.I):
                    in_references = True
                elif _NAMED_SECTION.match(text):
                    in_references = False
                # A converter can repeat a running header at the end of a
                # page. Its physical top margin cannot separate bottom prose.
                if (block.get("id") not in protected and not _NAMED_SECTION.match(text)
                        and text_counts[text] > 1
                        and block.get("bbox", [0, 0, 0, page["height"]])[3] < page["height"] * .065):
                    continue
                previous = None
                continue
            if block.get("translation_scope") in {"caption", "figure_content"}:
                continue
            # Keep first-page affiliation/footer text independent from article
            # prose. It must not mask a genuine continuation on the next page.
            ancillary = (text in metadata or bool(re.match(r"^(?:e[- ]?mail\s*:|https?://|doi\s*:)", text, re.I))
                         or (body_size and float(block.get("font_size", 0)) < body_size * .87
                             and len(text) < 220 and block.get("bbox", [0, 0])[1] > page["height"] * .85))
            if ancillary:
                continue
            if in_references or block.get("type") != "paragraph" or block.get("translation_scope") != "body":
                previous = None
                continue
            match = False
            if (previous and block.get("id") not in protected
                    and _paragraph_pair_matches(previous, block, starts[page["number"]])):
                previous_page = pages.get(previous.get("page"))
                tail = (previous.get("source_fragments") or [previous])[-1]
                last, first = tail["last_line_bbox"], block["first_line_bbox"]
                if previous.get("page") == page["number"]:
                    if _prose_continues(previous, block, page, starts[page["number"]]):
                        match = True
                    elif first[1] > last[3]:
                        a, b = tail["bbox"], block["bbox"]
                        overlap = max(0, min(a[2], b[2]) - max(a[0], b[0]))
                        same_column = overlap >= min(a[2] - a[0], b[2] - b[0]) * .75
                        corridor = fitz.Rect(max(a[0], b[0]), last[3], min(a[2], b[2]), first[1])
                        graphic_between = any(
                            figure.get("page") == page["number"] and corridor.intersects(fitz.Rect(figure["bbox"]))
                            for figure in document.get("figures", []) if figure.get("bbox"))
                        match = same_column and graphic_between
                # Full-page figures between the two halves carry no prose.
                elif (previous_page and page["number"] > previous_page["number"]
                      and not any(number in prose_pages for number in range(previous_page["number"] + 1, page["number"]))
                      and (last[3] >= previous_page["height"] * .7
                           or _bottom_graphic_follows(tail, previous_page, document.get("figures", []),
                                                     float(previous.get("font_size", 0))))
                      and block["bbox"][0] <= min(starts[page["number"]], default=block["bbox"][0]) + float(block.get("font_size", 0)) * 2):
                    match = True
            if match:
                group_id = previous.get("paragraph_id", previous["id"])
                members = groups.setdefault(group_id, [previous])
                members.append(block)
                previous["paragraph_id"] = block["paragraph_id"] = group_id
                previous["continues_to"] = block["id"]
                block["continuation_of"] = previous["id"]
            previous = block
    result = []
    for identifier, members in groups.items():
        text = _join_lines([{"text": member["text"]} for member in members])
        block_ids = [member["id"] for member in members]
        group = {"id": identifier, "block_ids": block_ids, "text": text}
        cached = old_groups.get((tuple(block_ids), text), {})
        if cached.get("text_zh"):
            group["text_zh"] = cached["text_zh"]
        result.append(group)
    document["paragraph_groups"] = result


def _metadata(pages: list[dict[str, Any]], pdf_metadata: dict[str, Any]) -> tuple[str, str, str, set[str]]:
    first = pages[0]
    blocks = first["blocks"]
    body_size = first["body_size"]
    candidates = [block for block in blocks if block["bbox"][1] < first["height"] * .42 and 8 <= len(block["text"]) <= 550 and not _CAPTION.match(block["text"]) and not _INSTITUTION.search(block["text"])]
    max_size = max((block["font_size"] for block in candidates), default=body_size)
    title_blocks = [block for block in candidates if block["font_size"] >= max_size * .88 and block["font_size"] >= body_size * 1.18]
    if title_blocks:
        title_blocks.sort(key=lambda block: (block["bbox"][1], block["bbox"][0]))
        anchor = title_blocks[0]
        title_blocks = [block for block in title_blocks if block["bbox"][1] <= anchor["bbox"][3] + max_size * 3.2]
        title = " ".join(block["text"] for block in title_blocks)
    else:
        metadata_title = _clean_text(pdf_metadata.get("title") or "")
        title = metadata_title if len(metadata_title) >= 8 and not re.search(r"untitled|microsoft|\.docx?$|\.tex$", metadata_title, re.I) else ""
        title_blocks = []
        if not title:
            candidate = next((block for block in candidates if block["bbox"][1] < first["height"] * .32 and len(block["text"]) > 15), None)
            title = candidate["text"] if candidate else "未识别标题的论文"
            title_blocks = [candidate] if candidate else []
    title_bottom = max((block["bbox"][3] for block in title_blocks), default=first["height"] * .13)
    author_candidates = [block for block in blocks if title_bottom - 1 <= block["bbox"][1] < min(first["height"] * .49, title_bottom + 100) and len(block["text"]) < 500 and block["type"] != "caption"]
    authors = ""
    for block in sorted(author_candidates, key=lambda block: (block["bbox"][1], block["bbox"][0])):
        text = block["text"]
        if _ABSTRACT.match(text) or _NAMED_SECTION.match(text) or _SECTION.match(text):
            break
        if _INSTITUTION.search(text) or _PUBLISHER_NAVIGATION.match(text) or len(text) > 350 or re.search(r"copyright|©|proceedings|accepted|published|received", text, re.I):
            continue
        if len(re.findall(r"[A-Za-z\u3400-\u9fff]", text)) > 3:
            authors = text
            continuation = next((candidate["text"] for candidate in author_candidates
                                 if candidate["id"] != block["id"]
                                 and re.match(r"^(?:&|and)\s+", candidate["text"], re.I)
                                 and block["bbox"][1] <= candidate["bbox"][1] <= block["bbox"][3] + body_size * 2
                                 and not _INSTITUTION.search(candidate["text"])), "")
            if continuation:
                authors += " " + continuation
            break
    if not authors:
        author_metadata = _clean_text(pdf_metadata.get("author") or "")
        if not _INSTITUTION.search(author_metadata) and not _PUBLISHER_NAVIGATION.match(author_metadata) and len(author_metadata) < 500:
            authors = author_metadata
    abstract_parts: list[str] = []
    started = False
    for page in pages[:2]:
        for block in page["blocks"]:
            text = block["text"]
            if not started:
                match = _ABSTRACT.match(text)
                if not match:
                    continue
                started = True
                text = text[match.end():].strip()
                if text:
                    abstract_parts.append(text)
                continue
            if _STOP_ABSTRACT.match(text) or block["type"] == "heading" or _SECTION.match(text) or len(" ".join(abstract_parts)) > 4200:
                return title[:1200], authors, _trim_abstract(" ".join(abstract_parts)), {block["id"] for block in title_blocks}
            if block["type"] == "paragraph":
                abstract_parts.append(text)
    return title[:1200], authors, _trim_abstract(" ".join(abstract_parts)), {block["id"] for block in title_blocks}


def _trim_abstract(text: str) -> str:
    text = re.split(r"\s+(?:Keywords?|Index Terms|关键词)\s*[:：.—–-]", text, maxsplit=1, flags=re.I)[0]
    return _clean_text(text)[:6000]


def _make_toc(document: fitz.Document, pages: list[dict[str, Any]], title_ids: set[str]) -> list[dict[str, Any]]:
    toc: list[dict[str, Any]] = []
    try:
        native = document.get_toc(simple=False)
    except (RuntimeError, ValueError):
        native = []
    for entry in native:
        level, title, page_number = entry[:3]
        title = _clean_text(str(title))
        if not title or not 1 <= page_number <= len(pages):
            continue
        item = {"id": f"toc-{len(toc) + 1}", "title": title, "title_zh": "", "page": int(page_number), "level": max(1, int(level))}
        comparable = re.sub(r"\W", "", title).casefold()
        matching = [block for block in pages[page_number - 1]["blocks"] if comparable and comparable in re.sub(r"\W", "", block["text"]).casefold()]
        if matching:
            item["block_id"] = matching[0]["id"]
        toc.append(item)
    if toc:
        return toc
    headings = [block for page in pages for block in page["blocks"] if block["type"] == "heading" and block["id"] not in title_ids]
    sizes = sorted({round(block["font_size"], 1) for block in headings}, reverse=True)
    for block in headings:
        title = block["text"]
        if len(title) < 3 or _CAPTION.match(title) or _INSTITUTION.search(title) or title.casefold() in {"abstract", "摘要", "keywords", "关键词"}:
            continue
        number = re.match(r"^(\d+(?:\.\d+)*)\.?\s+", title)
        level = min(5, number.group(1).count(".") + 1) if number else min(3, sizes.index(round(block["font_size"], 1)) + 1)
        toc.append({"id": f"toc-{len(toc) + 1}", "title": title, "title_zh": "", "page": block["page"], "level": level, "block_id": block["id"]})
        if len(toc) >= 180:
            break
    return toc


def _intersection_fraction(a: fitz.Rect, b: fitz.Rect) -> float:
    intersection = a & b
    return 0 if intersection.is_empty else intersection.get_area() / max(1, min(a.get_area(), b.get_area()))


def _x_overlap(a: fitz.Rect, b: fitz.Rect) -> float:
    return max(0, min(a.x1, b.x1) - max(a.x0, b.x0)) / max(1, min(a.width, b.width))


def _cluster_rectangles(rectangles: list[fitz.Rect], gap: float, page_rect: fitz.Rect) -> list[fitz.Rect]:
    clusters: list[fitz.Rect] = []
    for rectangle in sorted(rectangles, key=lambda rect: (rect.y0, rect.x0)):
        candidate = fitz.Rect(rectangle)
        changed = True
        while changed:
            changed = False
            for index, cluster in enumerate(clusters):
                expanded = fitz.Rect(candidate.x0 - gap, candidate.y0 - gap, candidate.x1 + gap, candidate.y1 + gap)
                if expanded.intersects(cluster):
                    candidate |= cluster
                    clusters.pop(index)
                    changed = True
                    break
        clusters.append(candidate & page_rect)
    return clusters


def _visual_regions(page: fitz.Page) -> tuple[list[fitz.Rect], list[fitz.Rect]]:
    page_rect = page.rect
    images: list[fitz.Rect] = []
    try:
        for info in page.get_image_info(xrefs=True):
            rect = fitz.Rect(info["bbox"]) & page_rect
            if rect.width > 30 and rect.height > 24 and 900 < rect.get_area() < page_rect.get_area() * .88:
                if not any(_intersection_fraction(rect, old) > .94 for old in images):
                    images.append(rect)
    except (RuntimeError, ValueError, TypeError):
        pass
    drawing_rectangles: list[fitz.Rect] = []
    try:
        for drawing in page.get_drawings():
            rect = fitz.Rect(drawing["rect"]) & page_rect
            # Background fills and page borders are not meaningful figures.
            if rect.is_empty:
                # Horizontal and vertical table rules have zero area.
                original = fitz.Rect(drawing["rect"])
                rect = fitz.Rect(original.x0 - .7, original.y0 - .7, original.x1 + .7, original.y1 + .7) & page_rect
            if 2 < max(rect.width, rect.height) and rect.get_area() < page_rect.get_area() * .75:
                drawing_rectangles.append(rect)
    except (RuntimeError, ValueError, TypeError):
        pass
    images = _cluster_rectangles(images, 12, page_rect)
    # Dense paths may number in the tens of thousands. Capping drawing inputs
    # keeps an unusual plotting page from monopolising the background worker.
    clusters = _cluster_rectangles(drawing_rectangles[:2500], 5, page_rect)
    drawings = [rect for rect in clusters if rect.width > 35 and rect.height > 20 and rect.get_area() > 1400]
    # Some publishers outline figure labels instead of embedding text. Small
    # vector clusters immediately beside a real graphic are part of that
    # graphic too, even though they are too short to be standalone figures.
    anchors = images + drawings
    drawings.extend(rect for rect in clusters if rect not in drawings
                    and rect.width > 10 and rect.height > 2 and rect.get_area() > 35
                    and any(fitz.Rect(anchor.x0 - 18, anchor.y0 - 18,
                                      anchor.x1 + 18, anchor.y1 + 18).intersects(rect)
                            for anchor in anchors))
    return images, drawings


def _horizontal_table_rules(page: fitz.Page) -> list[fitz.Rect]:
    """Keep open horizontal rules which area-based graphic clustering loses."""
    rules: list[fitz.Rect] = []
    try:
        for drawing in page.get_drawings():
            for item in drawing.get("items", []):
                segments = []
                if item[0] == "l":
                    segments = [(item[1], item[2])]
                elif item[0] == "re":
                    rectangle = fitz.Rect(item[1])
                    segments = [(rectangle.tl, rectangle.tr), (rectangle.bl, rectangle.br)]
                for left, right in segments:
                    if abs(left.y - right.y) <= 1.5 and abs(left.x - right.x) >= 45:
                        rect = fitz.Rect(min(left.x, right.x), min(left.y, right.y) - .7,
                                         max(left.x, right.x), max(left.y, right.y) + .7) & page.rect
                        if not any(abs(rect.y0 - old.y0) < 1 and abs(rect.x0 - old.x0) < 2
                                   and abs(rect.x1 - old.x1) < 2 for old in rules):
                            rules.append(rect)
    except (RuntimeError, ValueError, TypeError):
        return []
    return sorted(rules, key=lambda rect: (rect.y0, rect.x0))


def _ruled_table_region(caption: dict[str, Any], rules: list[fitz.Rect],
                        blocks: list[dict[str, Any]], page_rect: fitz.Rect) -> fitz.Rect | None:
    """Find a complete open-rule table, bounded by its own caption neighbours."""
    caption_rect = fitz.Rect(caption["bbox"])
    other_captions = [fitz.Rect(block["bbox"]) for block in blocks
                      if block["id"] != caption["id"] and block["type"] == "caption"]
    lower = min((rect.y0 for rect in other_captions if rect.y0 > caption_rect.y1
                 and _x_overlap(rect, caption_rect) > .25), default=page_rect.y1)
    upper = max((rect.y1 for rect in other_captions if rect.y1 < caption_rect.y0
                 and _x_overlap(rect, caption_rect) > .25), default=page_rect.y0)
    # Table captions usually precede their table, but captions below it occur
    # as well. Try the nearby following rules first, then the preceding ones.
    for below in (True, False):
        candidates = [rule for rule in rules if _x_overlap(rule, caption_rect) > .28
                      and (caption_rect.y1 < rule.y0 < lower if below else upper < rule.y1 < caption_rect.y0)]
        if not candidates:
            continue
        anchor = min(candidates, key=lambda rule: rule.y0 - caption_rect.y1 if below else caption_rect.y0 - rule.y1)
        gap = anchor.y0 - caption_rect.y1 if below else caption_rect.y0 - anchor.y1
        if gap > max(65, page_rect.height * .09):
            continue
        aligned = [rule for rule in candidates if abs(rule.x0 - anchor.x0) <= 12
                   and abs(rule.x1 - anchor.x1) <= max(12, anchor.width * .035)]
        if len(aligned) < 2:
            continue
        top, bottom = min(rule.y0 for rule in aligned), max(rule.y1 for rule in aligned)
        if bottom - top < 18 or bottom - top > page_rect.height * .78:
            continue
        region = fitz.Rect(min(rule.x0 for rule in aligned), top,
                           max(rule.x1 for rule in aligned), bottom)
        enclosed = [block for block in blocks if block["id"] != caption["id"]
                    and block["type"] != "caption"
                    and region.y0 - 2 <= block["bbox"][1] and block["bbox"][3] <= region.y1 + 2
                    and _x_overlap(region, fitz.Rect(block["bbox"])) > .5]
        # At least a header and body must lie between the rules. A decorative
        # underline alone is not evidence of a table.
        if len(enclosed) < 2 or sum(len(block["text"]) for block in enclosed) < 25:
            continue
        # A column header can sit between the caption and the first rule.
        header = [block for block in blocks if below and block["id"] != caption["id"]
                  and block["type"] != "caption" and block["bbox"][1] >= caption_rect.y1 - 1
                  and block["bbox"][3] <= region.y0 + 3 and _x_overlap(region, fitz.Rect(block["bbox"])) > .5]
        for block in enclosed + header:
            rect = fitz.Rect(block["bbox"])
            if rect.x0 >= region.x0 - 14 and rect.x1 <= region.x1 + 14:
                region |= rect
        return fitz.Rect(region.x0 - 4, region.y0 - 3,
                         region.x1 + 4, region.y1 + 3) & page_rect
    return None


def _expand_graphic_labels(region: fitz.Rect, caption: dict[str, Any],
                           data: dict[str, Any], page_rect: fitz.Rect) -> fitz.Rect:
    """Include short external axis labels and panel titles in the source crop."""
    result = fitz.Rect(region)
    caption_rect = fitz.Rect(caption["bbox"])
    search = fitz.Rect(region.x0 - 25, region.y0 - 22, region.x1 + 25, region.y1 + 22)
    for group in data.get("groups", []):
        for line in group:
            rect = fitz.Rect(line["bbox"])
            if not search.intersects(rect) or rect.intersects(caption_rect):
                continue
            if len(line["text"]) > 140 or len(re.findall(r"\b[A-Za-z]+\b", line["text"])) > 19:
                continue
            # Caption and article prose live outside the graphic's side of
            # the page. Never grow a below-caption graphic upward through it.
            if region.y1 <= caption_rect.y0 and rect.y1 > caption_rect.y0 - 2:
                continue
            if region.y0 >= caption_rect.y1 and rect.y0 < caption_rect.y1 + 2:
                continue
            if (_x_overlap(region, rect) > .25 or
                    min(region.y1, rect.y1) - max(region.y0, rect.y0) > min(region.height, rect.height) * .45):
                result |= rect
    expanded = fitz.Rect(result.x0 - 2, result.y0 - 2, result.x1 + 2, result.y1 + 2) & page_rect
    if region.y1 <= caption_rect.y0:
        expanded.y1 = min(expanded.y1, caption_rect.y0 - .5)
    elif region.y0 >= caption_rect.y1:
        expanded.y0 = max(expanded.y0, caption_rect.y1 + .5)
    return expanded


def _caption_region(caption: dict[str, Any], images: list[fitz.Rect], drawings: list[fitz.Rect], blocks: list[dict[str, Any]], page_rect: fitz.Rect, table: bool, table_rules: list[fitz.Rect] | None = None) -> tuple[fitz.Rect | None, list[fitz.Rect]]:
    """Return the crop and its genuinely detected visual components.

    Empty-space fallback crops have no confirmed components: they must never
    be used to suppress extracted text from the reading layout.
    """
    caption_rect = fitz.Rect(caption["bbox"])
    if table and table_rules:
        table_region = _ruled_table_region(caption, table_rules, blocks, page_rect)
        if table_region is not None:
            return table_region, [fitz.Rect(table_region)]
    candidates = []
    for rect in images + drawings:
        overlap = _x_overlap(caption_rect, rect)
        vertical_overlap = max(0, min(caption_rect.y1, rect.y1) - max(caption_rect.y0, rect.y0)) / max(1, min(caption_rect.height, rect.height))
        horizontal_gap = max(0, max(caption_rect.x0, rect.x0) - min(caption_rect.x1, rect.x1))
        side_caption = (not table and overlap < .28 and vertical_overlap > .45
                        and horizontal_gap < max(55, page_rect.width * .12)
                        and rect.width > page_rect.width * .16)
        if (overlap < .28 and not side_caption) or rect.height > page_rect.height * .88:
            continue
        if side_caption:
            # Journal layouts often place a narrow caption beside a large
            # multi-panel figure. Requiring x-overlap silently lost the vector
            # panels and left just one small embedded raster image.
            candidates.append((horizontal_gap + abs(caption_rect.y0 - rect.y0) * .12, rect))
            continue
        above_gap = caption_rect.y0 - rect.y1
        below_gap = rect.y0 - caption_rect.y1
        between = fitz.Rect(min(rect.x0, caption_rect.x0), min(rect.y1, caption_rect.y1),
                            max(rect.x1, caption_rect.x1), max(rect.y0, caption_rect.y0))
        if any(block["id"] != caption["id"] and
               (block["type"] == "caption" or block["type"] == "paragraph" and len(block["text"]) > 200) and
               between.y0 + 2 < block["bbox"][1] and block["bbox"][3] < between.y1 - 2 and
               _x_overlap(caption_rect, fitz.Rect(block["bbox"])) > .35 for block in blocks):
            continue
        if above_gap >= -12 and above_gap < page_rect.height * .38:
            score = max(0, above_gap) + abs((rect.x0 + rect.x1) / 2 - (caption_rect.x0 + caption_rect.x1) / 2) * .13
            if table:
                score += 35
            candidates.append((score, rect))
        elif below_gap >= -12 and below_gap < page_rect.height * .3:
            score = max(0, below_gap) + (0 if table else 80)
            candidates.append((score, rect))
        elif rect.intersects(caption_rect) and rect.get_area() > 2500:
            candidates.append((25, rect))
    if candidates:
        candidates.sort(key=lambda value: value[0])
        best = fitz.Rect(candidates[0][1])
        confirmed = [fitz.Rect(best)]
        # Join adjacent rows as well as columns of panels. A score based only
        # on distance to the caption previously discarded upper panel rows.
        remaining = []
        for rect in images + drawings:
            if _intersection_fraction(rect, best) > .98:
                continue
            own_gap = min(abs(caption_rect.y0 - rect.y1), abs(rect.y0 - caption_rect.y1))
            assigned_elsewhere = False
            for block in blocks:
                if block["id"] == caption["id"] or block["type"] != "caption":
                    continue
                other = fitz.Rect(block["bbox"])
                if _x_overlap(other, rect) > .4 and min(abs(other.y0 - rect.y1), abs(rect.y0 - other.y1)) + 8 < own_gap:
                    assigned_elsewhere = True
                    break
            if not assigned_elsewhere:
                remaining.append(rect)
        changed = True
        while changed:
            changed = False
            for rect in remaining[:]:
                same_side = (best.y1 <= caption_rect.y0 + 12 and rect.y1 <= caption_rect.y0 + 12 or
                             best.y0 >= caption_rect.y1 - 12 and rect.y0 >= caption_rect.y1 - 12 or
                             best.x0 >= caption_rect.x1 - 8 and rect.x0 >= caption_rect.x1 - 8 or
                             best.x1 <= caption_rect.x0 + 8 and rect.x1 <= caption_rect.x0 + 8)
                horizontal_gap = max(0, max(best.x0, rect.x0) - min(best.x1, rect.x1))
                vertical_gap = max(0, max(best.y0, rect.y0) - min(best.y1, rect.y1))
                if same_side and horizontal_gap <= max(42, page_rect.width * .1) and vertical_gap <= max(48, page_rect.height * .06):
                    best |= rect
                    confirmed.append(fitz.Rect(rect))
                    remaining.remove(rect)
                    changed = True
        return fitz.Rect(best.x0 - 4, best.y0 - 4, best.x1 + 4, best.y1 + 4) & page_rect, confirmed
    # A caption-like sentence without detected graphical content is preserved
    # as text. Guessing a whitespace crop here used to turn ordinary prose into
    # a truncated "figure" and was especially harmful for open-rule tables.
    return None, []


def _exclude_figure_labels(data: dict[str, Any], confirmed: list[tuple[fitz.Rect, bool]], protected_ids: set[str]) -> None:
    """Remove short duplicate labels only inside detected graphic components.

    The full figure remains in its PNG crop. Headings, captions, TOC targets,
    longer prose and large page regions are deliberately retained.
    """
    remove_ids: set[str] = set()
    page_area = data["width"] * data["height"]
    for region, table in confirmed:
        area = region.get_area()
        if area < 1400 or area > page_area * .45:
            continue
        eligible = []
        for block in data["blocks"]:
            if block["id"] in protected_ids or block["type"] not in ({"paragraph"} if table else {"paragraph", "equation"}):
                continue
            text = block["text"]
            if len(text) > (90 if block["type"] == "equation" else 160):
                continue
            # Sentence-like prose can belong to a neighbouring column or be
            # enclosed by a decorative rectangle instead of a real diagram.
            if len(re.findall(r"\b[A-Za-z]+\b", text)) > 14 and re.search(r"[.!?。！？]", text):
                continue
            rect = fitz.Rect(block["bbox"])
            if rect.height > max(40, block["font_size"] * 3.8) or rect.get_area() > area * .22:
                continue
            if region.x0 <= rect.x0 + .5 and region.y0 <= rect.y0 + .5 and region.x1 >= rect.x1 - .5 and region.y1 >= rect.y1 - .5:
                eligible.append(block)
        # A region containing substantial article prose is too ambiguous to
        # clean safely, even if its graphic bounding box was detected.
        if sum(len(block["text"]) for block in eligible) > 1200 or sum(fitz.Rect(block["bbox"]).get_area() for block in eligible) > area * .42:
            continue
        remove_ids.update(block["id"] for block in eligible)
    if remove_ids:
        data["blocks"] = [block for block in data["blocks"] if block["id"] not in remove_ids]


def _make_figures(document: fitz.Document, pages: list[dict[str, Any]], protected_ids: set[str] | None = None) -> list[dict[str, Any]]:
    figures = []
    for data in pages:
        page = document[data["number"] - 1]
        images, drawings = _visual_regions(page)
        table_rules = sorted(_horizontal_table_rules(page) + [fitz.Rect(rule) for rule in data.get("dotted_rules", [])],
                             key=lambda rect: (rect.y0, rect.x0))
        captions = [block for block in data["blocks"] if block["type"] == "caption"]
        used: list[fitz.Rect] = []
        confirmed: list[tuple[fitz.Rect, bool]] = []
        for caption in captions:
            match = _CAPTION.match(caption["text"])
            is_table = bool(match and match.group("kind").lower() in {"table", "表"})
            region, components = _caption_region(caption, images, drawings, data["blocks"], page.rect, is_table, table_rules)
            if region is None:
                continue
            if not is_table:
                region = _expand_graphic_labels(region, caption, data, page.rect)
            confirmed.extend((component, is_table) for component in components)
            used.append(region)
            figures.append({"id": f"p{data['number']}-{'table' if is_table else 'figure'}-{len(used)}", "page": data["number"], "caption": caption["text"], "caption_zh": "", "caption_block_id": caption["id"], "bbox": _bbox(region), "visual_regions": [_bbox(component) for component in components], "kind": "table" if is_table else "figure"})
        for rectangle in images:
            if rectangle.get_area() < page.rect.get_area() * .022 or any(_intersection_fraction(rectangle, region) > .55 for region in used):
                continue
            used.append(rectangle)
            confirmed.append((rectangle, False))
            figures.append({"id": f"p{data['number']}-figure-{len(used)}", "page": data["number"], "caption": "", "caption_zh": "", "caption_block_id": None, "bbox": _bbox(rectangle), "visual_regions": [_bbox(rectangle)], "kind": "figure"})
        _exclude_figure_labels(data, confirmed, protected_ids or set())
    return figures


@_serialized_pdf
def annotate_translation_scopes(document: dict[str, Any], pdf_bytes: bytes | None = None) -> bool:
    """Mark graphic text without changing original content or inferred crops.

    ``visual_regions`` contains only actual raster/vector components. Older
    cached documents can supply their source bytes to recover those components;
    a caption's inferred whitespace crop is never sufficient evidence to hide
    neighbouring prose. Returns whether the document metadata was changed.
    """
    changed = False
    figures = document.get("figures", [])
    missing = [figure for figure in figures if "visual_regions" not in figure]
    if missing and pdf_bytes:
        with _open_pdf(pdf_bytes) as source:
            page_regions: dict[int, list[fitz.Rect]] = {}
            for figure in missing:
                page_number = int(figure.get("page", 0))
                if not 1 <= page_number <= source.page_count:
                    continue
                if page_number not in page_regions:
                    images, drawings = _visual_regions(source[page_number - 1])
                    page_regions[page_number] = images + drawings
                crop = fitz.Rect(figure.get("bbox", (0, 0, 0, 0)))
                # Match genuinely detected components to the old figure crop,
                # not the whole crop itself (which may include nearby prose).
                regions = [region for region in page_regions[page_number]
                           if region.get_area() > 1400 and
                           (region & crop).get_area() >= region.get_area() * .85]
                figure["visual_regions"] = [_bbox(region) for region in regions]
                changed = True
    protected_ids = {item.get("block_id") for item in document.get("toc", []) if item.get("block_id")}
    title = re.sub(r"\s+", " ", str(document.get("title", ""))).strip()
    caption_ids = {figure.get("caption_block_id") for figure in figures if figure.get("caption_block_id")}
    figure_types = {"figure", "image", "table", "table_cell", "figure_label", "chart"}
    for page in document.get("pages", []):
        page_figures = [figure for figure in figures if int(figure.get("page", 0)) == int(page.get("number", 0))]
        for block in page.get("blocks", []):
            text = str(block.get("text", ""))
            normalized = re.sub(r"\s+", " ", text).strip()
            kind = str(block.get("type", "paragraph"))
            is_caption = kind == "caption" or block.get("id") in caption_ids or any(
                figure.get("caption") and normalized == re.sub(r"\s+", " ", str(figure["caption"])).strip()
                for figure in page_figures
            )
            scope = "caption" if is_caption else "body"
            if not is_caption:
                protected = block.get("id") in protected_ids or (title and normalized == title)
                section_heading = kind == "heading" and bool(_SECTION.match(text) or _NAMED_SECTION.match(text))
                if kind in figure_types or block.get("display_math"):
                    scope = "figure_content"
                elif not protected and not section_heading and block.get("bbox"):
                    rect = fitz.Rect(block["bbox"])
                    if not rect.is_empty:
                        for figure in page_figures:
                            if any(fitz.Rect(region[0] - 1, region[1] - 1, region[2] + 1, region[3] + 1).contains(rect)
                                   for region in figure.get("visual_regions", [])):
                                scope = "figure_content"
                                break
            if block.get("translation_scope") != scope:
                block["translation_scope"] = scope
                changed = True
            if scope == "figure_content" and "text_zh" in block:
                block.pop("text_zh")
                changed = True
    if all("visual_regions" in figure for figure in figures) and document.get("translation_scope_version") != 1:
        document["translation_scope_version"] = 1
        changed = True
    return changed


@_serialized_pdf
def extract_document(pdf_bytes: bytes, progress: Callable[[int], Any] | None = None) -> dict[str, Any]:
    """Extract a JSON-serializable reading layout without IO or OCR.

    ``progress`` receives integer percentages from 0 through 100. Callback
    exceptions are propagated so a caller can cancel its own background task.
    """
    with _open_pdf(pdf_bytes) as document:
        if progress:
            progress(0)
        raw_pages = []
        for index in range(document.page_count):
            try:
                raw_pages.append(_read_page(document[index], index + 1))
            except (RuntimeError, ValueError) as exc:
                raise ValueError(f"无法解析 PDF 第 {index + 1} 页，文件可能已损坏。") from exc
            if progress:
                progress(round((index + 1) / document.page_count * 64))
        _remove_margins(raw_pages)
        for page in raw_pages:
            page["blocks"] = _make_blocks(page)
        # Graphics require original PDF geometry. Their text is replaced with
        # the converter's complete captions below; no geometric body blocks
        # are retained when MarkItDown succeeds.
        figures = _make_figures(document, raw_pages)
        warnings = []
        source_markdown = ""
        markdown_pages = []
        try:
            def page_loader(index: int) -> bytes:
                with fitz.open() as single_page:
                    single_page.insert_pdf(document, from_page=index, to_page=index)
                    return single_page.tobytes()
            source_markdown, markdown_pages = convert_pdf_pages(
                pdf_bytes, document.page_count, page_loader, dryrun=DEFAULT_DRYRUN,
            )
        except Exception:
            warnings.append("MarkItDown 正文转换未完成，已使用 PDF 原始文字兼容解析；原始 PDF 仍可阅读。")
        for page in raw_pages:
            markdown = markdown_pages[page["number"] - 1] if markdown_pages else ""
            usable = bool(markdown.strip()) and not page["fragmented"]
            page["blocks"] = _markdown_blocks(markdown, page, figures) if usable else _make_blocks(page)
        for figure in figures:
            caption = figure.get("caption", "")
            label = _CAPTION.match(caption)
            if not label:
                continue
            label_key = _alignment_key(label.group(0))
            replacement = next((block for block in raw_pages[figure["page"] - 1]["blocks"]
                                if block["type"] == "caption" and _alignment_key(block["text"]).startswith(label_key)), None)
            if replacement:
                page = raw_pages[figure["page"] - 1]
                caption_rect = fitz.Rect(replacement["bbox"])
                continuations = []
                for block in page["blocks"]:
                    if block["id"] == replacement["id"] or block["type"] != "paragraph" or block.get("translation_scope") == "figure_content":
                        continue
                    rect = fitz.Rect(block["bbox"])
                    overlap = max(0, min(caption_rect.y1, rect.y1) - max(caption_rect.y0, rect.y0))
                    if (block.get("geometry_matched") and
                            abs(block["font_size"] - replacement["font_size"]) < .6 and
                            0 <= rect.x0 - caption_rect.x1 < page["width"] * .12 and
                            overlap >= min(caption_rect.height, rect.height) * .65 and
                            not rect.intersects(fitz.Rect(figure["bbox"]))):
                        continuations.append(block)
                for continuation in sorted(continuations, key=lambda block: (block["bbox"][0], block["bbox"][1])):
                    # MarkItDown separates the two columns beneath some wide
                    # figures. They remain one caption, never article prose.
                    replacement["text"] = _clean_text(replacement["text"] + " " + continuation["text"])
                    caption_rect |= fitz.Rect(continuation["bbox"])
                if continuations:
                    replacement["bbox"] = _bbox(caption_rect)
                    continuation_ids = {block["id"] for block in continuations}
                    page["blocks"] = [block for block in page["blocks"] if block["id"] not in continuation_ids]
                figure["caption"] = replacement["text"]
                figure["caption_block_id"] = replacement["id"]
        title, authors, abstract, title_ids = _metadata(raw_pages, document.metadata or {})
        if progress:
            progress(72)
        toc = _make_toc(document, raw_pages, title_ids)
        sparse_pages = [page["number"] for page in raw_pages if sum(len(line["text"]) for group in page["groups"] for line in group) < 50]
        if sparse_pages:
            if len(sparse_pages) == len(raw_pages):
                warnings.append("此 PDF 可能是扫描件或文字已转为轮廓，未检测到可提取的正文。本版本不包含 OCR；原始 PDF 仍可阅读。")
            else:
                numbers = "、".join(str(number) for number in sparse_pages[:15])
                warnings.append(f"第 {numbers}{' 等' if len(sparse_pages) > 15 else ''} 页可提取文字较少，可能含扫描内容。本版本不包含 OCR，这些页面的 AI 重排可能不完整。")
        if not abstract:
            warnings.append("未识别到独立摘要，将使用正文内容进行 AI 总结。")
        if not toc:
            warnings.append("未检测到 PDF 书签或明确的章节标题。")
        pages = [{key: page[key] for key in ("number", "width", "height", "blocks")} for page in raw_pages]
        result = {"title": title, "authors": authors, "abstract": abstract, "page_count": document.page_count, "pages": pages, "toc": toc, "figures": figures, "warnings": warnings,
                  "source_markdown": source_markdown, "converter": "markitdown" if source_markdown else "pymupdf", "geometry_engine": "pymupdf", "extraction_version": EXTRACTION_VERSION}
        annotate_translation_scopes(result)
        _merge_prose_continuations(result)
        annotate_paragraph_groups(result)
        if progress:
            progress(100)
        return result


def _page_index(document: fitz.Document, page: int) -> int:
    if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= document.page_count:
        raise ValueError(f"页码必须是 1 到 {document.page_count} 之间的整数。")
    return page - 1


def _render(page: fitz.Page, scale: float, clip: fitz.Rect | None = None) -> bytes:
    if not isinstance(scale, (int, float)) or isinstance(scale, bool) or not .1 <= scale <= 6:
        raise ValueError("渲染缩放比例必须在 0.1 到 6 之间。")
    rect = clip or page.rect
    effective_scale = min(float(scale), (DEFAULT_MAX_RENDER_PIXELS / max(1, rect.get_area())) ** .5)
    try:
        pixmap = page.get_pixmap(matrix=fitz.Matrix(effective_scale, effective_scale), clip=clip, alpha=False, colorspace=fitz.csRGB)
        return pixmap.tobytes("png")
    except (RuntimeError, ValueError) as exc:
        raise ValueError("PDF 页面渲染失败，请确认文件完整。") from exc


@_serialized_pdf
def render_page(pdf_bytes: bytes, page: int, scale: float = DEFAULT_RENDER_SCALE) -> bytes:
    """Return a PNG for the specified one-based PDF page."""
    with _open_pdf(pdf_bytes) as document:
        return _render(document[_page_index(document, page)], scale)


@_serialized_pdf
def render_figure(pdf_bytes: bytes, figure: dict[str, Any], scale: float = DEFAULT_FIGURE_SCALE) -> bytes:
    """Return a PNG for an extraction result's figure, table or formula crop."""
    if not isinstance(figure, dict):
        raise ValueError("图表参数无效。")
    with _open_pdf(pdf_bytes) as document:
        page = document[_page_index(document, figure.get("page"))]
        try:
            coordinates = figure["bbox"]
            if not isinstance(coordinates, (tuple, list)) or len(coordinates) != 4:
                raise ValueError("bbox")
            numeric_coordinates = [float(value) for value in coordinates]
            if not all(isfinite(value) for value in numeric_coordinates):
                raise ValueError("bbox")
            rect = fitz.Rect(numeric_coordinates) & page.rect
            if rect.is_empty or rect.is_infinite or rect.width < 1 or rect.height < 1:
                raise ValueError("bbox")
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("图表裁剪区域无效。") from exc
        return _render(page, scale, rect)


@_serialized_pdf
def get_thumbnail(pdf_bytes: bytes) -> bytes:
    """Return a small PNG preview of page one."""
    with _open_pdf(pdf_bytes) as document:
        page = document[0]
        return _render(page, max(.1, min(6, DEFAULT_THUMBNAIL_WIDTH / max(1, page.rect.width))))
