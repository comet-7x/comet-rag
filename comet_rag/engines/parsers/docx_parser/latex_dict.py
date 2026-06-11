"""
LaTeX math-mode symbol tables for OMML → LaTeX conversion.

Pure-data module — every identifier is a ``str`` constant, a format string,
or a ``dict`` / ``tuple`` thereof.  Contains no executable logic.

Format-string conventions
-------------------------
* ``CHR`` values use a single positional slot ``{0}`` for the base character:
  e.g. ``CHR["\\u0302"].format("x")`` → ``"\\\\hat{x}"``
* ``F``, ``LIM_FUNC``, ``LIM_UPP``, ``D``, ``RAD``, ``ARR``, ``M`` use named
  keyword slots — ``{num}``, ``{den}``, ``{lim}``, ``{text}``, ``{deg}`` —
  call via ``template.format(**kwargs)``
* ``SUB``, ``SUP`` also use the single positional slot ``{0}``

Adapted from https://github.com/xiilei/dwml/blob/master/dwml/latex_dict.py
on 30/04/2026.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Structural / escape constants
# ---------------------------------------------------------------------------

CHARS: tuple[str, ...] = ("{", "}", "_", "^", "#", "&", "$", "%")
"""LaTeX special characters that must be backslash-escaped in text runs."""

BLANK: str = ""
BACKSLASH: str = "\\"
ALN: str = "&"
"""Column-alignment separator inside LaTeX ``array`` environments."""

# ---------------------------------------------------------------------------
# CHR — combining diacritical marks → LaTeX accent commands
#
# Each value is a Python format string with a single positional slot ``{0}``
# representing the base character:
#   CHR["\\u0302"].format("x")  →  "\\hat{x}"
# ---------------------------------------------------------------------------

CHR: dict[str, str] = {
    # Unicode : Latex Math Symbols
    # Top accents
    "\u0300": "\\grave{{{0}}}",  # x̀ COMBINING GRAVE ACCENT
    "\u0301": "\\acute{{{0}}}",  # x́ COMBINING ACUTE ACCENT
    "\u0302": "\\hat{{{0}}}",  # x̂ COMBINING CIRCUMFLEX ACCENT
    "\u0303": "\\tilde{{{0}}}",  # x̃ COMBINING TILDE
    "\u0304": "\\bar{{{0}}}",  # x̄ COMBINING MACRON
    "\u0305": "\\overline{{{0}}}",  # x̅ COMBINING OVERLINE
    "\u0306": "\\breve{{{0}}}",  # x̆ COMBINING BREVE
    "\u0307": "\\dot{{{0}}}",  # ẋ COMBINING DOT ABOVE
    "\u0308": "\\ddot{{{0}}}",  # ẍ COMBINING DIAERESIS
    "\u0309": "\\ovhook{{{0}}}",  # x̉ COMBINING HOOK ABOVE
    "\u030a": "\\ocirc{{{0}}}",  # x̊ COMBINING RING ABOVE
    "\u030c": "\\check{{{0}}}",  # x̌ COMBINING CARON
    "\u0310": "\\candra{{{0}}}",  # x̐ COMBINING CANDRABINDU
    "\u0312": "\\oturnedcomma{{{0}}}",  # x̒ COMBINING TURNED COMMA ABOVE
    "\u0315": "\\ocommatopright{{{0}}}",  # x̕ COMBINING COMMA ABOVE RIGHT
    "\u031a": "\\droang{{{0}}}",  # x̚ COMBINING LEFT ANGLE ABOVE
    "\u0338": "\\not{{{0}}}",  # x̸ COMBINING LONG SOLIDUS OVERLAY
    "\u20d0": "\\leftharpoonaccent{{{0}}}",  # x⃐ COMBINING LEFT HARPOON ABOVE
    "\u20d1": "\\rightharpoonaccent{{{0}}}",  # x⃑ COMBINING RIGHT HARPOON ABOVE
    "\u20d2": "\\vertoverlay{{{0}}}",  # x⃒ COMBINING LONG VERTICAL LINE OVERLAY
    "\u20d6": "\\overleftarrow{{{0}}}",  # x⃖ COMBINING LEFT ARROW ABOVE
    "\u20d7": "\\vec{{{0}}}",  # x⃗ COMBINING RIGHT ARROW ABOVE
    "\u20db": "\\dddot{{{0}}}",  # x⃛ COMBINING THREE DOTS ABOVE
    "\u20dc": "\\ddddot{{{0}}}",  # x⃜ COMBINING FOUR DOTS ABOVE
    "\u20e1": "\\overleftrightarrow{{{0}}}",  # x⃡ COMBINING LEFT RIGHT ARROW ABOVE
    "\u20e7": "\\annuity{{{0}}}",  # x⃧ COMBINING ANNUITY SYMBOL
    "\u20e9": "\\widebridgeabove{{{0}}}",  # x⃩ COMBINING WIDE BRIDGE ABOVE
    "\u20f0": "\\asteraccent{{{0}}}",  # x⃰ COMBINING ASTERISK ABOVE
    # Bottom accents
    "\u0330": "\\wideutilde{{{0}}}",  # x̰ COMBINING TILDE BELOW
    "\u0331": "\\underbar{{{0}}}",  # x̱ COMBINING MACRON BELOW
    "\u20e8": "\\threeunderdot{{{0}}}",  # x⃨ COMBINING TRIPLE UNDERDOT
    "\u20ec": "\\underrightharpoondown{{{0}}}",  # x⃬ COMBINING RIGHTWARDS HARPOON WITH BARB DOWNWARDS
    "\u20ed": "\\underleftharpoondown{{{0}}}",  # x⃭ COMBINING LEFTWARDS HARPOON WITH BARB DOWNWARDS
    "\u20ee": "\\underleftarrow{{{0}}}",  # x⃮ COMBINING LEFT ARROW BELOW
    "\u20ef": "\\underrightarrow{{{0}}}",  # x⃯ COMBINING RIGHT ARROW BELOW
    # Over | group
    "\u23b4": "\\overbracket{{{0}}}",  # ⎴ TOP SQUARE BRACKET
    "\u23dc": "\\overparen{{{0}}}",  # ⏜ TOP PARENTHESIS
    "\u23de": "\\overbrace{{{0}}}",  # ⏞ TOP CURLY BRACKET
    # Under| group
    "\u23b5": "\\underbracket{{{0}}}",  # ⎵ BOTTOM SQUARE BRACKET
    "\u23dd": "\\underparen{{{0}}}",  # ⏝ BOTTOM PARENTHESIS
    "\u23df": "\\underbrace{{{0}}}",  # ⏟ BOTTOM CURLY BRACKET
}

# ---------------------------------------------------------------------------
# CHR_BO — big-operator Unicode characters → LaTeX commands (no argument slot)
#
# Used by ``<m:nary>`` to look up the operator symbol from its Unicode char.
# ---------------------------------------------------------------------------

CHR_BO: dict[str, str] = {
    # Big operators,
    "\u2140": "\\Bbbsum",  # ⅀
    "\u220f": "\\prod",  # ∏
    "\u2210": "\\coprod",  # ∐
    "\u2211": "\\sum",  # ∑
    "\u222b": "\\int",  # ∫
    "\u222c": "\\iint",  # ∬
    "\u222d": "\\iiint",  # ∭
    "\u222e": "\\oint",  # ∮
    "\u222f": "\\oiint",  # ∯
    "\u2230": "\\oiiint",  # ∰
    "\u22c0": "\\bigwedge",  # ⋀
    "\u22c1": "\\bigvee",  # ⋁
    "\u22c2": "\\bigcap",  # ⋂
    "\u22c3": "\\bigcup",  # ⋃
    "\u2a00": "\\bigodot",  # ⨀
    "\u2a01": "\\bigoplus",  # ⨁
    "\u2a02": "\\bigotimes",  # ⨂
}

# ---------------------------------------------------------------------------
# T — direct Unicode → LaTeX replacement table (no format slot)
#
# Used for Greek letters, relation/binary symbols, and characters whose
# pylatexenc text-mode mappings would be invalid in math environments.
# Unknown characters not present in T are kept as-is (raw Unicode), which
# modern renderers such as KaTeX and MathJax accept in math mode.
# ---------------------------------------------------------------------------

T: dict[str, str] = {
    # Whitespace
    "\u00a0": " ",  #   NON-BREAKING SPACE — pylatexenc maps to "~" (text-mode),
    # which escape_latex would mangle to "\~" (invalid in math mode).
    # Greek letters (Mathematical Italic block U+1D6FC–U+1D71B)
    "\U0001d6fc": "\\alpha ",  # 𝛼
    "\U0001d6fd": "\\beta ",  # 𝛽
    "\U0001d6fe": "\\gamma ",  # 𝛾
    "\U0001d6ff": "\\delta ",  # 𝛿
    "\U0001d700": "\\epsilon ",  # 𝜀
    "\U0001d701": "\\zeta ",  # 𝜁
    "\U0001d702": "\\eta ",  # 𝜂
    "\U0001d703": "\\theta ",  # 𝜃
    "\U0001d704": "\\iota ",  # 𝜄
    "\U0001d705": "\\kappa ",  # 𝜅
    "\U0001d706": "\\lambda ",  # 𝜆
    "\U0001d707": "\\mu ",  # 𝜇
    "\U0001d708": "\\nu ",  # 𝜈
    "\U0001d709": "\\xi ",  # 𝜉
    "\U0001d70a": "\\omicron ",  # 𝜊
    "\U0001d70b": "\\pi ",  # 𝜋
    "\U0001d70c": "\\rho ",  # 𝜌
    "\U0001d70d": "\\varsigma ",  # 𝜍
    "\U0001d70e": "\\sigma ",  # 𝜎
    "\U0001d70f": "\\tau ",  # 𝜏
    "\U0001d710": "\\upsilon ",  # 𝜐
    "\U0001d711": "\\phi ",  # 𝜑
    "\U0001d712": "\\chi ",  # 𝜒
    "\U0001d713": "\\psi ",  # 𝜓
    "\U0001d714": "\\omega ",  # 𝜔
    "\U0001d715": "\\partial ",  # 𝜕
    "\U0001d716": "\\varepsilon ",  # 𝜖
    "\U0001d717": "\\vartheta ",  # 𝜗
    "\U0001d718": "\\varkappa ",  # 𝜘
    "\U0001d719": "\\varphi ",  # 𝜙
    "\U0001d71a": "\\varrho ",  # 𝜚
    "\U0001d71b": "\\varpi ",  # 𝜛
    # Arrows / relation symbols
    "\u2190": "\\leftarrow ",  # ←
    "\u2191": "\\uparrow ",  # ↑
    "\u2192": "\\rightarrow ",  # →
    "\u2193": "\\downarrow ",  # ↓
    "\u2194": "\\leftrightarrow ",  # ↔
    "\u2195": "\\updownarrow ",  # ↕
    "\u2196": "\\nwarrow ",  # ↖
    "\u2197": "\\nearrow ",  # ↗
    "\u2198": "\\searrow ",  # ↘
    "\u2199": "\\swarrow ",  # ↙
    "\u2026": "\\ldots ",  # … HORIZONTAL ELLIPSIS — pylatexenc: \textellipsis (text-mode)
    "\u22ee": "\\vdots ",  # ⋮
    "\u22ef": "\\cdots ",  # ⋯
    "\u22f0": "\\adots ",  # ⋰
    "\u22f1": "\\ddots ",  # ⋱
    "\u2260": "\\ne ",  # ≠
    "\u2264": "\\leq ",  # ≤
    "\u2265": "\\geq ",  # ≥
    "\u2266": "\\leqq ",  # ≦
    "\u2267": "\\geqq ",  # ≧
    "\u2268": "\\lneqq ",  # ≨
    "\u2269": "\\gneqq ",  # ≩
    "\u226a": "\\ll ",  # ≪
    "\u226b": "\\gg ",  # ≫
    "\u2208": "\\in ",  # ∈
    "\u2209": "\\notin ",  # ∉
    "\u220b": "\\ni ",  # ∋
    "\u220c": "\\nni ",  # ∌
    # Ordinary symbols
    "\u221e": "\\infty ",  # ∞
    # Binary operators
    "\u00b1": "\\pm ",  # ±
    "\u2213": "\\mp ",  # ∓
    # Characters whose pylatexenc text-mode mappings are invalid in math environments
    "\u00f0": "\\eth ",  # ð — pylatexenc: \dh (tipa, not in KaTeX/MathJax)
    "\u0131": "\\imath ",  # ı — pylatexenc: \i (text-mode only)
    "\u2127": "\\mho ",  # ℧ — pylatexenc: \textmho (textcomp, not in KaTeX/MathJax)
    "\u212e": "e",  # ℮ — pylatexenc: \textestimated (no math equivalent; use 'e')
    "\u00c5": "\\mathring{A} ",  # Å — pylatexenc: \r{A} (text-mode only)
    # Multiplication / division (text-mode pylatexenc overrides)
    "\u00b7": "\\cdot ",  # · MIDDLE DOT — common in Chinese scientific notation
    "\u22c5": "\\cdot ",  # ⋅ DOT OPERATOR
    "\u2219": "\\bullet ",  # ∙ BULLET OPERATOR
    "\u00d7": "\\times ",  # × MULTIPLICATION SIGN
    "\u00f7": "\\div ",  # ÷ DIVISION SIGN
    "\u2212": "-",  # − MINUS SIGN
    # Degree / prime — avoid ^{} syntax: escape_latex would mangle bare ^ and braces
    "\u00b0": "\\circ ",  # ° DEGREE SIGN — caller context provides the ^
    "\u2032": "'",  # ′ PRIME
    "\u2033": "''",  # ″ DOUBLE PRIME
    # Superscript digits — avoid \texttwosuperior / \textthreesuperior from pylatexenc
    "\u00b2": "2",  # ²
    "\u00b3": "3",  # ³
    "\u00b9": "1",  # ¹
    # Integral / surface-integral operators
    # pylatexenc maps some to non-KaTeX commands; override or keep Unicode.
    "\u222f": "\\oiint ",  # ∯ SURFACE INTEGRAL — pylatexenc: \surfintegral (not in KaTeX)
    "\u2230": "\\oiiint ",  # ∰ VOLUME INTEGRAL — pylatexenc: \volintegral (not in KaTeX)
    "\u2231": "∱",  # ∱ CLOCKWISE INTEGRAL — no KaTeX equivalent; keep Unicode
    "\u2232": "∲",  # ∲ CLOCKWISE CONTOUR INTEGRAL — no KaTeX equivalent; keep Unicode
    "\u2233": "∳",  # ∳ ANTICLOCKWISE CONTOUR INTEGRAL — no KaTeX equivalent; keep Unicode
    # N-ary operators: ⨀⨁⨂ have KaTeX commands; ⨃⨄ do not — keep Unicode for those
    "\u2a00": "\\bigodot ",  # ⨀ N-ARY CIRCLED DOT OPERATOR
    "\u2a01": "\\bigoplus ",  # ⨁ N-ARY CIRCLED PLUS OPERATOR
    "\u2a02": "\\bigotimes ",  # ⨂ N-ARY CIRCLED TIMES OPERATOR
    "\u2a03": "⨃",  # ⨃ N-ARY UNION WITH DOT — no exact KaTeX equivalent; keep Unicode
    "\u2a04": "⨄",  # ⨄ N-ARY UNION WITH PLUS — no exact KaTeX equivalent; keep Unicode
    # Wave arrows — pylatexenc: \arrowwaveleft / \arrowwaveright (not in KaTeX); keep Unicode
    "\u219c": "↜",  # ↜ LEFTWARDS WAVE ARROW
    "\u219d": "↝",  # ↝ RIGHTWARDS WAVE ARROW
    # Mathematical Italic uppercase (U+1D434–U+1D44D)
    "\U0001d434": "A",  # 𝐴
    "\U0001d435": "B",  # 𝐵
    "\U0001d436": "C",  # 𝐶
    "\U0001d437": "D",  # 𝐷
    "\U0001d438": "E",  # 𝐸
    "\U0001d439": "F",  # 𝐹
    "\U0001d43a": "G",  # 𝐺
    "\U0001d43b": "H",  # 𝐻
    "\U0001d43c": "I",  # 𝐼
    "\U0001d43d": "J",  # 𝐽
    "\U0001d43e": "K",  # 𝐾
    "\U0001d43f": "L",  # 𝐿
    "\U0001d440": "M",  # 𝑀
    "\U0001d441": "N",  # 𝑁
    "\U0001d442": "O",  # 𝑂
    "\U0001d443": "P",  # 𝑃
    "\U0001d444": "Q",  # 𝑄
    "\U0001d445": "R",  # 𝑅
    "\U0001d446": "S",  # 𝑆
    "\U0001d447": "T",  # 𝑇
    "\U0001d448": "U",  # 𝑈
    "\U0001d449": "V",  # 𝑉
    "\U0001d44a": "W",  # 𝑊
    "\U0001d44b": "X",  # 𝑋
    "\U0001d44c": "Y",  # 𝑌
    "\U0001d44d": "Z",  # 𝑍
    # Mathematical Italic lowercase (U+1D44E–U+1D467; U+1D455 is unassigned in Unicode)
    "\U0001d44e": "a",  # 𝑎
    "\U0001d44f": "b",  # 𝑏
    "\U0001d450": "c",  # 𝑐
    "\U0001d451": "d",  # 𝑑
    "\U0001d452": "e",  # 𝑒
    "\U0001d453": "f",  # 𝑓
    "\U0001d454": "g",  # 𝑔
    "\U0001d456": "i",  # 𝑖
    "\U0001d457": "j",  # 𝑗
    "\U0001d458": "k",  # 𝑘
    "\U0001d459": "l",  # 𝑙
    "\U0001d45a": "m",  # 𝑚
    "\U0001d45b": "n",  # 𝑛
    "\U0001d45c": "o",  # 𝑜
    "\U0001d45d": "p",  # 𝑝
    "\U0001d45e": "q",  # 𝑞
    "\U0001d45f": "r",  # 𝑟
    "\U0001d460": "s",  # 𝑠
    "\U0001d461": "t",  # 𝑡
    "\U0001d462": "u",  # 𝑢
    "\U0001d463": "v",  # 𝑣
    "\U0001d464": "w",  # 𝑤
    "\U0001d465": "x",  # 𝑥
    "\U0001d466": "y",  # 𝑦
    "\U0001d467": "z",  # 𝑧
    # Plain (non-italic) Greek lowercase — Word equation editor sometimes inserts these
    "α": "\\alpha ",    # α GREEK SMALL LETTER ALPHA
    "β": "\\beta ",     # β GREEK SMALL LETTER BETA
    "γ": "\\gamma ",    # γ GREEK SMALL LETTER GAMMA
    "δ": "\\delta ",    # δ GREEK SMALL LETTER DELTA
    "ε": "\\varepsilon ",  # ε GREEK SMALL LETTER EPSILON
    "ζ": "\\zeta ",     # ζ GREEK SMALL LETTER ZETA
    "η": "\\eta ",      # η GREEK SMALL LETTER ETA
    "θ": "\\theta ",    # θ GREEK SMALL LETTER THETA
    "ι": "\\iota ",     # ι GREEK SMALL LETTER IOTA
    "κ": "\\kappa ",    # κ GREEK SMALL LETTER KAPPA
    "λ": "\\lambda ",   # λ GREEK SMALL LETTER LAMBDA
    "μ": "\\mu ",       # μ GREEK SMALL LETTER MU
    "ν": "\\nu ",       # ν GREEK SMALL LETTER NU
    "ξ": "\\xi ",       # ξ GREEK SMALL LETTER XI
    "π": "\\pi ",       # π GREEK SMALL LETTER PI
    "ρ": "\\rho ",      # ρ GREEK SMALL LETTER RHO
    "σ": "\\sigma ",    # σ GREEK SMALL LETTER SIGMA
    "τ": "\\tau ",      # τ GREEK SMALL LETTER TAU
    "υ": "\\upsilon ",  # υ GREEK SMALL LETTER UPSILON
    "φ": "\\varphi ",   # φ GREEK SMALL LETTER PHI
    "χ": "\\chi ",      # χ GREEK SMALL LETTER CHI
    "ψ": "\\psi ",      # ψ GREEK SMALL LETTER PSI
    "ω": "\\omega ",    # ω GREEK SMALL LETTER OMEGA
    # Plain Greek uppercase
    "Γ": "\\Gamma ",    # Γ
    "Δ": "\\Delta ",    # Δ
    "Θ": "\\Theta ",    # Θ
    "Λ": "\\Lambda ",   # Λ
    "Ξ": "\\Xi ",       # Ξ
    "Π": "\\Pi ",       # Π
    "Σ": "\\Sigma ",    # Σ
    "Υ": "\\Upsilon ",  # Υ
    "Φ": "\\Phi ",      # Φ
    "Ψ": "\\Psi ",      # Ψ
    "Ω": "\\Omega ",    # Ω
    # Special mathematical constants
    "ℏ": "\\hbar ",     # ℏ PLANCK CONSTANT OVER TWO PI
    # OMML equation-array alignment point (&) — visually rendered as | in Word
    "&": "\\vert ",          # & → \vert
}

# ---------------------------------------------------------------------------
# FUNC — named math functions → LaTeX command templates
#
# ``{fe}`` is the placeholder for the function argument (see FUNC_PLACE).
# Usage: FUNC["sin"].replace(FUNC_PLACE, arg_str)
# ---------------------------------------------------------------------------

FUNC: dict[str, str] = {
    "sin": "\\sin({fe})",
    "cos": "\\cos({fe})",
    "tan": "\\tan({fe})",
    "arcsin": "\\arcsin({fe})",
    "arccos": "\\arccos({fe})",
    "arctan": "\\arctan({fe})",
    "arccot": "\\arccot({fe})",
    "sinh": "\\sinh({fe})",
    "cosh": "\\cosh({fe})",
    "tanh": "\\tanh({fe})",
    "coth": "\\coth({fe})",
    "sec": "\\sec({fe})",
    "csc": "\\csc({fe})",
    "mod": "\\mod {fe}",
    "max": "\\max({fe})",
    "min": "\\min({fe})",
}

FUNC_PLACE: str = "{fe}"
"""Argument placeholder used inside ``FUNC`` templates."""

BRK: str = "\\\\"
"""Row separator inside LaTeX ``array`` (and similar) environments."""

# ---------------------------------------------------------------------------
# CHR_DEFAULT / POS / POS_DEFAULT — fallback accent and bar format strings
# ---------------------------------------------------------------------------

CHR_DEFAULT: dict[str, str] = {
    "ACC_VAL": "\\hat{{{0}}}",  # default accent when <m:chr> is absent
}

POS: dict[str, str] = {
    "top": "\\overline{{{0}}}",
    "bot": "\\underline{{{0}}}",
}

POS_DEFAULT: dict[str, str] = {
    "BAR_VAL": "\\overline{{{0}}}",  # default bar direction when <m:pos> is absent
}

# ---------------------------------------------------------------------------
# SUB / SUP — subscript and superscript wrappers
# ---------------------------------------------------------------------------

SUB: str = "_{{{0}}}"
"""Subscript wrapper: ``SUB.format(content)`` → ``_{content}``."""

SUP: str = "^{{{0}}}"
"""Superscript wrapper: ``SUP.format(content)`` → ``^{content}``."""

# ---------------------------------------------------------------------------
# F / F_DEFAULT — fraction format strings
#
# Keys match the ``m:val`` attribute of ``<m:type>`` inside ``<m:fPr>``.
# Named slots: ``{num}`` (numerator), ``{den}`` (denominator).
# ---------------------------------------------------------------------------

F: dict[str, str] = {
    "bar": "\\frac{{{num}}}{{{den}}}",
    "skw": r"^{{{num}}}/_{{{den}}}",
    "noBar": "\\genfrac{{}}{{}}{{0pt}}{{}}{{{num}}}{{{den}}}",
    "lin": "{{{num}}}/{{{den}}}",
}
F_DEFAULT: str = "\\frac{{{num}}}{{{den}}}"
"""Fraction template used when ``m:type`` is absent or unrecognised."""

# ---------------------------------------------------------------------------
# D / D_DEFAULT — delimiter (bracket) wrapper
#
# Named slots: ``{left}`` (opening delimiter), ``{text}`` (inner content),
# ``{right}`` (closing delimiter).
# An invisible delimiter is represented by ``"."`` (e.g. ``\left.``).
# ---------------------------------------------------------------------------

D: str = "\\left{left}{text}\\right{right}"
"""Bracket template: wraps content with ``\\left`` / ``\\right`` delimiters."""

D_DEFAULT: dict[str, str] = {
    "left": "(",
    "right": ")",
    "null": ".",  # \left. or \right. — invisible (null) delimiter
}

# ---------------------------------------------------------------------------
# RAD / RAD_DEFAULT — radical (root) format strings
# ---------------------------------------------------------------------------

RAD: str = "\\sqrt[{deg}]{{{text}}}"
"""Radical with explicit degree index: ``\\sqrt[n]{text}``."""

RAD_DEFAULT: str = "\\sqrt{{{text}}}"
"""Square root (no degree argument)."""

ARR: str = "\\begin{{array}}{{c}}{text}\\end{{array}}"
"""Single-column array environment used for multi-row equation arrays."""

# ---------------------------------------------------------------------------
# LIM_FUNC / LIM_TO / LIM_UPP — limit and overlay helpers
# ---------------------------------------------------------------------------

LIM_FUNC: dict[str, str] = {
    "lim": "\\lim_{{{lim}}}",
    "max": "\\max_{{{lim}}}",
    "min": "\\min_{{{lim}}}",
}
"""Named limit functions.  Named slot: ``{lim}`` (subscript expression)."""

LIM_TO: tuple[str, str] = ("\\rightarrow", "\\to")
"""Canonical and shortened forms of the limit arrow; ``do_lim`` normalises to ``\\to``."""

LIM_UPP: str = "\\overset{{{lim}}}{{{text}}}"
"""Upper-limit overlay template: ``\\overset{lim}{text}``."""

# ---------------------------------------------------------------------------
# GROUPCHR_ARROW — extensible arrow commands for <m:groupChr> with arrow chr
# Used when an arrow carries a label above it (pos="top").
# Format slot {0} receives the label text.
# ---------------------------------------------------------------------------

GROUPCHR_ARROW: dict[str, str] = {
    "←": "\\xleftarrow{{{0}}}",       # ←
    "→": "\\xrightarrow{{{0}}}",      # →
    "↔": "\\xleftrightarrow{{{0}}}",  # ↔
    "⇐": "\\xLeftarrow{{{0}}}",       # ⇐
    "⇒": "\\xRightarrow{{{0}}}",      # ⇒
    "⇔": "\\xLeftrightarrow{{{0}}}",  # ⇔
    "↦": "\\xmapsto{{{0}}}",          # ↦
}

# ---------------------------------------------------------------------------
# M — matrix environment
# ---------------------------------------------------------------------------

M: str = "\\begin{{matrix}}{text}\\end{{matrix}}"
"""Matrix environment.  Rows joined with ``BRK`` (``\\\\``); columns with ``ALN`` (``&``)."""
