# 独立 MinerU 的逐页 Markdown 扩展

这是 **Comet-RAG 自定义、默认关闭的部署扩展**，不是 MinerU 3.4.5 已发布的 HTTP
功能。它只安装到独立 MinerU 环境；不包含在 comet-rag wheel，也不从应用启动。
仅验收单实例 `mineru-api` + `vlm-http-client` + 完整 PDF。Router、混合版本 worker、
其他 backend 和页范围请求不在本扩展的验收范围内。

## 为什么需要部署侧扩展

3.4.5 的上游 `union_make([page_info], mode, image_dir)` 已能渲染逐页图片、HTML 表格、
列表和代码，但 `/tasks` 没有返回逐页 Markdown 的字段。legacy content list 不能无损
恢复复杂渲染；应用复制排版器或 import MinerU SDK 都不符合项目边界。

本补丁复用同一个上游渲染函数，不增加 Web 服务、代理、队列或应用依赖：

1. VLM finalize 按页调用原生段落合并函数，显式关闭跨页表格合并。
2. 原生全文输出后，用同一函数、同一 mode/图片目录渲染每页副本；校验逐页拼接与全文
   一致，拒绝残余 cross_page 或被跨页表格合并删除的内容。
3. 原生任务结果多返回一个 namespaced JSON 字段，健康响应声明扩展版本。

三个上游修改文件的版本/摘要在 [upstream.json](upstream.json)，已逐字节对照官方
3.4.5 release。安装器先查全部基线，在临时目录应用补丁和解析 Python 语法后才写入；
普通写入错误会恢复原文件。它不是跨文件断电事务，也不是整个运行时或权重的完整锁。
安装前停止该独立服务；使用可重建的专用环境。

## 安装与运行

在仓库根目录操作，系统需有 `patch`；不要在 Comet-RAG 的 `.venv` 执行安装：

```bash
uv venv /path/to/mineru-api-env --python 3.12
uv pip install --python /path/to/mineru-api-env/bin/python 'mineru==3.4.5'
/path/to/mineru-api-env/bin/python deploy/mineru/install.py

COMET_MINERU_PAGE_MARKDOWN_V1=1 \
MINERU_TABLE_MERGE_ENABLE=false \
MINERU_API_MAX_CONCURRENT_REQUESTS=1 \
MINERU_PROCESSING_WINDOW_SIZE=2 \
MINERU_VL_SERVER=http://<vlm-host>:<vlm-port> \
MINERU_VL_MODEL_NAME=opendatalab/MinerU2.5-2509-1.2B \
/path/to/mineru-api-env/bin/mineru-api --host 127.0.0.1 --port 8989 --max-concurrency 2
```

不接受不同版本、被修改的三份基线源码或重复覆盖安装。更新补丁时重建独立环境，不
跳过摘要守卫。运行开关只接受 `0` / `1`；开关为 `1` 时必须显式将
`MINERU_TABLE_MERGE_ENABLE` 设为 `false`，配置错误使健康/任务失败，不偷偷回到旧模式。

Comet-RAG 继续配置 `base_url` 为该文档 API，backend 为 `vlm-http-client`。应用只在
每次 health 声明扩展版本 1 时消费逐页结果；协商信息是本次提取的值对象，重提任务
沿用同一结果，不在共享适配器上保存可变开关。此模式不再请求冗余 legacy content list；
原生全文与逐页结果仍共享现有响应/Markdown/总超时预算。

远程文档 API 仍必须提供 HTTPS。上例是本机验证配置，不是公网部署方案。不要向
入库请求开放模型地址或 server_url。Router 未验证能力聚合和转发，不手填其 health
声明来冒充已支持。混合 worker 返回缺失或错误的扩展字段会明确失败。

## 最小 wire contract

协议仍为 MinerU API protocol v2；没有新端点或请求参数。`GET /health` 增加：

```json
{"extensions": {"comet_page_markdown": 1}}
```

`results.<filename>` 保留原生 `md_content`，增加：

```json
{
  "comet_page_markdown": {
    "version": 1,
    "boundary_mode": "physical",
    "page_count": 3,
    "pages": [
      {"page_idx": 0, "markdown": "第一页"},
      {"page_idx": 1, "markdown": ""},
      {"page_idx": 2, "markdown": "第三页"}
    ]
  }
}
```

`page_count` 是原生完整 PDF 页记录数，`page_idx` 必须为 0 到 count−1 的连续整数，
bool/float 不作整数接收。空页也必须列出；消费端保留全部页对象，例如上例为1、2、3，
规范化只为非空正文生成内容块，页号仍为1、3。所有非空片段用两个换行拼接必须逐字等于原生全文，不能用
strip、文本搜索、占位符或补偿页号修正不一致。页码是 PDF 物理页，不是印刷页码。

声明了扩展却返回缺失字段、错误版本/页数/页号/正文或无法重建全文时，提取明确失败，
不把损坏响应降为 section 成功。未声明扩展的原服务保持既有 legacy 投影及 section
回退；这条兼容路径不能当作复杂 PDF 的页验收。

