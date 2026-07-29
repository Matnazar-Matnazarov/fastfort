.DEFAULT_GOAL := help
.PHONY: help install test test-all test-pg test-mysql lint fmt types check cov clean services-up services-down

help:  ## Show the available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install:  ## Set up the development environment
	uv sync --all-extras

test:  ## Run the test suite against SQLite
	uv run pytest -q

test-all:  ## Run against SQLite, PostgreSQL and MySQL (services must be up)
	uv run pytest --db=all -q

test-pg:  ## Run against PostgreSQL only
	uv run pytest --db=postgres -q

test-mysql:  ## Run against MySQL only
	uv run pytest --db=mysql -q

cov:  ## Produce a coverage report
	uv run pytest --cov --cov-report=term-missing --cov-report=html
	@echo "HTML report: htmlcov/index.html"

lint:  ## Check linting and formatting
	uv run ruff check .
	uv run ruff format --check .

fmt:  ## Auto-fix and format the code
	uv run ruff check --fix .
	uv run ruff format .

types:  ## Run the type checker
	uv run mypy fastfort

check: lint types test  ## Every quality gate -- run this before opening a pull request

services-up:  ## Start the local test databases
	docker compose -f docker-compose.test.yml up -d --wait

services-down:  ## Stop the local test databases and drop their data
	docker compose -f docker-compose.test.yml down -v

clean:  ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml dist build
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
