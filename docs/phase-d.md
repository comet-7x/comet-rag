# 阶段 D 文本与图片闭环

2026-10-29：四种真实检索组合、HTTP 临时文件清理、DOCX 内嵌图和新进程持久读回已通过。图片资料在服务容器重启后的复验仍被自动审批阻止，本阶段保持未完成。

## 实现与边界

`AsyncImageParser` 验证实际字节 SHA/size、格式、像素和单帧，Pillow verify 后重新解码以拒绝截断图像；原始字节按 hash 写入 scope 的持久目录，冲突不会覆盖。保存原始像素坐标和 EXIF，不自动旋转、OCR 或生成描述。PNG/JPEG/WebP/GIF/BMP/TIFF 单帧入口有 31 项单元测试。

原生 DOCX 的独立图片、表格图片、普通/行内公式图片都进入实际 ContentPart；来源 Asset 链保存但不额外产生检索表示。Flat builder 的 text/media/separate/joint 选择进入 profile。joint 仅表达实际联合输入，模型不支持时明确拒绝。

`AsyncCLIPEmbedder` 使用固定本地 safetensors，文本与单图片处于 512 维共享空间。实际配置、tokenizer/processor 文件 SHA、`use_fast=False` 进入 space。文本不截断，图片有字节/像素预算，输出维度/finite/归一化校验；23 项轻量模型边界测试不加载真实模型。

## 真实持久验证

实际执行 `examples.persistent_multimodal` 的 ingest 与全新进程 verify，范围 `phase-d-24f96d492310`。PostgreSQL 17.11、Milvus 2.6.23、CLIP 权重和公开图片指纹见 [示例](../examples/README.md)。写入五份资料：两个独立文字描述、两张真实图片、一个包含猫图的 DOCX。

HTTP 下载经回环服务器真实传输，解析返回后下载临时目录已经清空。图像内容不带伪造文本/文件名表示，实际模型读取图片。每份 manifest 的明确 record IDs/指纹、实际表示、unit 与持久图片字节 SHA 均核对通过。

| 查询→资料 | 猫命中 | 狗命中 | 原始 cosine |
|---|---|---|---|
| 文本→文本 | text-cat | text-dog | 1.000 / 1.000 |
| 文本→图片 | image-cat | image-dog | 0.3710 / 0.3582 |
| 图片→文本 | text-cat | text-dog | 0.3710 / 0.3582 |
| 图片→图片 | image-cat | image-dog | 1.000 / 1.000 |

八个查询在新进程重新建立模型/存储客户端后再次通过。DOCX 内嵌猫图能由文字查询检索且返回可读 Asset。没有只用内存存储代替持久后端。

这些样本用于验证模态和资源闭环。文本查询复用入库描述，图片查询复用相同原图，因此不证明未见资料的泛化质量。没有付费模型、GPU、联合图文模型、PPTX 或整页截图验收。

## 剩余出口

本轮运行没有重启服务容器。文本范围曾在阶段 B 真正重启 PostgreSQL/Milvus 后读回通过；不能据此声称本次图片范围已经经历相同复验。

自动审批两次拒绝脚本重启本次独立验证 Milvus 容器，理由 `[Interfere With Workloads]`，即便已找回本会话创建该容器/独立卷的成功记录。已取消脚本中的重启动作，继续完成新进程重连。需要用户允许该专用测试工作负载重启后，执行同一 scope 的 verify 才能关闭本阶段。
