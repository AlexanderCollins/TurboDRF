.PHONY: test test-fast test-cov lint format docs clean

LINT_PATHS = turbodrf/ tests/

test:
	pytest

test-fast:
	pytest -x --ff

test-cov:
	pytest --cov=turbodrf --cov-report=html --cov-report=term

lint:
	black --check $(LINT_PATHS)
	isort --check-only $(LINT_PATHS)
	flake8 $(LINT_PATHS)

format:
	black $(LINT_PATHS)
	isort $(LINT_PATHS)

docs:
	sphinx-build -W --keep-going -b html docs docs/_build/html

clean:
	rm -rf .pytest_cache .coverage htmlcov build dist *.egg-info docs/_build
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -delete
