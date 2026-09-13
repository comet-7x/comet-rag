# M2 后 P1 Todo

> 状态：P1-T1～P1-T4 全部完成
> 规格：`tasks/post_m2_p1_spec.md`

## P1-T1：DOCX 提取入口

- [x] 新增 `DocxDocumentExtractor` 并实现 `DocumentExtractorPort`
- [x] 同步/异步入口复用 converter、parser、cleaner 的对应路径
- [x] 内建 DOCX Hook 改为适配提取器，快照与 Pipeline 行为不变
- [x] 增加实现无关契约与格式配置测试

## P1-T2：来源加载契约

- [x] 新增 `ports/source.py`：来源值对象、`LoadedResource`、`SourceLoaderPort`
- [x] 保留 `SourceContent` / `LoaderContent` 兼容导入和运行时身份
- [x] `LoaderRoute`、`Pipeline` 面向 Port，而不是强制继承 `BaseLoader`
- [x] Local、URL、S3 运行共享契约；反向验证契约能捕获资源泄漏

## P1-T3：公共门面

- [x] 新增 `comet_rag.loaders`
- [x] 核心实现直接导出，S3 实现惰性导出
- [x] 未安装 S3 extra 时，导入核心门面仍成功且错误信息可行动
- [x] 更新 Loader 与 Pipeline 使用文档

## P1-T4：重复逻辑收敛与验收

- [x] 统一扩展名确认、基础 metadata 与临时文件登记/释放
- [x] 清理消费者释放后仍增长的 URL 临时路径账本
- [x] 跑默认单测、Ruff、Pyright、core-only 与相关集成测试
- [x] 更新 `architecture_plan.md` 真实状态并创建面向 `develop` 的 PR

## BaseParser 结论（已由后续结构重构取代）

本阶段曾暂时保留 `BaseParser`。后续结构重构确认它只有 DOCX 一个实现且没有多态
调用方，已经删除；格式专属 parser/converter/type 统一归入 `engines/documents/<format>/`。
