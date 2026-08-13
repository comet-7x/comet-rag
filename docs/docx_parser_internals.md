# DOCX 解析底层逻辑笔记

> 适用代码：`comet_rag/engines/parsers/docx_parser/`
> 覆盖文件：`docx_parser.py` · `omml.py` · `latex_dict.py`

---

## 第一层：DOCX 文件是什么

**DOCX 就是一个 ZIP 压缩包。** 把任何 `.docx` 改名为 `.zip` 解压，里面是这样的结构：

```
word/
  document.xml          ← 正文内容（最核心）
  numbering.xml         ← 列表编号定义
  styles.xml            ← 样式表（Heading 1 是什么字体等）
  theme/
  media/
    image1.png          ← 嵌入图片的原始字节
  _rels/
    document.xml.rels   ← 关系表（image rId → 实际文件的映射）
[Content_Types].xml
```

`document.xml` 是整个文档的**唯一真相来源**。文字、表格、图片锚点、公式，都按照**在页面上出现的物理顺序**写在这一个 XML 文件里。

---

## 第二层：为什么 python-docx 用起来会"缺东西"

`python-docx` 给你提供了 `doc.paragraphs`、`doc.tables` 这样的接口。**问题在于这是两个独立的列表，不反映顺序。**

举个例子，文档结构实际上是：

```
段落A → 表格1 → 段落B → 公式1 → 图片1 → 段落C
```

但 `python-docx` 给你的是：

```python
doc.paragraphs  # → [段落A, 段落B, 段落C]   没有表格、公式、图片
doc.tables  # → [表格1]                   独立列表，顺序信息丢失
```

`python-docx` 根本不解析公式（OMML），图片只给你关系 ID 而不帮你提取字节。

**结论：必须直接操作底层 XML，才能保证内容顺序和完整性。**

---

## 第三层：XML 长什么样——建立直觉

解压 `document.xml`，正文 `<w:body>` 下面的结构大约是这样的：

```xml
<w:body>

  <!-- 普通段落 -->
  <w:p>
    <w:pPr>
      <w:pStyle w:val="Heading1"/>   <!-- 段落样式：标题1 -->
    </w:pPr>
    <w:r>                             <!-- run = 一段连续格式相同的文字 -->
      <w:rPr><w:b/></w:rPr>          <!-- run 属性：加粗 -->
      <w:t>这是标题</w:t>
    </w:r>
  </w:p>

  <!-- 表格 -->
  <w:tbl>
    <w:tr>                            <!-- table row -->
      <w:tc><w:p>...</w:p></w:tc>    <!-- table cell，内部还是段落 -->
    </w:tr>
  </w:tbl>

  <!-- 含公式的段落 -->
  <w:p>
    <w:r><w:t>设 </w:t></w:r>
    <m:oMath>                         <!-- OMML，python-docx 不认识 -->
      <m:r><m:t>x</m:t></m:r>
      <m:f>
        <m:num><m:r><m:t>1</m:t></m:r></m:num>
        <m:den><m:r><m:t>2</m:t></m:r></m:den>
      </m:f>
    </m:oMath>
    <w:r><w:t> 时</w:t></w:r>
  </w:p>

  <!-- 含图片的段落 -->
  <w:p>
    <w:r>
      <w:drawing>                     <!-- 图片锚点 -->
        <wp:extent cx="1234" cy="567"/>
        <a:blip r:embed="rId5"/>      <!-- rId5 → word/media/image1.png -->
      </w:drawing>
    </w:r>
  </w:p>

</w:body>
```

**关键结论：`<w:body>` 的直接子节点的顺序就是内容的真实顺序。** 必须遍历这个顺序，而不是用 `doc.paragraphs` 这种预分类列表。

### XML 命名空间

XML 标准要求每个标签有唯一的命名空间，防止不同规范的同名标签冲突。`w:p` 的完整写法是：

```
{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p
```

代码里的命名空间常量：

```python
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"  # Word 主体
_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"  # OMML 公式
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"  # DrawingML 图形
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"  # 关系
_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
```

查找子节点时必须带完整命名空间：

```python
element.find(f"{{{_W}}}rPr")  # 找 <w:rPr>
element.find(f"{{{_M}}}t")  # 找 <m:t>
```

---

## 第四层：`_walk` — 代码的核心骨架

```python
# docx_parser.py
def _walk(self, container: Any) -> None:
    for child in container:  # 按 XML 的物理顺序遍历
        tag = _qname(child)  # 取 local name，如 "p" / "tbl" / "sdt"
        if tag == "p":
            self._handle_paragraph(child)
        elif tag == "tbl":
            self._close_list()
            self._handle_table(child)
        elif tag == "sdt":  # 结构化内容控件（目录、书签等容器）
            sdt_content = child.find(f"{{{_W}}}sdtContent")
            if sdt_content is not None:
                self._walk(sdt_content)  # 递归穿透
```

