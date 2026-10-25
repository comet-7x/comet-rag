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
bool/float 不作整数接收。空页也必须列出；消费端只生成有正文的页对象，保留原物理页
号，例如上例为 1、3。所有非空片段用两个换行拼接必须逐字等于原生全文，不能用
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
