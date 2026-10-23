# 向量数据库查询与过滤适配复核

复核日期：2026-10-23。对象为 Milvus、Qdrant、Weaviate、Chroma、OceanBase，以及
Comet-RAG 当前代码。本文区分官方说明、源码/本地 SDK 验证、架构建议和未验收事项。
没有部署并跑通五个后端，不能把文档支持误写成项目适配完成。

后续第十批已冻结当前两实现的 [Filter 契约](filter_semantics.md)：bool 与数字分离、
null 查询值拒绝、数值查询收紧到 ±(2**53-1)，并统一删除的空条件守卫。本报告第九批
的 int64 输入域和待统一描述保留为历史起点；现行规则以上述专门契约为准。

## 结论

需要后端适配边界，但不能靠操作符名称翻译实现“所有数据库的所有查询都等价”。
建议分成稳定的可移植查询子集、显式声明的扩展能力、以及独立的后端管理接口。
过滤谓词、候选生成、排序融合、分页聚合和一致性是不同职责；不应全部塞入 Filter AST。

模型 A 的总体方向有价值，但能力矩阵和若干关键推论不可靠。尤其不能据其矩阵认定
Chroma 是后过滤、Milvus 无全文/地理/时间类型，或只有 Qdrant 有短语能力。
“最大交集”也不是仅看算子名称就能算出的集合，必须指定版本、字段类型和存储模型。

本仓库当前只实现 Milvus + InMemory；本次修复已有 Milvus 查询值绑定，没有新增后端、
依赖、schema、公共 Filter AST 或 AsyncVectorStore 方法。引入第三个实现、改变抽象
接口或向量 schema 仍须按 AGENTS.md/spec §7 做明确决策。

## 1. 资料入口与版本纪律

