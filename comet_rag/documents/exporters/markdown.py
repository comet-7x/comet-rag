from __future__ import annotations

import re
from html import escape
from urllib.parse import quote, urlsplit

from .._tables import table_dimensions, validate_table_expansion
from ..schemas import (
    Asset,
    Block,
    FormulaContent,
    InlineCode,
    InlineFormula,
    ParsedDocument,
    TextRun,
)

_IMAGE_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/bmp",
    "image/tiff",
    "image/avif",
}


def _escape_text(value: str, *, html: bool) -> str:
    value = escape(value, quote=False)
    if html:
        # Entities also keep Markdown from reinterpreting text inside inline HTML.
        return re.sub(
            r"[\\`*_{}\[\]()#+.!|~$\-\r\n\t]",
            lambda match: f"&#{ord(match[0])};",
            value,
        )
    return re.sub(r"([\\`*_{}\[\]()#+.!|~$-])", r"\\\1", value)


def _code(value: str) -> str:
    # Markdown code spans normalize whitespace; HTML preserves the original string.
    return (
        f'<code style="white-space: pre-wrap">{_escape_text(value, html=True)}</code>'
    )


def _fence(value: str, language: str = "") -> str:
    delimiter = "`" * max(
        3, 1 + max((len(match[0]) for match in re.finditer(r"`+", value)), default=0)
    )
    ending = "" if value.endswith("\n") else "\n"
    return f"{delimiter}{language}\n{value}{ending}{delimiter}"


