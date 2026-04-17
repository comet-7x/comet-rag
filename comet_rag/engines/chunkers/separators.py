# fmt: off

SEPARATORS_EN = [
    "\n\n\n",  # Triple blank line — chapter / major section boundary
    "\n\n",    # Double blank line — paragraph boundary
    "\n",      # Single line break — line boundary
    ". ",      # Period + space — sentence ending (space avoids splitting decimals)
    "! ",      # Exclamation + space — exclamatory sentence ending
    "? ",      # Question mark + space — interrogative sentence ending
    "; ",      # Semicolon + space — clause boundary
    ", ",      # Comma + space — phrase boundary
    " ",       # Space — word boundary
    "",        # Empty string — character level (last resort)
]

SEPARATORS_ZH = [
    "\n\n\n",  # 三个换行 — 章节边界
    "\n\n",    # 双换行 — 段落边界
    "\n",      # 单换行 — 行边界
    "。",      # 句号 — 陈述句句末
    "！",      # 全角感叹号 — 感叹句句末
    "？",      # 全角问号 — 疑问句句末
    "；",      # 全角分号 — 分句边界
    "，",      # 全角逗号 — 短语边界
    "、",      # 顿号 — 并列词语边界
    "",        # 字符级 — 兜底（中文无词间空格）
]

SEPARATORS_JA = [
    "\n\n\n",  # 三重改行 — 章・節の境界
    "\n\n",    # 二重改行 — 段落境界
    "\n",      # 単一改行 — 行境界
    "。",      # 句点 — 文末
    "！",      # 全角感嘆符 — 感嘆文末
    "？",      # 全角疑問符 — 疑問文末
    "；",      # 全角セミコロン — 節境界
    "、",      # 読点 — 句内区切り
    "",        # 文字レベル — 最終手段（日本語は単語間スペースなし）
]

SEPARATORS_KO = [
    "\n\n",    # 두 줄 바꿈 — 단락 경계
    "\n",      # 줄 바꿈 — 행 경계
    ". ",      # 반각 마침표 + 공백 — 문장 끝
    "! ",      # 반각 느낌표 + 공백 — 감탄문 끝
    "? ",      # 반각 물음표 + 공백 — 의문문 끝
    "。",      # 전각 마침표 — 문장 끝 (한자 혼용 문서)
    "！",      # 전각 느낌표 — 감탄문 끝
    "？",      # 전각 물음표 — 의문문 끝
    "; ",      # 반각 세미콜론 — 절 경계
    ": ",      # 반각 콜론 — 설명 도입
    ", ",      # 반각 쉼표 — 구 경계
    " ",       # 공백 — 어절 경계 (한국어는 띄어쓰기로 어절 구분)
    "",        # 문자 단위 — 최후 수단
]


SEPARATORS_CODE_PY = [
    "\nclass ",      # Class definition
    "\nasync def ",  # Async function / coroutine definition
    "\ndef ",        # Module-level function definition
    "\n    def ",    # Method definition — 4-space indent (PEP8 standard)
    "\n\tdef ",      # Method definition — tab indent (compatibility)
    "\n\n",          # Blank line between logical blocks
    "\n",            # Line break
    " ",             # Token boundary
    "",              # Character level (last resort)
]

SEPARATORS_CODE_TS = [
    "\nenum ",                  # Enum declaration
    "\ninterface ",             # Interface declaration
    "\nnamespace ",             # Namespace / module declaration
    "\ntype ",                  # Type alias declaration
    "\nexport default class ",  # Default-exported class
    "\nexport abstract class ", # Exported abstract class
    "\nexport class ",          # Named exported class
    "\nclass ",                 # Non-exported class
    "\nexport async function ",  # Exported async function
    "\nexport function ",       # Exported named function
    "\nasync function ",        # Non-exported async function
    "\nfunction ",              # Non-exported named function
    "\nexport const ",          # Exported constant / arrow function binding
    "\nexport let ",            # Exported mutable binding
    "\nconst ",                 # Constant declaration / arrow function
    "\nlet ",                   # Mutable variable declaration
    "\nvar ",                   # Legacy variable declaration
    "\nif ",                    # If statement
    "\nfor ",                   # For loop
    "\nwhile ",                 # While loop
    "\nswitch ",                # Switch statement
    "\ncase ",                  # Switch case branch
    "\ndefault ",               # Default case branch
    "\n\n",                     # Blank line between logical blocks
    "\n",                       # Line break
    " ",                        # Token boundary
    "",                         # Character level (last resort)
]

