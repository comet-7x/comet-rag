# 设置变量
APP_NAME := comet_rag
PYTHON := uv run
PYTEST := uv run pytest
RUFF := uvx ruff
# pyright 走 uv run 而非 uvx：它必须在**项目自己的 venv 里**跑，否则看不到
# 已安装的依赖与类型存根（lxml-stubs 等），会把一切第三方 import 报成未解析。
PYRIGHT := uv run pyright

.PHONY: help install run dev test lint typecheck format pre-commit migrate db-up db-down clean


help: ## 显示此帮助信息
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'


install: ## 安装依赖并配置 pre-commit
	# --extra all（milvus + server）而非裸 uv sync：少了它们，pyright 会把
	# fastapi / sqlalchemy / pymilvus 全报成"无法解析的导入"，make lint 直接红。
	# 刻意不用 --all-extras —— 那会把 mineru 的数 GB 依赖一起拖下来（M2 才要）。
	uv sync --extra all
	uv run pre-commit install

clean: ## 清理缓存文件 (__pycache__, .pytest_cache, .ruff_cache)
	find . -type d -name "__pycache__" -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache
	rm -rf build/ dist/ *.egg-info


run: ## 生产模式启动服务
	$(PYTHON) python -m $(APP_NAME).api.main

dev: ## 开发模式启动服务 (热重载)
	$(PYTHON) fastapi dev $(APP_NAME)/api/main.py


lint: ## 执行静态检查 (ruff + pyright)
	$(RUFF) check .
	$(PYRIGHT)

typecheck: ## 只跑类型检查 (pyright)
	$(PYRIGHT)

format: ## 格式化代码 (ruff)
	$(RUFF) format .
	$(RUFF) check . --fix

test: ## 运行所有测试并生成覆盖率报告
	$(PYTEST) tests/

pre-commit: format lint ## 执行代码规范全量检查
	uv run pre-commit run --all-files


db-up: ## 启动所有基础服务 (Postgres, Milvus, Redis)
	docker-compose -f docker/docker-compose.dev.yml up -d

db-down: ## 停止所有基础服务
	docker-compose -f docker/docker-compose.dev.yml down

migrate: ## 执行数据库迁移 (Alembic)
	$(PYTHON) alembic upgrade head

migration-gen: ## 生成新的迁移文件 (使用方式: make migration-gen msg="fix_user_table")
	$(PYTHON) alembic revision --autogenerate -m "$(msg)"