`_qname` 把带命名空间的完整 tag 剥离成本地名：

```python
def _qname(element: Any) -> str:
    return etree.QName(element).localname


# "{http://...}p" → "p"
```

这段代码的设计思想是：**按顺序遍历 XML，遇到不同 tag 分发到不同处理函数**，保证了块的输出顺序与文档原始顺序一致。

---

## 第五层：一个段落的处理流程

`_handle_paragraph` 一次性完成了五件事：

```
1. 提取纯文本         _get_paragraph_text()
      ↓
2. 找出公式并标记     _handle_equations()
      ↓ 返回带 <eq>…</eq> 标记的字符串
3. 提取格式化元素     _get_paragraph_elements()
      ↓ 返回 [(文字, 格式, URL), ...]
4. 拼装富文本         _build_rich_text()
      ↓ 返回 "**加粗** $公式$ 普通文字"
5. 判断段落类型并输出 heading / list / equation / caption / text
```

**为什么要分「纯文本」和「格式化元素」两步提取？**

公式识别（步骤 2）需要按照 XML 节点的位置顺序来插入公式标记，而格式化（步骤 3）是在 run 层面独立做的。两者最后在 `_build_rich_text` 里合并。

### `_handle_equations` 的工作原理

```python
for subt in element.iter():
    tag = _qname(subt)
    if tag == "t" and f"{{{_M}}}" not in subt.tag:
        # w:t → 普通文字，记录到 combined
        only_texts.append(subt.text)
        combined.append(subt.text)
    elif "oMath" in subt.tag and "oMathPara" not in subt.tag:
        # m:oMath → 公式，转换后以 <eq>…</eq> 标记插入
        latex = self._convert_omath(subt)
        marker = f"<eq>{latex}</eq>"
        only_eqs.append(marker)
        combined.append(marker)
```

`combined` 保持了「文字片段」和「公式标记」在段落中的原始顺序，最终用于精确拼接出带位置的字符串。

---

## 第六层：OMML → LaTeX，完整拆解

### OMML 结构示例

公式 $\frac{1}{2}$ 在 XML 里长这样：

```xml
<m:oMath>
  <m:f>
    <m:num>
      <m:r><m:t>1</m:t></m:r>
    </m:num>
    <m:den>
      <m:r><m:t>2</m:t></m:r>
    </m:den>
  </m:f>
</m:oMath>
```

公式的 XML 结构是嵌套树，对应的 LaTeX 也是嵌套结构，**树的递归遍历天然对应公式的递归转换**。

### `tag2meth` 路由表

`omml.py` 的核心设计：每个 OMML 标签绑定一个处理方法。

```python
tag2meth: ClassVar[dict[str, Callable]] = {
    "acc": do_acc,  # 重音符  \hat{x}, \vec{x}
    "r": do_r,  # 叶子文字节点
    "bar": do_bar,  # 上划线/下划线  \overline{x}
    "sub": do_sub,  # 下标  _{...}
    "sup": do_sup,  # 上标  ^{...}
    "f": do_f,  # 分数  \frac{num}{den}
    "func": do_func,  # 函数  \sin(x)
    "groupChr": do_groupchr,  # 组合符号  \overbrace, \underbrace
    "d": do_d,  # 括号对  \left(...\right)
    "rad": do_rad,  # 根号  \sqrt[n]{x}
    "eqArr": do_eqarr,  # 方程组
    "limLow": do_limlow,  # 下极限  \lim_{n→∞}
    "limUpp": do_limupp,  # 上覆盖  \overset{...}{...}
    "lim": do_lim,  # 极限表达式
    "m": do_m,  # 矩阵
    "mr": do_mr,  # 矩阵行
    "nary": do_nary,  # n元运算符 ∑ ∫ ∏
}
```

### 分数的完整调用链

```
oMath2Latex.__init__
  → process_children(oMath)
      → 发现子节点 <m:f>，stag = "f"
      → tag2meth["f"] = do_f，调用之

def do_f(self, elm):
    c = self.process_children_dict(elm)
    # c = {"num": "1", "den": "2", "fPr": <Pr对象>}
    pr = c.get("fPr")
    # pr.type = None（没有 <m:type>），用 F_DEFAULT
    latex_s = get_val(pr.type, default=F_DEFAULT, store=F)
    # F_DEFAULT = "\\frac{{{num}}}{{{den}}}"
    return latex_s.format(num="1", den="2")
    # → "\\frac{1}{2}"
```

### `_direct_tags`：透明容器

某些 OMML 标签只是结构性包装，不产生任何 LaTeX 输出，内容直接拼接：

