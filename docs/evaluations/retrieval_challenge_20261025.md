# 第二十五批：长文、标题孤块与实体混淆

本轮先扩样、冻结标签，再以真实模型比较原实现与标题合并原型。原型有收益也有回退，
因此不设为默认。生产代码只修复确定的跳级标题祖先路径错误；不改变 BM25、重排默认值、
页/section 硬边界、schema 或核心 Port。

## 语料与实验边界

- 原始 7 文档 / 18 查询 / 15 证据回归集保持独立，指纹仍为
  `77b1547c8df1560faddfb5b6f23e6ae1818f42cff7fce6546ac3801687f51c21`。
- 新增 [challenge 语料](../../evals/retrieval/challenge/corpus.json) 与
  [标签](../../evals/retrieval/challenge/judgments.json)：11 份版本文档、31 查询、26 证据，
  25 条正查询、6 条无答案；两个 KB，旧版本仍留在索引但不可见。
- 五份长手册中四份约 2 千字符，一份 12505 字符；最长文档尾部证据起于 12464。
  长附录是明确标注的合成演练记录，包含重复模板与不同故障主题，不冒充真实文档多样性。
- 包含星桥/星桥云/港湾、生产/实验/归档作业、否定引用、DBQ-71/72/73、授权码、
  标题本身含事实、末尾纯标题、只有标题的文档、同级空标题和跳级标题。
- 作者先审查询适用主体及 26 个原文证据，再冻结文件；未依据模型排名改标签。
  仍未经过独立双人或用户标注复核，不据此确定验收阈值。

扩展集指纹：`76d06b536676b174e892ce162bce33713638c4b75d1cb5979d563de68dbf9557`。
child=100 / overlap=15 / parent=300 字符、fetch_k=50、top_k=5。原实现索引 315 child、
146 parent，候选预算不再覆盖全部块。模型沿用现有 .env 的 Qwen3-VL embedding/reranker；
探测 4096 维及重排响应通过，权重版本仍 unknown。子进程仅对配置主机直连，不修改 .env。

## 标题合并原型：有条件收益，不足以默认采用

仅在连续、同 metadata/page 的无正文标题后紧跟更深标题时，将其前缀与后代组成同一
逻辑 section；文本仍为原文连续切片，不删除标题，不跨同级标题。原型只在隔离实验
进程临时替换策略并 finally 恢复，未进入生产配置或开关。其 child/parent 为 308/134。

| 内存精确召回 | 原证据@1 | 原证据@3 | 原证据@5 | 合并证据@1 | 合并证据@3 | 合并证据@5 |
|---|---:|---:|---:|---:|---:|---:|
| dense / 重排关 | 0.60 | 0.84 | 0.94 | 0.60 | 0.84 | 0.98 |
| keyword / 重排关 | 0.64 | 0.80 | 0.96 | 0.60 | 0.80 | 0.94 |
| hybrid / 重排关 | 0.64 | 0.86 | 0.96 | 0.64 | 0.90 | 0.96 |
| hybrid / 重排开 | 0.74 | 0.92 | 0.96 | 0.74 | 0.92 | 0.96 |

具体对照没有只看均值：

- `auth_code`：原关键词首位 `leaf_fact[33,47)` 含授权码 5931；合并后首位变成
  `leaf_fact[0,33)` 的“值班速查/非紧急事项”，授权码在第二位，证据@1 从 1 降为 0。
  事实没有丢失，变化来自分块对 BM25 长度/词频及候选分配的影响。
- `cloud_pair`：合并后关键词证据@5 从 0.5 降为 0，前五条不再包含任一完整目标句；
  同题 dense 从 0.5 升为 1。不能用一种模式的收益覆盖另一种模式的回退。
- `rto_pair`：dense 前五证据由 0 升为 0.5，仍未完整回答；无答案索引页依然排在前面。
- hybrid + reranker 的三项证据指标没有提升。没有足够证据为生产默认值承担边界变化。

被否决原型的准确代码如下，留作复现实验说明，不增加运行时死分支：

```python
from dataclasses import replace
from comet_rag.engines.documents.markdown.structure import MarkdownStructureAnalyzer,_ATX_HEADING
from comet_rag.ports.document import DocumentBlock

def info(text):
 headings=MarkdownStructureAnalyzer._headings(text)
 if not headings or headings[0].start!=0:return None,False
 lines=text.splitlines(keepends=True)
 n=1 if _ATX_HEADING.match(lines[0].removesuffix('\n')) else 2
 return headings[0].level,not ''.join(lines[n:]).strip()

def grouped(document):
 blocks=[];current=None;level=None;only=False
 for block in document.blocks:
  following,heading_only=info(document.markdown[block.start_char:block.end_char])
  if current is not None and only and following is not None and level is not None and following>level and current.end_char==block.start_char and current.metadata==block.metadata and current.page_number==block.page_number:
   current=DocumentBlock('section',0,current.start_char,block.end_char,block.heading_path,metadata=block.metadata)
  else:
   if current is not None:blocks.append(current)
   current=block
  level,only=following,heading_only
 if current is not None:blocks.append(current)
 return replace(document,blocks=tuple(replace(b,ordinal=i) for i,b in enumerate(blocks)))
```

## 关键词实体混淆：原生 analyzer 也不是语义过滤器

