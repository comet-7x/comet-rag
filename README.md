# python-template

Python 项目开发模版

# 使用说明

1. fork 本仓库到自己的个人仓库
2. 修改项目信息
3. 使用 `uv sync` 同步 `pre-commit` 配置到本地
4. 执行 `pre-commit` 初始化本地 Linter
5. 项目 Python 设定为 Python 3.12
6. commit message 遵循 [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) 规范
7. 修改完代码后，经过 `pre-commit` 检查，提交 pr，并指定 @anine09 为 reviewer

**注意：无法通过 CI 检查的代码，将会被打回修改！**
