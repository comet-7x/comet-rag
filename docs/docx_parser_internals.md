# DOCX 解析底层逻辑笔记

> 适用代码：`comet_rag/engines/documents/docx/`
> 覆盖文件：`parser.py` · `formatting.py` · `omml.py` · `latex_dict.py`

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
# parser.py
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

`_get_paragraph_elements()` 按 XML 顺序处理普通 run、Hyperlink、字段结果和公式，
生成文字/格式/URL 元组或公式片段。`_build_rich_text()` 再合并为 Markdown；
`_handle_paragraph()` 负责结合标题、编号与图片信息生成对应块。

### 格式解析与缓存边界

`formatting.py::RunFormatResolver` 只处理 run 格式；文档遍历、段落分类与 Markdown
输出仍由 `parser.py` 负责。每个属性的优先级保持为：

1. run 直接格式。
2. 字符样式及其继承链；样式 id 或名称含 `hyperlink` 时跳过整条字符样式链。
3. 段落样式及其继承链。
4. 都未定义时使用 `False`。

只有 `None` 表示继续继承，显式 `False` 会终止查找。有效的 `w:em` 最终将下划线
标记置为 `True`。下划线目前不生成 Markdown 标记，`docDefaults` 也不参与既有
格式解析；本次性能改造保留这两个边界。

python-docx 1.2.0 的默认样式查询会遍历样式表，旧实现对每个 run 的四个属性重复
查询字符和段落样式。解析器现在按 `(style_id, style_type)` 缓存 SDK 查询结果，
仍由 SDK 决定默认、缺失、错误类型和多个默认样式的回退规则；按样式 XML 元素与
属性缓存继承结果，保留 `None` 与 `False` 的区别。查询与继承均按需执行，避免读取
被直接格式遮住的非法样式值。

缓存只属于一次 `parse()`，在现有解析锁内创建，成功或失败都在 `finally` 中释放。
同一解析器处理新文档，或原文档修改样式后重新解析，都会重新读取样式；调用期间
不得并发修改输入文档。正文、表格与两种链接路径使用同一解析器，页眉页脚仍走
原来的纯文本提取。回归与反向验证记录见 `tasks/v2_plan.md` 第六批。

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
# parser.py _extract_images()

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
parser.py
├── DocxParser.parse()
│   ├── _walk(body)                   ← 按 XML 顺序遍历，保证内容顺序
│   │   ├── _handle_paragraph()
│   │   │   ├── _get_paragraph_elements()  ← run / 链接 / 字段 / OMML
│   │   │   └── _build_rich_text()    ← 文字格式与公式片段拼成 Markdown
│   │   └── _handle_table()
│   └── _add_headers_footers()        ← header/footer 存在 section，单独处理

formatting.py
├── RunFormat                        ← 四项已解析格式标记
└── RunFormatResolver                ← 每次 parse 内的惰性样式查找与继承缓存

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
from comet_rag.engines.documents.docx.omml import oMath2Latex
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

## 有效 XML 与不完整字段边界

列表属性按Word命名空间URI读取，不要求来源声明`w`前缀；默认命名空间的普通段落
和编号段落均可解析。段落末尾缺字段end时保留已出现的显示结果，不输出指令文字。
题注只匹配SEQ指令token，REF/NOTEREF中包含SEQ的锚点不当作题注。生成XML回归见
`tests/unit/engines/test_docx_valid_input_edges.py`；既有快照未重生成。该修复不扩展
文本框、嵌套表或二进制OLE支持，也不声称完整嵌套字段状态机。
