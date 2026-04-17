SEPARATORS_EN = [
    "\n\n\n",  # Multiple line breaks (sections)
    "\n\n",  # Paragraph breaks
    "\n",  # Line breaks
    ". ",  # Sentence endings
    "! ",  # Exclamation endings
    "? ",  # Question endings
    "; ",  # Semicolon breaks
    ", ",  # Comma breaks
    " ",  # Word breaks
    "",  # Character level
]

SEPARATORS_ZH = [
    "\n\n\n",  # 多个换行（章节）
    "\n\n",  # 段落分隔
    "\n",  # 换行
    "。",  # 句号
    "！",  # 感叹号
    "？",  # 问号
    "；",  # 分号
    "，",  # 逗号
    "、",  # 顿号
    "",  # 字符级（中文无词间空格）
]

SEPARATORS_JA = [
    "\n\n\n",  # 複数改行（章・節）
    "\n\n",  # 段落区切り
    "\n",  # 改行
    "。",  # 句点
    "！",  # 感嘆符
    "？",  # 疑問符
    "；",  # セミコロン
    "、",  # 読点
    "",  # 文字レベル（日本語は単語間スペースなし）
]

SEPARATORS_KO = [
    "\n\n",  # 段落
    "\n",  # 换行
    ". ",
    "! ",
    "? ",  # 句末（通常带空格）
    "。",
    "！",
    "？",  # 全角句末
    "; ",
    ": ",  # 句中
    ", ",  # 逗号
    " ",  # 单词间距（韩语书写有空格隔开词组）
    "",  # 字符
]


SEPARATORS_CODE_PY = [
    # First, try to split along class definitions
    "\nclass ",
    "\nasync def ",
    "\ndef ",
    "\n    def ",  # 4-space indent (PEP8 standard)
    "\n\tdef ",    # tab indent (compatibility)
    # Now split by the normal type of lines
    "\n\n",
    "\n",
    " ",
    "",
]

SEPARATORS_CODE_TS = [
    "\nenum ",
    "\ninterface ",
    "\nnamespace ",
    "\ntype ",
    # Split along class definitions
    "\nexport default class ",
    "\nexport abstract class ",
    "\nexport class ",
    "\nclass ",
    # Split along function definitions
    "\nexport async function ",
    "\nexport function ",
    "\nasync function ",
    "\nfunction ",
    "\nexport const ",
    "\nexport let ",
    "\nconst ",
    "\nlet ",
    "\nvar ",
    # Split along control flow statements
    "\nif ",
    "\nfor ",
    "\nwhile ",
    "\nswitch ",
    "\ncase ",
    "\ndefault ",
    # Split by the normal type of lines
    "\n\n",
    "\n",
    " ",
    "",
]

SEPARATORS_CODE_JS = [
    # Split along class definitions
    "\nexport default class ",
    "\nexport class ",
    "\nclass ",
    # Split along function definitions
    "\nexport async function ",
    "\nexport function ",
    "\nasync function ",
    "\nfunction ",
    "\nexport const ",
    "\nexport let ",
    "\nconst ",
    "\nlet ",
    "\nvar ",
    # Split along control flow statements
    "\nif ",
    "\nfor ",
    "\nwhile ",
    "\nswitch ",
    "\ncase ",
    "\ndefault ",
    # Split by the normal type of lines
    "\n\n",
    "\n",
    " ",
    "",
]

SEPARATORS_CODE_JAVA = [
    # Split along class/type definitions
    "\nclass ",
    "\ninterface ",
    "\nenum ",
    "\nrecord ",
    "\n@interface ",
    # Split along method definitions
    "\npublic ",
    "\nprotected ",
    "\nprivate ",
    "\nstatic ",
    "\nabstract ",
    # Split along control flow statements
    "\nif ",
    "\nfor ",
    "\nwhile ",
    "\nswitch ",
    "\ncase ",
    # Split by the normal type of lines
    "\n\n",
    "\n",
    " ",
    "",
]

SEPARATORS_CODE_C = [
    # Split along struct/type definitions
    "\nstruct ",
    "\ntypedef ",
    "\nunion ",
    "\nenum ",
    # Split along function definitions
    "\nvoid ",
    "\nint ",
    "\nfloat ",
    "\ndouble ",
    "\nchar ",
    "\nunsigned ",
    "\nlong ",
    "\nstatic ",
    "\nextern ",
    # Split along control flow statements
    "\nif ",
    "\nfor ",
    "\nwhile ",
    "\nswitch ",
    "\ncase ",
    # Split by the normal type of lines
    "\n\n",
    "\n",
    " ",
    "",
]

