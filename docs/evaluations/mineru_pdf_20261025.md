# 第二十八批：MinerU 原生解析与页事实边界

本批确认既有地址提供 MinerU VLM 模型推理，而非文档 API。独立启动官方 MinerU
3.4.5 后，两份完整公开 PDF 均能通过项目现有 HTTP 适配器解析；复杂文档仍回退
section。真实正文三页子集与重复句两页样本的原生页事实、规范化和 page 分块通过。
**整份复杂 PDF 的 page、独立人工标签及固定模型工件验收仍未完成。**

## 配置与上游协议

只读探测区分了两种服务角色，没有向原配置地址上传资料，也没有修改 `.env`：

| 探测 | 观测 |
|---|---|
| 配置路径 `/v1/health` | 404 |
| 同源根路径 `/health` | 200、空正文 |
| 根 `/openapi.json` | 模型推理路由，无 `/tasks` |
| `/v1/models` | `opendatalab/MinerU2.5-2509-1.2B` |

所以仅去掉 `/v1` 也不能修复协议。200 不等于 protocol v2；不以健康探测成功作为
上传依据，不自动猜服务路径。非回环 HTTP 仍被现有校验拒绝。

本轮在 `/tmp` 专用环境安装 `mineru==3.4.5`，启动原生 `mineru-api`，只监听回环。
由该服务的 `MINERU_VL_SERVER` / `MINERU_VL_MODEL_NAME` 连接现有模型后端；项目只
请求文档 API 的 `/health`、`/tasks`、状态和结果。没有实现自定义网关、SDK 路径或
pypdf 生产 fallback。API 任务并发 1、处理窗口 2、远端 VLM 客户端并发 2；三个预算
不同。原生健康响应为 healthy、version=3.4.5、protocol_version=2。

独立环境实际分发包：mineru-vl-utils 1.2.1、FastAPI 0.162.0、httpx 0.28.1、
pypdfium2 5.10.1、pypdf 6.21.2；未安装 Torch/vLLM/transformers。这些是本次外部
HTTP 模式的观测清单，不是新增的应用依赖或完整环境锁。