当前 InMemory 使用单字/ASCII 参考分词；Milvus 使用既有原生 `chinese` analyzer。
官方说明其采用 Jieba 与 cnalphanumonly；本轮没有重写分词器或更改 schema。见
[Milvus Chinese analyzer](https://milvus.io/docs/chinese-analyzer.md)。Milvus 2.6.22 的实际
run_analyzer 输出“星桥”为一个词，“星桥云”为“星桥、云”，并保留 DBQ-71。
否定句中的“不是”仍是普通词项，不能把词匹配当成主体或否定逻辑约束。

在既定测试库以随机前缀运行三个原生后端对照（原分块策略），复用已有 AsyncMilvusStore，
使用 Strong 与相同模型、预算、标签。文档元数据/父块仍在独立内存中；没有生产 SQL 写入。

| 原生 Milvus | Recall@5 | 证据@1 | 证据@3 | 证据@5 |
|---|---:|---:|---:|---:|
| keyword / 重排关 | 0.96 | 0.60 | 0.76 | 0.96 |
| keyword / 重排开 | 0.98 | 0.74 | 0.92 | 0.94 |
| hybrid / 重排开 | 1.00 | 0.74 | 0.92 | 0.96 |

原生 K 关闭重排的证据@1 为 0.60，参考内存为 0.64，证据@5 同为 0.96。
它把 `star_long_checksum` 的尾部 SH-31 句提升到首位，纠正参考实现首位选到星桥云的
混淆；但 `auth_code` 首位只有“值班速查”，`star_steps` 首位是操作说明而非完整步骤句。
开启重排改善首位覆盖，也可能降低前五证据覆盖；不能宣称原生分词或重排总能改善。
因此不加硬编码实体规则，不从自然语言自动制造硬过滤。比较、交叉引用、否定与未知
实体需要更大标注集和明确产品语义。InMemory 与 Milvus 不承诺相同排序。

所有自建 Milvus 集合在 finally 按精确 KB/随机前缀清理；既有集合基线 1→1。未改已有
集合、schema、analyzer 或用户数据。

## 确定的生产缺陷与修正

`MarkdownStructureAnalyzer` 原来按“栈深 = heading level - 1”截断祖先；跳级时两者不等。
例如 `# Root → #### Deep → ### Middle` 会错得 `(Root, Deep, Middle)`。
改为栈中保存真实级别，遇到新标题先弹出所有同级/更深项，再入栈，得到 `(Root, Middle)`。
四个手写边界用例中，旧实现三个失败、Setext 一个通过；修正后均通过。没有重写标题
语法、改变原文/区间、合并 section 或删除标题，也没有新增 metadata 字段。
层级策略继续保留页面/section 硬边界、父块不重叠和子块在父块内的约束。

新生成 heading_path 值修正，已存旧 metadata 不原地回填；若要修正旧资料路径，应建立
新 revision 重建，旧任务仍按已保存计划恢复。原 DOCX 快照不变。

最终代码重跑扩展四组，质量指标与原策略四组逐项一致；此修正是结构正确性改造，
没有冒充排序收益。另重跑原始 18 查询六组，保留上一轮标题孤块、实体混淆和无答案边界。

## 复现与后续

```bash
uv run python -m evals.retrieval generate --data evals/retrieval/challenge --out /tmp/challenge-corpus
uv run python -m evals.retrieval baseline --data evals/retrieval/challenge --out /tmp/challenge-offline
uv run python -m evals.retrieval real --data evals/retrieval/challenge --config /path/to/models.yaml \
  --model-revision unknown --out /tmp/challenge-models
```

CLI/配置对象入口均支持选取语料。低层采集器支持注入调用方持有的既有后端，必须声明
后端身份；采集器不关闭外部存储，生命周期与清理归调用方。CLI 默认仍为独立内存，
没有新增写入生产存储的命令行开关。挑战回归纳入独立 CI，不进入普通单测的时间预算。

21 组主要比较/复核共 573 次检索，分别归档为原型 8 组、原生后端 3 组、最终代码扩展
4 组及原集 6 组，不跨数据集混合均值。另有格式前复核，未挑最好一轮；格式化已确认
AST 相同。模型版本 unknown、作者标签与模板化合成语料仍限制结论，延迟不作生产 SLO。
原始 run/report、冻结记录、分词输出、被否决原型、代码指纹与清理证据位于
`/tmp/comet-v2-batch25-validation/`；原始运行结果和凭据不提交。

下一步仍需独立标签复核、真实文档分布/PDF/page、固定权重、token 预算、ANN/精确召回
对照、答案存在性设计及目标部署验收。六条无答案的非空返回不等于生成答案错误；本轮
没有答案生成链路，也不新增任意拒答阈值。M4-T10 与 H/I 不关闭。

## 验证记录

最终 unit 3369 passed（9.91s）、integration 295 passed/2 skipped、E2E 50、benchmark
28 通过。五项反向分别检出旧祖先栈、删标题事实、忽略语料选择、缺后端身份及提前关闭
外部 Store；最后一项曾因参考 close 为 no-op 未检出，加强可观察关闭替身后才检出。
覆盖率整体 92.80%、tasks 95.97%，Ruff/Pyright、core-only 冒烟及资源清理通过；
预算仍接近 10s，跳过项不算通过。完整边界见 [V2 记录](../../tasks/v2_plan.md)。
