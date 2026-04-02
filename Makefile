.PHONY: run pre-commit test help

# 默认目标：显示帮助信息
help:
	@echo "可用命令:"
	@echo "  make run         - 启动项目服务 (uv run main.py)"
	@echo "  make pre-commit  - 执行代码规范检查和自动修复 (ruff & pre-commit)"
	@echo "  make test        - 运行所有测试用例 (pytest)"

# 启动项目服务
run:
	uv run comet_rag/main.py

# 执行代码规范检查、格式化以及 pre-commit 钩子
pre-commit:
	uvx ruff check --fix .
	uvx ruff format .
	uvx pre-commit run --all-files

# 运行所有测试用例
test:
	uv run pytest
