"""
Production-grade DOCX parser for Comet-RAG.

Block types emitted
-------------------
text      — body paragraph            {type, content, style}
heading   — heading paragraph         {type, content, level, is_numbered}
list      — ordered / unordered list  {type, attribute, ilevel, content: [Block, …]}
table     — table block               {type, content, rows, row_count, col_count}
image     — embedded image            {type, content, format, name, alt_text, width_px, height_px}
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

# ---------------------------------------------------------------------------
# XML namespace constants
# ---------------------------------------------------------------------------
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
        return f"[{inner}]({url_esc})"

    return f"{prefix}{inner}{suffix}"


def _replace_outside_eq(text: str, old: str, new: str) -> str:
    """Replace *old* → *new* only in non-equation segments of *text*."""
    parts = re.split(r"(<eq>.*?</eq>)", text, flags=re.DOTALL)
    result = []
    for part in parts:
        if part.startswith("<eq>") and part.endswith("</eq>"):
            result.append(part)
        else:
            result.append(part.replace(old, new, 1))
    return "".join(result)


def _table_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    header, *body = rows
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

    def __init__(self) -> None:
        self._doc: DocumentObject | None = None
        self._doc_part: Any = None
        self._blocks: list[Block] = []
        # Active list tracking
        self._pre_num_id: int = -1
        self._pre_ilevel: int = -1
        self._list_stack: list[Block] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse(self, document: DocxDocument) -> DocxParsedContent:
        self._doc = document.elements
        self._doc_part = self._doc.part
        self._blocks = []
        self._pre_num_id = -1
        self._pre_ilevel = -1
        self._list_stack = []

        self._walk(self._doc.element.body)
        self._add_headers_footers()

        return DocxParsedContent(blocks=self._blocks, metadata=document.metadata)

    # ------------------------------------------------------------------
    # Document walk
    # ------------------------------------------------------------------

    def _walk(self, container: Any) -> None:
        for i, child in enumerate(container):
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

    # ------------------------------------------------------------------
    # Paragraph dispatch
    # ------------------------------------------------------------------

    def _handle_paragraph(self, element: Any) -> None:  # noqa: C901 (complexity ok here)
        paragraph = Paragraph(element, self._doc) # pyright: ignore[reportArgumentType]
        plain_text = self._get_paragraph_text(paragraph)
        text_with_eq, equations = self._handle_equations(element, plain_text)
        p_elems = self._get_paragraph_elements(paragraph)
        rich_text = self._build_rich_text(p_elems, text_with_eq, equations)

        style_name = (paragraph.style.name if paragraph.style else None) or "Normal"
        h_level = self._get_heading_level(paragraph)
        numid, ilevel = self._get_numId_ilvl(paragraph)
        if numid == 0:
            numid = None

        # Extract images that live inside this paragraph element
        image_blocks = self._extract_images(element)

        # --- List item (non-heading) ---
        if numid is not None and ilevel is not None and h_level is None:
            if rich_text:
                self._add_list_item(
                    numid, ilevel, rich_text, self._is_numbered_list(numid, ilevel)
                )
            self._blocks.extend(image_blocks)
            return

        # All non-list branches close any active list first
        self._close_list()

        # --- Heading ---
        if h_level is not None:
            if rich_text:
                self._blocks.append(
                    {
                        "type": "heading",
                        "level": h_level,
                        "content": rich_text,
                        "is_numbered": "<w:numPr>" in element.xml,
                    }
                )

        # --- Standalone equation ---
        elif equations and not plain_text.strip():
            eq_content = re.sub(
                r"<eq>(.*?)</eq>", r"\1", text_with_eq or "", flags=re.DOTALL
            ).strip()
            if eq_content:
                self._blocks.append(
                    {"type": "equation", "content": f"$${eq_content}$$"}
                )

        # --- Caption (SEQ-field based detection) ---
        elif self._is_caption(element):
            if rich_text:
                self._blocks.append({"type": "caption", "content": rich_text})

        # --- Body text ---
        elif rich_text:
            self._blocks.append(
                {"type": "text", "content": rich_text, "style": style_name}
            )

        self._blocks.extend(image_blocks)

    # ------------------------------------------------------------------
    # Inner-content iterator
    # Handles inline w:sdt content controls and transparent wrappers that
    # python-docx's iter_inner_content() does not traverse.
    # ------------------------------------------------------------------

    def _iter_inner_content(
        self,
        paragraph: Paragraph,
        container: Any | None = None,
    ) -> Iterator[Run | Hyperlink]:
        if container is None:
            container = paragraph._element
        for child in container:
            tag = _qname(child)
            if tag == "r":
                yield Run(child, paragraph)
            elif tag == "hyperlink":
                yield Hyperlink(child, paragraph)
            elif tag == "sdt":
                sdt_content = child.find(f"{{{_W}}}sdtContent")
                if sdt_content is not None:
                    yield from self._iter_inner_content(paragraph, sdt_content)
            elif tag in _TRANSPARENT_INLINE:
                yield from self._iter_inner_content(paragraph, child)

    def _get_paragraph_text(self, paragraph: Paragraph) -> str:
        return "".join(c.text or "" for c in self._iter_inner_content(paragraph))

    # ------------------------------------------------------------------
    # Rich paragraph elements
    # ------------------------------------------------------------------

    def _get_paragraph_elements(self, paragraph: Paragraph) -> list[_PElem]:  # noqa: C901
        """
        Return (text, format, url) tuples for every visible run in the paragraph.

        Handles:
        - w:hyperlink wrappers (external URLs)
        - w:fldChar / w:instrText field-code hyperlinks
        - Hidden runs (w:vanish, w:webHidden)
        - Consecutive same-format runs are merged for compactness
        """
        elems: list[_PElem] = []

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
            # ---- Hyperlink element ----
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

            # ---- fldChar (field boundary marker) ----
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

            # ---- instrText: extract HYPERLINK url ----
            instr = c._element.find(f"{{{_W}}}instrText")
            if instr is not None:
                if _field_phase == "instr" and instr.text:
                    m = re.search(r'HYPERLINK\s+"([^"]+)"', instr.text)
                    if m:
                        _field_url = m.group(1)
                continue  # never displayable

            # ---- Skip non-display runs inside an instr block ----
            if _field_in and _field_phase == "instr":
                continue

            # ---- Accumulate field result display text ----
            if _field_in and _field_phase == "result":
                if c._element.find(f"{{{_W}}}t") is not None:
                    _field_text += c.text or ""
                    if _field_fmt is None:
                        _field_fmt = _get_run_fmt(c)
                continue

            # ---- Hidden run ----
            if _is_hidden_run(c):
                continue

            # ---- Normal run ----
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
        return elems

    def _build_rich_text(
        self,
        elems: list[_PElem],
        text_with_eq: str,
        equations: list[str],
    ) -> str:
        """
        Combine paragraph elements (with formatting / hyperlinks) into a final
        rich-text string. When equations are present they are spliced in from
        *text_with_eq* and converted to $…$ notation.
        """
        if not elems and not equations:
            return ""

        if not equations:
            return "".join(_apply_fmt_and_url(t, f, u) for t, f, u in elems)

        # Inject Markdown formatting into the equation-annotated text, then
        # convert <eq>…</eq> markers to $…$ inline notation.
        result = text_with_eq
        for text, fmt, url in elems:
            if not text:
                continue
            formatted = _apply_fmt_and_url(text, fmt, url)
            if formatted != text:
                result = _replace_outside_eq(result, text, formatted)

        result = re.sub(r"<eq>(.*?)</eq>", r"$\1$", result, flags=re.DOTALL)
        return result

    # ------------------------------------------------------------------
    # Equation handling
    # ------------------------------------------------------------------

    def _handle_equations(
        self, element: Any, paragraph_text: str
    ) -> tuple[str, list[str]]:
        """
        Scan *element* for OMML math nodes.

        Returns (text_with_eq_markers, [equation_markers]).
        When no equations are found, returns (paragraph_text, []).
        """
        only_texts: list[str] = []
        only_eqs: list[str] = []
        combined: list[str] = []

        for subt in element.iter():
            tag = _qname(subt)
            # Plain text node — exclude math namespace <m:t>
            if tag == "t" and f"{{{_M}}}" not in subt.tag:
                if isinstance(subt.text, str):
                    only_texts.append(subt.text)
                    combined.append(subt.text)
            # OMML equation (skip oMathPara container to avoid double-processing)
            elif "oMath" in subt.tag and "oMathPara" not in subt.tag:
                latex = self._convert_omath(subt)
                if latex:
                    marker = f"<eq>{latex}</eq>"
                    only_eqs.append(marker)
                    combined.append(marker)

        if not only_eqs:
            return paragraph_text, []

        # Sanity check: can we reconstruct the paragraph text from w:t nodes?
        concat_plain = re.sub(r"\s+", "", "".join(only_texts)).strip()
        concat_para = re.sub(r"\s+", "", paragraph_text).strip()
        if concat_plain != concat_para:
            return paragraph_text, []

        # Splice equation markers into the paragraph text, preserving whitespace
        output = paragraph_text
        pos = 0
        for fragment in combined:
            if not fragment:
                continue
            if fragment.startswith("<eq>"):
                output = output[:pos] + fragment + output[pos:]
                pos += len(fragment)
            else:
                idx = output[pos:].find(fragment)
                if idx >= 0:
                    pos += idx + len(fragment)

        return output, only_eqs

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

    # ------------------------------------------------------------------
    # Images
    # ------------------------------------------------------------------

    def _extract_images(self, para_element: Any) -> list[Block]:
        blocks: list[Block] = []
        for drawing in para_element.iter(qn("w:drawing")):
            docPr = drawing.find(f".//{qn('wp:docPr')}")
            name = docPr.get("name", "") if docPr is not None else ""
            alt_text = docPr.get("descr", "") if docPr is not None else ""

            extent = drawing.find(f".//{qn('wp:extent')}")
            width_px = (
                int(extent.get("cx", 0)) // _EMU_PER_PX if extent is not None else 0
            )
            height_px = (
                int(extent.get("cy", 0)) // _EMU_PER_PX if extent is not None else 0
            )

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
                    "width_px": width_px,
                    "height_px": height_px,
                }
            )
        return blocks

    # ------------------------------------------------------------------
    # Tables
    # ------------------------------------------------------------------

    def _handle_table(self, element: Any) -> None:
        from docx.table import Table

        table = Table(element, self._doc)  # pyright: ignore[reportArgumentType]
        raw_rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]

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

    # ------------------------------------------------------------------
    # List helpers
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Heading detection
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Caption detection
    # ------------------------------------------------------------------

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
