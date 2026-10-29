from __future__ import annotations

import io
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal
from zipfile import BadZipFile, ZipFile

from lxml import etree

from comet_rag._async import run_sync
from comet_rag.documents.schemas import (
    Block,
    FormulaContent,
    InlineCode,
    InlineFormula,
    Location,
    ParsedDocument,
    ParseIssue,
    ParseReport,
    ParseResult,
    ParsingInfo,
    RichText,
    TableCell,
    TableContent,
    TextRun,
)
from comet_rag.sources.types import LocalResource

from ..archive import ArchiveLimits
from ..base import _read_resource
from .package import MC, WP, A, M, Package, R, V, integer, q, val
from .styles import Numbering, Styles, on_off

_TYPE = Literal["paragraph", "heading", "list_item", "code"]
_TRANSPARENT = frozenset(q(name) for name in ("bdo", "customXml", "dir", "smartTag"))
_MARKS = frozenset(
    q(name)
    for name in (
        "bookmarkStart",
        "bookmarkEnd",
        "proofErr",
        "permStart",
        "permEnd",
        "commentRangeStart",
        "commentRangeEnd",
        "moveFromRangeStart",
        "moveFromRangeEnd",
        "moveToRangeStart",
        "moveToRangeEnd",
        "lastRenderedPageBreak",
    )
)


@dataclass(slots=True)
class _Image:
    asset_id: str
    alt_text: str | None
    element: etree._Element
    layout: dict[str, object]


@dataclass(slots=True)
class _Field:
    instruction: str = ""
    result: bool = False
    link: str | None = None


type _Inline = TextRun | InlineCode | InlineFormula
type _Token = _Inline | _Image