SEPARATORS_CODE_JS = [
    "\nexport default class ",  # Default-exported class
    "\nexport class ",          # Named exported class
    "\nclass ",                 # Non-exported class
    "\nexport async function ",  # Exported async function
    "\nexport function ",       # Exported named function
    "\nasync function ",        # Non-exported async function
    "\nfunction ",              # Non-exported named function
    "\nexport const ",          # Exported constant / arrow function binding
    "\nexport let ",            # Exported mutable binding
    "\nconst ",                 # Constant declaration / arrow function
    "\nlet ",                   # Mutable variable declaration
    "\nvar ",                   # Legacy variable declaration
    "\nif ",                    # If statement
    "\nfor ",                   # For loop
    "\nwhile ",                 # While loop
    "\nswitch ",                # Switch statement
    "\ncase ",                  # Switch case branch
    "\ndefault ",               # Default case branch
    "\n\n",                     # Blank line between logical blocks
    "\n",                       # Line break
    " ",                        # Token boundary
    "",                         # Character level (last resort)
]

SEPARATORS_CODE_JAVA = [
    "\nclass ",       # Class declaration
    "\ninterface ",   # Interface declaration
    "\nenum ",        # Enum declaration
    "\nrecord ",      # Record class declaration (Java 16+)
    "\n@interface ",  # Annotation type declaration
    "\npublic ",      # Public method / field / nested class
    "\nprotected ",   # Protected method / field
    "\nprivate ",     # Private method / field
    "\nstatic ",      # Static method / initializer / field
    "\nabstract ",    # Abstract method / class
    "\nif ",          # If statement
    "\nfor ",         # For loop
    "\nwhile ",       # While loop
    "\nswitch ",      # Switch statement
    "\ncase ",        # Switch case branch
    "\n\n",           # Blank line between logical blocks
    "\n",             # Line break
    " ",              # Token boundary
    "",               # Character level (last resort)
]

SEPARATORS_CODE_C = [
    "\nstruct ",    # Struct definition
    "\ntypedef ",   # Type alias definition
    "\nunion ",     # Union definition
    "\nenum ",      # Enum definition
    "\nvoid ",      # Void-returning function definition
    "\nint ",       # int-typed function or global variable
    "\nfloat ",     # float-typed function or global variable
    "\ndouble ",    # double-typed function or global variable
    "\nchar ",      # char-typed function or global variable
    "\nunsigned ",  # unsigned-typed function or global variable
    "\nlong ",      # long-typed function or global variable
    "\nstatic ",    # Static function or file-scoped variable
    "\nextern ",    # External symbol declaration
    "\nif ",        # If statement
    "\nfor ",       # For loop
    "\nwhile ",     # While loop
    "\nswitch ",    # Switch statement
    "\ncase ",      # Switch case branch
    "\n\n",         # Blank line between logical blocks
    "\n",           # Line break
    " ",            # Token boundary
    "",             # Character level (last resort)
]

SEPARATORS_CODE_CPP = [
    "\ntemplate ",   # Template declaration (class / function template)
    "\nnamespace ",  # Namespace block
    "\nclass ",      # Class definition
    "\nstruct ",     # Struct definition (value-type idiom in C++)
    "\nunion ",      # Union definition
    "\nenum ",       # Enum definition (includes scoped enum class)
    "\ntypedef ",    # Legacy type alias
    "\npublic:",     # Public access specifier section
    "\nprotected:",  # Protected access specifier section
    "\nprivate:",    # Private access specifier section
    "\nvoid ",       # Void-returning function definition
    "\nint ",        # int-typed function or variable
    "\nfloat ",      # float-typed function or variable
    "\ndouble ",     # double-typed function or variable
    "\nchar ",       # char-typed function or variable
    "\nunsigned ",   # unsigned-typed function or variable
    "\nlong ",       # long-typed function or variable
    "\nauto ",       # auto-deduced type (C++11+)
    "\nstatic ",     # Static member / file-scoped function
    "\nextern ",     # External symbol declaration
    "\nif ",         # If statement
    "\nfor ",        # For loop
    "\nwhile ",      # While loop
    "\nswitch ",     # Switch statement
    "\ncase ",       # Switch case branch
    "\n\n",          # Blank line between logical blocks
    "\n",            # Line break
    " ",             # Token boundary
    "",              # Character level (last resort)
]

SEPARATORS_CODE_GO = [
    "\nfunc ",   # Function or method definition
    "\nvar ",    # Variable declaration block
    "\nconst ",  # Constant declaration block
    "\ntype ",   # Type definition (struct, interface, alias)
    "\nif ",     # If statement
    "\nfor ",    # For loop (Go's only loop construct)
    "\nswitch ", # Switch statement
    "\ncase ",   # Switch / type-switch case branch
    "\n\n",      # Blank line between logical blocks
    "\n",        # Line break
    " ",         # Token boundary
    "",          # Character level (last resort)
]

SEPARATORS_CODE_PHP = [
    "\nfunction ", # Function definition
    "\nclass ",    # Class definition
    "\nif ",       # If statement
    "\nforeach ",  # Foreach loop
    "\nwhile ",    # While loop
    "\ndo ",       # Do-while loop
    "\nswitch ",   # Switch statement
    "\ncase ",     # Switch case branch
    "\n\n",        # Blank line between logical blocks
    "\n",          # Line break
    " ",           # Token boundary
    "",            # Character level (last resort)
]

