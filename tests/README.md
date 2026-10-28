# V2 测试

默认 `uv run --locked pytest` 只收集 `tests/unit`，不访问真实网络、GPU 或数据库。阶段 0 验证包和现有纯行为：ZIP/XML 资源限额、RRF 单元排名以及 DOCX/PDF 来源夹具。旧服务和接口的测试已随旧代码移除，旧分支保留历史；没有用 skip 隐藏这些用例。

```bash
make test
make coverage
uv run --locked pytest --collect-only -q
```

`fixtures/docx/build.py` 保留标题/富文本/列表、表格/合并/网格缺口、图片、公式和页眉页脚样例；`fixtures/pdf.py` 保留文本层与扫描样例。测试核对生成文件的原始结构和内容。来源夹具可继续适配新版解析器，旧解析器 JSON 快照不作为 V2 契约。

新的不变量使用独立预期值和反例，迁移保护时保留错误路径。守卫应注入对应错误证明会失败；不从实现常量自动生成期望。

后续 `tests/contracts` 放可由多个新后端实现共用的行为断言，`tests/integration` 放真实协议、持久化、重启与范围隔离检查；有实际实现时再创建。显式选择真实集成用例，缺少后端应说明未验收，不能把跳过算作通过。阶段 0 无真实集成测试，单元覆盖率只描述当前少量实现。
