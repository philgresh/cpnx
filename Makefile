.PHONY: format lint test docs-serve docs-build

VENV = .venv
BIN = $(VENV)/bin

format:
	$(BIN)/ruff format src/ tests/
	$(BIN)/ruff check --fix src/ tests/

lint:
	$(BIN)/ruff check src/ tests/

test:
	$(BIN)/pytest tests/ -v

docs-serve:
	$(BIN)/mkdocs serve

docs-build:
	$(BIN)/mkdocs build --strict
