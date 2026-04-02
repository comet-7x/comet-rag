# 设置变量
APP_NAME := comet_rag
PYTHON := uv run
PYTEST := uv run pytest
RUFF := uvx ruff
MYPY := uvx mypy

.PHONY: help install run dev test lint format pre-commit migrate db-up db-down clean

# ==============================================================================
# 帮助信息 (自动化解析展示)
# ==============================================================================
help: ## 显示此帮助信息
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ==============================================================================
# 基础建设 & 依赖管理
# ==============================================================================
install: ## 安装依赖并配置 pre-commit
	uv sync
	uv run pre-commit install

clean: ## 清理缓存文件 (__pycache__, .pytest_cache, .mypy_cache)
	find . -type d -name "__pycache__" -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	rm -rf build/ dist/ *.egg-info

# ==============================================================================
# 开发 & 运行
# ==============================================================================
run: ## 生产模式启动服务
	$(PYTHON) python -m $(APP_NAME).main

dev: ## 开发模式启动服务 (热重载)
	$(PYTHON) fastapi dev $(APP_NAME)/main.py

# ==============================================================================
# 质量保证 (QA)
# ==============================================================================
lint: ## 执行静态检查 (ruff + mypy)
	$(RUFF) check .
	$(MYPY) .

format: ## 格式化代码 (ruff)
	$(RUFF) format .

test: ## 运行所有测试并生成覆盖率报告
	$(PYTEST) tests/

pre-commit: format lint ## 执行代码规范全量检查
	uv run pre-commit run --all-files

# ==============================================================================
# 基础设施 & 数据库 (针对你的 Docker 配置)
# ==============================================================================
db-up: ## 启动所有基础服务 (Postgres, Milvus, Redis)
	docker-compose -f docker/docker-compose.dev.yml up -d

db-down: ## 停止所有基础服务
	docker-compose -f docker/docker-compose.dev.yml down

migrate: ## 执行数据库迁移 (Alembic)
	$(PYTHON) alembic upgrade head

migration-gen: ## 生成新的迁移文件 (使用方式: make migration-gen msg="fix_user_table")
	$(PYTHON) alembic revision --autogenerate -m "$(msg)"
