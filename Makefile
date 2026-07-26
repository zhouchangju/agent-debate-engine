.PHONY: check format test typecheck lint build

check: lint typecheck test

format:
	ruff format .
	ruff check --fix .

lint:
	ruff format --check .
	ruff check .

typecheck:
	mypy src

test:
	python -m coverage erase
	pytest --cov=agent_debate --cov-report=term-missing

build:
	python -m build
