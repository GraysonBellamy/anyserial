# Convenience targets — all real work happens in uv.
# Users who don't like make can run the `uv run ...` commands directly.

.DEFAULT_GOAL := help
.PHONY: help install sync lint format typecheck test test-all cov bench docs clean \
        act-list act-lint act-typecheck act-unit act-integration act-build act-ci act-pr act-clean

ACT ?= act

help:  ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install:  ## Install dev environment
	uv sync --all-extras --all-groups

sync:  ## Refresh lockfile + install
	uv lock && uv sync --all-extras --all-groups

lint:  ## Ruff check
	uv run ruff check
	uv run ruff format --check

format:  ## Ruff format + autofix
	uv run ruff format
	uv run ruff check --fix

typecheck:  ## mypy + pyright
	uv run mypy
	uv run pyright

test:  ## Run tests (fast)
	uv run pytest

test-all:  ## Run tests on all supported Python versions
	uv run --python 3.13 pytest
	uv run --python 3.14 pytest

cov:  ## Run tests with coverage
	uv run pytest --cov --cov-report=term-missing --cov-report=html

bench:  ## Run benchmarks (requires bench group)
	uv sync --all-extras --group bench --group test
	mkdir -p benchmarks/results
	uv run pytest benchmarks/ --benchmark-only \
		--benchmark-json=benchmarks/results/$$(git rev-parse --short HEAD).json

docs:  ## Serve Zensical docs locally
	uv run --group docs zensical serve

clean:  ## Remove build artifacts and caches
	rm -rf build/ dist/ *.egg-info
	rm -rf .pytest_cache .mypy_cache .ruff_cache .pyright .hypothesis
	rm -rf htmlcov .coverage coverage.xml
	rm -rf site/
	find . -type d -name __pycache__ -exec rm -rf {} +

# ---- act: run CI workflows locally (config in .actrc) ----

act-list:  ## List jobs act will run
	$(ACT) -l

act-lint:  ## Run lint job locally
	$(ACT) -j lint

act-typecheck:  ## Run typecheck job locally
	$(ACT) -j typecheck

act-unit:  ## Run unit tests (Linux, py3.13)
	$(ACT) -j test-unit --matrix os:ubuntu-latest --matrix python:3.13

act-integration:  ## Run Linux integration tests (py3.13)
	$(ACT) -j test-integration-linux --matrix python:3.13

act-build:  ## Run build job (sdist + wheel)
	$(ACT) -j build

act-ci: act-lint act-typecheck act-unit act-integration act-build  ## Full runnable CI subset

act-pr:  ## Simulate pull_request event
	$(ACT) pull_request

act-clean:  ## Remove act artifacts + local container state
	rm -rf /tmp/act-artifacts
	- docker container prune -f --filter label=com.github.actions.runner
