# Document Normalization TODO

## DN-T1：契约与策略

- [x] 增加 `NormalizedDocument` 与 `VisionDescriptionPort`。
- [x] 定义 `DocumentNormalizationStrategy`。
- [x] 实现保守、幂等的 `MarkdownDocumentNormalizer`。

## DN-T2：流水线装配

- [x] ExtractHook 改为返回 `ExtractedDocument`。
- [x] 同步/异步库 Pipeline 在 Chunker 前规范化。
- [x] IngestRunner 在写 Task context 前规范化并复验大小。
- [x] PDF 组合根直接传递提取结果，不丢弃 metadata。

## DN-T3：目录收敛

- [x] 删除通用 `BaseCleaner`。
- [x] 将视觉描述契约从 `engines/cleaners` 下沉到 `ports/vision.py`。
- [x] 保留 `DocxCleaner` 为 DOCX 实现内部步骤。

## DN-T4：验收

- [x] 更新测试 Hook、示例与架构文档。
- [x] 运行 Ruff、Pyright、默认单测及集成测试。
- [x] 提交本轮重构。
