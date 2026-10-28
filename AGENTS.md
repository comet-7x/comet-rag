# Comet-RAG V2

当前分支整体重写为独立组件库，现行细化设计在本地 `tmp/refactoring/overview.md`（v1.3）。旧实现保留在 `refactor/enterprise-reliability`；祖先目录的旧库与服务规格不适用于 V2。

## 范围与当前进度

- 阶段 0 只建立可安装、可检查的骨架，迁入独立算法、资源保护和来源夹具。持久入库、搜索、解析器和模型适配器按 A–F 实现；不把骨架检查当作功能验收。
- 不保留旧导入别名、签名包装、服务运行时或 legacy 包。新目录有实际实现时才创建，不预建空 Protocol、占位类或依赖组。
- 新存储使用独立 `index_scope` 重建，验证后由调用方切换。第一套持久后端为 PostgreSQL + Milvus；内存实现服务于契约测试。
- 不提供在线版本激活、后台 GC、HTTP 服务、持久任务或回答生成。

## 代码与依赖

- Python >=3.12；模块使用 `from __future__ import annotations`，公开边界有类型标注。内部值对象优先 `dataclass(slots=True)`，组件边界优先 Protocol。
- 纯内容/身份/数据类型不依赖 SDK、数据库或运行时。模型不导入 indexing；indexing 不导入 stores；SDK 放所属适配器。
- `chunkers` 依赖文档数据，`indexing` 物化单元与实际表示，`docstores` 保存快照与清单，`retrieval` 归并和融合排名，`pipelines` 编排。
- I/O 类型使用 `AsyncXxx` 和无 `a` 前缀的方法，纯计算同步。不机械创建同步/异步双份接口。
- 注入的客户端由创建者关闭，模块导入不建连接。取消阻塞工作时等待线程结束再释放输入；批量失败停止派发并排干已启动工作。
- 身份使用带版本的确定性编码与 SHA-256。保留文本空白、parts 次序和明确的坐标版本；不持久化凭据或临时签名地址。
- 迁移算法保留资源限额、完整输入校验、metadata 隔离和数据损失保护。注释写边界或原因，详见 `docs/comment_style.md`。

## 验证

```bash
make install
make check
make coverage
```

- `tests/unit` 不访问真实网络、模型或数据库。契约与真实集成在有对应新实现时加入，显式选择集成用例；缺少后端不是验收通过。
- 守卫要用对应错误输入证明会失败，期望值独立于实现。不要删除针对现有 V2 行为的失败测试或全局 skip。
- 核心 wheel 必须在仓库外、无开发依赖的环境验证导入。构建必须排除本地资料、凭据和清理备份。
- DOCX/PDF 夹具保留可读生成代码，不把旧解析器 JSON 快照当作新结构契约。

## 工作区

只修改当前 worktree。批量清理前核对目标与现有修改；不 reset/clean 其他 worktree，不删除本地资料、私有配置、数据库、集合或数据卷。不要从另一个 checkout 的环境推断 V2 可用。

提交和推送须由用户要求；commit 与 PR 不加 AI 署名。
