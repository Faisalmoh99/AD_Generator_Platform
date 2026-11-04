.PHONY: setup lint format typecheck test check serve

setup:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy --strict src tests

test:
	uv run pytest

# Full quality gate: must pass before every commit.
check: lint typecheck test

serve:
	uv run adsmith serve
