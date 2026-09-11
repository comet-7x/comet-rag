# Specification：M2 后 P1 公共入口收敛

> 状态：已完成（v1.0）
> 依据：`tasks/architecture_plan.md` P1
> GitHub Issue：#52
> 范围：DocumentExtractor 与 Loader 公共概念、契约和入口；不包含 M3 检索设计
> 完成日期：2026-09-10

## 1. 目标

1. DOCX 与 PDF 在高层都通过 `DocumentExtractorPort` 表达，格式配置留在具体实现。
2. Local、URL、S3 Loader 共享一个标准库级 Port 和一套行为契约。
3. 用户只需从 `comet_rag.loaders` 发现 Loader；内部实现仍按依赖归属分层。
4. 合并重复的文件信息、内容类型确认和临时文件登记逻辑，不改变现有加载结果。

## 2. 决策

### P1-D1：DOCX 配置属于实现，不属于 Port

`DocumentExtractorPort` 继续只接收受管本地路径、文件名与媒体类型。标题编号、图片、
页眉页脚及 ZIP 防护上限通过 `DocxDocumentExtractor` 构造参数注入；通用 Port 不增加
DOCX 字段或 `**kwargs`。

### P1-D2：SourceLoaderPort 包含批量能力

`AutoLoader` 依赖叶子 Loader 的批量实现来复用 URL/S3 客户端，因此批量加载不是可有
可无的便利方法。Port 包含 `load` / `aload`、`batch_load` / `abatch_load` 和同步/异步
清理；只暴露通用的 `max_concurrency`，下载请求和对象存储选项仍属于具体适配器。

### P1-D3：值对象下沉，旧名称保留

`LoadedResource` 与来源描述位于 `ports/source.py`，只依赖标准库。`LoaderContent` 作为
兼容别名保留，既有 `comet_rag.engines.loaders` 导入路径本阶段不删除。

### P1-D4：统一入口不等于统一物理目录

`comet_rag.loaders` 直接导出核心 Loader，惰性导出 S3 Loader。导入门面本身不得要求
安装 `server` extra，不得把 boto3/aioboto3 引入 `engines`。

### P1-D5：URL 文件名采用最终响应事实

URL 经重定向后，metadata 的 `file_name` 优先取最终响应 URL 的 basename；最终 URL
没有文件名时再回退到原始请求 URL，最后才使用临时文件名。来源标识仍保留原始 URL，
两者分别表达“从哪里请求”和“实际下载了什么”。

### P1-D6：Loader 关闭不因临时文件残留而中断

显式调用 `LoadedResource.cleanup()` 删除失败时抛出错误并保留登记，便于调用方立即处理；
Loader 的 `cleanup()` / `acleanup()` 属于整体 shutdown，删除失败时记录 warning、保留账本
供重试，但仍继续关闭自建连接池，避免 context manager 退出时用清理错误覆盖业务异常。

## 3. 成功标准

- [x] `DocxDocumentExtractor` 通过通用提取器契约，现有 DOCX 快照不变。
- [x] `SourceLoaderPort` 不依赖第三方包，Local、URL、S3 均满足共享契约。
- [x] `LoadedResource.cleanup()` 幂等，消费者释放后 Loader 不再长期保留临时路径。
- [x] `from comet_rag.loaders import ...` 覆盖核心与可选 S3 API；core-only 导入通过。
- [x] 文件类型确认与基础 metadata 只有一份规则来源。
- [x] 默认单测、Ruff、Pyright 与分层守卫全部通过，默认单测仍小于 10 秒。

## 4. 非目标

- 不移动约 2,000 行 Loader 实现来追求目录对称。
- 不删除旧导入路径，不在本阶段承诺移除版本。
- 不改变 Task、向量库 schema、MinerU wire contract 或 M3 检索接口。
- 不因只有一个进程内 Parser 就顺手删除 `BaseParser`；在本阶段末单独记录结论。

## 5. 验收记录

- 默认单测：1786 passed、19 skipped、177 deselected、1 xfailed，pytest 9.18s。
- Loader/DOCX 定向：117 + 56 passed（DOCX 含 1 个可选真实文档 skip）。
- MinIO 集成：6 passed、1 个可选用例 skip。
- Ruff、Pyright、AST 分层守卫与隔离 core-only 导入均通过。
