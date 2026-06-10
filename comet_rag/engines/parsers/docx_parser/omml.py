"""
OMML (Office Math Markup Language) → LaTeX converter.

Adapted from:
  https://github.com/xiilei/dwml/blob/master/dwml/omml.py  (MIT licence)
  https://github.com/xiilei/dwml/blob/master/dwml/latex_dict.py

Changes from the original MinerU version
-----------------------------------------
* ``pylatexenc`` dependency removed; unknown Unicode characters are kept as-is
  (modern renderers such as KaTeX / MathJax accept raw Unicode in math mode).
* ``load()`` / ``load_string()`` entry points removed (not needed here).
* All ``latex_dict`` constants live in the companion ``latex_dict.py`` module.

Only ``re`` and ``lxml`` (a python-docx transitive dependency) are required
at runtime; ``loguru`` is used for debug logging only.

Public API
----------
::

    from comet_rag.engines.parsers.docx_parser.omml import oMath2Latex

    latex_str: str = str(oMath2Latex(omath_element))
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterator
from typing import Any, ClassVar

from loguru import logger

from comet_rag.engines.parsers.docx_parser.latex_dict import (
    ALN,
    ARR,
    BACKSLASH,
    BLANK,
    BRK,
    CHARS,
    CHR,
    CHR_BO,
    CHR_DEFAULT,
    D_DEFAULT,
    F_DEFAULT,
    FUNC,
    FUNC_PLACE,
    LIM_FUNC,
    LIM_TO,
    LIM_UPP,
    POS,
    POS_DEFAULT,
    RAD,
    RAD_DEFAULT,
    SUB,
    SUP,
    D,
    F,
    M,
    T,
)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

SCR_TO_LATEX: dict[str, str] = {
    "script": "\\mathscr{{{0}}}",
    "fraktur": "\\mathfrak{{{0}}}",
    "double-struck": "\\mathbb{{{0}}}",
    "sans-serif": "\\mathsf{{{0}}}",
    "monospace": "\\mathtt{{{0}}}",
}
"""Maps the ``m:val`` of ``<m:scr>`` to a LaTeX math-alphabet command."""

OMML_NS: str = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
"""Expanded namespace prefix prepended to all ``m:*`` OMML element tags."""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _process_unicode(s: str) -> str:
    """
    Map a single character to its LaTeX math-mode equivalent.

    Lookup order: ``T`` symbol table → character as-is.
    Modern renderers (KaTeX, MathJax) accept raw Unicode inside math mode,
    so passing unknown characters through unchanged is safe in practice.
    """
    return T.get(s, s)


def escape_latex(strs: str) -> str:
    """Backslash-escape LaTeX special characters that appear unescaped in *strs*."""
    last: str | None = None
    result: list[str] = []
    strs = strs.replace(r"\\", "\\")
    for c in strs:
        if (c in CHARS) and (last != BACKSLASH):
            result.append(BACKSLASH + c)
        else:
            result.append(c)
        last = c
    return BLANK.join(result)


def get_val(
    key: str | None,
    default: str | None = None,
    store: dict[str, str] | None = CHR,
) -> str | None:
    """
    Look up *key* in *store*, with a fallback to *default*.

    * ``key`` is ``None`` → return *default*.
    * *store* is falsy (``None`` or empty) → return *key* as-is (no lookup).
    * Otherwise → ``store.get(key, key)`` (returns *key* itself when not found).
    """
    if key is not None:
        return key if not store else store.get(key, key)
    return default


# ---------------------------------------------------------------------------
# Tag-dispatch base class
# ---------------------------------------------------------------------------


class Tag2Method:
    """
    Mixin that dispatches lxml elements to handler methods via a class-level table.

    Subclasses populate ``tag2meth`` mapping OMML local tag names to unbound
    handler methods.  ``call_method`` strips the OMML namespace prefix from
    an element's tag and invokes the matching handler.

    The three ``process_children*`` helpers iterate over an element's direct
    OMML children, dispatch each one, and aggregate results as a list, dict,
    or concatenated string respectively.
    """

    #: Maps OMML local tag names to handler methods.
    #: Overridden in each concrete subclass with its own dispatch table.
    tag2meth: ClassVar[dict[str, Callable[..., Any]]] = {}

    def call_method(self, elm: Any, stag: str | None = None) -> Any:
        """
        Dispatch *elm* to the handler registered for its OMML tag.

        If *stag* is provided it is used directly (avoids re-computing the
        local tag name, useful when iterating children).

        Returns the handler's return value, or ``None`` if no handler is registered.
        """
        if stag is None:
            stag = elm.tag.replace(OMML_NS, "")
        method = self.tag2meth.get(str(stag))
        if method:
            return method(self, elm)
        return None

    def process_children_list(
        self,
        elm: Any,
        include: Collection[str] | None = None,
    ) -> Iterator[tuple[str, Any, Any]]:
        """
        Yield ``(local_tag, result, child_element)`` for each OMML child of *elm*.

        Skips children whose tag:
        * does not belong to the OMML namespace, or
        * is not in *include* (when *include* is provided).

        For each accepted child, ``call_method`` is tried first; on ``None``,
        ``process_unknow`` is used as a fallback.  Children where both return
        ``None`` are silently skipped.
        """
        for _e in list(elm):
            if OMML_NS not in _e.tag:
                continue
            stag = _e.tag.replace(OMML_NS, "")
            if include and (stag not in include):
                continue
            t = self.call_method(_e, stag=stag)
            if t is None:
                t = self.process_unknow(_e, stag)
                if t is None:
                    continue
            yield (stag, t, _e)

    def process_children_dict(
        self,
        elm: Any,
        include: Collection[str] | None = None,
    ) -> dict[str, Any]:
        """Return ``{local_tag: result}`` for all accepted OMML children."""
        return {stag: t for stag, t, _ in self.process_children_list(elm, include)}

    def process_children(
        self,
        elm: Any,
        include: Collection[str] | None = None,
    ) -> str:
        """Concatenate stringified results of all accepted OMML children."""
        return BLANK.join(
            t if not isinstance(t, Tag2Method) else str(t)
            for _, t, _ in self.process_children_list(elm, include)
        )

    def process_unknow(self, elm: Any, stag: str) -> Any:
        """
        Fallback handler called when *stag* has no entry in ``tag2meth``.

        Base implementation returns ``None`` (child is silently skipped).
        Subclasses override this to handle structural wrapper tags such as
        ``<m:e>`` (expression slot) or ``<m:*Pr>`` (property elements).
        """
        return None


# ---------------------------------------------------------------------------
# Property element
# ---------------------------------------------------------------------------


class Pr(Tag2Method):
    """
    Generic OMML property element (``<m:*Pr>``).

    Processes its OMML children and exposes the recognised attribute values
    (``chr``, ``pos``, ``begChr``, ``endChr``, ``type``) as instance
    attributes via ``__getattr__``.  Attribute names not stored in the internal
    dict return ``None`` instead of raising ``AttributeError``.

    The ``text`` attribute accumulates any ``BRK`` markers inserted by
    ``do_brk``, letting callers prepend line-break hints to their output.
    """

    text: str = ""

    #: Tags whose ``m:val`` attribute is captured into the internal dict.
    __val_tags: ClassVar[tuple[str, ...]] = ("chr", "pos", "begChr", "endChr", "type")

    def __init__(self, elm: Any) -> None:
        self.__innerdict: dict[str, str | None] = {}
        self.text = self.process_children(elm)

    def __str__(self) -> str:
        return self.text

    def __getattr__(self, name: str) -> str | None:
        # Accessing self.__innerdict here resolves to self._Pr__innerdict via
        # Python name-mangling, which was set unconditionally in __init__.
        return self.__innerdict.get(name, None)

    # ------------------------------------------------------------------
    # Tag handlers
    # ------------------------------------------------------------------

    def do_brk(self, elm: Any) -> str:
        """``<m:brk>`` — explicit line break; stores ``BRK`` and returns it."""
        self.__innerdict["brk"] = BRK
        return BRK

    def do_common(self, elm: Any) -> None:
        """
        Store the ``m:val`` attribute for recognised property child tags.

        Returns ``None`` so the child is excluded from ``process_children``
        output (side-effect only: populates the internal attribute dict).
        """
        stag = elm.tag.replace(OMML_NS, "")
        if stag in self.__val_tags:
            self.__innerdict[stag] = elm.get(f"{OMML_NS}val")

    tag2meth: ClassVar[dict[str, Callable[..., Any]]] = {
        "brk": do_brk,
        "chr": do_common,
        "pos": do_common,
        "begChr": do_common,
        "endChr": do_common,
        "type": do_common,
    }


# ---------------------------------------------------------------------------
# Main converter
# ---------------------------------------------------------------------------


class oMath2Latex(Tag2Method):
    """
    Convert an OMML ``<m:oMath>`` element to a LaTeX math-mode string.

    Implements the tag-dispatch pattern from ``Tag2Method``.  Each ``do_*``
    method handles one OMML construct and returns the corresponding LaTeX
    fragment.

    Usage::

        from lxml import etree
        omath_el = etree.fromstring("<m:oMath ...>...</m:oMath>")
        latex: str = str(oMath2Latex(omath_el))
    """

    #: Structural container tags whose content is simply concatenated
    #: (no additional LaTeX wrapper is needed around them).
    _direct_tags: ClassVar[tuple[str, ...]] = (
        "box",
        "sSub",
        "sSup",
        "sSubSup",
        "num",
        "den",
        "deg",
        "e",
    )

    def __init__(self, element: Any) -> None:
        self._latex: str = self.process_children(element)

    def __str__(self) -> str:
        # Collapse double-spaces that can arise from concatenating fragments
        return self._latex.replace("  ", " ")

    def process_unknow(self, elm: Any, stag: str) -> str | Pr | None:
        """
        Fallback for tags not in ``tag2meth``.

        * Tags in ``_direct_tags`` → concatenate their children transparently.
        * Tags ending in ``Pr`` → parse as a ``Pr`` property object and return it.
        * Anything else → return ``None`` (skip silently).
        """
        if stag in self._direct_tags:
            return self.process_children(elm)
        if stag.endswith("Pr"):
            return Pr(elm)
        return None

    @property
    def latex(self) -> str:
        """Raw LaTeX string before double-space normalisation."""
        return self._latex

    # ------------------------------------------------------------------
    # Tag handlers — each handles one OMML math construct
    # ------------------------------------------------------------------

    def do_acc(self, elm: Any) -> str:
        """``<m:acc>`` — accented character: ``\\hat{x}``, ``\\tilde{x}``, …"""
        c = self.process_children_dict(elm)
        latex_s = get_val(c["accPr"].chr, default=CHR_DEFAULT.get("ACC_VAL"), store=CHR)
        return latex_s.format(c["e"])  # type: ignore[union-attr]

    def do_bar(self, elm: Any) -> str:
        """``<m:bar>`` — overline or underline decoration."""
        c = self.process_children_dict(elm)
        pr = c["barPr"]
        latex_s = get_val(pr.pos, default=POS_DEFAULT.get("BAR_VAL"), store=POS)
        return pr.text + latex_s.format(c["e"])  # type: ignore[union-attr]

    def do_d(self, elm: Any) -> str:
        """``<m:d>`` — delimiter pair (parentheses, brackets, braces, …)."""
        c = self.process_children_dict(elm)
        pr = c["dPr"]
        null = D_DEFAULT.get("null")
        s_val = get_val(pr.begChr, default=D_DEFAULT.get("left"), store=T)
        e_val = get_val(pr.endChr, default=D_DEFAULT.get("right"), store=T)
        return pr.text + D.format(  # type: ignore[union-attr]
            left=null if not s_val else escape_latex(s_val),
            text=c["e"],
            right=null if not e_val else escape_latex(e_val),
        )

    def do_sub(self, elm: Any) -> str:
        """``<m:sub>`` — subscript content slot (child of ``<m:sSub>``)."""
        return SUB.format(self.process_children(elm))

    def do_sup(self, elm: Any) -> str:
        """``<m:sup>`` — superscript content slot (child of ``<m:sSup>``)."""
        return SUP.format(self.process_children(elm))

    def do_f(self, elm: Any) -> str:
        """``<m:f>`` — fraction (bar, skewed, no-bar, or linear style)."""
        c = self.process_children_dict(elm)
        pr = c.get("fPr")
        if pr is None:
            logger.debug("Missing fPr in fraction; using default")
            return F_DEFAULT.format(num=c.get("num"), den=c.get("den"))
        latex_s = get_val(pr.type, default=F_DEFAULT, store=F)
        return pr.text + latex_s.format(num=c.get("num"), den=c.get("den"))  # type: ignore[union-attr]

    def do_func(self, elm: Any) -> str:
        """``<m:func>`` — named function applied to an argument."""
        c = self.process_children_dict(elm)
        return c.get("fName").replace(FUNC_PLACE, c.get("e"))  # type: ignore[union-attr]

    def do_fname(self, elm: Any) -> str:
        """
        ``<m:fName>`` — function name inside ``<m:func>``.

        Resolves the name text to a FUNC command template; appends
        ``FUNC_PLACE`` when the template does not already contain it
        (so that ``do_func`` can substitute the argument).
        """
        parts: list[str] = []
        for stag, t, _ in self.process_children_list(elm):
            if stag == "r":
                parts.append(FUNC.get(t, t) if isinstance(t, str) else str(t))
            elif isinstance(t, str):
                parts.append(t)
        joined = BLANK.join(parts)
        return joined if FUNC_PLACE in joined else joined + FUNC_PLACE

    def do_groupchr(self, elm: Any) -> str:
        """``<m:groupChr>`` — group character overlay (e.g. overbrace, underbrace)."""
        c = self.process_children_dict(elm)
        pr = c["groupChrPr"]
        return pr.text + get_val(pr.chr).format(c["e"])  # type: ignore[union-attr]

    def do_rad(self, elm: Any) -> str:
        """``<m:rad>`` — radical: square root (no degree) or nth root (with degree)."""
        c = self.process_children_dict(elm)
        text = c.get("e")
        deg = c.get("deg")
        return RAD.format(deg=deg, text=text) if deg else RAD_DEFAULT.format(text=text)

    def do_eqarr(self, elm: Any) -> str:
        """
        ``<m:eqArr>`` — vertically-aligned equation array.

        A single-row array is unwrapped.  A trailing ``\\#(tag)`` annotation
        in the single row is converted to ``\\tag{tag}`` for numbered equations.
        Multiple rows are wrapped in an ``array`` environment.
        """
        rows = [t for _, t, _ in self.process_children_list(elm, include=("e",))]
        if len(rows) == 1:
            row = rows[0]
            tag_match = re.search(r"\\#\s*\(([^)]*)\)\s*$", row)
            if tag_match:
                return (
                    f"{row[: tag_match.start()].rstrip()}\\tag{{{tag_match.group(1)}}}"
                )
            return row
        return ARR.format(text=BRK.join(rows))

    def do_limlow(self, elm: Any) -> str:
        """
        ``<m:limLow>`` — expression with a subscript limit (e.g. ``\\lim_{n→∞}``).

        Raises ``RuntimeError`` for function names not in ``LIM_FUNC``.
        """
        t = self.process_children_dict(elm, include=("e", "lim"))
        latex_s = LIM_FUNC.get(t["e"])  # type: ignore[arg-type]
        if not latex_s:
            raise RuntimeError(f"Unsupported lim function: {t['e']!r}")
        return latex_s.format(lim=t.get("lim"))

    def do_limupp(self, elm: Any) -> str:
        """``<m:limUpp>`` — expression with a superscript overlay (``\\overset``)."""
        t = self.process_children_dict(elm, include=("e", "lim"))
        return LIM_UPP.format(lim=t.get("lim"), text=t.get("e"))

    def do_lim(self, elm: Any) -> str:
        """``<m:lim>`` — limit expression; normalises ``\\rightarrow`` → ``\\to``."""
        return self.process_children(elm).replace(LIM_TO[0], LIM_TO[1])

    def do_m(self, elm: Any) -> str:
        """``<m:m>`` — matrix; rows joined by ``\\\\``, columns by ``&``."""
        rows: list[str] = []
        for stag, t, _ in self.process_children_list(elm):
            if stag == "mr":
                rows.append(t)
        return M.format(text=BRK.join(rows))

    def do_mr(self, elm: Any) -> str:
        """``<m:mr>`` — matrix row; column cells (``<m:e>``) joined by ``&``."""
        return ALN.join(
            t for _, t, _ in self.process_children_list(elm, include=("e",))
        )

    def do_nary(self, elm: Any) -> str:
        """
        ``<m:nary>`` — n-ary operator (∑, ∫, ∏, …) with optional sub/sup limits.

        The operator Unicode character is looked up in ``CHR_BO``; defaults to
        ``\\int`` when the character is absent or unrecognised.
        """
        res: list[str] = []
        bo: str = ""
        for stag, t, _ in self.process_children_list(elm):
            if stag == "naryPr":
                bo = get_val(t.chr, default="\\int", store=CHR_BO)  # type: ignore[union-attr]
            else:
                res.append(t)
        return bo + BLANK.join(res)

    def do_r(self, elm: Any) -> str:
        """
        ``<m:r>`` — math run (leaf text node).

        Processing steps:

        1. Collect the text of the ``<m:t>`` child, mapping each character
           through ``_process_unicode`` (T-dict lookup, unknown chars kept as-is).
        2. Escape LaTeX special characters with ``escape_latex``.
        3. Restore literal ``{`` / ``}`` if they were NOT in the raw source —
           i.e. if ``escape_latex`` introduced ``\\{`` / ``\\}`` that would break
           subsequent format-string processing of the fragment.
        4. Apply a math-alphabet command (``\\mathscr``, ``\\mathbb``, …) when
           ``<m:rPr><m:scr>`` specifies a script style.
        """
        _str: list[str] = []
        _base: list[str] = []
        found_text = elm.findtext(f"./{OMML_NS}t")
        if found_text:
            for s in found_text:
                _str.append(_process_unicode(s))
                _base.append(s)

        proc = escape_latex(BLANK.join(_str))
        base = BLANK.join(_base)

        # Restore braces introduced by escape_latex that weren't in the source,
        # so they don't interfere with format-string slots in later processing.
        if "{" not in base and "\\{" in proc:
            proc = proc.replace("\\{", "{")
        if "}" not in base and "\\}" in proc:
            proc = proc.replace("\\}", "}")

        rPr = elm.find(f"{OMML_NS}rPr")
        if rPr is not None:
            scr = rPr.find(f"{OMML_NS}scr")
            if scr is not None:
                template = SCR_TO_LATEX.get(scr.get(f"{OMML_NS}val", ""))
                if template and proc.strip():
                    proc = template.format(proc.strip())

        return proc

    tag2meth: ClassVar[dict[str, Callable[..., Any]]] = {
        "acc": do_acc,
        "r": do_r,
        "bar": do_bar,
        "sub": do_sub,
        "sup": do_sup,
        "f": do_f,
        "func": do_func,
        "fName": do_fname,
        "groupChr": do_groupchr,
        "d": do_d,
        "rad": do_rad,
        "eqArr": do_eqarr,
        "limLow": do_limlow,
        "limUpp": do_limupp,
        "lim": do_lim,
        "m": do_m,
        "mr": do_mr,
        "nary": do_nary,
    }