| 后端 | 官方开源仓库 | 本次主要技术依据 |
|---|---|---|
| Milvus | [milvus-io/milvus](https://github.com/milvus-io/milvus) | [2.6 过滤说明](https://milvus.io/docs/v2.6.x/boolean.md)、[模板](https://milvus.io/docs/v2.5.x/filtering-templating.md)、[固定 PyMilvus 2.6.17 源码](https://github.com/milvus-io/pymilvus/blob/v2.6.17/pymilvus/client/prepare.py) |
| Qdrant | [qdrant/qdrant](https://github.com/qdrant/qdrant) | [过滤](https://qdrant.tech/documentation/search/filtering/)、[Query API](https://api.qdrant.tech/api-reference/search/query-points) |
| Weaviate | [weaviate/weaviate](https://github.com/weaviate/weaviate) | [条件参考](https://docs.weaviate.io/weaviate/api/graphql/filters)、[过滤执行](https://docs.weaviate.io/weaviate/concepts/filtering) |
| Chroma | [chroma-core/chroma](https://github.com/chroma-core/chroma) | [metadata 过滤](https://docs.trychroma.com/docs/querying-collections/metadata-filtering)、[Python API](https://docs.trychroma.com/reference/python/collection)、[查询管线](https://cookbook.chromadb.dev/core/advanced/queries/) |
| OceanBase | [oceanbase/oceanbase](https://github.com/oceanbase/oceanbase) | [4.3.5 向量功能](https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000002826817)、[4.6.2 HYBRID_SEARCH](https://www.oceanbase.com/docs/common-oceanbase-database-ai-1000000006781911) |

查询日期不是服务器兼容版本。Milvus 默认文档已指向 3.0，不能把默认文档新功能套到
本项目固定的 PyMilvus 2.6.17；同理，Qdrant 当前 Query API 标识 1.20.x，Weaviate
新嵌套过滤文档含 preview，Chroma 本地/分布式/Cloud 路径也有差别。本文没有核实每项
能力的首次引入补丁版本；未确定的版本边界不作为部署最低版本推荐。

## 2. 模型 A 的主要纠错

| 原判断 | 复核结论与影响 |
|---|---|
| Chroma ANN top_k 后再 where 过滤 | 常规本地管线先在 metadata 段求符合条件的 ID，再交 ANN。不能据此推荐它会系统性丢失过滤后的 top-k。[查询管线](https://cookbook.chromadb.dev/core/advanced/queries/) |
| Milvus 无 count API，无法做过滤计数 | 没有独立 count 方法不等于不支持计数：query 的 `output_fields=["count(*)"]` 可配 filter。仓库 count 已经这样实现。[查询说明](https://milvus.io/docs/get-and-scalar-query.md?file=query.md) |
| Milvus 无文本/短语过滤 | 有 TEXT_MATCH 与 PHRASE_MATCH，分别需要相应字段/索引设置，且不能与 BM25 排序混为一谈。[文本](https://milvus.io/docs/keyword-match.md)、[短语](https://milvus.io/docs/phrase-match.md) |
| Milvus 无地理/时间一等类型 | 当前官方资料已有 GEOMETRY、TIMESTAMPTZ；需要按具体版本/schema 验证，不能笼统标“无”。[Geometry](https://milvus.io/docs/geometry-field.md)、[2.6 SDK DataType](https://milvus.io/api-reference/cpp/v2.6.x/Collections/DataType.md) |
| Chroma metadata 不支持数组 | 当前文档有同类型标量数组及 contains/not_contains，仍不表示支持任意嵌套对象。[数组约束](https://docs.trychroma.com/docs/querying-collections/metadata-filtering#using-array-metadata) |
| Qdrant Eq 全部直接 MatchValue | 浮点不能直接 match，需用上下界相等的 range 或业务定义的容差；类型是翻译输入的一部分。[官方过滤实践](https://qdrant.tech/documentation/search-patterns/vector-search-filtering/) |
| Weaviate ContainsNone 就是普适 NOT IN | 它是属性/分词集合上的“不包含”，不能未经字段与缺失值分析直接当作通用逻辑补集。[条件语义](https://docs.weaviate.io/weaviate/api/graphql/filters) |
| 结构化 DSL 没有注入风险 | 可避免把值拼成查询语法，但不能替代字段准入、权限强制条件、资源限制、SQL 参数绑定和错误处理；这是工程推论，不是任何厂商的安全保证 |
| AST 遍历器就是唯一语义保证 | 它只表达参考解释，不能证明真实数据库执行一致；预期结果仍需手写，差分测试不能从被测遍历器自我推导 |
| 字典 DSL 不能无歧义/有界 | 外部 JSON 可用带 tag 的递归 schema、命名空间与限额；后端客户端本身也广泛用结构化对象。强类型 AST 和经过验证的 JSON DSL 可以共存 |
| 不支持原生 NOT 就绝不可能表示 | 在明确的二值语义与总字段域下，可用德摩根律和叶子补集重写；缺失/null/数组会破坏简单等价，还要防止指数膨胀，故不能无条件接受或否定 |

OceanBase 4.6.2 文档包含 bool.must_not 与全文/向量混合查询，故旧表格的问号和“无全文”
不能延续为当前否定结论。但这并不证明 4.4.1 或任意社区版安装都有相同 DSL。

## 3. “查询条件”应拆成哪些能力

| 维度 | 应描述的契约 | 不应混用的概念 |
|---|---|---|
| 字段筛选 | 等值、范围、集合、布尔、存在性、null、数组、嵌套、文本、geo | 文本包含与 BM25 relevance |
| 候选来源 | dense、sparse、BM25、多向量、按 ID 找近邻 | 所有数据库都必须使用同一种索引 |
| 检索约束 | top_k、距离阈值、候选预算、精确/近似、过滤约束 | 同一 filter 一定返回相同排序 |
| 融合重排 | RRF、加权、模型重排、分组去重 | 后端原生混搜默认配置就是服务现有 RRF |
| 结果形态 | 投影、分数及指标、游标、排序、聚合、count | ANN 页码等于稳定快照分页 |
| 数据与读写 | tenant/collection/partition、一致性、可见性、删除 | 相同谓词自动保证相同事务语义 |

Qdrant Query API 自身已涵盖预取、多阶段、多向量和融合；Weaviate 查询入口区分 vector、
BM25、hybrid、聚合；Chroma 传统 query/get 与新 search 路径不可混写。OceanBase 的
SQL WHERE、近似向量排序和 HYBRID_SEARCH 又是不同入口。因此，一个只覆盖 Eq/In
的翻译器无法称为“全查询适配”。[Qdrant Query](https://api.qdrant.tech/api-reference/search/query-points)、
[Weaviate 查询分类](https://docs.weaviate.io/weaviate/search)、[Chroma 管线](https://cookbook.chromadb.dev/core/advanced/queries/)。

## 4. 谓词能力对照：语法支持不等于语义相同

下表是能力类别和入口索引，不能直接复制成兼容性声明；具体字段类型、版本、索引
条件必须由适配器启动检查和集成测试落实。

| 条件族 | Milvus | Qdrant | Weaviate | Chroma | OceanBase |
|---|---|---|---|---|---|
| 标量等值/不等 | 比较表达式 | match / 逻辑排除；float 用 range | Equal/NotEqual，text 受 tokenizer 影响 | eq/ne | SQL 比较；DSL term/布尔组合按版本 |
| 集合候选 | IN/NOT IN | any/except；类型受限 | ContainsAny/None 或 OR，需字段语义确认 | in/nin | SQL IN；DSL terms |
| 数值范围 | 比较表达式 | range | 比较算子 | gt/gte/lt/lte | SQL 比较；DSL range |
| 布尔组合 | AND/OR/NOT | must/should/must_not/min_should | And/Or/Not | and/or；传统 where 无通用顶层 not | SQL 三值逻辑；DSL bool |
| 空值/缺失 | nullable 字段与 JSON 路径需分别核对 | is_null 与 is_empty 不等价 | IsNull，需索引设置；空串/空数组也可能归入 null-state | 不能假设支持原生 null/exists 全集 | SQL IS NULL；JSON 路径/DSL 需单独定义 |
| 数组成员/嵌套 | ARRAY/JSON 运算 | 数组通常 any，nested 保证同元素 | 集合算子；新嵌套过滤有版本/preview 限制 | 同类型标量数组，非任意嵌套对象 | SQL JSON/数组和 DSL 的支持分别评估 |
| 文本模式 | LIKE；新正则依版本 | text/phrase、prefix/substring 有版本和索引要求 | Like 通配符、分词集合 | document contains/regex 等依 API；metadata 数组 contains 是另一回事 | SQL/全文索引；DSL 全文与 wildcard 依版本 |
| 地理/日期 | 新数据类型与专用表达式，需建字段 | geo、datetime_range | geo、date | 通常需编码为可比较标量，勿宣称原生等价 | SQL 空间/日期；不可直接类推到全部 DSL |

依据分散在 [Milvus 2.6 过滤](https://milvus.io/docs/v2.6.x/boolean.md)、
[Qdrant 过滤](https://qdrant.tech/documentation/search/filtering/)、
[Weaviate 条件参考](https://docs.weaviate.io/weaviate/api/graphql/filters)、
[Chroma metadata](https://docs.trychroma.com/docs/querying-collections/metadata-filtering)、
[Chroma TS document 条件类型](https://docs.trychroma.com/reference/typescript/)、
[OB HYBRID_SEARCH](https://www.oceanbase.com/docs/common-oceanbase-database-ai-1000000006781911)。

## 5. 最影响正确性的语义反例

1. **缺失与 null**：当前内存实现用 metadata.get，缺失与显式 None 都会匹配 None。
   SQL NULL 比较是三值逻辑；Qdrant empty/null 分开；Weaviate null-state 还涉及空容器。
   不能把这些都翻成一个 IsNull 后宣布相同。
2. **标量 IN 与数组 contains**：查询 `tag in [a,b]` 可以指存储的标量属于候选集合，
   也可以指存储数组与查询集合相交。两种语义必须用不同节点，不能复用 In 偷换。
3. **嵌套同元素**：两个条件分别命中数组的不同元素，不等于存在一个元素满足两条件。
   Qdrant nested 正是为此提供明确约束。[官方示例](https://qdrant.tech/documentation/search-patterns/vector-search-filtering/)
4. **标量类型**：Python 的 True == 1 为真；数据库 bool/int 未必相等；int64 溢出、
   float 精度、NaN/Inf、数字字符串都必须定义，不能从 Any 推定可移植。
5. **字符串相等**：Weaviate word tokenizer 的匹配不是标识符的逐字相等；需要 field
   tokenization 等字段配置。SQL collation 还可能影响大小写、重音与尾随空格。
6. **否定与缺失**：以总集合求 Not(Eq(k,v)) 与只在存在 k 的对象里求 Ne(k,v) 不必相同。
   所以不能仅靠把 NOT 往叶子推就保证等价。
7. **空集合**：In(k,[]) 是否永假，空 And 是否永真，必须先有语言规范，再看是否可
   编译为常量；不能生成空 DSL 后意外变成“不过滤”。

前五类中的数据库细节见 [Qdrant payload 类型](https://qdrant.tech/documentation/concepts/payload/)、
[Weaviate tokenization](https://docs.weaviate.io/weaviate/tutorials/tokenization)、
[Milvus null 说明](https://milvus.io/docs/basic-operators.md)。其余为本项目应制定的语义，
不是已经得到五库实测支持的结论。

## 6. 过滤计划、召回与计数

Chroma 的预过滤纠错已见上文。Weaviate 用 inverted index allow-list 配合 HNSW，
而非把匹配集全部暴力算距离。[执行说明](https://docs.weaviate.io/weaviate/concepts/filtering)
Qdrant 会按索引和基数选择计划，不能简单归类为纯 pre 或 post。
[计划与索引](https://qdrant.tech/documentation/search-patterns/vector-search-filtering/)
OceanBase 的检索策略随版本演进，具体控制参数甚至有补丁门槛；不能把“4.3.5+”当作
所有策略保证。[版本化参数说明](https://docs.oceanbase.com/oceanbase-database/V4.4.2-ob_vector_search_strategy)

API 要保证的是返回项满足强制谓词，ANN 在匹配集合上的召回质量另行验收。即使使用
pre-filter，也不能承诺满足任意请求时恰好返回 k 条，更不能要求不同 ANN 索引 ID、
分数和排名逐条一致。匹配数量不足、阈值、预算、索引可见性等都可能缩短结果。

Chroma 常规 count 无 where 参数，而 get 支持 where/分页。[Python API](https://docs.trychroma.com/reference/python/collection)
可用过滤分页枚举实现计数，但要处理快照、并发写入、超时与规模成本。若仓库要求
准确 count(filter)，新增适配器必须兑现它或明确改变架构决策，不能偷偷让契约 skip。
同样，Milvus 先查 ID 再 delete 的当前实现有并发窗口与大结果集问题，本次参数化
没有解决删除原子性或无界枚举，不能借此宣称删除契约在所有竞争条件下已保证。

## 7. 建议架构与职责

**对外：经过验证的 JSON 查询模型。** HTTP DTO 可以是带判别字段的递归 JSON；
设置深度、节点数、集合长度、字符串长度与分页预算。区分用户谓词与强制 tenant/KB/
revision 谓词，后者由服务组合，不能由用户覆盖。

**内部：小型强类型谓词 IR。** 建议先定义 ScalarEq、ScalarIn、数值 Range、And、Or；
Not、Exists、IsNull、ArrayAny/All、NestedAny 等只有语义规范和真实需求齐备再进入。
值采用明确标量联合，路径采用分段字段引用，避免把点号同时解释为真实键与嵌套路径。
AST 是合理内部形式，但不是唯一可行方案；JSON DTO 经校验转换成 dataclass IR 即可。

**规划：能力与 schema 检查。** 能力应按 backend+server version+SDK version+schema+
index+operation+模式声明，而非一个 SUPPORTED_FILTER_OPS 静态集合。计划结果至少区分
原生、精确重写、有界枚举、拒绝。默认拒绝不等价或超预算的查询；显式允许的昂贵路径
要给调用方可预期的预算和一致性说明。

**编译：每后端纯 lowering。** 输出模板+绑定值、SDK 条件对象或 SQLAlchemy 表达式。
编译器不做网络 I/O，但必须拒绝当前后端无法表达的字段/操作；“只 lowering 不解释”
不能变成忽略后端类型限制。它不负责权限裁决、客户端生命周期、重试和索引自动创建。

**执行：适配器。** 使用上游官方客户端、原生异步和复用连接，解释响应/分数/游标，
管理超时与取消。参数化不适用于动态表名/列名/JSON 路径的所有位置，字段映射仍需受控。
RPC 错误不得触发“去掉 filter 再试”，检索后过滤也不能假装等价于原生过滤。

**扩展：命名能力，而非强塞共用 Port。** geo、后端原生混搜、多向量、聚合等可以用
独立可选 Port 暴露。原生管理入口若未来需要，放在明确的 backend-specific 管理层；
禁止让供应商表达式进入本项目后端中立 AsyncVectorStore 的 filter 参数。

采用 SQLAlchemy/现有官方 builder 不等于违反引擎纯计算纪律：适配器在 infrastructure，
真正的依赖检查取决于导入位置与安装分组。LangChain 等是否采用也应按实际收益、
依赖体积和语义保真度评估，不能仅凭库名作原则性否决。

## 8. 测试与分阶段落地

例如业务要求“tenant 为 acme、category 为 manual 或 faq、epoch 至少为 100”，
IR 应表达为标量 Eq、ScalarIn、Gte 的 And；以下只是目标映射，不是本轮已实现接口：

| 后端 | 对应表达形态 | 成立前提 |
|---|---|---|
| Milvus | `metadata["tenant"] == {p0} and metadata["category"] in {p1} and metadata["epoch"] >= {p2}`，值单独绑定 | JSON 路径具有约定类型；当前 compiler 仍只有 Eq/In，Range 为下一阶段 |
| Qdrant | must 内三个 FieldCondition；MatchValue、MatchAny、Range(gte) | 字段映射到 payload，配置合适索引；不得把 float 当 MatchValue |
| Weaviate | tenant.equal AND (category.equal(manual) OR category.equal(faq)) AND epoch.greater_or_equal | 字段已映射，文本按完整字段匹配、数字类型一致 |
| Chroma | `$and` 中 tenant 的 eq、category 的 in、epoch 的 gte | scalar metadata，遵循当前 where 的树结构约束 |
| OceanBase | SQLAlchemy 的列比较与 IN，或对应版本 DSL 的 bool.filter/term/terms/range | 列/JSON 投影映射受控，值绑定，collation 与缺失值策略已确定 |

tenant 等强制谓词由服务加入，各后端不暴露可让用户替换整棵系统过滤树的入口。字段
映射本身属于 adapter schema profile，不应通过随意把字符串中的点号替换成路径来实现。

1. 手写字段域、真值表和反例，含缺失/null、空集合、bool/int、Unicode、引号、控制符、
   数组不同元素、有限数值边界。不要把参考解释器的输出直接当全部预期值。
2. 编译器单测检查结构/参数分离、绑定值原样保留、输入不变、稳定命名与限额拒绝。
3. 用真实 SDK serializer/请求构造验证传输，避免 AsyncMock 把拼错的参数名也判通过。
4. 真实后端用标量 query/scroll/get 或精确检索做谓词集合差分，ANN 单独检验无违规项、
   召回率和排序指标；不同后端不要求近似 top-k 一模一样。
5. 故障注入覆盖 RPC 超时/取消、参数不支持、分页中断、空 ID、删除响应丢失；反向注入
   漏传 filter_params、参数碰撞、漏括号、字段路径逃逸和去过滤重试，确认断言变红。

建议先完成现有两实现的正式类型/缺失/null 规范，再做 Filter AST 决策与第二个真实后端
试点。五库并行实现会把语义、schema、索引与运维问题同时放大；当前研究还不足以选择
一个“所有后端都逐字一致”的完整操作符交集。

## 9. 本轮代码改动与明确限制

新增 Milvus compile_filter，现有 dense、keyword、count、delete 的选取查询统一使用
filter_params；删除 ID 使用独立 ids 占位符，与 metadata 的 f0/f1… 不碰撞。字段键
使用 JSON 字面量转义，补足控制字符。保留旧 build_expression 供现有导入兼容，生产
RPC 已不使用它；它不是新查询 API，也不作为参数化失败的回退路径。

绑定域为 str/bool/int64/有限 float，以及同类型的 list/tuple/set 候选集合。None、
对象、异构集合和非法数值明确抛 ValueError：这是当前 Milvus 输入的可见收紧，避免
旧实现把任意对象字符串化、生成 null/nan 或到远端才失败。尤其不宣称已经统一 InMemory
对 None、bool/int 和复杂对象的比较；这些属于下一阶段契约决策。空数组已验证 SDK
能编码，但服务器端实际行为仍需真实集成验收。

本地检查了 PyMilvus 2.6.17：AsyncMilvusClient 将 filter_params 映射到 expr_params，
Prepare.prepare_expression_template 序列化为 protobuf；None、含 None 数组不能正常绑定，
部分异构数组也失败。上述是本地可重复 SDK 证据，不是“所有 filter 都能直接绑定”。

社区资料用于发现回归风险而非推翻当前规范，例如 [Chroma 0.5.20 大集合过滤问题](https://github.com/chroma-core/chroma/issues/4089)
与 [Chroma 1.0.13 query/get 差异报告](https://github.com/chroma-core/chroma/issues/5367)。
issue 的现象、版本和状态必须保留，不能归纳成所有版本的固定查询计划。

验证结果记录于 tasks/v2_plan.md 第九批；本报告不代表五个后端已实测适配。
