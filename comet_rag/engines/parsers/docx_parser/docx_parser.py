"""
Production-grade DOCX parser for Comet-RAG.

Block types emitted
-------------------
text      — body paragraph            {type, content, style}
heading   — heading paragraph         {type, content, level, is_numbered[, number]}
list      — ordered / unordered list  {type, attribute, ilevel, content: [Block, …]}
table     — table block               {type, content, rows, row_count, col_count}
image     — embedded image            {type, content, format, name, alt_text, id, width_px, height_px}
equation  — standalone equation       {type, content}   (LaTeX or raw text)
caption   — figure / table caption    {type, content}
header    — page header               {type, content}
footer    — page footer               {type, content}

Inline rich text in ``content`` uses Markdown conventions:
  **bold**  *italic*  ***bold+italic***  ~~strikethrough~~  [text](url)
  Inline equations:   $latex$
  Standalone equations are wrapped in $$…$$.

OMML → LaTeX conversion is handled by the built-in ``_omml`` module
(no external dependencies beyond lxml and loguru).
"""

import base64
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from docx.document import Document as DocumentObject
from docx.oxml.ns import qn
from docx.text.hyperlink import Hyperlink
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from loguru import logger
from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from comet_rag.engines.converters.types import DocxDocument
from comet_rag.engines.parsers.docx_parser.omml import oMath2Latex as _oMath2Latex
from comet_rag.engines.parsers.types import Block, DocxParsedContent

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"

_R_EMBED = f"{{{_R}}}embed"
_XML_VAL = f"{{{_W}}}val"
_EMU_PER_PX = 9525  # 914 400 EMU/inch ÷ 96 DPI

# Inline wrapper tags treated as transparent pass-throughs
_TRANSPARENT_INLINE: frozenset[str] = frozenset(
    {"bdo", "customXml", "dir", "fldSimple", "ins", "moveTo", "smartTag"}
)


@dataclass(frozen=True, eq=True)
class _Fmt:
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strikethrough: bool = False


_PLAIN = _Fmt()

# (text, formatting, hyperlink-url | None)
_PElem = tuple[str, _Fmt, str | None]


@dataclass(frozen=True)
class _EqSeg:
    latex: str


_Seg = _PElem | _EqSeg


def _merge_segs(elems: list[_Seg]) -> list[_Seg]:
    """Merge adjacent text segments that share identical format and URL."""
    merged: list[_Seg] = []
    for seg in elems:
        if (
            merged
            and not isinstance(seg, _EqSeg)
            and not isinstance(merged[-1], _EqSeg)
            and seg[1] == merged[-1][1]
            and seg[2] == merged[-1][2]
        ):
            prev = merged[-1]
            merged[-1] = (prev[0] + seg[0], prev[1], prev[2])
        else:
            merged.append(seg)
    return merged


def _qname(element: Any) -> str:
    return etree.QName(element).localname


def _apply_fmt_and_url(text: str, fmt: _Fmt, url: str | None) -> str:
    """Wrap *text* with Markdown formatting markers and/or a hyperlink."""
    stripped = text.strip()
    if not stripped:
        return text

    prefix = text[: len(text) - len(text.lstrip())]
    suffix = text[len(text.rstrip()) :]

    inner = stripped
    if fmt.bold and fmt.italic:
        inner = f"***{inner}***"
    elif fmt.bold:
        inner = f"**{inner}**"
    elif fmt.italic:
        inner = f"*{inner}*"
    if fmt.strikethrough:
        inner = f"~~{inner}~~"

    if url and url.strip() not in ("", "."):
        url_esc = url.replace("(", "%28").replace(")", "%29")
        return f"{prefix}[{inner}]({url_esc}){suffix}"

    return f"{prefix}{inner}{suffix}"


def _table_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    escaped_rows = [
        [cell.replace("|", "\\|").replace("\n", "<br>") for cell in row] for row in rows
    ]
    header, *body = escaped_rows
    sep = ["---"] * len(header)
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(sep) + " |",
        *("| " + " | ".join(row) + " |" for row in body),
    ]
    return "\n".join(lines)


def _is_hidden_run(run: Run) -> bool:
    rpr = run._element.find(f"{{{_W}}}rPr")
    if rpr is None:
        return False
    return (
        rpr.find(f"{{{_W}}}webHidden") is not None
        or rpr.find(f"{{{_W}}}vanish") is not None
    )


