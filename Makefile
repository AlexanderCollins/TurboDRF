.PHONY: test test-fast test-cov test-all lint format clean

test:
	pytest

test-fast:
	pytest -x --ff

test-cov:
	pytest --cov=turbodrf --cov-report=html --cov-report=term

test-all:
	tox

lint:
	flake8 turbodrf/
	black --check turbodrf/
	isort --check-only turbodrf/

format:
	black turbodrf/
	isort turbodrf/

clean:
	rm -rf .pytest_cache
	rm -rf .coverage
	rm -rf htmlcov
	rm -rf .tox
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -delete