```python
_direct_tags = (
    "box",  # 盒子容器
    "sSub",  # 下标结构的外层
    "sSup",  # 上标结构的外层
    "sSubSup",  # 上下标结构
    "num",  # 分子
    "den",  # 分母
    "deg",  # 根号次数
    "e",  # 通用表达式槽
)
```

### `do_r`：最底层叶节点

```python
def do_r(self, elm: Any) -> str:
    # 1. 取 <m:t> 里的文字
    found_text = elm.findtext(f"./{OMML_NS}t")

    # 2. 每个字符查 T 字典（数学斜体字母、希腊字母等）
    for s in found_text:
        _str.append(_process_unicode(s))  # T.get(s, s)

    # 3. 转义 LaTeX 特殊字符（{ } # & 等）
    proc = escape_latex("".join(_str))

    # 4. 如果有 <m:rPr><m:scr> 指定字体（mathscr/mathbb 等），包装命令
    scr = rPr.find(f"{OMML_NS}scr")
    if scr is not None:
        template = SCR_TO_LATEX.get(scr.get(f"{OMML_NS}val", ""))
        proc = template.format(proc.strip())

    return proc
```

### `latex_dict.py` 三张核心字典

| 字典 | 键 | 值 | 用途 |
|---|---|---|---|
| `CHR` | `"̂"` 等组合变音符 | `"\\hat{{{0}}}"` 格式字符串 | `do_acc` / `do_groupchr` 查表 |
| `CHR_BO` | `"∑"` 等大运算符字符 | `"\\sum"` 等命令 | `do_nary` 查操作符 |
| `T` | `"\U0001d6fc"` 数学斜体字符等 | `"\\alpha "` 等命令 | `do_r` 字符级替换 |

---

## 第七层：图片提取原理

```python
# docx_parser.py _extract_images()

# 1. 找到 <w:drawing> 内的图片元素
blip = drawing.find(f".//{qn('a:blip')}")

# 2. 取关系 ID（如 "rId5"）
rId = blip.get(_R_EMBED)  # _R_EMBED = "{...relationships}embed"

# 3. 通过关系表找到实际的图片文件 part
image_part = self._doc_part.rels[rId].target_part
# 关系表存在 word/_rels/document.xml.rels，python-docx 已解析好

# 4. 取原始字节，Base64 编码
content = base64.b64encode(image_part.blob).decode()
```

---

## 代码知识地图

```
docx_parser.py
├── DocxParser.parse()
│   ├── _walk(body)                   ← 按 XML 顺序遍历，保证内容顺序
│   │   ├── _handle_paragraph()
│   │   │   ├── _get_paragraph_text()
│   │   │   ├── _handle_equations()   ← 找 m:oMath，调 oMath2Latex
│   │   │   ├── _get_paragraph_elements()  ← 处理 w:r / w:hyperlink / fldChar
│   │   │   └── _build_rich_text()    ← 把公式标记和 Markdown 格式拼在一起
│   │   └── _handle_table()
│   └── _add_headers_footers()        ← header/footer 存在 section，单独处理

omml.py
├── oMath2Latex                       ← 主转换器，入口
│   ├── tag2meth                      ← OMML标签 → 方法 的路由表
│   ├── do_f / do_rad / do_nary / ... ← 每个方法处理一种数学结构
│   ├── do_r                          ← 叶节点：字符 → LaTeX 查表
│   └── process_unknow                ← 透明容器 / Pr属性对象的兜底逻辑
├── Pr                                ← 属性元素（<m:fPr> <m:accPr> 等）
│   └── do_common / do_brk
└── Tag2Method                        ← 基类，提供 process_children* 递归框架

latex_dict.py                         ← 纯数据，所有映射表（无逻辑）
├── CHR      组合变音符 → 格式字符串
├── CHR_BO   大运算符字符 → LaTeX命令
└── T        Unicode字符 → LaTeX命令
```

---

## 调试技巧

### 查看任意元素的原始 XML

```python
from lxml import etree

print(etree.tostring(element, pretty_print=True).decode())
```

### 单独测试公式转换

```python
from comet_rag.engines.parsers.docx_parser.omml import oMath2Latex
from lxml import etree

xml = """<m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">
  <m:f>
    <m:num><m:r><m:t>1</m:t></m:r></m:num>
    <m:den><m:r><m:t>2</m:t></m:r></m:den>
  </m:f>
</m:oMath>"""

el = etree.fromstring(xml)
print(str(oMath2Latex(el)))  # → \frac{1}{2}
```

### 查看 document.xml 原文

```python
import zipfile

with zipfile.ZipFile("your_file.docx") as z:
    print(z.read("word/document.xml").decode())
```

---

## 核心设计思想（一句话总结）

> XML 的树形结构 = 数学公式的嵌套结构，树的递归遍历天然对应公式的递归转换。`_walk` 保证内容顺序，`oMath2Latex` 的 `tag2meth` 路由表 + `process_children` 递归保证公式结构完整转换。