## 验收与切换

```bash
COMET_TEST_MINERU_URL=http://127.0.0.1:8989 \
COMET_TEST_MINERU_BACKEND=vlm-http-client \
COMET_TEST_MINERU_PDF_DIR=/path/to/locked-public-pdfs \
uv run pytest tests/integration/test_mineru_e2e.py -m integration

uv run python -m evals.pdf.parse --pdf-dir /path/to/locked-public-pdfs \
  --mineru-url http://127.0.0.1:8989 --require-pages --out /tmp/comet-pdf-new-run
```

公开来源、二进制锁及取得方式见 [PDF 工具](../../evals/pdf/README.md)。缺服务或未设置
公开样本目录会 skip，不能算通过；给定样本目录后字节或页验证错误必须 fail。

逐页后处理可能改变旧的跨页引用列表/表格合并及 canonical 正文。按既有新命名空间
重建、核验、切读和回退协议接入，不能在服务地址不变时在线切换开关，再把旧索引当
新页索引。取消本扩展可用新的干净 3.4.5 环境或关闭开关；这不会自动回滚已写入的索引。

页片段与上游 Markdown 一致不证明 OCR 字词正确、脚注完整、模型工件固定或人工标签
独立。它也不保证每份长 HTML 表格都能在一个 chunk 内；页硬边界与完整证据预算分别
验收。真实结果和仍未关闭门禁见[第二十九批报告](../../docs/evaluations/mineru_pages_20261025.md)。

2026-10-26已补验 `opendatalab/MinerU2.5-Pro-2605-1.2B`：沿用该独立环境，仅替换
`MINERU_VL_SERVER` 的上游端口为9919和 `MINERU_VL_MODEL_NAME` 即可完成真实解析。
Comet-RAG的 `mineru.base_url` 仍指向文档API，不直接指向VLM端口。上游默认开启
`image_analysis`，Pro模型会产生新的图像解读；原生CLI可用 `--image-analysis false`
作单独诊断。没有改应用默认配置，结果与引用边界见[补验报告](../../docs/evaluations/mineru_pro2605_20261026.md)。

2026-10-27已在干净的4.0.12环境固定同一Pro模型，完成60/7/80页整本对照及60页关闭
图像分析诊断。原生FULL能保留背封并精确重建逐页结果，但V1 HTTP尚无等价逐页
Markdown导出；图片内嵌与ZIP体积也超过现有响应预算，不能直接移除本补丁或仅改
应用服务地址。实验、协议边界和采用前置见[4.x独立验证](../../docs/evaluations/mineru4_pro2605_20261027.md)。
本安装器仍只接受3.4.5，不把旧补丁安装到4.x；生产默认未切换。

原生structured_content已有[离线映射与证据契约](../../docs/mineru_v1_design.md)：
页、标题与表体可保留，图像解读独立；现有生产接口仍会丢部分结构。
独立`python -m evals.pdf.capture`现已完成V1采集及32MiB产物／36MiB累计接收／900秒
统一deadline验证，详见[采集器结果](../../docs/evaluations/mineru_v1_capture_20261027.md)。
它不读取生产配置，不加载MinerU SDK，不接线ExtractorPort。暖缓存真实实验发现原始
字节提交带可选SHA参数会提前返回并可能丢回包，因此完整上传后核对原生文件元数据
SHA，不自动重提。不把V1网络验证视作本补丁或生产证据持久化已经迁移。

后续[证据持久化与恢复协议](../../docs/mineru_evidence_lifecycle.md)采用既有MinIO独立
前缀作为生产目标位置，已完成本地不可变包、页内标题视图及真实PostgreSQL协议演练。
本轮不提供生产S3写入或对象GC。固定4.0.12的DELETE file只移除文件视图，原生终态
保留及共享引用决定底层回收；实际部署须另核查保留参数，不能把取消、404或默认TTL
当作物理排干证明。没有修改本安装器、上游9919服务或现有生产配置。

证据的[S3独立适配](../../docs/mineru_s3_evidence.md)已补原生条件写与有界回读，但当前
目标MinIO条件创建／拒绝覆盖语义实测未通过。保持发布门禁；本机参考端成功不能
代替目标验收，也不表示应直接升级为参考旧版。没有切换文档API或生产存储配置。

后续[MinIO诊断](../../docs/evaluations/minio_diagnosis_20261027.md)已核实目标2023构建与
原生条件写原因；固定上游修正源码的隔离候选通过协议及故障验证，但不是官方发布，
上游仓库也已声明停止维护。先确定可持续维护的部署候选及迁移／回退验收，再考虑
生产接线；本安装器、目标存储、MinerU服务和生产配置均未修改。

2026-10-28已完成SeaweedFS4.51本机隔离验收，V1采集／S3实现迁入正式基础设施，
父块引用保护与读取服务通过真实PG恢复；不等于本安装器或生产文档API已迁移。
固定制品、迟到探测写和未启用范围见[本批报告](../../docs/evaluations/seaweed_evidence_20261028.md)。