class AsyncDocxParser:
    def __init__(
        self,
        *,
        archive_limits: ArchiveLimits | None = None,
        max_bytes: int = 256 * 1024 * 1024,
        max_table_cells: int = 10_000,
    ) -> None:
        for name, value in (
            ("max_bytes", max_bytes),
            ("max_table_cells", max_table_cells),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if archive_limits is not None and not isinstance(archive_limits, ArchiveLimits):
            raise TypeError("archive_limits must be ArchiveLimits")
        self._limits = archive_limits or ArchiveLimits()
        self._max_bytes = max_bytes
        self._max_table_cells = max_table_cells

    async def parse(self, resource: LocalResource, *, output_dir: Path) -> ParseResult:
        return await run_sync(self._parse, resource, output_dir)

    def _parse(self, resource: LocalResource, output_dir: Path) -> ParseResult:
        data = _read_resource(resource, self._max_bytes)
        try:
            with ZipFile(io.BytesIO(data)) as archive:
                package = Package(archive, output_dir, self._limits)
                return _Mapper(package, self._max_table_cells).parse(resource)
        except (BadZipFile, etree.XMLSyntaxError) as exc:
            raise ValueError(
                f"invalid native DOCX resource: {resource.source.source_id}"
            ) from exc


class _Mapper:
    def __init__(self, package: Package, max_table_cells: int) -> None:
        self.package = package
        self.styles = Styles(package)
        self.numbering = Numbering(package)
        self.max_table_cells = max_table_cells
        self.blocks: list[Block] = []
        self.issues: list[ParseIssue] = []
        self._serial = 0
        self._table_cells = 0
        self._fields: list[_Field] = []

    def identifier(self) -> str:
        value = f"docx_{self._serial}"
        self._serial += 1
        return value

    def location(self, element: etree._Element, part: str) -> tuple[Location, ...]:
        return (
            Location(element_id=f"{part}:{element.getroottree().getpath(element)}"),
        )

    def issue(
        self, code: str, message: str, element: etree._Element, part: str
    ) -> None:
        self.issues.append(
            ParseIssue(code, message, locations=self.location(element, part))
        )

    def parse(self, resource: LocalResource) -> ParseResult:
        main = self.package.main
        root = self.package.xml(main, q("document"))
        bodies = root.findall(q("body"))
        if len(bodies) != 1:
            raise ValueError("DOCX must have one document body")
        for child in root:
            if child.tag != q("body"):
                self.issue(
                    "unsupported_element",
                    f"Document element {child.tag!r} is not mapped.",
                    child,
                    main,
                )
        self.walk(bodies[0], main)
        seen: set[str] = set()
        for section in root.iter(q("sectPr")):
            for reference in section:
                if reference.tag not in {q("headerReference"), q("footerReference")}:
                    continue
                role = "header" if reference.tag == q("headerReference") else "footer"
                relation = self.package.related(main, reference.get(q("id", R)), role)
                if relation.external:
                    raise ValueError("DOCX header/footer must be an internal part")
                if relation.target not in seen:
                    seen.add(relation.target)
                    container = self.package.xml(
                        relation.target, q("hdr" if role == "header" else "ftr")
                    )
                    self.walk(container, relation.target, role=role)
        for relation in self.package.relationships(main).values():
            if relation.kind in {f"{R}/footnotes", f"{R}/endnotes", f"{R}/comments"}:
                if relation.external:
                    raise ValueError("external DOCX annotation part")
                container = self.package.xml(relation.target)
                if any(
                    node.tag
                    in {q("t"), q("oMath", M), q("drawing"), q("pict"), q("tbl")}
                    for node in container.iter()
                ):
                    self.issue(
                        "unsupported_annotations",
                        "Footnotes, endnotes or comments are not included in document blocks.",
                        container,
                        relation.target,
                    )
        return ParseResult(
            ParsedDocument(
                source=resource.source,
                parsing=ParsingInfo(
                    "native-docx", version="1", config={"formula_format": "omml"}
                ),
                blocks=tuple(self.blocks),
                assets=tuple(self.package.assets.values()),
            ),
            ParseReport("partial" if self.issues else "complete", tuple(self.issues)),
        )

    def walk(
        self,
        container: etree._Element,
        part: str,
        *,
        parent: str | None = None,
        role: str | None = None,
        depth: int = 0,
    ) -> tuple[str, ...]:
        if depth > 64:
            raise ValueError("DOCX content nesting exceeds 64 levels")
        if depth == 0:
            self._fields.clear()
        members: list[str] = []
        for child in container:
            if child.tag == q("p"):
                members.extend(self.paragraph(child, part, parent, role))
            elif child.tag == q("tbl"):
                members.append(self.table(child, part, parent, role, depth))
            elif child.tag in _MARKS or child.tag in {
                q("sectPr"),
                q("tcPr"),
                q("sdtPr"),
                q("sdtEndPr"),
            }:
                continue
            elif child.tag == q("sdt"):
                content = child.find(q("sdtContent"))
                if content is None:
                    raise ValueError("DOCX content control has no sdtContent")
                members.extend(
                    self.walk(content, part, parent=parent, role=role, depth=depth + 1)
                )
            else:
                if child.tag not in _TRANSPARENT:
                    self.issue(
                        "unsupported_element",
                        f"Content wrapper {child.tag!r} is not fully mapped; available descendants are retained.",
                        child,
                        part,
                    )
                members.extend(
                    self.walk(child, part, parent=parent, role=role, depth=depth + 1)
                )
        if depth == 0 and self._fields:
            self.issue(
                "unclosed_field",
                "Unclosed field display text is retained without evaluating its instruction.",
                container,
                part,
            )
            self._fields.clear()
        return tuple(members)

    def paragraph(
        self, element: etree._Element, part: str, parent: str | None, role: str | None
    ) -> tuple[str, ...]:
        kind: _TYPE = "paragraph"
        level = self.styles.heading(element)
        marker = None
        layout: dict[str, object] = {}
        style_names = self.styles.names(self.styles.paragraph(element))
        if level is not None:
            kind = "heading"
        else:
            num = self.styles.property(element, f"{q('numPr')}/{q('numId')}")
            if num is not None and (nid := integer(val(num), "numbering id")) != 0:
                kind = "list_item"
                ilvl = self.styles.property(element, f"{q('numPr')}/{q('ilvl')}")
                level = integer(val(ilvl, "0"), "list level")
                if level > 8:
                    raise ValueError("DOCX list level exceeds 8")
                marker, layout = self.numbering.marker(
                    nid,
                    level,
                    lambda code, message: self.issue(code, message, element, part),
                )
            elif self.styles.is_code(element):
                kind = "code"
        indent = self.styles.property(element, q("ind"))
        if indent is not None:
            layout["indent_twips"] = {
                etree.QName(key).localname: value
                for key, value in indent.attrib.items()
            }
        if any(name.lower() == "caption" for name in style_names) or any(
            re.match(r"\s*SEQ\b", node.text or "", re.IGNORECASE)
            for node in element.iter(q("instrText"))
        ):
            role = role or "caption"
        metadata: dict[str, object] = {"styles": style_names} if style_names else {}
        if kind == "heading" and self.styles.property(element, q("numPr")) is not None:
            self.issue(
                "heading_numbering",
                "Heading text is retained; automatic heading numbering is not evaluated.",
                element,
                part,
            )
        tokens = self.inlines(element, element, part)
        members: list[str] = []
        inlines: list[_Inline] = []

        def flush(*, empty: bool = False) -> None:
            if not inlines and not empty:
                return
            block_id = self.identifier()
            continuation = kind == "list_item" and bool(members)
            if kind == "code" and all(
                isinstance(item, (TextRun, InlineCode)) for item in inlines
            ):
                block = Block(
                    block_id,
                    "code",
                    code="".join(
                        item.text
                        for item in inlines
                        if isinstance(item, (TextRun, InlineCode))
                    ),
                )
            elif (
                kind == "paragraph"
                and len(inlines) == 1
                and isinstance(inlines[0], InlineFormula)
            ):
                block = Block(block_id, "formula", formula=inlines[0].formula)
            else:
                if kind == "code":
                    self.issue(
                        "code_formula_fallback",
                        "Code paragraph contains a formula and is retained as rich text.",
                        element,
                        part,
                    )
                block = Block(
                    block_id,
                    "paragraph" if kind == "code" or continuation else kind,
                    rich_text=RichText(tuple(inlines)),
                    level=None if continuation else level,
                    marker=None if continuation else marker,
                )
            block = replace(
                block,
                parent_block_id=parent,
                role=role,
                locations=self.location(element, part),
                layout=layout,
                metadata=metadata,
            )
            self.blocks.append(block)
            members.append(block_id)
            inlines.clear()

        for token in tokens:
            if isinstance(token, _Image):
                # 图片开头的列表项仍需一个承载编号的首片段。
                flush(empty=kind == "list_item" and not members)
                block_id = self.identifier()
                self.blocks.append(
                    Block(
                        block_id,
                        "image",
                        asset_id=token.asset_id,
                        alt_text=token.alt_text,
                        parent_block_id=parent,
                        role=role,
                        layout=token.layout,
                        locations=self.location(token.element, part),
                    )
                )
                members.append(block_id)
            else:
                inlines.append(token)
        flush(empty=not members and not inlines)
        return tuple(members)

    def inlines(
        self, container: etree._Element, paragraph: etree._Element, part: str
    ) -> list[_Token]:
        tokens: list[_Token] = []
        fields = self._fields

        def field_link(instruction: str, element: etree._Element) -> str | None:
            match = re.match(
                r'\s*HYPERLINK\s+(?:"([^"]*)"|(\S+))', instruction, re.IGNORECASE
            )
            if match:
                target = match[1] if match[1] is not None else match[2]
                anchor = re.search(r'\\l\s+"([^"]+)"', instruction)
                if target == "\\l":
                    return f"#{anchor[1]}" if anchor else None
                return f"{target}#{anchor[1]}" if anchor else target
            self.issue(
                "unsupported_field",
                "Field instruction is not evaluated; its stored display result is retained.",
                element,
                part,
            )
            return None

        def visit(
            node: etree._Element,
            link: str | None = None,
            run: etree._Element | None = None,
            depth: int = 0,
        ) -> None:
            if depth > 64:
                raise ValueError("DOCX inline nesting exceeds 64 levels")
            tag = node.tag
            if not isinstance(tag, str):
                return
            if tag in {q("pPr"), q("rPr"), q("sdtPr"), q("sdtEndPr")} or tag in _MARKS:
                return
            if tag == q("r"):
                rpr = node.find(q("rPr"))
                if rpr is not None and any(
                    on_off(hidden)
                    for name in ("vanish", "webHidden")
                    if (hidden := rpr.find(q(name))) is not None
                ):
                    self.issue(
                        "hidden_text",
                        "Hidden run content is retained; visibility is not represented by TextStyle.",
                        node,
                        part,
                    )
                for child in node:
                    visit(child, link, node, depth + 1)
                return
            if tag == q("fldChar"):
                boundary = node.get(q("fldCharType"))
                if boundary == "begin":
                    fields.append(_Field())
                elif boundary == "separate" and fields:
                    fields[-1].result = True
                    fields[-1].link = field_link(fields[-1].instruction, node)
                elif boundary == "end" and fields:
                    fields.pop()
                else:
                    raise ValueError("unbalanced DOCX field boundary")
                return
            if tag == q("instrText"):
                if not fields:
                    self.issue(
                        "unsupported_field",
                        "Field instruction has no field boundary.",
                        node,
                        part,
                    )
                else:
                    fields[-1].instruction += node.text or ""
                return
            if fields and not fields[-1].result:
                return
            if tag == q("hyperlink"):
                rid = node.get(q("id", R))
                target = link
                if rid:
                    target = self.package.related(part, rid, "hyperlink").target
                anchor = node.get(q("anchor"))
                if anchor:
                    target = f"{target or ''}#{anchor}"
                if target is None:
                    raise ValueError("DOCX hyperlink has no target")
                for child in node:
                    visit(child, target, run, depth + 1)
                return
            if tag == q("fldSimple"):
                target = field_link(node.get(q("instr"), ""), node)
                for child in node:
                    visit(child, target or link, run, depth + 1)
                return
            if tag == q("oMath", M):
                # shortcut: 原样保存 OMML；有逐构造可靠性验证后再增加公式转译。
                formula = FormulaContent(
                    etree.tostring(node, encoding="unicode", with_tail=False), "omml"
                )
                tokens.append(InlineFormula(formula))
                self.issue(
                    "omml_preserved",
                    "Formula is retained as native OMML; no LaTeX translation is claimed.",
                    node,
                    part,
                )
                return
            if tag == q("oMathPara", M):
                for child in node:
                    if child.tag != q("oMathParaPr", M):
                        visit(child, link, run, depth + 1)
                return
            if tag in {q("drawing"), q("pict")}:
                tokens.extend(self.images(node, part))
                return
            text: str | None = None
            if tag in {q("t"), q("delText")}:
                text = node.text or ""
            elif tag in {q("tab"), q("ptab")}:
                text = "\t"
                if tag == q("ptab"):
                    self.issue(
                        "unsupported_positioned_tab",
                        "Positioned tab is retained as a tab without its alignment rule.",
                        node,
                        part,
                    )
            elif tag in {q("br"), q("cr")}:
                text = "\n"
                break_type = node.get(q("type"), "textWrapping")
                if break_type != "textWrapping":
                    self.issue(
                        "layout_break",
                        f"Native {break_type} break is retained as a newline; no page positions are inferred.",
                        node,
                        part,
                    )
            elif tag == q("noBreakHyphen"):
                text = "\u2011"
            elif tag == q("softHyphen"):
                text = "\u00ad"
            if text is not None:
                if run is None:
                    self.issue(
                        "unsupported_text_wrapper",
                        "Text outside a native run is retained without inherited run style.",
                        node,
                        part,
                    )
                style = (
                    self.styles.run_style(paragraph, run) if run is not None else None
                )
                active_link = link or next(
                    (
                        field.link
                        for field in reversed(fields)
                        if field.result and field.link
                    ),
                    None,
                )
                if (
                    run is not None
                    and self.styles.is_code(paragraph, run)
                    and not active_link
                ):
                    tokens.append(InlineCode(text))
                else:
                    tokens.append(TextRun(text, style, active_link))
                return
            if tag == q("sdt"):
                content = node.find(q("sdtContent"))
                if content is None:
                    raise ValueError("DOCX content control has no sdtContent")
                for child in content:
                    visit(child, link, run, depth + 1)
                return
            if tag == q("AlternateContent", MC):
                fallback = node.find(q("Fallback", MC))
                choice = node.find(q("Choice", MC))
                selected = fallback if fallback is not None else choice
                self.issue(
                    "alternate_content",
                    "One compatibility branch is retained; alternate representation fidelity is unknown.",
                    node,
                    part,
                )
                if selected is not None:
                    for child in selected:
                        visit(child, link, run, depth + 1)
                return
            if tag not in _TRANSPARENT:
                self.issue(
                    "unsupported_element",
                    f"Inline element {tag!r} is not fully mapped; available descendants are retained.",
                    node,
                    part,
                )
            for child in node:
                visit(child, link, run, depth + 1)
            if node.text and not len(node):
                tokens.append(TextRun(node.text, link=link))

        for child in container:
            visit(child)
        return tokens

    def images(self, element: etree._Element, part: str) -> list[_Image]:
        result: list[_Image] = []
        refs = [
            (node, node.get(q("embed", R)) or node.get(q("link", R)))
            for node in element.iter(q("blip", A))
        ]
        refs += [
            (node, node.get(q("id", R))) for node in element.iter(q("imagedata", V))
        ]
        properties = next(element.iter(q("docPr", WP)), None)
        alt_text = (
            (properties.get("descr") or properties.get("title"))
            if properties is not None
            else None
        )
        extent = next(element.iter(q("extent", WP)), None)
        layout: dict[str, object] = {}
        if extent is not None:
            layout["width_emu"] = integer(extent.get("cx"), "drawing width", minimum=1)
            layout["height_emu"] = integer(
                extent.get("cy"), "drawing height", minimum=1
            )
        for node, rid in refs:
            relation = self.package.related(part, rid, "image")
            if relation.external:
                self.issue(
                    "external_image",
                    "External image is not fetched and has no persisted local asset.",
                    node,
                    part,
                )
                continue
            asset = self.package.image(relation)
            result.append(_Image(asset.asset_id, alt_text, node, layout))
        unsupported = {
            q("chart", "http://schemas.openxmlformats.org/drawingml/2006/chart"),
            q("txbxContent"),
            q("graphicData", A),
        }
        for node in element.iter():
            if node.tag in unsupported and (
                node.tag != q("graphicData", A)
                or node.get("uri")
                != "http://schemas.openxmlformats.org/drawingml/2006/picture"
            ):
                self.issue(
                    "unsupported_drawing",
                    f"Drawing content {node.tag!r} has no native block mapping.",
                    node,
                    part,
                )
        if not refs:
            self.issue(
                "unsupported_drawing",
                "Drawing has no embedded image representation.",
                element,
                part,
            )
        return result

    def table(
        self,
        element: etree._Element,
        part: str,
        parent: str | None,
        role: str | None,
        depth: int,
    ) -> str:
        grid = element.find(q("tblGrid"))
        if grid is None:
            raise ValueError("DOCX table is missing tblGrid")
        columns = len(grid.findall(q("gridCol")))
        rows = element.findall(q("tr"))
        if not columns:
            raise ValueError("DOCX table has an empty layout grid")
        self._table_cells += max(1, len(rows)) * columns
        if self._table_cells > self.max_table_cells:
            raise ValueError(f"DOCX tables exceed {self.max_table_cells} layout cells")
        block_id = self.identifier()
        insertion = len(self.blocks)
        cells: list[TableCell] = []
        active: dict[tuple[int, int], int] = {}
        for row_number, row in enumerate(rows):
            row_properties = row.find(q("trPr"))
            before = integer(
                val(
                    row_properties.find(q("gridBefore"))
                    if row_properties is not None
                    else None,
                    "0",
                ),
                "gridBefore",
            )
            after = integer(
                val(
                    row_properties.find(q("gridAfter"))
                    if row_properties is not None
                    else None,
                    "0",
                ),
                "gridAfter",
            )
            column = before
            if before + after > columns:
                raise ValueError("DOCX table gaps exceed layout grid")
            header_node = (
                row_properties.find(q("tblHeader"))
                if row_properties is not None
                else None
            )
            header = on_off(header_node) if header_node is not None else None
            next_active: dict[tuple[int, int], int] = {}
            for child in row:
                if child.tag not in {q("trPr"), q("tc")}:
                    self.issue(
                        "unsupported_table_element",
                        f"Table row content {child.tag!r} has no cell mapping.",
                        child,
                        part,
                    )
            for cell in row.findall(q("tc")):
                properties = cell.find(q("tcPr"))
                span = integer(
                    val(
                        properties.find(q("gridSpan"))
                        if properties is not None
                        else None,
                        "1",
                    ),
                    "gridSpan",
                    minimum=1,
                )
                if column + span + after > columns:
                    raise ValueError("DOCX cell span exceeds layout grid")
                if properties is not None and properties.find(q("hMerge")) is not None:
                    self.issue(
                        "unsupported_horizontal_merge",
                        "Legacy hMerge is retained as separate source cells; merge topology is unknown.",
                        cell,
                        part,
                    )
                merge = properties.find(q("vMerge")) if properties is not None else None
                mode = val(merge, "continue") if merge is not None else None
                if mode not in {None, "continue", "restart"}:
                    raise ValueError("invalid DOCX vertical merge value")
                key = (column, span)
                start = len(self.blocks)
                members = self.walk(
                    cell, part, parent=block_id, role=role, depth=depth + 1
                )
                if mode == "continue":
                    if key not in active:
                        raise ValueError(
                            "DOCX vertical merge has no matching previous cell"
                        )
                    index = active[key]
                    original = cells[index]
                    member_ids = set(members)
                    meaningful = tuple(
                        block.block_id
                        for block in self.blocks[start:]
                        if block.block_id in member_ids
                        and (block.rich_text is None or bool(block.rich_text.inlines))
                    )
                    if meaningful:
                        self.issue(
                            "merged_cell_content",
                            "Vertical continuation cell contains content; it is retained in the merged cell.",
                            cell,
                            part,
                        )
                    # 空续格段落不另占单元格；只筛选本格新建块，避免反复扫描整个文档。
                    discard = member_ids - set(meaningful)
                    self.blocks[start:] = [
                        block
                        for block in self.blocks[start:]
                        if block.block_id not in discard
                    ]
                    cells[index] = replace(
                        original,
                        row_span=original.row_span + 1,
                        block_ids=original.block_ids + meaningful,
                        locations=original.locations + self.location(cell, part),
                    )
                    next_active[key] = index
                else:
                    cells.append(
                        TableCell(
                            row_number,
                            column,
                            members,
                            column_span=span,
                            header=header,
                            locations=self.location(cell, part),
                        )
                    )
                    if mode == "restart":
                        next_active[key] = len(cells) - 1
                column += span
            if column + after != columns:
                raise ValueError("DOCX table row width disagrees with layout grid")
            active = next_active
        for child in element:
            if child.tag not in {q("tblPr"), q("tblGrid"), q("tr")}:
                self.issue(
                    "unsupported_table_element",
                    f"Table content {child.tag!r} has no cell mapping.",
                    child,
                    part,
                )
        self.blocks.insert(
            insertion,
            Block(
                block_id,
                "table",
                table=TableContent(cells=tuple(cells), rows=len(rows), columns=columns),
                parent_block_id=parent,
                role=role,
                locations=self.location(element, part),
            ),
        )
        return block_id