def _resolve_style_bool(style_obj: Any, attr: str) -> bool | None:
    """Walk the style inheritance chain and resolve a boolean font attribute."""
    style = style_obj
    while style is not None:
        font = getattr(style, "font", None)
        if font is not None:
            if attr == "underline":
                value = font.underline
            elif attr == "strikethrough":
                value = font.strike
            else:
                value = getattr(font, attr, None)
            if value is not None:
                return bool(value)
        style = getattr(style, "base_style", None)
    return None


def _get_run_fmt(run: Run) -> _Fmt:
    """Resolve run formatting with full style-inheritance chain."""

    def resolve(attr: str) -> bool:
        direct = (
            run.underline
            if attr == "underline"
            else (
                run.font.strike if attr == "strikethrough" else getattr(run, attr, None)
            )
        )
        if direct is not None:
            return bool(direct)

        # Character-style chain — skip Hyperlink default style to avoid
        # spurious underlines on every hyperlink run.
        run_style = getattr(run, "style", None)
        sid = str(getattr(run_style, "style_id", "") or "").lower()
        sname = str(getattr(run_style, "name", "") or "").lower()
        if "hyperlink" not in sid and "hyperlink" not in sname:
            v = _resolve_style_bool(run_style, attr)
            if v is not None:
                return v

        # Paragraph-style chain
        parent = getattr(run, "_parent", None)
        v = _resolve_style_bool(getattr(parent, "style", None), attr)
        if v is not None:
            return v
        return False

    underline = resolve("underline")
    # CJK emphasis mark (w:em) also renders as an underline-like decoration
    rpr = run._element.find(f"{{{_W}}}rPr")
    if rpr is not None:
        em = rpr.find(f"{{{_W}}}em")
        if em is not None and em.get(_XML_VAL, "") not in ("", "none"):
            underline = True

    return _Fmt(
        bold=resolve("bold"),
        italic=resolve("italic"),
        underline=underline,
        strikethrough=resolve("strikethrough"),
    )


