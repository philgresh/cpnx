.PHONY: format lint test complexity docs-serve docs-build

VENV = .venv
BIN = $(VENV)/bin

format:
	$(BIN)/ruff format src/ tests/
	$(BIN)/ruff check --fix src/ tests/

lint:
	$(BIN)/ruff check src/ tests/

test:
	$(BIN)/pytest tests/ -v

complexity:
	$(BIN)/radon cc src/cpnx -s -a
	@bad=$$($(BIN)/radon cc src/cpnx --min C -s); \
	if [ -n "$$bad" ]; then \
		echo "Complexity gate FAILED — rank C or worse found:"; \
		echo "$$bad"; \
		exit 1; \
	fi; \
	echo "Complexity gate passed: all blocks rank A/B."

docs-serve:
	$(BIN)/mkdocs serve

docs-build:
	$(BIN)/mkdocs build --strict
