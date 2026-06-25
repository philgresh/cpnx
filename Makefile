.PHONY: format lint test

VENV = .venv
BIN = $(VENV)/bin

format:
	$(BIN)/ruff format src/ tests/
	$(BIN)/ruff check --fix src/ tests/

lint:
	$(BIN)/ruff check src/ tests/

test:
	$(BIN)/pytest tests/ -v
