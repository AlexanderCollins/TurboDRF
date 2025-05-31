.PHONY: test test-fast test-cov test-all lint format clean release-patch release-minor release-major release quick-release-patch quick-release-minor version

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

# Release commands
release-patch:
	@echo "Creating patch release..."
	@python scripts/release.py patch

release-minor:
	@echo "Creating minor release..."
	@python scripts/release.py minor

release-major:
	@echo "Creating major release..."
	@python scripts/release.py major

release:
	@if [ -z "$(VERSION)" ]; then \
		echo "Usage: make release VERSION=0.1.10"; \
		exit 1; \
	fi
	@python scripts/release.py $(VERSION)

# Quick release with auto-generated notes (requires GitHub CLI)
quick-release-patch:
	@python scripts/release.py patch
	@gh release create v$$(python -c "import turbodrf; print(turbodrf.__version__)") --generate-notes --title "TurboDRF v$$(python -c "import turbodrf; print(turbodrf.__version__)")"

quick-release-minor:
	@python scripts/release.py minor
	@gh release create v$$(python -c "import turbodrf; print(turbodrf.__version__)") --generate-notes --title "TurboDRF v$$(python -c "import turbodrf; print(turbodrf.__version__)")"

# Show current version
version:
	@python -c "import turbodrf; print(f'Current version: {turbodrf.__version__}')"
