# 阶段 D 文本与图片闭环

2026-10-29：四种真实检索组合、HTTP 临时文件清理、DOCX 内嵌图，以及 PostgreSQL/Milvus 真正重启后的同 scope 新进程持久读回全部通过，阶段 D 的真实验收完成。

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

## 真正服务重启验收

在代码 `925d906580059cc6385a421cee741cfee095631c` 上，为本轮新建独立 Compose 项目 `comet-rag-v2-acceptance-47e151d5d11b`，PostgreSQL/etcd/Milvus 均使用新卷，端口随机绑定回环地址。新 scope 为 `phase-d-restart-47e151d5d11b`，先执行五文档 ingest 和八次查询，再显式执行：

```bash
docker restart \
  comet-rag-v2-acceptance-47e151d5d11b-postgres-1 \
  comet-rag-v2-acceptance-47e151d5d11b-milvus-1
```

命令返回 exit 0。两个容器 ID 与 Created 均未变化，StartedAt 如下（UTC）；PostgreSQL ID 前缀为 `92db676661e6`，Milvus 为 `e30817916851`。

| 容器 | 重启前 StartedAt | 重启后 StartedAt |
|---|---|---|
| PostgreSQL | 2026-10-28T23:18:36.330612218Z | 2026-10-28T23:20:14.940902042Z |
| Milvus | 2026-10-28T23:18:39.84324247Z | 2026-10-28T23:20:18.122455085Z |

随后从全新 Python 进程执行同 scope 的 `examples.persistent_multimodal --phase verify`：五文档完整 manifest、明确 record IDs/指纹、实际表示、unit、图片 SHA、DOCX 内嵌图和八次查询全部再次通过，查询分数与重启前一致。示例内部计时为 ingest 52.96 秒、重启后 verify 2.35 秒，包含各自的模型加载与存储就绪等待；容器重启时间不在其中。

原始证据位于本地 `tmp/acceptance-restart-47e151d5d11b.json`，资源与状态在 `tmp/phase-d-restart-47e151d5d11b`。凭据只经内存/环境传递，不在报告中；测试容器与数据卷保留。该结果证明本次正常服务重启后可读，不证明断电耐久或跨机器资源可用。

此前旧测试容器的重启曾被自动审批拒绝，未对被拒目标执行动作。本次新建项目的显式重启动作已获工具批准并实际成功，验收只使用上述新 scope 的证据。