上游主分支已进入 4.x，入口和文件位置调整；本批仅确认 3.4.5 的协议。文档依据固定
到 [3.4.5 API 源码](https://github.com/opendatalab/MinerU/blob/mineru-3.4.5-released/mineru/cli/fast_api.py)、
[请求定义](https://github.com/opendatalab/MinerU/blob/mineru-3.4.5-released/mineru/cli/api_request.py)
与[上游兼容说明](https://opendatalab.github.io/MinerU/usage/compatibility/)，修复原来随
master 漂移的链接；不把 4.x 同名命令当作已通过兼容验证。

## 实际 PDF 和 page 观测

原始文件沿用 [来源锁](../../evals/pdf/pages.json)，保持 NIST 归属及许可。两份原 PDF
共 87 个物理页，完整上传，没有用参考提取文本代替 PDF。本次原生正文不覆盖或修改
第二十七批的 pypdf 快照、24 题标签或数据指纹。

Republished courtesy of the National Institute of Standards and Technology.

| 输入 | 本轮最终工具耗时 | 实际规范化字符 | 页事实结果 |
|---|---:|---:|---|
| 完整 SP 800-145，7 页 | 8.04s | 10,054 | 无；15 个 section |
| 完整 SP 800-144，80 页 | 117.36s | 220,812 | 无；56 个 section |
| SP 800-145 正文三页派生 PDF | 5.05s | 7,282 | 原生页 1、2、3；3 个 page blocks |
| 生成的两页重复句样本 | 1.05s | 135 | 原生页 1、2；重复句分别位于两页 |

上述耗时是单次 wall time，不是 p95 或吞吐基准。早期完整探测分别为 11.11s 和
139.40s；80 页文件早期 Markdown 为 221,366 字符，最终原生/规范化为 220,812。
原始观测均保留，没有挑最好一轮或将差异归因于未经确认的因素。

完整文件的 content list 含图片、表格、列表等项，现有投影无法无损重建全文，因此
保留原生 Markdown、回退 section。封面或前置表格也会使整份文本页无法获得 page
blocks。这是当前已确认的能力限制；没有复刻上游复杂排版器、忽略未知块或靠搜索
重复正文猜物理页。未来应先核查上游可复用的逐页 Markdown 能力，再决定最小协议
方案，不能为了通过样本放宽完整重建守卫。

三页派生样本由独立 pypdf 6.10.0 复制原文件物理页 5、6、7，不提取再排版。派生文件
SHA-256 为 `8b2040e3ffbab6075411c63b8d60aa614f76d5ee85fc8d97551dcd04b9853848`，
新 PDF 的物理页 **1→5、2→6、3→7**。三页重新渲染检查，文本与原页逐页一致；
适配器仍返回新文件的 1–3，没有将其偷换成原始 5–7。页片段精确重建原生全文，
规范化及 800/80 page 分块的字符区间和硬边界通过。不代表完整原文件的 page 已通过。

真实文本/光栅扫描 PDF 已通过 API→提取→分块→内存入库→检索；模型使用常量替身，
故这证明解析与服务链路，不证明检索模型质量。另一个集成用例检查真实两页重复句、
不同页身份、原文区间和 page 硬边界。两个样本均渲染检查，二进制不入仓库。

解析成功也不保证字词无误：原生结果把署名 Grance 提取成 Grace；
[官方发布页](https://csrc.nist.gov/pubs/sp/800/145/final)列明作者 Tim Grance。
未在客户端按已知标签改正文，没有把这次成功解析当作 OCR 正确率验收。800-145 的
三个 page_footnote 项也不在上游 Markdown 中；页片段与原生 Markdown 一致不等于
原 PDF 的脚注和全部内容完整，适配器没有从 content list 私自补入另一种正文。

## 确认问题与改造

1. legacy content list 的 `type` 若为 list/dict，会在集合成员判断抛 `TypeError`，
   中断本应保留正文的提取。先复现两个失败，再校验字符串类型，按既有约定放弃页事实。
2. 模型后端空 200 健康响应只有通用 JSON 报错，难以识别配置角色错误。同步/异步
   适配器现在指向“protocol v2 文档 API，不能直接连接 vLLM”的诊断；仍在上传前失败，
   不探测任意地址、不回退上传，也不将所有不健康响应误称为模型服务。
3. 增加独立 `evals.pdf.parse` 核验工具：所有来源字节先查锁，再顺序上传；解析后再查
   来源摘要。实际正文和页片段存外部目录，报告区分 verified 与
   unavailable_section_fallback。检查声明页数范围、精确重建、规范化、原文 span 和
   分块硬边界；不保证 OCR 字词正确、所有页均被识别或页号经过独立人工裁决。
4. 工具强制新输出目录，避免失败重跑旁留旧成功报告；解析错误和非法页事实直接失败，
   不从旧报告、参考快照或模型替身补成功。命令见 [工具说明](../../evals/pdf/README.md)。

新增 20 项 unit、1 项真实集成；原有 MinerU 真实链也本轮补验。修复前目标测试为
4 failed / 4 passed，修复后定向 92 项通过。另在独立源码副本执行 11 种变异：错误
页范围、丢弃重建/规范化守卫、伪造 page 成功、错误正文/cross-page/空分块、来源
未验证、重用旧报告、畸形类型与错误服务诊断，全部由目标断言检出，无收集/夹具错误。

## 独立标签与固定权重的验收边界

| 项目 | 可接受的证据 | 当前状态 |
|---|---|---|
| 标签独立性 | 未参与拟定的人阅读盲审原 PDF，逐题给出 answerable/unanswerable/uncertain、0/1/2 文档等级、物理页及原文 quote；记录角色和复核范围 | 未收到复核结果，仍 pending |
| 标签裁决 | 对照两份标注记录逐项裁决，分列跨页证据，发布新版本与指纹；保留作者初稿 | 未完成，不覆盖旧标签或用结果调金标 |
| 原生解析标签 | 基于实际 MinerU canonical 正文和物理页重新核对证据映射 | 未完成；不能用 pypdf 的旧字符 span 直接评分 |
| 固定 embedding/reranker | 两份权重、tokenizer/chat template 的不可变 revision/摘要；引擎版本、dtype/量化、池化/指令/截断设置及硬件清单 | 原部署只返回模型名，仍 unknown |
| 固定解析模型 | 比较解析变化时，同样需 MinerU VLM 工件及 API/客户端版本与相关配置 | 客户端版本已记录，远端 VLM 工件未锁定 |
| 质量达标 | 裁决后先冻结验收阈值、候选/分块/完整证据预算与部署工件，再重复逐查询对照；解释全部关键回退 | 小样本高分、健康 200 或探针相同都不能替代 |

本轮不重复做“旧 pypdf 标签×新 MinerU 文本”的无效排名实验，也不修改生产默认
分块/重排。512/1024 仍只代表各返回正文独立编码的完整排名前缀预算，不包含联合
query、指令和模板，也不是同推理成本。M4-T10/H/I、复杂整份 page、目标部署和用户
资料切换均保持独立门禁。

## 全量验证

| 检查 | 结果 |
|---|---|
| 普通 unit，两轮 | 3449 passed、15 skipped、1 xfailed；10.47s / 11.08s，均超过10秒，预算未关闭 |
| unit 插桩 | 3449 passed，19.96s；89.44%，不作为普通预算 |
| integration | 297 passed、1 skipped、59条上游ARQ close警告；561.19s；Milvus集合1→1 |
| E2E / benchmark | 50 / 39 passed；4.85s / 10.73s |
| 合并覆盖率 | 整体92.88%，tasks908/943=96.29% |
| 定向 / 反向 | 92项unit、2项真实MinerU定向；11种独立变异全部检出 |
| 静态与轻安装 | 全仓Ruff/Pyright；复用既有core-only环境的库与生成DOCX冒烟 |

普通计时已排除其他本轮验证负载；最慢项仍是既有并发/取消等待，新增16项核验工具
测试定向0.13s。两次超预算均保留，不以反复重跑挑过线结果或降低断言代替治理。
MinIO未配置的1项skip不算通过；MinerU本轮没有skip。隔离测试PG关闭fsync，
不构成断电可靠性验证。原生MinerU13项任务完成、零失败、排队/处理均为零后SIGTERM
关停，回环监听已关闭；自建PG/Redis两个容器、两个记录卷和网络已精确清理。
原始解析、派生映射、临时服务日志、11 种反向验证和完整检查记录位于
`/tmp/comet-v2-batch28-validation/`；服务输出和测试样本不提交，端点与凭据不写报告。

格式检查补充：全仓 `ruff format --check` 报18个既有文件需格式化，已逐一确认相对
本批基线无改动。本批7个Python文件的格式检查通过；没有把历史格式问题记为已修复。

图文收尾：224个本地文档链接、README/默认/revision配置冒烟及8张Mermaid通过；
交互图固定到 `debd891e7468f375cc2a36121ae496433ed8188f`，来源、布局、离线产物、
严格provenance及回执字节/SHA均通过。新增独立解析核验HTTP路径，文档API节点是
接口角色，不表示核验地址等同生产部署。受既有file://浏览器策略限制，未做本轮
browser-check或感知验收，不沿用旧截图/回执。

外部服务关停记录补充：原生API已打印application shutdown complete，PID消失，
执行器返回143符合本轮SIGTERM；上游Python resource_tracker同时报告1个未主动
归还的semaphore并尝试清理。保留该警告，不将监听关闭解释为上游无资源泄漏证明。
