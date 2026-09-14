# Document Normalization Specification

> 状态：已完成
> 前置：仓库结构归一化完成
> 范围：统一提取结果、跨格式规范化和 Cleaner/Vision 契约归属；不引入 PDF/OCR 依赖
> 完成日期：2026-09-13

## 1. 目标

1. DOCX、MinerU 及未来 PDF/OCR 提取器都返回 `ExtractedDocument`，Hook 不再把
   提取结果提前压扁成无来源信息的字符串。
2. 所有提取结果在进入 Chunker 前必须经过同一个 `DocumentNormalizationStrategy`。
3. 格式专属清洗留在对应实现内部，跨格式规则集中在
   `engines/documents/normalization/`。
4. 删除含义模糊的顶层 `engines/cleaners/`；视觉模型契约下沉到 `ports/`。

## 2. 边界决策

### DN-D1：Extractor 与 Parser 不是同一层抽象

`DocumentExtractorPort` 是上层统一调用的完整能力。轻量、进程内、确定性的原生解析器
位于 `engines/documents/<format>/`；远程服务、模型 SDK、GPU 运行时和需要显式关闭的
客户端位于 `infrastructure/extractors/`。具体 Parser 只是某个原生 Extractor 的内部步骤。

### DN-D2：提取结果与规范结果分开命名

- `ExtractedDocument`：提取器已经映射到项目通用字段，但尚未执行跨格式文本规范化。
- `NormalizedDocument`：可安全交给 Chunker 的规范 Markdown 与复制后的文档元数据。

本阶段不提前定义 `DocumentBlock`。页码、bbox、表格、图片资产等字段必须由 M4 的
真实 Chunking/IndexPlan 用例反推，不能先造一个没有消费者的万能块模型。

### DN-D3：通用规范化必须保守且幂等

首版只执行不会改变 Markdown 结构含义的规则：

- CRLF/CR 统一为 LF，移除 UTF-8 BOM 与 NUL；
- 文本使用 Unicode NFC；
- 非代码围栏区域把 NBSP 统一为空格、去掉行尾水平空白；
- 非代码围栏区域把连续空行压缩为一个空行；
- 去掉文档首尾空行；
- fenced code block 的内容、缩进和内部空行保持不变。

同一文档规范化两次必须得到相同结果，输入 metadata 不得被原地修改。

### DN-D4：两类 Cleaner 不混用

格式专属清洗处理 DOCX 页眉页脚、PDF 阅读顺序、OCR 行合并等来源事实，跟随具体
Extractor。通用 Normalizer 只处理所有格式共享的表示规则，不猜页边界、不删除业务
内容、不修复供应商特有错误。

### DN-D5：同步/异步链路使用同一策略

Normalizer 是纯计算 Strategy，只提供同步 `normalize()`。异步 Pipeline 在线程池执行，
避免大文档规范化阻塞事件循环；同步入口直接调用。服务入库在持久化 Task context 前
完成规范化，并对提取文本和规范文本都执行既有大小限制。

## 3. 目标链路

```text
SourceLoaderPort
  → DocumentExtractorPort / ExtractHook
  → ExtractedDocument
  → DocumentNormalizationStrategy
  → NormalizedDocument
  → ChunkingStrategy
  → Chunk / IndexPlan
```

## 4. 验收标准

- [x] 所有 ExtractHook 返回 `ExtractedDocument`，同步与异步一致。
- [x] DOCX、MinerU 和自定义测试 Hook 均经过同一个 Normalizer。
- [x] 规范化规则有边界测试、幂等测试和代码围栏保护测试。
- [x] `VisionDescriptionPort` 位于 `ports/`，实现仍位于 `infrastructure/models/vision/`。
- [x] `engines/cleaners/` 删除，DOCX 专属 Cleaner 保留在 `documents/docx/`。
- [x] 不新增依赖，不修改数据库或向量库 schema。
- [x] Ruff、Pyright、默认单测和相关集成测试通过。
