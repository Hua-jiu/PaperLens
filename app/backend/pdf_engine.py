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
# Text on either side of an uncertain boundary shown to the layout reviewer.
DEFAULT_REVIEW_CONTEXT_CHARS = 220
DEFAULT_REVIEW_KEY_CHARS = 60
# An unconfident reference this much longer than its list's median may
# contain several entries.
DEFAULT_REVIEW_SPLIT_RATIO = 1.6
DEFAULT_REVIEW_MAX_CANDIDATES = 160
# Roles the reviewer may assign to a block of uncertain kind.
REVIEW_ROLES = ("body", "heading", "caption", "figure_text", "furniture")
# Regions a vision model may draw on a page image, in 0-1000 coordinates.
VISION_REGION_TYPES = ("title", "heading", "paragraph", "list", "caption", "figure", "table",
                       "equation", "reference", "footnote", "header", "footer")
DEFAULT_VISION_SCALE = 1.6
# Share of a page's PDF text its regions must contain to be trusted.
DEFAULT_VISION_MIN_COVERAGE = .85
EXTRACTION_VERSION = 14

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
# Some embedded fonts carry their weight only in the name: "AdvOT1234.B",
# "Minion-Bd" or TeX's "CMBX10".
_BOLD_FONT = re.compile(r"bold|black|heavy|semibold|demibold|^(?:[A-Z]{6}\+)?cmbx|[.\-_,](?:b|bd|bi|sb)$", re.I)
_LIST_MARKER = re.compile(r"^(?:\[\d{1,3}\]|\d{1,3}[.)]|[•▪◦●■∙·*]|[-–—])\s+")
# Compared without spaces, as letter-spaced small capitals ("RE FERENCES")
# arrive with spaces inside the word.
_REFERENCES_HEADING = re.compile(
    r"(?:\d+(?:\.\d+)*\.?)?(?:references(?:andnotes)?|bibliography|literaturecited|workscited|参考文献)[:：.]?", re.I)
# Back matter that follows a reference list, often as a run-in heading.
_BACK_MATTER = re.compile(
    r"^(?:acknowledg(?:e)?ments?|funding|author contributions|competing interests|conflicts? of interest|"
    r"data availability|code availability|supplementary (?:materials?|information)|additional information|"
    r"publisher[’']?s note|declarations?|ethics (?:approval|statement|declarations?))\b", re.I)
# Evidence that a block is part of a citation rather than appendix prose.
_CITATION_EVIDENCE = re.compile(
    r"\b(?:1[89]|20)\d{2}[a-z]?\b|arxiv|doi|https?://|\bet al\b|\bproc\b|journal|conference|\bvol\b|\bpp\b|press|preprint", re.I)
