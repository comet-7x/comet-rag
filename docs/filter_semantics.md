# Filter 契约

生效于 V2 第十批（2026-10-23）。本规范定义 InMemory 与 Milvus 共同兑现的行为，
不是五库查询能力的并集。公共导入仍为 comet_rag.ports.Filter；值域与解释位于
comet_rag.ports.filtering，只有标准库依赖。

## 类型与组合

Filter 是 Mapping[str, FilterValue]。标量只接受 Python 内建 str、bool、int、float，
不接收自定义对象、数字字符串转换、枚举或数值类型子类。字段键必须为非空字符串。
字段名按字面含义匹配：a.b 是一个完整键，不解释成两级路径。字段间隐式 AND。

FilterValue 可以是一个标量，或由同一 Python 标量类型构成的 list、tuple、set。
集合表示“存储标量属于候选集合”，不是存储数组的成员匹配，也不是数组整体相等。
混合类型候选（包括 int/float 混合或 bool/int 混合）拒绝；重复候选无额外意义。
Python set 自身在进入本 API 前的去重不可逆，不承诺恢复已经合并的 True/1 等元素。
规范化将候选复制为独立 list，避免 I/O 等待期间调用方修改集合改变已提交查询。

| 条件 | 行为 |
|---|---|
| filter=None 或 {} | 无过滤；读取全部符合其他检索约束的记录 |
| {v: []} | 永不匹配；不会当成无过滤 |
| {v: True} | 只匹配布尔 true，不匹配整数 1 或浮点 1.0 |
| {v: 1}、{v: 1.0} | 两者都匹配数值 1/1.0；不匹配 true 或字符串 1 |
| {v: [1]}、{v: [1.0]} | 同上；不匹配存储数组 [1] |
| {v: ""} | 匹配空字符串，不匹配 null 或缺失字段 |
| 缺失字段、null、数组或对象 | 不匹配任何本规范的标量等值/IN 谓词 |
| {v: None} 或候选含 None | ValueError；HTTP 请求校验失败，不猜测为 IS NULL/Exists |

暂无 OR、NOT、范围、嵌套、Exists、IsNull 或 ArrayContains 运算。若未来需要，应新增
有区分度的谓词模型并经决策扩展；不能通过特殊字典键或 None 偷渡新操作符。

## 数值精度

所有查询数值必须有限，且位于闭区间 [-(2**53-1), 2**53-1]。拒绝 NaN、正负 Inf
和越界整数/浮点数；不夹取、不舍入、不转字符串。范围内 int/float 用数值相等，
不要求 Python 类型相同；bool 独立。正零和负零数值相等。

这比上一批的 int64 查询范围更窄，原因来自真实 Milvus 2.6.22 探测：用浮点
9007199254740992.0 比较 JSON 路径，会同时命中整数 9007199254740992 和
9007199254740993；Python 的相等判定不同。只宣称 int64 加有限 float 不足以保证
一致性，因此选择保守的可移植查询域。高精度 ID 应使用字符串或另行设计专门字段，
不能把 ID 擅自改存 double。本批没有重写已存 metadata 或改变向量 schema。

## 校验与职责

normalize_filter 完整验证并复制条件，不依赖存储中有没有记录或第一条是否命中。
HTTP SearchRequest 与库用例 SearchQuery 在 Pydantic 转换前校验，保留 bool/int/float
类型；服务在执行前复核，防止构造后直接赋非法值绕过入口。

两种存储的 dense、keyword、count、delete 都在过滤执行前调用同一验证职责。Milvus
在首次 collection 发现 I/O 之前完成编译/快照；空关键词和空 IDs 也不能绕过校验。
matches_filter 是对已验证条件的纯解释器；存储入口负责完整校验，它不负责读取元数据、
连接管理或服务降级。Milvus compiler 只把验证后的条件翻译为模板与绑定参数，不再
维护一套独立类型规则。

非法 Filter 在存储层抛 ValueError，在 HTTP DTO 处形成请求校验错误；不静默忽略字段，
不把坏值强制转字符串，不移除过滤重试。检索取消和 RPC 超时继续按既有机制传播。

## 删除范围守卫

读取的无过滤语义不等于允许隐式删除全库。delete 必须提供 IDs 或非空 Filter；
ids=None 且 filter=None/{} 均拒绝。ids=[] 明确表示零条删除，但仍校验 Filter。
有 IDs 又有 Filter 时两者取交集。清库有独立的 collection 生命周期操作。

这是已确认的行为收紧：原 InMemory 对 delete(filter={}) 会删全库，而 Milvus 的
先查 ID 路径会因空表达式且无 limit 报错。本批不通过构造恒真表达式去绕过服务器，
而是把删除所需的明确范围统一为契约。

## 验收方法与边界

共享的 tests/contracts/filter_semantics.py 使用手写真值表，在同一批含 bool/int/float/
字符串/null/缺失/数组/对象的记录上检查 dense、keyword、count 与 delete。它还检查
条件无匹配时，非法后续字段也必须失败，不能先产生部分删除。

单测另验证非法类型、数值范围、候选复制、HTTP/用例保留标量类型、空库完整校验及
Milvus 首次 await 前的快照。真实用例补充安全数值上下界、字面点号路径、恶意值绑定，
并运行既有原生异步索引恢复/取消等故障生命周期测试。验证计数见 tasks/v2_plan.md。

真实验收固定在已有 zhihao_test_database，使用随机集合前缀，清理范围只涵盖测试集合。
显式要求中间件可用，不能用 skip 充当通过。本机代理例外只设置在测试进程的目标主机，
不改变全局代理或应用数据库配置。

本规范不改变 ANN 本身的召回误差、分数定义、Milvus 先查后删的并发窗口和大结果集
枚举限制，也不扩展到任意额外 JSON 索引配置。没有新增其他向量后端或持久化字段。
