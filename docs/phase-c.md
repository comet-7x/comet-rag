# 阶段 C 来源与文档保真

2026-10-29：本地、HTTP、原生 DOCX 与公开 AWS S3 匿名只读验证通过；MinerU 尚无可用的显式部署，阶段 C 保持未完成。兼容 S3、认证与版本对象行为未验证。

## 来源

`LocalSource` 借用原件，读取实际字节 SHA-256。`HttpSource` 与 `S3Source` 通过 async context manager 提供完整本地资源；由该上下文创建的临时目录在成功、异常和取消时释放。阻塞 SDK 取消后等线程结束才清理输入，注入客户端由创建者关闭。

HTTP 使用注入的 httpx client 和必选准入策略，每次重定向发送前校验；不跨域重新注入认证。限制重定向次数、声明和实际字节数，拒绝部分响应和非 identity 内容编码。保留 ETag/Last-Modified 等版本事实，它们不冒充实际内容 hash。带已知敏感 query 的 URL 要求显式 source_id，不持久保存临时签名定位。准入回调不保证 DNS 与实际连接 IP 绑定，这由调用方的网络策略负责。

S3 使用显式 namespace 与注入的同步 boto3-compatible client；保留 Bucket/Key/VersionId，请求的 VersionId 不得被替换。流式读取与实际预算/hash 核验，Body 必须关闭；ETag、VersionId、LastModified 和 SHA 分开保存。

33 项 remote 单元测试和两项真实回环 HTTP 集成验证逐跳准入、认证剥离、字节预算/长度、来源、临时目录与取消所有权。单元测试不访问真实服务。

## 原生 DOCX

直接映射 OOXML：段落/标题/列表、run 样式继承、字体、链接、字段、内容控件、tabs/newlines、表格/合并/网格缺口/嵌套、图片、页眉页脚。OMML 保存原生表达式及 partial 诊断，不声称全部公式已转为 LaTeX。

ZIP/XML、元素与解压预算、非法引用和不支持的 meaningful 内容都有反例测试。真实生成的 DOCX 及图片进入持久检索示例；未构造虚假页码或依赖旧 JSON 快照。`complete` 仅表示无已知遗漏，不保证 Word 所有格式已支持。

## Markdown 导出

`documents.exporters.export_markdown(document)` 同步从当前 blocks 和 Asset 引用派生 Markdown，不保留旧缓存。36 项单元测试实际解析生成的 TXT/DOCX，覆盖空白、富文本、链接、合并/嵌套表格、代码围栏和图片引用；OMML/MathML 与歧义公式保留源码及诊断。核心 wheel 的双进程读回也核对相同快照的导出结果。未对外部 Markdown 渲染器做浏览器验收。

## MinerU

显式导入 `documents.parsers.mineru.AsyncMinerUParser`，仅实现已核对的官方 MinerU 2.6.8 `/file_parse` multipart Markdown 契约。上传前实际读字节并核对 SHA/size/预算和 PDF 头/尾。返回 schema、version/backend、JSON 类型/重复键和响应预算严格校验，不跟随重定向。

请求只启用 Markdown。输出原样保存为一个 text Block，始终 `partial`、`coverage=None`，不伪造页、原生表格或可读图片 Asset。记录请求的 parse_method；协议不返回内部实际选择，`auto` 不能被解释为已证明某种解析分支。46 项 MockTransport 单元测试通过，只证明协议映射和失败边界。

## 公开 AWS S3 真实验收

`tests/integration/test_public_s3.py` 实际访问 [AWS Open Data 收录的 NOAA GHCN](https://registry.opendata.aws/noaa-ghcn/) 公开对象 `s3://noaa-ghcn-pds/ghcnd-version.txt`。使用 boto3 1.43.120、us-east-1 的 S3 endpoint 与 `UNSIGNED`；禁用本机 AWS 配置、凭据提供链和代理读取，不需要账户，不写入或删除远端对象。结果为 **1 passed，14.75 秒**。

独立 HTTPS 下载提供预期字节并与 SDK 的 ETag 对照，实际 resolver 下载后核对 bytes、SHA-256、ETag、LastModified、大小、locator 与稳定 source_id。真正解析 UTF-8 文本后释放下载目录，快照仍可读；再次获取身份相同，缩小预算会拒绝交付并清理，注入 client 在创建者关闭前仍能调用。

本轮对象为 270 字节，SHA-256 为 `959e77dcb434a98cc3add57ae21d6db0aebe79256288ca3c6bb53296e268c084`。该版本文件会更新，测试每次独立获取预期内容并检查 ETag 一致，不将当次 hash 固化为未来服务数据要求。对象无 VersionId；此次只证明 AWS 匿名只读路径。

## 未完成的真实验收

- 兼容 S3、认证和版本对象：专用服务集成入口已有，缺配置直接失败、不 skip。自动审批拒绝执行本次选用的 `chrislusf/seaweedfs:3.85`，理由为 `[Code from External]`；未启动或以其它入口绕过。AWS 匿名只读结果不覆盖这些行为。
- MinerU：没有可用的显式 2.6.8 部署 endpoint。没有搜索私有配置/凭证，也没有临时安装本地 MinerU 模型/GPU 代替外部部署。没有证明识别质量或部署响应确实符合源码。

严格入口：

```bash
# 公开 AWS S3 匿名只读，需要外部网络，不需要凭据。
python -m pytest tests/integration/test_public_s3.py -q

# 专用兼容 S3；调用方提供 endpoint/access key/secret/region 环境变量。
python -m pytest tests/integration/test_remote_sources.py -k real_s3 -q

# COMET_RAG_TEST_MINERU_URL 为完整 /file_parse endpoint。
python -m pytest tests/integration/test_mineru.py -q
```

环境变量名称和全部运行范围见 [测试说明](../tests/README.md)。C 的必需外部引擎出口仍是 MinerU，通过前不把 C 或最终全阶段验收标为完成；S3 的真实支持结论仅覆盖上述 AWS 匿名只读路径。