# "[12]", "12." or a bare "12" opening a reference entry.
_REFERENCE_NUMBER = re.compile(r"^\s*(?:\[(?P<bracket>\d{1,4})\]|(?P<dot>\d{1,4})\.(?=\s|[A-Z])|(?P<bare>\d{1,4})\s+(?=[A-Z]))")
# An author-year entry opens with a surname followed by initials or a name.
_AUTHOR_START = re.compile(r"^[A-Z][\w'’\-]+(?:\s+(?:de|van|von|der|da|del|di|le|la)\b)*,?\s+(?:[A-Z]\.|[A-Z]{1,3}\b|[A-Z][a-z]+)")
_ITEM_START = re.compile(r"^(?:\[\d+\]\s*|\((?:\d+|[A-Za-z]|[ivxlcdmIVXLCDM]+)\)\s+|(?:\d+|[A-Za-z]|[ivxlcdmIVXLCDM]+)[.)](?:\s+|$)|[•▪◦*]\s*|[-–—]\s+)")
# Trailing citation markers, footnote symbols and closing quotes may follow
# the punctuation that actually ends a sentence or a heading.
_SENTENCE_END = re.compile(r"[.!?:;。！？：；][\s\d,\-–\[\]()\"'’”»*†‡]*$")
# Words that cannot end an English sentence, and abbreviations whose full
# stop does not end one either.
_OPEN_END = re.compile(
    r"(?:\b(?:a|an|the|of|to|in|on|for|with|and|or|nor|but|by|from|as|at|into|onto|via|than|between|among|across|"
    r"within|without|through|under|over|about|after|before|during|per|versus|is|are|was|were|be|been|being|has|have|"
    r"that|which|whose|where|its|their|our|this|these|those|both|each|such|not|using|e\.g\.|i\.e\.|al\.|cf\.|vs\.|"
    r"figs?\.|eqs?\.|refs?\.|no\.)|[,&(\[{/–-])$", re.I)


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
    # Zero-width spaces separate list numbers from their text in some PDFs.
    text = re.sub("[\u200b-\u200d\u2060\ufeff]", "", text)
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
    bold_chars = 0
    for span in spans:
        text = span.get("text", "")
        if previous and fragments and text and not fragments[-1].endswith(" ") and not text.startswith(" "):
            gap = float(span["bbox"][0]) - float(previous["bbox"][2])
            if gap > max(1.3, min(float(span.get("size", 10)), float(previous.get("size", 10))) * 0.18):
                fragments.append(" ")
        fragments.append(text)
        weighted_sizes.append((float(span.get("size", 10)), max(1, len(text.strip()))))
        if span.get("flags", 0) & 16 or _BOLD_FONT.search(span.get("font", "")):
            bold_chars += len(text.strip())
        previous = span
    text = _clean_text("".join(fragments))
    if not text:
        return None
    size = sum(value * weight for value, weight in weighted_sizes) / sum(weight for _, weight in weighted_sizes)
    return {
        "text": text, "bbox": tuple(line["bbox"]), "size": size,
        # A bold run-in label ("Datasets.", "TF-Sapiens:") does not make the
        # rest of its line a heading. Most of the characters must be bold.
        "bold": bold_chars >= len(text.replace(" ", "")) * .6,
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
    # Bold author lines carry affiliation numbers, commas or footnote marks.
    byline = bool(re.search(r"[A-Za-z]\d+(?:,\d+)*(?:\s|,|$)|[∗*†‡§]|^&", text)) or text.count(",") >= 2
    if (short and all(line["bold"] for line in lines) and size >= body_size * .95 and len(text) < 120
            and not terminal_sentence and not byline):
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
            marker = len(segment) == 1 and bool(_LIST_MARKER.fullmatch(segment[0]["text"] + " "))
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
    column_lines = [line["bbox"] for line, key in original_lines if len(key) >= 40]
    page_figures = [figure for figure in figures or [] if figure["page"] == page["number"]]
    figure_lines = [(line, key) for line, key in original_lines if len(key) >= 3 and any(
        fitz.Rect(figure["bbox"]).contains(fitz.Rect(line["bbox"])) for figure in page_figures)]
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
            figure_text = False
            if not aligned and figure_lines:
                # Converters emit a figure's labels as one run without spaces,
                # which matches no single line. Its pieces are figure lines.
                covered = [line for line, key in figure_lines if key in comparable]
                if sum(len(_alignment_key(line["text"])) for line in covered) >= len(comparable) * .5:
                    aligned, figure_text = covered, True
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
            visual = not caption_start and (table_markup or figure_text or any(region.contains(rect) for region in regions))
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
            if (kind == "paragraph" and scope == "body" and previous is not None and previous["type"] == "heading"
                    and previous["aligned"] and aligned and _heading_tail(previous, aligned[0], text, column_lines, page["body_size"])):
                # The converter can begin a paragraph with the wrapped second
                # line of the heading above it.
                kind = "heading"
            # A complete section name, or a heading of another size, ends a
            # heading even when the converter puts the next one on its line.
            heading_level_change = (kind == "heading" and previous is not None and previous["type"] == "heading"
                                    and (bool(_NAMED_SECTION.match(_join_lines(previous["lines"])))
                                         or (aligned and previous["aligned"]
                                             and abs(aligned[0]["size"] - previous["aligned"][-1]["size"]) > max(.6, aligned[0]["size"] * .08))))
            separate = (previous is None or scope != previous["scope"] or
                        (kind == "heading") != (previous["type"] == "heading") or heading_level_change or
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
            # Large type alone does not make a heading: a lead paragraph or
            # an unlabelled abstract runs over several lines of sentences.
            if part["type"] == "heading" and len(part["lines"]) >= 4 and len(text) > 220 and re.search(r"[a-z][.!?]\s+[A-Z]", text):
                part["type"] = "paragraph"
            blocks.append({"id": f"p{page['number']}-b{len(blocks) + 1}",
                           "page": page["number"], "type": part["type"], "text": text,
                           "bbox": _bbox(part["rect"]),
                           "first_line_bbox": _bbox(aligned[0]["bbox"]) if aligned else None,
                           "last_line_bbox": _bbox(aligned[-1]["bbox"]) if aligned else None,
                           "font_size": round(median(line["size"] for line in aligned), 2) if aligned else page["body_size"],
                           "text_source": "markitdown", "geometry_matched": bool(aligned),
                           "translation_scope": part["scope"]})
    return blocks


def _sentence_continues(left: str, right: str) -> bool:
    """Whether two pieces of text clearly read as one sentence.

    The left piece must stay open: it ends with a word or mark that cannot
    close a sentence, or it ends without closing punctuation and the right
    piece resumes in lower case. List items, captions and section numbers
    begin their own unit unless the left piece is visibly unfinished.
    """
    left, right = left.rstrip(), right.lstrip()
    if not left or not right or _CAPTION.match(right) or _NAMED_SECTION.match(right) or _NAMED_SECTION.match(left):
        return False
    open_end = bool(_OPEN_END.search(left))
    if not open_end and _SENTENCE_END.search(left):
        return False
    if _ITEM_START.match(right) or (_SECTION.match(right) and not open_end):
        return False
    return open_end or bool(re.match(r"[a-z\u3400-\u9fff]", right.lstrip("(（[‘'\"“")))


def _line_wraps(last: list[float] | tuple, right_text: str, size: float, columns: list[list[float]]) -> bool:
    """Whether the next word could not fit on the last line of its column.

    A heading or a prose line ends early by choice, but a wrapped line stops
    because the first following word would overflow the column edge.
    """
    # The narrowest text regions around the line are its column, not a
    # spanning abstract or a neighbouring column.
    containers = [rect for rect in columns if rect[0] - size <= last[0] and last[2] <= rect[2] + size]
    if not containers:
        return False
    narrowest = min(rect[2] - rect[0] for rect in containers)
    column = [rect for rect in containers if rect[2] - rect[0] <= narrowest * 1.1 + size]
    left, right = median(rect[0] for rect in column), median(rect[2] for rect in column)
    word = re.match(r"[\u3400-\u9fff]|\S*", right_text.lstrip()).group(0)
    # Latin glyphs average about half an em; a CJK character is one em.
    width = size if re.match(r"[\u3400-\u9fff]", word) else len(word) * size * .45
    # A long URL or identifier fits nowhere, so the line must also be full.
    return last[2] - left >= (right - left) * .6 and last[2] + size * .25 + width >= right - size * .5


def _heading_tail(part: dict[str, Any], line: dict[str, Any], text: str,
                  columns: list[tuple], body_size: float) -> bool:
    """Whether a converter line continues the heading part above it.

    Only a heading set apart from prose by weight or size qualifies; a body
    line taken for a numbered heading is repaired by _rejoin_split_sentences.
    """
    tail = part["aligned"][-1]
    if not tail["bold"] and tail["size"] < body_size * 1.1:
        return False
    size = max(tail["size"], line["size"])
    gap = line["bbox"][1] - tail["bbox"][3]
    heading_text = _join_lines(part["lines"])
    return (abs(tail["size"] - line["size"]) <= max(.3, size * .03) and tail["bold"] == line["bold"]
            and -size * .3 <= gap <= size * .9 and _line_wraps(tail["bbox"], text, size, columns)
            and _sentence_continues(heading_text, text) and _looks_like_heading(heading_text + " " + text))


def _looks_like_heading(text: str) -> bool:
    body = text[_SECTION.match(text).end():] if _SECTION.match(text) else text
    return len(text) < 160 and not re.search(r"\S[.!?]\s+[A-Z]", body)


def _rejoin_split_sentences(page: dict[str, Any]) -> None:
    """Rejoin one sentence that layout analysis split around a heading.

    A two-line heading can arrive as two headings, and a prose line opening
    with a number or a bold label as a heading followed by its own remainder.
    Adjacent lines of one column with a matching size are rejoined when the
    first line wraps and the text reads as one sentence. Paragraph pairs are
    left to _merge_prose_continuations, which also spans columns and pages.
    """
    # Text blocks of more than a short line show where each column ends.
    columns = [block for block in page["blocks"] if block["type"] in {"paragraph", "heading"}
               and len(block["text"]) >= 60 and block.get("geometry_matched") is not False]
    merged: list[dict[str, Any]] = []
    for block in page["blocks"]:
        previous = merged[-1] if merged else None
        if previous is None or not _sentence_split(previous, block, columns):
            merged.append(block)
            continue
        text = _join_lines([{"text": previous["text"]}, {"text": block["text"]}])
        heading = previous["type"] == block["type"] == "heading" and _looks_like_heading(text)
        if not heading and previous["type"] == "heading":
            previous["font_size"] = block["font_size"]
        previous.update({"type": "heading" if heading else "paragraph", "text": text,
                         "bbox": _bbox(fitz.Rect(previous["bbox"]) | fitz.Rect(block["bbox"])),
                         "last_line_bbox": block["last_line_bbox"]})
        previous.pop("text_zh", None)
    page["blocks"] = merged


def _sentence_split(previous: dict[str, Any], current: dict[str, Any], columns: list[dict[str, Any]]) -> bool:
    kinds = (previous["type"], current["type"])
    if "heading" not in kinds or not set(kinds) <= {"heading", "paragraph"}:
        return False
    if any(block.get("translation_scope", "body") != "body" or block.get("geometry_matched") is False
           for block in (previous, current)):
        return False
    last, first = previous.get("last_line_bbox"), current.get("first_line_bbox")
    if not last or not first:
        return False
    size = max(float(previous["font_size"]), float(current["font_size"]))
    tolerance = max(.3, size * .03) if kinds == ("heading", "heading") else max(.6, size * .08)
    if abs(float(previous["font_size"]) - float(current["font_size"])) > tolerance:
        return False
    gap = first[1] - last[3]
    overlap = min(last[2], first[2]) - max(last[0], first[0])
    # Double-spaced manuscripts leave more than one em between lines.
    if not (-size * .3 <= gap <= size * 1.3 and overlap >= min(last[2] - last[0], first[2] - first[0]) * .5):
        return False
    # The pair itself cannot show where its own column ends.
    if not _line_wraps(last, current["text"], size,
                       [block["bbox"] for block in columns if block is not previous and block is not current]):
        return False
    if _sentence_continues(previous["text"], current["text"]):
        return True
    # Title-case headings resume in upper case. A wrapped numbered heading
    # directly above an unnumbered line of the same size is still one title.
    return (kinds == ("heading", "heading") and bool(_SECTION.match(previous["text"]))
            and not _SECTION.match(current["text"]) and not _SENTENCE_END.search(previous["text"])
            and gap <= size * .6 and _looks_like_heading(previous["text"] + " " + current["text"]))


def _ends_references(text: str) -> bool:
    """Whether a heading opens a section after a reference list, such as "1 Methods"."""
    number = _SECTION.match(text)
    return bool(_NAMED_SECTION.match(text[number.end():] if number else text)) or _appendix_heading(text, "heading")


def _references_heading(text: str) -> bool:
    return bool(_REFERENCES_HEADING.fullmatch(re.sub(r"\s+", "", text)))


def _layout_flags(page: dict[str, Any], raw: dict[str, Any], figures: list[dict[str, Any]]) -> list[str]:
    """Name the signs that a page's reading layout went wrong.

    ``unplaced``: much text has no PDF position.
    ``figure_text``: converter table markup from a graphic sits in the prose.
    ``fragmented``: many short scraps, as from split author lines or formulas.
    ``coverage``: the reading text is much shorter or longer than the PDF text
    outside graphics, so text is missing or repeated.
    """
    visible = [block for block in page["blocks"] if not block.get("hidden")
               and block.get("translation_scope") != "figure_content" and block["type"] not in {"figure", "table"}]
    characters = sum(len(block["text"]) for block in visible)
    if not visible or characters < 200:
        return []
    unplaced = sum(len(block["text"]) for block in visible if block.get("geometry_matched") is False)
    markup = [block for block in visible if block["type"] != "table" and block["text"].count("|") >= 4]
    # Split author lines arrive as short "headings" as well.
    scraps = [block for block in visible if len(block["text"].strip()) < 25 and block["type"] != "reference"
              and not _NAMED_SECTION.match(block["text"].strip()) and not _SECTION.match(block["text"].strip())]
    graphics = [fitz.Rect(figure["bbox"]) for figure in figures if figure["page"] == page["number"]]
    source = sum(len(line["text"]) for group in raw["groups"] for line in group
                 if not any(graphic.contains(fitz.Rect(line["bbox"])) for graphic in graphics))
    flags = []
    if unplaced >= max(200, characters * .15):
        flags.append("unplaced")
    if markup:
        flags.append("figure_text")
    if len(scraps) >= 6 and len(scraps) >= len(visible) * .3:
        flags.append("fragmented")
    if source >= 400 and not .7 <= characters / source <= 1.35:
        flags.append("coverage")
    return flags


def _hide_repeated_furniture(pages: list[dict[str, Any]]) -> None:
    """Hide converter text without a PDF position that recurs on many pages.

    Watermarks, licence stamps and running footers can reach the converter
    while lying outside the margins that _remove_margins inspects.
    """
    occurrences: dict[str, set[int]] = {}
    for page in pages:
        for block in page["blocks"]:
            if block.get("geometry_matched") is False:
                occurrences.setdefault(_margin_key(block["text"]), set()).add(page["number"])
    needed = max(3, ceil(len(pages) * .25))
    for page in pages:
        for block in page["blocks"]:
            if block.get("geometry_matched") is False and len(occurrences.get(_margin_key(block["text"]), ())) >= needed:
                block["hidden"], block["translation_scope"] = True, "figure_content"


def _mark_table_notes(pages: list[dict[str, Any]], figures: list[dict[str, Any]]) -> None:
    """Treat smaller type directly below a table as its note, not as prose."""
    for figure in figures:
        if figure["kind"] != "table":
            continue
        page = pages[figure["page"] - 1]
        bottom, table = figure["bbox"][3], figure["bbox"]
        for block in sorted(page["blocks"], key=lambda item: item["bbox"][1]):
            rect, size = block["bbox"], float(block["font_size"])
            overlap = min(rect[2], table[2]) - max(rect[0], table[0])
            if (block["type"] != "paragraph" or block.get("translation_scope", "body") != "body"
                    or block.get("geometry_matched") is False or size > page["body_size"] * .93
                    or overlap < min(rect[2] - rect[0], table[2] - table[0]) * .6
                    or not -size <= rect[1] - bottom <= size * 2.5):
                continue
            block["type"], block["translation_scope"] = "caption", "caption"
            bottom = rect[3]


def _reference_marker(text: str) -> tuple[str, int] | None:
    match = _REFERENCE_NUMBER.match(text)
    if not match:
        return None
    kind = next(key for key in ("bracket", "dot", "bare") if match.group(key))
    return kind, int(match.group(kind))


def _appendix_heading(text: str, kind: str) -> bool:
    """An appendix title, such as "A Proofs" or the small capitals "B DATASETS"."""
    text = text.strip()
    if len(text) > 90 or re.search(r"[.,;:]$", text):
        return False
    if re.match(r"(?:appendix|supplementary (?:materials?|information|methods))\b", text, re.I):
        return True
    # A converter can separate the number of "A.2 IMPLEMENTATION" from its title.
    if re.fullmatch(r"[A-H](?:\.\d{1,2})+\.?", text):
        return True
    match = re.match(r"[A-H](?:\.\d{1,2})*\.?\s+(?=[A-Z])", text)
    if not match or "," in text[:30]:
        return False
    letters = re.findall(r"[A-Za-z]", text[match.end():])
    return kind == "heading" or sum(letter.isupper() for letter in letters) >= len(letters) * .7


def _reference_regions(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find each reference list and the blocks it consists of.

    A list runs from its heading to the next section: a larger heading or
    body text, a named section, an appendix, a short heading without an entry
    number, or several text blocks in a row without any citation evidence.
    Captions, graphics and smaller page furniture inside it are skipped.
    """
    sequence = [(page, block) for page in pages for block in page["blocks"]]
    regions: list[dict[str, Any]] = []
    index = 0
    while index < len(sequence):
        page, heading = sequence[index]
        index += 1
        if len(heading["text"]) > 40 or not _references_heading(heading["text"]):
            continue
        following = [block for _, block in sequence[index:index + 12]
                     if block["type"] in {"paragraph", "heading"} and block.get("geometry_matched") is not False]
        if not following:
            continue
        size = median(float(block["font_size"]) for block in following[:8])
        members: list[tuple[dict[str, Any], dict[str, Any]]] = []
        end = None
        # A heading low in the left column precedes the right column in
        # reading order, although converters often emit that column first.
        if heading["bbox"][2] < page["width"] * .55:
            members += [(page, block) for block in page["blocks"][:page["blocks"].index(heading)]
                        if block["bbox"][0] >= page["width"] * .45 and block["type"] in {"paragraph", "heading"}
                        and block.get("geometry_matched") is not False
                        and abs(float(block["font_size"]) - size) <= max(.6, size * .08)]
        while index < len(sequence):
            candidate_page, block = sequence[index]
            text, kind = block["text"].strip(), block["type"]
            same = abs(float(block["font_size"]) - size) <= max(.6, size * .08)
            bbox = block.get("bbox") or [0, 0, 0, 0]
            # An entry number at the top of a column belongs to the list.
            if (len(text) < 80 and not _reference_marker(text) and not re.fullmatch(r"\[?\d{1,4}[.\]]", text)
                    and (bbox[3] < candidate_page["height"] * .075 or bbox[1] > candidate_page["height"] * .93)):
                # Running headers and folios neither end nor belong to a list.
                index += 1
                continue

            def prose(item: dict[str, Any]) -> bool:
                # Author lists and titles of an entry split into lines lack a
                # year as well, but prose is mostly lower-case words.
                value = item["text"].strip()
                words = re.findall(r"[A-Za-z]+", value)
                return (len(value) >= 40 and not _CITATION_EVIDENCE.search(value) and not _reference_marker(value)
                        and sum(word.islower() for word in words) >= len(words) * .65)

            ahead = [item for _, item in sequence[index:index + 10]
                     if item["type"] in {"paragraph", "heading"} and not re.fullmatch(r"\[?\d{1,4}[.\]]?", item["text"].strip())
                     and abs(float(item["font_size"]) - size) <= max(.6, size * .08)][:4]
            if (_references_heading(text) or _BACK_MATTER.match(text) or _appendix_heading(text, kind)
                    or (same and kind in {"paragraph", "heading"} and len(ahead) == 4 and all(map(prose, ahead)))
                    or (kind == "heading" and (_NAMED_SECTION.match(text) or not same
                                               or (len(text) < 60 and not _reference_marker(text) and "," not in text)))
                    or (not same and float(block["font_size"]) > size and len(text) > 200)):
                end = (candidate_page, block)
                break
            index += 1
            # Converter text without PDF lines (rotated stamps) cannot be rebuilt.
            if (same and kind not in {"caption", "table"} and not block.get("display_math")
                    and block.get("translation_scope", "body") == "body" and block.get("geometry_matched") is not False):
                members.append((candidate_page, block))
        if members:
            regions.append({"size": size, "start": (page, heading), "end": end, "members": members})
    return regions


def _reads_after(rect: list[float] | tuple, anchor: list[float], columns: bool, inclusive: bool = False) -> bool:
    """Whether text follows an anchor block in reading order, or belongs to it.

    On a multi-column page, a column left of the anchor precedes it and one
    to its right follows it. On a single-column page only height counts, so a
    run-in line beside a short heading is not mistaken for another column.
    """
    if columns and rect[2] < anchor[0]:
        return False
    if columns and rect[0] > anchor[2]:
        return True
    return (rect[1] + rect[3]) / 2 > (anchor[1] if inclusive else anchor[3] - 1)


def _reference_rows(page: dict[str, Any], region: dict[str, Any],
                    figures: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    """Rebuild one page of a reference list from its PDF lines by column.

    Converters separate hanging entry numbers from their text, and justified
    narrow columns from each other. Joining lines by baseline restores both.
    """
    size = region["size"]
    (start_page, start), end = region["start"], region["end"]
    # Captions, tables and formulas can share the list's type size. Their
    # blocks are excluded; converter alignment is too unreliable to include
    # only the lines of the list's own blocks.
    blocked = [fitz.Rect(figure["bbox"]) for figure in figures if figure["page"] == page["number"]]
    blocked += [fitz.Rect(block["bbox"]) + (-1, -1, 1, 1) for block in page["blocks"]
                if (block["type"] in {"caption", "table", "equation"} or block.get("translation_scope") in {"caption", "figure_content"})
                and block.get("geometry_matched") is not False
                and fitz.Rect(block["bbox"]).get_area() < page["width"] * page["height"] * .5]
    candidates = [line for group in page["groups"] for line in group
                  if abs(line["size"] - size) <= max(.6, size * .08)
                  and not any(area.contains(fitz.Rect(line["bbox"])) for area in blocked)]
    # Lines of a single-column list mostly span more than half of the page.
    columns = sum(line["bbox"][2] - line["bbox"][0] > page["width"] * .55 for line in candidates) < len(candidates) * .3
    lines = []
    for line in candidates:
        rect = line["bbox"]
        if page["number"] == start_page["number"] and not _reads_after(rect, start["bbox"], columns):
            continue
        if end and page["number"] == end[0]["number"] and _reads_after(rect, end[1]["bbox"], columns, inclusive=True):
            continue
        lines.append(line)
    if not lines:
        return []
    # A hanging number is now part of its entry; a number alone is a folio.
    rows = [row for row in _rebuild_lines(lines) if not _PAGE_MARKER.fullmatch(row["text"])]
    if not rows:
        return []
    # Columns are separated by gutters no line crosses. A single-column list
    # has lines wider than half the page, which then join every interval.
    narrow = [row for row in rows if row["bbox"][2] - row["bbox"][0] < page["width"] * .45]
    basis = narrow if len(narrow) >= len(rows) * .7 else rows
    columns: list[list[float]] = []
    for x0, x1 in sorted((row["bbox"][0], row["bbox"][2]) for row in basis):
        if columns and x0 <= columns[-1][1] + size * .8:
            columns[-1][1] = max(columns[-1][1], x1)
        else:
            columns.append([x0, x1])

    def column(row: dict[str, Any]) -> int:
        middle = (row["bbox"][0] + row["bbox"][2]) / 2
        inside = [index for index, (x0, x1) in enumerate(columns) if x0 - size <= row["bbox"][0] <= x1]
        return inside[0] if inside else min(range(len(columns)), key=lambda index: abs(sum(columns[index]) / 2 - middle))

    return sorted(((column(row), row) for row in rows), key=lambda item: (item[0], item[1]["bbox"][1], item[1]["bbox"][0]))


def _segment_references(rows: list[tuple[int, int, dict[str, Any]]], size: float) -> list[dict[str, Any]]:
    """Split ordered reference lines into entries.

    Numbered lists follow their sequence, so a page number or a wrapped
    "2020" never starts an entry. Author-year lists use their hanging indent,
    or the gap and short final line that end an entry.
    """
    markers = [_reference_marker(row["text"]) for _, _, row in rows]
    kinds = Counter(marker[0] for marker in markers if marker)
    kind = kinds.most_common(1)[0][0] if kinds else None
    values = [marker[1] for marker in markers if marker and marker[0] == kind]
    steps = sum(1 for a, b in zip(values, values[1:]) if b == a + 1)
    numbered = kind is not None and steps >= 2 and steps >= (len(values) - 1) * .5
    lefts: dict[tuple[int, int], float] = {}
    rights: dict[tuple[int, int], float] = {}
    for page, column, row in rows:
        key = (page, column)
        lefts[key] = min(lefts.get(key, row["bbox"][0]), row["bbox"][0])
        rights[key] = max(rights.get(key, row["bbox"][2]), row["bbox"][2])
    indented = sum(row["bbox"][0] > lefts[(page, column)] + size * .8 for page, column, row in rows)
    hanging = indented >= len(rows) * .2
    gaps = [row["bbox"][1] - previous["bbox"][3]
            for (page, column, previous), (page_b, column_b, row) in zip(rows, rows[1:])
            if (page, column) == (page_b, column_b)]
    typical_gap = median(gaps) if gaps else 0
    entries: list[dict[str, Any]] = []
    expected = None
    for index, (page, column, row) in enumerate(rows):
        marker = markers[index]
        previous = rows[index - 1] if index else None
        if numbered:
            start = bool(marker and marker[0] == kind and (expected is None or expected <= marker[1] <= expected + 3))
        elif hanging:
            start = row["bbox"][0] <= lefts[(page, column)] + size * .4
        else:
            short = previous is not None and previous[2]["bbox"][2] < rights[(previous[0], previous[1])] - size * 2
            ended = previous is not None and bool(re.search(r"[.)\]]\s*$", previous[2]["text"]))
            spaced = (previous is not None and (previous[0], previous[1]) == (page, column)
                      and row["bbox"][1] - previous[2]["bbox"][3] > typical_gap + size * .35)
            start = spaced or (short and ended and bool(_AUTHOR_START.match(row["text"])))
        if start or not entries:
            if numbered and marker and marker[0] == kind:
                expected = marker[1] + 1
            entries.append({"type": "reference", "rows": [],
                            "number": marker[1] if numbered and marker and marker[0] == kind else None,
                            "confident": numbered or hanging})
        entries[-1]["rows"].append((page, column, row))
    for entry in entries:
        entry["text"] = _join_lines([row for _, _, row in entry["rows"]])
    if numbered:
        entries = _split_skipped_numbers(entries, kind)
    return entries


def _split_skipped_numbers(entries: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    """Recover entries whose number does not open a line of its own."""
    result: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        following = next((item["number"] for item in entries[index + 1:] if item["number"] is not None), None)
        pieces = [entry]
        if entry["number"] is not None and following is not None and following > entry["number"] + 1:
            text, parts, number = entry["text"], [], entry["number"] + 1
            while number < following:
                pattern = {"bracket": rf"\s\[{number}\]\s*", "dot": rf"\s{number}\.\s+(?=[A-Z])",
                           "bare": rf"\s{number}\s+(?=[A-Z])"}[kind]
                match = re.search(pattern, text)
                if not match:
                    break
                parts.append((text[:match.start()].strip(), number - 1))
                text = text[match.start():].strip()
                number += 1
            if parts:
                pieces = [dict(entry, text=part, number=value) for part, value in parts]
                pieces.append(dict(entry, text=text, number=number - 1))
            # A number that is still missing leaves this boundary open.
            if number < following:
                pieces[-1]["confident"] = False
        result.extend(pieces)
    return result


def _back_matter_row(row: dict[str, Any]) -> bool:
    """Whether a line opens acknowledgements or similar sections."""
    match = _BACK_MATTER.match(row["text"])
    return bool(match) and (match.group(0).isupper() or row["bold"]
                            or bool(re.match(r"\s*[:.]", row["text"][match.end():])))


def _rebuild_references(pages: list[dict[str, Any]], figures: list[dict[str, Any]]) -> None:
    """Replace reference list blocks with one ``reference`` block per entry.

    An entry keeps the region where it starts; parts in another column or on
    the next page are listed in ``source_fragments``, as for merged prose.
    """
    by_number = {page["number"]: page for page in pages}
    for region in _reference_regions(pages):
        member_pages = sorted({page["number"] for page, _ in region["members"]})
        rows = [(number, column, row) for number in member_pages
                for column, row in _reference_rows(by_number[number], region, figures)]
        stop = next((index for index, (_, _, row) in enumerate(rows) if _back_matter_row(row)), len(rows))
        entries = _segment_references(rows[:stop], region["size"])
        # Back matter set in the list's type size can follow the last entry
        # within the same block; it stays prose, one paragraph per column.
        for number, column, row in rows[stop:]:
            if (not entries or entries[-1]["type"] != "paragraph" or _back_matter_row(row)
                    or entries[-1]["rows"][-1][:2] != (number, column)):
                entries.append({"type": "paragraph", "rows": [], "number": None, "confident": True})
            entries[-1]["rows"].append((number, column, row))
        if not entries:
            continue
        removed = {id(block) for _, block in region["members"]}
        placed: dict[int, list[dict[str, Any]]] = {}
        for entry in entries:
            fragments: list[dict[str, Any]] = []
            for number, column, row in entry["rows"]:
                if fragments and (fragments[-1]["page"], fragments[-1]["column"]) == (number, column):
                    fragments[-1]["rect"] |= fitz.Rect(row["bbox"])
                    fragments[-1]["lines"].append(row)
                else:
                    fragments.append({"page": number, "column": column, "rect": fitz.Rect(row["bbox"]), "lines": [row]})
            first = fragments[0]
            block = {"page": first["page"], "type": entry["type"],
                     "text": entry.get("text") or _join_lines([row for _, _, row in entry["rows"]]),
                     "bbox": _bbox(first["rect"]),
                     "first_line_bbox": _bbox(first["lines"][0]["bbox"]), "last_line_bbox": _bbox(first["lines"][-1]["bbox"]),
                     "font_size": round(region["size"], 2), "text_source": "pdf", "geometry_matched": True,
                     "translation_scope": "body"}
            if entry["type"] == "reference":
                block["reference_confident"] = entry["confident"]
            if entry["number"] is not None:
                block["reference_number"] = entry["number"]
            if len(fragments) > 1:
                block["source_fragments"] = [{"page": fragment["page"], "bbox": _bbox(fragment["rect"]),
                                              "text": _join_lines(fragment["lines"])} for fragment in fragments]
            placed.setdefault(first["page"], []).append(block)
        start_page, start = region["start"]
        for number in sorted(set(member_pages) | set(placed)):
            page = by_number[number]
            positions = [index for index, block in enumerate(page["blocks"]) if id(block) in removed]
            if positions:
                at = positions[0]
            elif number == start_page["number"]:
                at = next(index for index, block in enumerate(page["blocks"]) if block is start) + 1
            else:
                at = 0
            kept = [block for block in page["blocks"] if id(block) not in removed]
            if number == start_page["number"]:
                # Entries emitted before their heading still follow it.
                at = kept.index(start) + 1 if start in kept else at
            references = placed.get(number, [])
            offset = sum(1 for block in kept if block["id"].startswith(f"p{number}-r"))
            for index, block in enumerate(references, offset + 1):
                block["id"] = f"p{number}-r{index}"
            page["blocks"] = kept[:at] + references + kept[at:]


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
                if _references_heading(text):
                    in_references = True
                elif _ends_references(text):
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
    forced = {tuple(pair) for pair in document.get("layout_links", [])}
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
                if _references_heading(text):
                    in_references = True
                elif _ends_references(text):
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
            # A continuation confirmed by the AI layout review.
            match = match or bool(previous and (previous["id"], block["id"]) in forced)
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


def _review_anchor(block: dict[str, Any], page: int, end: str) -> dict[str, Any]:
    """Identify a block by page and text, as IDs change on re-extraction."""
    key = _alignment_key(block["text"])
    return {"page": page, "type": block.get("type"),
            end: key[-DEFAULT_REVIEW_KEY_CHARS:] if end == "tail" else key[:DEFAULT_REVIEW_KEY_CHARS]}


def _review_signature(candidate: dict[str, Any]) -> str:
    anchors = [candidate.get(name) for name in ("before", "after", "block")]
    return "|".join(f"{anchor['page']}:{anchor.get('tail', '')}:{anchor.get('head', '')}" for anchor in anchors if anchor)


def layout_review_candidates(document: dict[str, Any]) -> list[dict[str, Any]]:
    """List the boundaries and reference entries that local rules leave open.

    A prose boundary qualifies when the left side does not end a sentence;
    reference boundaries and long entries qualify only in lists whose entries
    could not be separated by numbering or a hanging indent. A block's role
    is questioned when it has no PDF position, is a heading with sentences,
    or sits directly below a graphic. Candidates that an earlier review
    already decided are omitted.
    """
    reviewed = set(document.get("layout_review", {}).get("reviewed", []))
    protected = {item.get("block_id") for item in document.get("toc", [])}
    protected.update(figure.get("caption_block_id") for figure in document.get("figures", []))
    metadata = {document.get("title"), document.get("authors")}
    candidates: list[dict[str, Any]] = []

    def excerpt(text: str, tail: bool) -> str:
        text = " ".join(text.split())
        return text[-DEFAULT_REVIEW_CONTEXT_CHARS:] if tail else text[:DEFAULT_REVIEW_CONTEXT_CHARS]

    previous = None
    for page in document.get("pages", []):
        for block in page.get("blocks", []):
            text = block.get("text", "").strip()
            kind, scope = block.get("type"), block.get("translation_scope", "body")
            if scope in {"caption", "figure_content"} or kind in {"caption", "table"}:
                continue
            if kind not in {"paragraph", "reference"} or block.get("geometry_matched") is False or text in metadata:
                previous = None
                continue
            # A running header such as the article DOI interrupts the prose
            # it is printed between; it is neither side of the boundary.
            if block.get("hidden") or re.fullmatch(r"(?:https?://|doi:?\s*)\S+", text, re.I):
                continue
            pair = previous
            previous = (page["number"], block)
            if pair is None or block.get("id") in protected:
                continue
            before_page, before = pair
            if (before["type"] != kind or page["number"] - before_page > 1
                    or block.get("continuation_of") == before.get("id")):
                continue
            left = before["text"].rstrip()
            if kind == "paragraph":
                if (len(left) < 20 or len(text) < 20 or (_SENTENCE_END.search(left) and not _OPEN_END.search(left))
                        or _ITEM_START.match(text) or _CAPTION.match(text) or _NAMED_SECTION.match(text)):
                    continue
            elif (block.get("reference_confident", True) or _reference_marker(text)
                  or (_AUTHOR_START.match(text) and _SENTENCE_END.search(left))):
                continue
            candidates.append({"kind": "join", "type": kind,
                               "before": _review_anchor(before, before_page, "tail"),
                               "after": _review_anchor(block, page["number"], "head"),
                               "before_text": excerpt(left, True), "after_text": excerpt(text, False)})
    graphics: dict[int, list[list[float]]] = {}
    for figure in document.get("figures", []):
        if figure.get("bbox"):
            graphics.setdefault(figure.get("page"), []).append(figure["bbox"])
    for page in document.get("pages", []):
        blocks = page.get("blocks", [])
        for index, block in enumerate(blocks):
            text = block.get("text", "").strip()
            kind, scope = block.get("type"), block.get("translation_scope", "body")
            if block.get("hidden") or block.get("layout_role") or scope != "body" or text in metadata or len(text) < 30:
                continue
            rect, size = block.get("bbox") or [0, 0, 0, 0], float(block.get("font_size") or 0)
            # Converter text without a PDF position may be a figure's labels.
            unplaced = block.get("geometry_matched") is False and kind == "paragraph"
            long_heading = kind == "heading" and (len(text) > 120 or len(re.findall(r"[.!?]\s+[A-Z]", text)) >= 1)
            below_graphic = kind == "paragraph" and any(
                min(rect[2], area[2]) - max(rect[0], area[0]) >= min(rect[2] - rect[0], area[2] - area[0]) * .6
                and -size <= rect[1] - area[3] <= size * 2.5 for area in graphics.get(page["number"], []))
            if not (unplaced or long_heading or below_graphic):
                continue
            neighbours = [item.get("text", "") for item in (blocks[index - 1] if index else {}, blocks[index + 1] if index + 1 < len(blocks) else {})]
            candidates.append({"kind": "role", "type": kind, "block": _review_anchor(block, page["number"], "head"),
                               "text": text[:DEFAULT_REVIEW_CONTEXT_CHARS * 3],
                               "before_text": excerpt(neighbours[0], True), "after_text": excerpt(neighbours[1], False)})
    references = [(page["number"], block) for page in document.get("pages", []) for block in page.get("blocks", [])
                  if block.get("type") == "reference"]
    typical = median(len(block["text"]) for _, block in references) if references else 0
    for number, block in references:
        if not block.get("reference_confident", True) and len(block["text"]) >= max(250, typical * DEFAULT_REVIEW_SPLIT_RATIO):
            candidates.append({"kind": "split", "type": "reference", "block": _review_anchor(block, number, "head"),
                               "text": block["text"][:DEFAULT_REVIEW_CONTEXT_CHARS * 8]})
    fresh = [candidate for candidate in candidates if _review_signature(candidate) not in reviewed]
    for index, candidate in enumerate(fresh[:DEFAULT_REVIEW_MAX_CANDIDATES], 1):
        candidate["id"] = f"{candidate['kind'][0]}{index}"
        candidate["signature"] = _review_signature(candidate)
    return fresh[:DEFAULT_REVIEW_MAX_CANDIDATES]


def _find_review_block(document: dict[str, Any], anchor: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Find a block by text; a converter can repeat text in another kind of block."""
    found = []
    for page in document.get("pages", []):
        if page["number"] != anchor.get("page"):
            continue
        for block in page.get("blocks", []):
            key = _alignment_key(block.get("text", ""))
            if (("head" in anchor and anchor["head"] and key.startswith(anchor["head"]))
                    or ("tail" in anchor and anchor["tail"] and key.endswith(anchor["tail"]))):
                found.append((page, block))
    same_type = [item for item in found if item[1].get("type") == anchor.get("type")]
    return (same_type or found or [None])[0]


def _split_reference_text(text: str, starts: list[str]) -> list[str]:
    """Split before each verbatim entry opening, in order, at word starts."""
    pieces, position = [], 0
    normalized = " ".join(text.split())
    for start in starts:
        start = " ".join(str(start).split())
        if len(start) < 8:
            continue
        found = normalized.find(start, position + 1)
        if found <= 0 or normalized[found - 1] != " ":
            continue
        pieces.append(normalized[position:found].strip())
        position = found
    pieces.append(normalized[position:].strip())
    return [piece for piece in pieces if piece]


def apply_layout_review(document: dict[str, Any], decisions: list[dict[str, Any]]) -> bool:
    """Apply accepted review decisions; return whether the layout changed.

    Joins on one page, and reference entries across pages, become one block
    whose other regions are kept in ``source_fragments``. Prose continuing
    on the next page is only linked, so each page keeps its own text.
    """
    changed = False
    links = [list(pair) for pair in document.get("layout_links", [])]
    for decision in decisions:
        if decision.get("kind") == "role":
            anchor = decision.get("block", {})
            found = _find_review_block(document, anchor)
            role = decision.get("role")
            # A role was judged for one kind of block; text repeated in a
            # block of another kind must keep its own role.
            if not found or role not in REVIEW_ROLES or found[1].get("type") != anchor.get("type"):
                continue
            block = found[1]
            block["layout_role"] = role
            if role in {"figure_text", "furniture"}:
                block["hidden"] = True
                block["translation_scope"] = "figure_content"
                block.pop("text_zh", None)
            else:
                block.pop("hidden", None)
                block["type"] = {"body": "paragraph", "heading": "heading", "caption": "caption"}[role]
                block["translation_scope"] = "caption" if role == "caption" else "body"
            if role != "heading":
                # A heading title generated from this block no longer applies.
                document["toc"] = [item for item in document.get("toc", [])
                                   if item.get("block_id") != block["id"] or item.get("title") != block["text"]]
            changed = True
            continue
        if decision.get("kind") == "split":
            found = _find_review_block(document, decision.get("block", {}))
            if not found or found[1].get("type") != "reference":
                continue
            page, block = found
            pieces = _split_reference_text(block["text"], decision.get("starts", []))
            if len(pieces) < 2:
                continue
            index = page["blocks"].index(block)
            replacement = []
            for number, piece in enumerate(pieces):
                item = dict(block, text=piece, reference_confident=True)
                item.pop("text_zh", None)
                item.pop("reference_number", None)
                if number:
                    item["id"] = f"{block['id']}-{number}"
                    item.pop("source_fragments", None)
                replacement.append(item)
            page["blocks"][index:index + 1] = replacement
            changed = True
            continue
        before, after = (_find_review_block(document, decision.get(name, {})) for name in ("before", "after"))
        if not before or not after or before[1] is after[1] or before[1].get("type") != after[1].get("type"):
            continue
        (before_page, first), (after_page, second) = before, after
        if re.fullmatch(r"(?:https?://|doi:?\s*)\S+", first["text"].strip(), re.I):
            continue
        if first.get("type") == "paragraph" and before_page is not after_page:
            if [first["id"], second["id"]] not in links:
                links.append([first["id"], second["id"]])
                document["layout_links"] = links
                changed = True
            continue

        def fragments(block: dict[str, Any], page: dict[str, Any]) -> list[dict[str, Any]]:
            return block.get("source_fragments") or [{"page": page["number"], "bbox": block.get("bbox"), "text": block["text"]}]

        first["source_fragments"] = fragments(first, before_page) + fragments(second, after_page)
        first["text"] = _join_lines([{"text": first["text"]}, {"text": second["text"]}])
        if before_page is after_page:
            first["last_line_bbox"] = second.get("last_line_bbox")
        first.pop("text_zh", None)
        after_page["blocks"].remove(second)
        changed = True
    if changed:
        annotate_paragraph_groups(document)
    return changed


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
        # A name can share its block with an affiliation and an e-mail address
        # ("Ashish Vaswani∗ Google Brain ..."); its marked leading name counts.
        names = []
        for block in sorted(author_candidates, key=lambda block: (block["bbox"][1], block["bbox"][0])):
            match = re.match(r"((?:[A-ZÀ-ÖØ-ÞŁ][\w'’.\-]*\s+){1,3}[A-ZÀ-ÖØ-ÞŁ][\w'’\-]+)\s*[∗*†‡]", block["text"])
            if match and match.group(1) not in names:
                names.append(match.group(1))
        authors = ", ".join(names)
    if not authors:
        author_metadata = _clean_text(pdf_metadata.get("author") or "")
        if not _INSTITUTION.search(author_metadata) and not _PUBLISHER_NAVIGATION.match(author_metadata) and len(author_metadata) < 500:
            authors = author_metadata
    abstract_parts: list[str] = []
    started = False
    # Some journals print an unlabelled abstract in larger type on page one.
    lead = next((block for block in first["blocks"] if block["type"] == "paragraph" and len(block["text"]) >= 400
                 and float(block["font_size"]) >= body_size * 1.1 and not _INSTITUTION.search(block["text"][:200])), None)
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
    if not abstract_parts and lead:
        abstract_parts.append(lead["text"])
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
def read_page_lines(pdf_bytes: bytes, page_number: int) -> list[dict[str, Any]]:
    """Return a page's text lines for apply_vision_layout."""
    with _open_pdf(pdf_bytes) as source:
        raw = _read_page(source[_page_index(source, page_number)], page_number)
    return [line for group in raw["groups"] for line in group]


def apply_vision_layout(document: dict[str, Any], lines: list[dict[str, Any]], page_number: int,
                        regions: list[dict[str, Any]]) -> None:
    """Rebuild one page from the regions a vision model drew on its image.

    The model decides only structure and order. Text always comes from the
    PDF lines inside each region (see read_page_lines), so recognition errors
    cannot enter it. Regions that miss too much text are rejected outright.
    """
    page = next(item for item in document["pages"] if item["number"] == page_number)
    width, height = page["width"], page["height"]
    rects = [fitz.Rect(*(float(value) * scale / 1000 for value, scale in zip(region["bbox"], (width, height, width, height))))
             for region in regions]
    owned: list[list[dict[str, Any]]] = [[] for _ in regions]
    stray: list[dict[str, Any]] = []
    total = assigned = 0
    for line in lines:
        center = fitz.Point((line["bbox"][0] + line["bbox"][2]) / 2, (line["bbox"][1] + line["bbox"][3]) / 2)
        total += len(line["text"])
        containing = [index for index, rect in enumerate(rects) if (rect + (-3, -3, 3, 3)).contains(center)]
        if containing:
            owned[min(containing, key=lambda index: rects[index].get_area())].append(line)
            assigned += len(line["text"])
        else:
            stray.append(line)
    if total and assigned < total * DEFAULT_VISION_MIN_COVERAGE:
        raise ValueError(f"视觉识别的区域只覆盖第 {page_number} 页 {assigned * 100 // total}% 的文字，未采用。")
    # Text just outside a drawn box joins the nearest text region.
    textual = [index for index, region in enumerate(regions) if region["type"] not in {"figure", "table", "header", "footer"}]
    for line in stray:
        center = fitz.Point((line["bbox"][0] + line["bbox"][2]) / 2, (line["bbox"][1] + line["bbox"][3]) / 2)
        if textual:
            owned[min(textual, key=lambda index: rects[index].distance_to(center))].append(line)
    translations = {" ".join(block["text"].split()): block["text_zh"] for block in page["blocks"] if block.get("text_zh")}
    kinds = {"title": "heading", "heading": "heading", "paragraph": "paragraph", "list": "paragraph",
             "footnote": "paragraph", "caption": "caption", "reference": "reference", "equation": "equation"}
    blocks: list[dict[str, Any]] = []
    figures: list[dict[str, Any]] = []
    for index, (region, rect, members) in enumerate(zip(regions, rects, owned), 1):
        kind, identifier = region["type"], f"p{page_number}-v{index}"
        if kind in {"header", "footer"}:
            continue
        if kind in {"figure", "table"}:
            figure = {"id": f"p{page_number}-vision-{kind}-{index}", "page": page_number, "kind": kind,
                      "bbox": _bbox(rect), "visual_regions": [_bbox(rect)], "caption": "", "caption_zh": ""}
            figures.append(figure)
            blocks.append({"id": identifier, "page": page_number, "type": "figure", "figure_id": figure["id"], "text": "",
                           "bbox": _bbox(rect), "font_size": 0, "translation_scope": "figure_content", "geometry_matched": True})
            continue
        if not members:
            continue
        # Joining baselines first returns superscripts to their line.
        rows = sorted(_rebuild_lines(members), key=lambda row: (row["bbox"][1], row["bbox"][0]))
        bounds = fitz.Rect(rows[0]["bbox"])
        for row in rows[1:]:
            bounds |= fitz.Rect(row["bbox"])
        block = {"id": identifier, "page": page_number, "type": kinds[kind],
                 "text": _join_lines(rows, equation=kind == "equation"), "bbox": _bbox(bounds),
                 "first_line_bbox": _bbox(rows[0]["bbox"]), "last_line_bbox": _bbox(rows[-1]["bbox"]),
                 "font_size": round(median(row["size"] for row in rows), 2), "text_source": "vision",
                 "geometry_matched": True, "translation_scope": "caption" if kind == "caption" else "body"}
        if kind == "equation":
            block["display_math"] = True
        if kind == "reference":
            block["reference_confident"] = True
        cached = translations.get(" ".join(block["text"].split()))
        if cached and kind != "equation":
            block["text_zh"] = cached
        blocks.append(block)
    # A figure's caption is the caption region next to it in reading order.
    for figure in figures:
        position = next(index for index, block in enumerate(blocks) if block.get("figure_id") == figure["id"])
        for neighbour in (blocks[position + 1] if position + 1 < len(blocks) else None, blocks[position - 1] if position else None):
            if neighbour and neighbour["type"] == "caption" and not any(item.get("caption_block_id") == neighbour["id"] for item in figures):
                figure.update(caption=neighbour["text"], caption_block_id=neighbour["id"])
                break
    page["blocks"] = blocks
    page.pop("layout_flags", None)
    page["vision_layout"] = True
    document["figures"] = [figure for figure in document.get("figures", []) if figure.get("page") != page_number] + figures
    headings = [block for block in blocks if block["type"] == "heading"]
    for item in document.get("toc", []):
        if item.get("page") == page_number and item.get("block_id") not in {block["id"] for block in blocks}:
            key = _alignment_key(item.get("title", ""))
            match = next((block for block in headings if key and key in _alignment_key(block["text"])), None)
            if match:
                item["block_id"] = match["id"]
            else:
                item.pop("block_id", None)
    annotate_translation_scopes(document)
    annotate_paragraph_groups(document)


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
        # A page dominated by a table's small type would make its prose look
        # like headings. Its body size ignores the text inside graphics.
        for page in raw_pages:
            areas = [fitz.Rect(figure["bbox"]) for figure in figures if figure["page"] == page["number"]]
            if not areas:
                continue
            weighted = Counter()
            for group in page["groups"]:
                for line in group:
                    if not any(area.contains(fitz.Rect(line["bbox"])) for area in areas):
                        weighted[round(line["size"] * 2) / 2] += len(line["text"])
            if weighted:
                page["body_size"] = float(weighted.most_common(1)[0][0])
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
        _mark_table_notes(raw_pages, figures)
        _hide_repeated_furniture(raw_pages)
        # Headings feed the title and the TOC, so restore them first.
        for page in raw_pages:
            _rejoin_split_sentences(page)
        _rebuild_references(raw_pages, figures)
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
        for page, raw in zip(result["pages"], raw_pages):
            flags = _layout_flags(page, raw, figures)
            if flags:
                page["layout_flags"] = flags
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