SEPARATORS_CODE_R = [
    "\nfunction ",   # Function definition (assigned via <- or =)
    "\nsetClass(",   # S4 class definition
    "\nsetMethod(",  # S4 method definition for a generic
    "\nsetGeneric(", # S4 generic function declaration
    "\nif ",         # If statement
    "\nelse ",       # Else branch
    "\nfor ",        # For loop
    "\nwhile ",      # While loop
    "\nrepeat ",     # Repeat loop (infinite loop with explicit break)
    "\nlibrary(",    # Package loading — attaches namespace to search path
    "\nrequire(",    # Package loading — conditional, returns FALSE on failure
    "\n\n",          # Blank line between logical blocks
    "\n",            # Line break
    " ",             # Token boundary
    "",              # Character level (last resort)
]

SEPARATORS_CODE_RUST = [
    "\nstruct ",       # Struct definition
    "\nenum ",         # Enum definition
    "\ntrait ",        # Trait definition
    "\nimpl ",         # Implementation block (inherent or trait impl)
    "\nmod ",          # Module declaration
    "\npub async fn ", # Public async function
    "\npub fn ",       # Public function
    "\nasync fn ",     # Non-public async function
    "\nfn ",           # Non-public function definition
    "\npub const ",    # Public constant
    "\nconst ",        # Module-level constant
    "\nlet ",          # Variable binding (immutable by default)
    "\nuse ",          # Import / re-export declaration
    "\nif ",           # If expression
    "\nwhile ",        # While loop
    "\nfor ",          # For-in loop (iterator-based)
    "\nloop ",         # Infinite loop
    "\nmatch ",        # Pattern match expression
    "\n\n",            # Blank line between logical blocks
    "\n",              # Line break
    " ",               # Token boundary
    "",                # Character level (last resort)
]

SEPARATORS_MDX = [
    "\n# ",     # H1 heading — document title / top-level section
    "\n## ",    # H2 heading — major section
    "\n### ",   # H3 heading — subsection
    "\n#### ",  # H4 heading — sub-subsection
    "\n\n",     # Blank line — paragraph boundary
    "\n```",    # Fenced code block boundary
    "\n",       # Line break
    ". ",       # Sentence ending
    "! ",       # Exclamatory sentence ending
    "? ",       # Interrogative sentence ending
    "; ",       # Clause boundary
    ", ",       # Phrase boundary
    " ",        # Word boundary
    "",         # Character level (last resort)
]

SEPARATORS_CSV = [
    "\nRow ",  # Row boundary — matches CSVLoader's "Row N:" header format
    "\n",      # Raw line break — fallback row boundary
    " | ",     # Pipe separator — column boundary in display format
    ", ",      # Comma separator — value boundary
    " ",       # Token boundary
    "",        # Character level (last resort)
]

SEPARATORS_JSON = [
    "\n\n",  # Major structural gap — boundary between top-level objects
    "\n",    # Line break — boundary between properties
    "},",    # End of nested object within an array
    "],",    # End of nested array within an array or object
    ", ",    # Property / element separator
    ": ",    # Key-value pair separator
    " ",     # Token boundary
    "",      # Character level (last resort)
]

SEPARATORS_XML = [
    "\n\n",  # Major element gap — boundary between sibling elements
    "\n",    # Line break — boundary between tags or text lines
    ">",     # Tag-closing boundary — splits after end of opening/closing tag
    ". ",    # Sentence ending in element text content
    "! ",    # Exclamatory sentence in element text content
    "? ",    # Interrogative sentence in element text content
    ", ",    # List item boundary in element text content
    " ",     # Word boundary
    "",      # Character level (last resort)
]

SEPARATORS_CODE_HTML = [
    "<body",    # Document body root
    "<div",     # Generic block container
    "<p",       # Paragraph
    "<br",      # Line break element
    "<li",      # List item
    "<h1",      # Level-1 heading
    "<h2",      # Level-2 heading
    "<h3",      # Level-3 heading
    "<h4",      # Level-4 heading
    "<h5",      # Level-5 heading
    "<h6",      # Level-6 heading
    "<span",    # Inline container
    "<table",   # Table root
    "<tr",      # Table row
    "<td",      # Table data cell
    "<th",      # Table header cell
    "<ul",      # Unordered list
    "<ol",      # Ordered list
    "<header",  # Page / section header landmark
    "<footer",  # Page / section footer landmark
    "<nav",     # Navigation landmark
    "<head",    # Document metadata section
    "<style",   # Embedded CSS block
    "<script",  # Embedded JS block
    "<meta",    # Metadata element
    "<title",   # Document title element
    "\n\n",     # Blank line between logical blocks
    "\n",       # Line break
    " ",        # Token boundary
    "",         # Character level (last resort)
]
# fmt: on