SEPARATORS_CODE_CPP = [
    # Split along class/type definitions
    "\ntemplate ",
    "\nnamespace ",
    "\nclass ",
    "\nstruct ",
    "\nunion ",
    "\nenum ",
    "\ntypedef ",
    # Split along access specifiers
    "\npublic:",
    "\nprotected:",
    "\nprivate:",
    # Split along function definitions
    "\nvoid ",
    "\nint ",
    "\nfloat ",
    "\ndouble ",
    "\nchar ",
    "\nunsigned ",
    "\nlong ",
    "\nauto ",
    "\nstatic ",
    "\nextern ",
    # Split along control flow statements
    "\nif ",
    "\nfor ",
    "\nwhile ",
    "\nswitch ",
    "\ncase ",
    # Split by the normal type of lines
    "\n\n",
    "\n",
    " ",
    "",
]

SEPARATORS_CODE_GO = [
    # Split along function definitions
    "\nfunc ",
    "\nvar ",
    "\nconst ",
    "\ntype ",
    # Split along control flow statements
    "\nif ",
    "\nfor ",
    "\nswitch ",
    "\ncase ",
    # Split by the normal type of lines
    "\n\n",
    "\n",
    " ",
    "",
]

SEPARATORS_CODE_PHP = [
    # Split along function definitions
    "\nfunction ",
    # Split along class definitions
    "\nclass ",
    # Split along control flow statements
    "\nif ",
    "\nforeach ",
    "\nwhile ",
    "\ndo ",
    "\nswitch ",
    "\ncase ",
    # Split by the normal type of lines
    "\n\n",
    "\n",
    " ",
    "",
]

SEPARATORS_CODE_R = [
    # Split along function definitions
    "\nfunction ",
    # Split along S4 class and method definitions
    "\nsetClass(",
    "\nsetMethod(",
    "\nsetGeneric(",
    # Split along control flow statements
    "\nif ",
    "\nelse ",
    "\nfor ",
    "\nwhile ",
    "\nrepeat ",
    # Split along package loading
    "\nlibrary(",
    "\nrequire(",
    # Split by the normal type of lines
    "\n\n",
    "\n",
    " ",
    "",
]

SEPARATORS_CODE_RUST = [
    # Split along type/trait definitions
    "\nstruct ",
    "\nenum ",
    "\ntrait ",
    "\nimpl ",
    "\nmod ",
    # Split along function definitions
    "\npub async fn ",
    "\npub fn ",
    "\nasync fn ",
    "\nfn ",
    "\npub const ",
    "\nconst ",
    "\nlet ",
    "\nuse ",
    # Split along control flow statements
    "\nif ",
    "\nwhile ",
    "\nfor ",
    "\nloop ",
    "\nmatch ",
    # Split by the normal type of lines
    "\n\n",
    "\n",
    " ",
    "",
]

SEPARATORS_MDX = [
    "\n# ",    # H1 headers (top-level sections)
    "\n## ",   # H2 headers (major sections)
    "\n### ",  # H3 headers (subsections)
    "\n#### ", # H4 headers (sub-subsections)
    "\n\n",    # Paragraph breaks
    "\n```",   # Code block boundaries
    "\n",      # Line breaks
    ". ",      # Sentence endings
    "! ",      # Exclamation endings
    "? ",      # Question endings
    "; ",      # Semicolon breaks
    ", ",      # Comma breaks
    " ",       # Word breaks
    "",        # Character level
]

SEPARATORS_CSV = [
    "\nRow ",  # Row boundaries (from CSVLoader format)
    "\n",      # Line breaks
    " | ",     # Column separators
    ", ",      # Comma separators
    " ",       # Word breaks
    "",        # Character level
]

SEPARATORS_JSON = [
    "\n\n",    # Object/array boundaries
    "\n",      # Line breaks
    "},",      # Object endings
    "],",      # Array endings
    ", ",      # Property separators
    ": ",      # Key-value separators
    " ",       # Word breaks
    "",        # Character level
]

SEPARATORS_XML = [
    "\n\n",    # Element boundaries
    "\n",      # Line breaks
    ">",       # Tag endings
    ". ",      # Sentence endings (for text content)
    "! ",      # Exclamation endings
    "? ",      # Question endings
    ", ",      # Comma separators
    " ",       # Word breaks
    "",        # Character level
]

SEPARATORS_CODE_HTML = [
    # First, try to split along HTML tags
    "<body",
    "<div",
    "<p",
    "<br",
    "<li",
    "<h1",
    "<h2",
    "<h3",
    "<h4",
    "<h5",
    "<h6",
    "<span",
    "<table",
    "<tr",
    "<td",
    "<th",
    "<ul",
    "<ol",
    "<header",
    "<footer",
    "<nav",
    # Head
    "<head",
    "<style",
    "<script",
    "<meta",
    "<title",
    "",
]