class DocxParser:
    """Parse a DocxDocument into a list of semantically typed blocks."""

    def __init__(self, *, heading_numbers: bool = False) -> None:
        self._doc: DocumentObject | None = None
        self._doc_part: Any = None
        self._blocks: list[Block] = []
        # Active list tracking
        self._pre_num_id: int = -1
        self._pre_ilevel: int = -1
        self._list_stack: list[Block] = []
        # Heading auto-number extraction
        self._heading_numbers = heading_numbers
        self._h_counters: list[int] = [0] * 9
        self._h_numId: int | None = None
        self._h_fmts: list[str] = []
        self._h_starts: list[int] = []
        self._styles_root: Any = None
        self._footnotes: dict[int, str] | None = None

    def parse(self, document: DocxDocument) -> DocxParsedContent:
        self._doc = document.elements
        self._doc_part = self._doc.part
        self._blocks = []
        self._pre_num_id = -1
        self._pre_ilevel = -1
        self._list_stack = []
        self._h_counters = [0] * 9
        self._h_numId = None
        self._h_fmts = []
        self._h_starts = []
        self._styles_root = None
        self._footnotes = None

        self._walk(self._doc.element.body)
        self._add_headers_footers()

        return DocxParsedContent(blocks=self._blocks, metadata=document.metadata)

    def _walk(self, container: Any) -> None:
        for _, child in enumerate(container):
            tag = _qname(child)
            if tag == "p":
                self._handle_paragraph(child)
            elif tag == "tbl":
                self._close_list()
                self._handle_table(child)
            elif tag == "sdt":
                sdt_content = child.find(f"{{{_W}}}sdtContent")
                if sdt_content is not None:
                    self._walk(sdt_content)
            elif tag in _TRANSPARENT_INLINE:
                self._walk(child)

    def _handle_paragraph(self, element: Any) -> None:  # noqa: C901 (complexity ok here)
        paragraph = Paragraph(element, self._doc)  # pyright: ignore[reportArgumentType]
        p_elems = self._get_paragraph_elements(paragraph)
        eq_segs = [s for s in p_elems if isinstance(s, _EqSeg)]
        has_plain_text = any(s[0].strip() for s in p_elems if not isinstance(s, _EqSeg))
        rich_text = self._build_rich_text(p_elems)

        style_name = (paragraph.style.name if paragraph.style else None) or "Normal"
        h_level = self._get_heading_level(paragraph)
        numid, ilevel = self._get_numId_ilvl(paragraph)
        if numid == 0:
            numid = None

        # Extract images that live inside this paragraph element
        image_blocks = self._extract_images(element)

        # List item (non-heading)
        if numid is not None and ilevel is not None and h_level is None:
            if rich_text:
                self._add_list_item(
                    numid, ilevel, rich_text, self._is_numbered_list(numid, ilevel)
                )
            self._blocks.extend(image_blocks)
            return

        # All non-list branches close any active list first
        self._close_list()

        # Heading
        if h_level is not None:
            if rich_text:
                h_numid = self._resolve_heading_numid(element)
                block: Block = {
                    "type": "heading",
                    "level": h_level,
                    "content": rich_text,
                    "is_numbered": h_numid is not None,
                }
                if self._heading_numbers and h_numid is not None:
                    num_str = self._compute_heading_number(h_numid, h_level)
                    if num_str:
                        block["number"] = num_str
                self._blocks.append(block)

        # Standalone equation
        elif eq_segs and not has_plain_text:
            eq_content = " ".join(s.latex for s in eq_segs).strip()
            if eq_content:
                self._blocks.append(
                    {"type": "equation", "content": f"$${eq_content}$$"}
                )

        # Caption (SEQ-field based detection)
        elif self._is_caption(element):
            if rich_text:
                self._blocks.append({"type": "caption", "content": rich_text})

        # Body text
        elif rich_text:
            self._blocks.append(
                {"type": "text", "content": rich_text, "style": style_name}
            )

        self._blocks.extend(image_blocks)

    # Inner-content iterator
    # Handles inline w:sdt content controls and transparent wrappers that
    # python-docx's iter_inner_content() does not traverse.
    def _iter_inner_content(
        self,
        paragraph: Paragraph,
        container: Any | None = None,
    ) -> Iterator[Run | Hyperlink | Any]:
        if container is None:
            container = paragraph._element
        for child in container:
            tag = _qname(child)
            if tag == "r":
                yield Run(child, paragraph)
            elif tag == "hyperlink":
                yield Hyperlink(child, paragraph)
            elif tag == "oMath":
                yield child
            elif tag == "oMathPara":
                yield from self._iter_inner_content(paragraph, child)
            elif tag == "sdt":
                sdt_content = child.find(f"{{{_W}}}sdtContent")
                if sdt_content is not None:
                    yield from self._iter_inner_content(paragraph, sdt_content)
            elif tag in _TRANSPARENT_INLINE:
                yield from self._iter_inner_content(paragraph, child)

    def _get_paragraph_elements(self, paragraph: Paragraph) -> list[_Seg]:  # noqa: C901
        """
        Return an ordered list of text and equation segments for the paragraph.

        Handles:
        - w:hyperlink wrappers (external URLs)
        - w:fldChar / w:instrText field-code hyperlinks
        - Hidden runs (w:vanish, w:webHidden)
        - m:oMath / m:oMathPara inline and block equations
        - Consecutive same-format runs are merged for compactness
        """
        elems: list[_Seg] = []

        # Field-code hyperlink tracking state
        _field_in = False
        _field_phase: str | None = None  # "instr" | "result"
        _field_url: str | None = None
        _field_text = ""
        _field_fmt: _Fmt | None = None

        # Current accumulation group (merges consecutive same-format runs)
        _grp_text = ""
        _grp_fmt: _Fmt | None = None

        def _flush() -> None:
            nonlocal _grp_text, _grp_fmt
            if _grp_text:
                elems.append((_grp_text, _grp_fmt or _PLAIN, None))
            _grp_text = ""
            _grp_fmt = None

        for c in self._iter_inner_content(paragraph):
            # Equation element
            if not isinstance(c, (Run, Hyperlink)):
                _flush()
                latex = self._convert_omath(c)
                if latex:
                    elems.append(_EqSeg(latex=latex))
                continue

            # Hyperlink element
            if isinstance(c, Hyperlink):
                address = c.address
                # Keep only external URLs; skip internal anchors (TOC noise)
                url = address if (address and "://" in address) else None
                _flush()
                for h_run in c.runs:
                    if _is_hidden_run(h_run):
                        continue
                    h_text = h_run.text or ""
                    if h_text:
                        elems.append((h_text, _get_run_fmt(h_run), url))
                continue

            if not isinstance(c, Run):
                continue

            # fldChar (field boundary marker)
            fld_char = c._element.find(f"{{{_W}}}fldChar")
            if fld_char is not None:
                fld_type = fld_char.get(f"{{{_W}}}fldCharType")
                if fld_type == "begin":
                    _flush()
                    _field_in = True
                    _field_phase = "instr"
                    _field_url = _field_text = ""
                    _field_fmt = None
                elif fld_type == "separate":
                    _field_phase = "result"
                elif fld_type == "end":
                    if _field_text.strip():
                        elems.append(
                            (_field_text, _field_fmt or _PLAIN, _field_url or None)
                        )
                    _field_in = False
                    _field_phase = _field_url = None
                    _field_text = ""
                    _field_fmt = None
                continue  # fldChar runs carry no displayable text

            # instrText: extract HYPERLINK url
            instr = c._element.find(f"{{{_W}}}instrText")
            if instr is not None:
                if _field_phase == "instr" and instr.text:
                    m = re.search(r'HYPERLINK\s+"([^"]+)"', instr.text)
                    if m:
                        _field_url = m.group(1)
                continue  # never displayable

            # Skip non-display runs inside an instr block
            if _field_in and _field_phase == "instr":
                continue

            # Accumulate field result display text
            if _field_in and _field_phase == "result":
                if c._element.find(f"{{{_W}}}t") is not None:
                    _field_text += c.text or ""
                    if _field_fmt is None:
                        _field_fmt = _get_run_fmt(c)
                continue

            # Hidden run
            if _is_hidden_run(c):
                continue

            # Footnote reference
            fn_ref = c._element.find(f"{{{_W}}}footnoteReference")
            if fn_ref is not None:
                fn_id = int(fn_ref.get(f"{{{_W}}}id", -1))
                fn_text = self._load_footnotes().get(fn_id, "")
                if fn_text:
                    _flush()
                    elems.append((f"（{fn_text}）", _PLAIN, None))
                continue

            # Normal run
            text = c.text or ""
            if not text:
                continue
            fmt = _get_run_fmt(c)

            if fmt == _grp_fmt or _grp_fmt is None:
                _grp_fmt = fmt
                _grp_text += text
            else:
                _flush()
                _grp_fmt = fmt
                _grp_text = text

        _flush()
        return _merge_segs(elems)

    def _build_rich_text(self, elems: list[_Seg]) -> str:
        return "".join(
            f"${seg.latex}$"
            if isinstance(seg, _EqSeg)
            else _apply_fmt_and_url(seg[0], seg[1], seg[2])
            for seg in elems
        )

    def _convert_omath(self, element: Any) -> str:
        """Convert an oMath element to LaTeX, falling back to raw text on error."""
        try:
            return str(_oMath2Latex(element)).strip()
        except Exception as exc:
            logger.debug(f"oMath2Latex failed: {exc}")
            return "".join(
                node.text
                for node in element.iter()
                if _qname(node) == "t" and node.text
            )

    def _extract_images(self, para_element: Any) -> list[Block]:
        blocks: list[Block] = []
        for drawing in para_element.iter(qn("w:drawing")):
            docPr = drawing.find(f".//{qn('wp:docPr')}")
            name = docPr.get("name", "") if docPr is not None else ""
            alt_text = docPr.get("descr", "") if docPr is not None else ""
            docPr_id = docPr.get("id", "") if docPr is not None else ""

            extent = drawing.find(f".//{qn('wp:extent')}")
            width_px = 0
            height_px = 0
            if extent is not None:
                try:
                    width_px = int(extent.get("cx", 0)) // _EMU_PER_PX
                    height_px = int(extent.get("cy", 0)) // _EMU_PER_PX
                except (ValueError, TypeError):
                    pass

            blip = drawing.find(f".//{qn('a:blip')}")
            if blip is None:
                continue
            rId = blip.get(_R_EMBED)
            if rId not in self._doc_part.rels:
                continue

            image_part = self._doc_part.rels[rId].target_part
            blocks.append(
                {
                    "type": "image",
                    "content": base64.b64encode(image_part.blob).decode(),
                    "format": image_part.content_type.split("/")[-1],
                    "name": name,
                    "alt_text": alt_text,
                    "id": docPr_id,
                    "width_px": width_px,
                    "height_px": height_px,
                }
            )
        return blocks

    def _cell_to_text(self, cell: Any) -> str:
        """Extract rich text from a table cell, including inline equations."""
        parts: list[str] = []
        for para in cell.paragraphs:
            elems = self._get_paragraph_elements(para)
            text = self._build_rich_text(elems).strip()
            if text:
                parts.append(text)
        return "\n".join(parts)

    def _handle_table(self, element: Any) -> None:
        from docx.table import Table

        table = Table(element, self._doc)  # pyright: ignore[reportArgumentType]
        raw_rows = [
            [self._cell_to_text(cell) for cell in row.cells] for row in table.rows
        ]

        # python-docx repeats merged-cell text; deduplicate adjacent duplicates
        cleaned: list[list[str]] = []
        for row in raw_rows:
            deduped: list[str] = [row[0]] if row else []
            for cell in row[1:]:
                if cell != deduped[-1]:
                    deduped.append(cell)
            cleaned.append(deduped)

        if not any(any(r) for r in cleaned):
            return

        # Pad all rows to the same width
        max_cols = max(len(r) for r in cleaned)
        for row in cleaned:
            row.extend("" for _ in range(max_cols - len(row)))

        self._blocks.append(
            {
                "type": "table",
                "content": _table_to_markdown(cleaned),
                "rows": cleaned,
                "row_count": len(cleaned),
                "col_count": max_cols,
            }
        )

    def _get_numId_ilvl(self, paragraph: Paragraph) -> tuple[int | None, int | None]:
        numPr = paragraph._element.find(
            ".//w:numPr", namespaces=paragraph._element.nsmap
        )
        if numPr is None:
            return None, None

        def _int(el: Any) -> int | None:
            if el is None:
                return None
            try:
                return int(el.get(_XML_VAL))
            except (TypeError, ValueError):
                return None

        return (
            _int(numPr.find("w:numId", namespaces=paragraph._element.nsmap)),
            _int(numPr.find("w:ilvl", namespaces=paragraph._element.nsmap)),
        )

    def _is_numbered_list(self, numId: int, ilvl: int) -> bool:
        """Return True when the list level uses a numeric numbering format."""
        _NUMBERED = {
            "decimal",
            "lowerRoman",
            "upperRoman",
            "lowerLetter",
            "upperLetter",
            "decimalZero",
        }
        try:
            numbering_part = next(
                (p for p in self._doc.part.package.parts if "numbering" in p.partname),  # pyright: ignore[reportOptionalMemberAccess]
                None,
            )
            if numbering_part is None:
                return False

            ns = {"w": _W}
            root = numbering_part.element  # pyright: ignore[reportAttributeAccessIssue]

            num_el = root.find(f".//w:num[@w:numId='{numId}']", ns)
            if num_el is None:
                return False

            abs_id_el = num_el.find(".//w:abstractNumId", ns)
            if abs_id_el is None:
                return False
            abs_id = abs_id_el.get(_XML_VAL)

            abs_num = root.find(f".//w:abstractNum[@w:abstractNumId='{abs_id}']", ns)
            if abs_num is None:
                return False

            lvl = abs_num.find(f".//w:lvl[@w:ilvl='{ilvl}']", ns)
            if lvl is None:
                return False

            fmt_el = lvl.find(".//w:numFmt", ns)
            if fmt_el is None:
                return False

            return fmt_el.get(_XML_VAL) in _NUMBERED
        except Exception as exc:
            logger.debug(f"_is_numbered_list: {exc}")
            return False

    def _add_list_item(
        self, numid: int, ilevel: int, content: str, is_ordered: bool
    ) -> None:
        attr = "ordered" if is_ordered else "unordered"
        item: Block = {"type": "text", "content": content}

        # New list sequence (different numid or no active list)
        if self._pre_num_id == -1 or self._pre_num_id != numid:
            if self._pre_num_id != -1:
                self._close_list()
            list_block: Block = {
                "type": "list",
                "attribute": attr,
                "ilevel": ilevel,
                "content": [item],
            }
            self._blocks.append(list_block)
            self._list_stack = [list_block]
            self._pre_num_id = numid
            self._pre_ilevel = ilevel
            return

        # Deeper nesting → open a child list block
        if ilevel > self._pre_ilevel:
            child: Block = {
                "type": "list",
                "attribute": attr,
                "ilevel": ilevel,
                "content": [item],
            }
            self._list_stack[-1]["content"].append(child)
            self._list_stack.append(child)
            self._pre_ilevel = ilevel
            return

        # Shallower nesting → pop back to the matching level
        if ilevel < self._pre_ilevel:
            while len(self._list_stack) > 1 and self._list_stack[-1]["ilevel"] > ilevel:
                self._list_stack.pop()
            self._list_stack[-1]["content"].append(item)
            self._pre_ilevel = ilevel
            return

        # Same level
        self._list_stack[-1]["content"].append(item)

    def _close_list(self) -> None:
        self._pre_num_id = -1
        self._pre_ilevel = -1
        self._list_stack = []

    def _get_styles_root(self) -> Any | None:
        if self._styles_root is not None:
            return self._styles_root
        try:
            part = next(
                (
                    p
                    for p in self._doc.part.package.parts # pyright: ignore[reportOptionalMemberAccess]
                    if p.partname.endswith("styles.xml")
                ),  # pyright: ignore[reportOptionalMemberAccess]
                None,
            )
            if part is not None:
                self._styles_root = part.element  # pyright: ignore[reportAttributeAccessIssue]
        except Exception as exc:
            logger.debug(f"_get_styles_root: {exc}")
        return self._styles_root

    def _load_footnotes(self) -> dict[int, str]:
        """Lazily parse word/footnotes.xml → {footnote_id: plain_text}."""
        if self._footnotes is not None:
            return self._footnotes
        self._footnotes = {}
        try:
            part = next(
                (
                    p
                    for p in self._doc.part.package.parts
                    if p.partname.endswith("footnotes.xml")
                ),  # pyright: ignore[reportOptionalMemberAccess]
                None,
            )
            if part is None:
                return self._footnotes
            # FootnotesPart is a generic Part (no .element); parse blob directly.
            root = etree.fromstring(part.blob)  # pyright: ignore[reportAttributeAccessIssue]
            ns = {"w": _W}
            for fn_el in root.findall("w:footnote", ns):
                fn_id_str = fn_el.get(f"{{{_W}}}id")
                if fn_id_str is None:
                    continue
                fn_id = int(fn_id_str)
                if fn_id <= 0:  # skip separator (-1) and continuation (0) markers
                    continue
                text = "".join(
                    t.text for t in fn_el.iter(f"{{{_W}}}t") if t.text
                ).strip()
                if text:
                    self._footnotes[fn_id] = text
        except Exception as exc:
            logger.debug(f"_load_footnotes: {exc}")
        return self._footnotes

    def _resolve_heading_numid(self, element: Any) -> int | None:
        """
        Return the numId for a heading paragraph, or None if not auto-numbered.

        Checks the paragraph's own <w:pPr>/<w:numPr> first, then walks the
        style inheritance chain (needed when numPr lives in styles.xml rather
        than in the paragraph itself, which is the common case in Word).
        """
        ns = {"w": _W}

        # 1. Paragraph-level override
        numpr = element.find(f"{{{_W}}}pPr/{{{_W}}}numPr")
        if numpr is not None:
            numid_el = numpr.find(f"{{{_W}}}numId")
            if numid_el is not None:
                val = int(numid_el.get(_XML_VAL, 0))
                return None if val == 0 else val  # val=0 means "suppress"

        # 2. Style inheritance chain
        pstyle = element.find(f"{{{_W}}}pPr/{{{_W}}}pStyle")
        if pstyle is None:
            return None
        style_id = pstyle.get(_XML_VAL)
        if not style_id:
            return None

        root = self._get_styles_root()
        if root is None:
            return None

        visited: set[str] = set()
        current = style_id
        while current and current not in visited:
            visited.add(current)
            style_el = root.find(f".//w:style[@w:styleId='{current}']", ns)
            if style_el is None:
                break
            numpr = style_el.find(".//w:numPr", ns)
            if numpr is not None:
                numid_el = numpr.find("w:numId", ns)
                if numid_el is not None:
                    val = int(numid_el.get(_XML_VAL, 0))
                    return None if val == 0 else val
            based_on = style_el.find("w:basedOn", ns)
            current = based_on.get(_XML_VAL) if based_on is not None else None

        return None

    def _load_heading_formats(self, num_id: int) -> tuple[list[str], list[int]]:
        """Load (format_strings, start_values) for each level of numId from numbering.xml."""
        try:
            numbering_part = next(
                (p for p in self._doc.part.package.parts if "numbering" in p.partname),  # pyright: ignore[reportOptionalMemberAccess]
                None,
            )
            if numbering_part is None:
                return [], []
            ns = {"w": _W}
            root = numbering_part.element  # pyright: ignore[reportAttributeAccessIssue]

            num_el = root.find(f".//w:num[@w:numId='{num_id}']", ns)
            if num_el is None:
                return [], []
            abs_id_el = num_el.find(".//w:abstractNumId", ns)
            if abs_id_el is None:
                return [], []
            abs_id = abs_id_el.get(_XML_VAL)

            abs_num = root.find(f".//w:abstractNum[@w:abstractNumId='{abs_id}']", ns)
            if abs_num is None:
                return [], []

            fmts: list[str] = []
            starts: list[int] = []
            for lvl_el in sorted(
                abs_num.findall("w:lvl", ns),
                key=lambda e: int(e.get(f"{{{_W}}}ilvl", 0)),
            ):
                lvl_text = lvl_el.find("w:lvlText", ns)
                fmts.append(lvl_text.get(_XML_VAL, "") if lvl_text is not None else "")
                start_el = lvl_el.find("w:start", ns)
                starts.append(
                    int(start_el.get(_XML_VAL, 1)) if start_el is not None else 1
                )
            return fmts, starts
        except Exception as exc:
            logger.debug(f"_load_heading_formats: {exc}")
            return [], []

    def _compute_heading_number(self, num_id: int, h_level: int) -> str:
        """Return the auto-number string (e.g. '1.2.3') for a numbered heading."""
        if self._h_numId is None:
            self._h_fmts, self._h_starts = self._load_heading_formats(num_id)
            self._h_numId = num_id
            for i, s in enumerate(self._h_starts[:9]):
                self._h_counters[i] = s - 1

        idx = h_level - 1
        self._h_counters[idx] += 1
        for i in range(idx + 1, 9):
            reset_to = self._h_starts[i] - 1 if i < len(self._h_starts) else 0
            self._h_counters[i] = reset_to

        if idx < len(self._h_fmts):
            fmt = self._h_fmts[idx]
            for i in range(9, 0, -1):
                fmt = fmt.replace(f"%{i}", str(self._h_counters[i - 1]))
            return fmt

        return ".".join(str(self._h_counters[i]) for i in range(h_level))

    def _get_heading_level(self, paragraph: Paragraph) -> int | None:
        """
        Walk the paragraph's style inheritance chain.
        Return a 1-based heading level, or None if the paragraph is not a heading.
        """
        if paragraph.style is None:
            return None

        def _extract(s: str) -> int | None:
            s_lower = s.lower().strip()
            if s_lower == "title":
                return 1
            if "heading" in s_lower:
                m = re.search(r"\d+$", s.strip())
                return int(m.group()) if m else 2
            return None

        style = paragraph.style
        while style is not None:
            for val in (style.name or "", style.style_id or ""):
                level = _extract(val)
                if level is not None:
                    return level
            style = getattr(style, "base_style", None)
        return None

    def _is_caption(self, element: Any) -> bool:
        """Detect Word captions: they contain a SEQ field instruction."""
        for instr in element.findall(f".//{{{_W}}}instrText"):
            if instr.text and "SEQ" in instr.text:
                return True
        return False

    # ------------------------------------------------------------------
    # Headers / Footers
    # ------------------------------------------------------------------

    def _add_headers_footers(self) -> None:
        """Append header and footer text blocks for each document section."""
        doc = self._doc
        if doc is None:
            return

        odd_even = doc.settings.odd_and_even_pages_header_footer
        for section in doc.sections:
            hdrs = [section.header]
            if odd_even:
                hdrs.append(section.even_page_header)
            if section.different_first_page_header_footer:
                hdrs.append(section.first_page_header)

            seen_h: set[str] = set()
            for hdr in hdrs:
                text = " ".join(
                    p.text.strip() for p in hdr.paragraphs if p.text.strip()
                )
                if text and not text.isdigit() and text not in seen_h:
                    seen_h.add(text)
                    self._blocks.append({"type": "header", "content": text})

            ftrs = [section.footer]
            if odd_even:
                ftrs.append(section.even_page_footer)
            if section.different_first_page_header_footer:
                ftrs.append(section.first_page_footer)

            seen_f: set[str] = set()
            for ftr in ftrs:
                text = " ".join(
                    p.text.strip() for p in ftr.paragraphs if p.text.strip()
                )
                if text and not text.isdigit() and text not in seen_f:
                    seen_f.add(text)
                    self._blocks.append({"type": "footer", "content": text})
