.PHONY: help install lint typecheck format test coverage build check pre-commit db-up db-down

help:
	@printf '%s\n' \
	  'install     安装锁定的开发依赖' \
	  'check       执行静态检查、单元测试与构建' \
	  'lint        Ruff 与 Pyright 检查' \
	  'format      格式化 Python 代码' \
	  'test        运行单元测试' \
	  'coverage    运行单元测试并显示覆盖率' \
	  'build       构建 wheel 与源码包' \
	  'pre-commit  执行全量提交检查' \
	  'db-up       启动隔离的 V2 PostgreSQL 与 Milvus' \
	  'db-down     停止 V2 中间件，保留数据卷'

install:
	uv sync --locked

lint:
	uv run --locked ruff check .
	uv run --locked ruff format --check .
	uv run --locked pyright

typecheck:
	uv run --locked pyright

format:
	uv run --locked ruff check . --fix
	uv run --locked ruff format .

test:
	uv run --locked pytest

coverage:
	uv run --locked pytest --cov=comet_rag --cov-report=term-missing

build:
	uv build

check: lint test build

pre-commit:
	uv run --locked pre-commit run --all-files

db-up:
	docker compose up -d

db-down:
	docker compose down