def _destination(value: str, *, path: bool = False) -> str | None:
    if "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    if path:
        target = quote(value, safe="/~-._")
        return "/%2F" + target[2:] if target.startswith("//") else target
    parsed = urlsplit(value)
    if parsed.scheme:
        if (
            parsed.scheme.lower() not in {"http", "https", "mailto"}
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        if parsed.scheme.lower() in {"http", "https"} and not parsed.hostname:
            return None
    elif parsed.netloc or ":" in value.split("/", 1)[0]:
        return None
    return quote(value, safe="/:?#[]@!$&'+,;=%~-._" if parsed.scheme else "/?#=%~-._")


def _attribute(value: str) -> str:
    return (
        escape(value, quote=True)
        .replace("\r", "&#13;")
        .replace("\n", "&#10;")
        .replace("\t", "&#9;")
    )


def _run(run: TextRun, *, html: bool) -> str:
    style = run.style
    preserve = re.search(r"\s{2,}|[\r\n\t]|^\s|\s$", run.text) is not None
    as_html = html or style is not None or preserve
    value = _escape_text(run.text, html=as_html)
    if style is not None:
        for enabled, tag in (
            (style.bold, "strong"),
            (style.italic, "em"),
            (style.underline, "u"),
            (style.strike, "s"),
        ):
            if enabled:
                value = f"<{tag}>{value}</{tag}>"
        if style.script in {"superscript", "subscript"}:
            tag = "sup" if style.script == "superscript" else "sub"
            value = f"<{tag}>{value}</{tag}>"
    if style is not None or preserve:
        css = "white-space: pre-wrap"
        if style is not None and style.color is not None:
            css += f"; color: {style.color}"
        if style is not None and style.size_pt is not None:
            css += f"; font-size: {style.size_pt}pt"
        font = (
            f' data-font="{_attribute(style.font)}"'
            if style is not None and style.font is not None
            else ""
        )
        value = f'<span style="{css}"{font}>{value}</span>'
    if run.link is not None:
        target = _destination(run.link)
        if target is None:
            value += f" [link omitted: {_code(run.link)}]"
        elif as_html:
            value = f'<a href="{_attribute(target)}">{value}</a>'
        else:
            value = f"[{value}](<{target}>)"
    return value


def export_markdown(document: ParsedDocument, *, max_table_cells: int = 10_000) -> str:
    """从当前 blocks 派生 Markdown 数据，不提供 HTML 消毒或公式执行保证。

    链接只接受 HTTP(S)、mailto 和相对目标；已知栅格图片仅引用路径/URL，
    不读取文件或嵌入 bytes。HTML/OMML/MathML 保留源码，歧义 LaTeX 也回退源码。
    生成的 HTML 保留空白与单元格跨度，显示效果取决于调用方的 Markdown 渲染器。
    """
    if not isinstance(document, ParsedDocument):
        raise TypeError("export_markdown requires ParsedDocument")
    validate_table_expansion(document, max_table_cells)
    blocks = {block.block_id: block for block in document.blocks}
    assets = {asset.asset_id: asset for asset in document.assets}
    members = {
        member
        for block in document.blocks
        if block.table is not None
        for cell in block.table.cells or ()
        for member in cell.block_ids
    }

    def image(asset: Asset, alt: str | None, *, html: bool) -> str:
        media = asset.media
        if media.path is not None:
            target = _destination(str(media.path), path=True)
        elif media.url is not None:
            target = _destination(media.url)
        else:
            target = None
        if target is None or asset.media_type not in _IMAGE_TYPES:
            if media.path is not None:
                source = str(media.path)
            elif media.url is not None:
                source = media.url
            else:
                source = f"inline bytes ({len(media.data or b'')} bytes)"
            details = f"image asset {asset.asset_id}; {asset.media_type or 'unknown media type'}; sha256={asset.content_hash}; source={source}"
            label = f"{_escape_text(alt, html=html)} " if alt is not None else ""
            return f"{label}[image not embedded: {_code(details)}]"
        label = alt if alt is not None else asset.asset_id
        if html:
            return f'<img src="{_attribute(target)}" alt="{_attribute(label)}">'
        return f"![{_escape_text(label, html=False)}](<{target}>)"

    def formula(value: FormulaContent, *, inline: bool, html: bool) -> str:
        pieces = []
        if value.expression is not None:
            if (
                value.format == "latex"
                and not any(char in value.expression for char in "$<&`\r")
                and not (inline and "\n" in value.expression)
            ):
                expression = (
                    f"${value.expression}$" if inline else f"$$\n{value.expression}\n$$"
                )
                pieces.append(
                    _escape_text(expression, html=True) if html else expression
                )
            else:
                source = (
                    _code(value.expression)
                    if inline or html
                    else _fence(value.expression, value.format or "")
                )
                separator = " " if inline or html else "\n\n"
                pieces.append(f"[formula source: {value.format}]{separator}{source}")
        if value.asset_id is not None:
            pieces.append(image(assets[value.asset_id], "formula image", html=html))
        return (" " if inline or html else "\n\n").join(pieces)

    def render(block: Block, *, html: bool = False) -> str:
        if block.rich_text is not None:
            parts = []
            for inline in block.rich_text.inlines:
                if isinstance(inline, InlineCode):
                    parts.append(_code(inline.text))
                elif isinstance(inline, InlineFormula):
                    parts.append(formula(inline.formula, inline=True, html=html))
                else:
                    parts.append(_run(inline, html=html))
            content = "".join(parts)
            if block.kind == "heading":
                level = block.level
                if level is None:
                    candidate = block.layout.get("heading_level")
                    level = (
                        candidate if type(candidate) is int and candidate > 0 else None
                    )
                if level is not None and 1 <= level <= 6:
                    return (
                        f"<h{level}>{content}</h{level}>"
                        if html
                        else f"{'#' * level} {content}"
                    )
                content = f"[heading level {level if level is not None else 'unknown'}] {content}"
            if block.kind == "list_item":
                marker = block.marker
                if marker is None and block.layout.get("numbering_format") == "bullet":
                    template = block.layout.get("marker_template")
                    marker = template if isinstance(template, str) else None
                depth = block.level or 0
                label = _code(marker) if marker is not None else "[list marker unknown]"
                if html:
                    return f'<p style="white-space: pre-wrap" data-list-level="{depth}">{label} {content}</p>'
                prefix = (
                    marker
                    if marker is not None
                    and re.fullmatch(r"[-+*]|[0-9]{1,9}[.)]", marker)
                    else f"- {label}"
                )
                return "    " * depth + prefix + " " + content
            return (
                f'<p style="white-space: pre-wrap">{content}</p>' if html else content
            )
        if block.code is not None:
            language = block.language or ""
            known = re.fullmatch(r"[A-Za-z0-9_+.-]*", language) is not None
            details = []
            if block.filename is not None:
                details.append(f"[code file: {_code(block.filename)}]")
            if not known:
                details.append(f"[code language: {_code(language)}]")
            content = (
                f"<pre><code>{_escape_text(block.code, html=True)}</code></pre>"
                if html
                else _fence(block.code, language if known else "")
            )
            return ("\n" if html else "\n\n").join((*details, content))
        if block.formula is not None:
            content = formula(block.formula, inline=False, html=html)
            if block.formula_number is not None:
                content += f" [formula number: {_escape_text(block.formula_number, html=html)}]"
            return content
        if block.asset_id is not None:
            return image(assets[block.asset_id], block.alt_text, html=html)
        table = block.table
        if table is None:
            raise ValueError("block has no exportable content")
        if table.html is not None:
            source = (
                f"<pre><code>{_escape_text(table.html, html=True)}</code></pre>"
                if html
                else _fence(table.html, "html")
            )
            return "[table HTML source; cell structure unavailable]\n\n" + source
        if table.asset_id is not None:
            return "[table cell structure unavailable] " + image(
                assets[table.asset_id], None, html=html
            )
        cells = table.cells or ()
        rows, columns = table_dimensions(table)
        by_position = {(cell.row, cell.column): cell for cell in cells}
        output = ["<table>"]
        for row in range(rows):
            output.append("<tr>")
            for column in range(columns):
                cell = by_position.get((row, column))
                if cell is None:
                    if not any(
                        cell.row <= row < cell.row + cell.row_span
                        and cell.column <= column < cell.column + cell.column_span
                        for cell in cells
                    ):
                        output.append('<td data-unknown="true">[unknown cell]</td>')
                    continue
                tag = "th" if cell.header is True else "td"
                attributes = f' rowspan="{cell.row_span}" colspan="{cell.column_span}"'
                if cell.header is None:
                    attributes += ' data-header="unknown"'
                content = "\n".join(
                    render(blocks[member], html=True) for member in cell.block_ids
                )
                output.append(f"<{tag}{attributes}>{content}</{tag}>")
            output.append("</tr>")
        output.append("</table>")
        return "\n".join(output)

    return "\n\n".join(
        render(block) for block in document.blocks if block.block_id not in members
    )
