# Contributing to TurboDRF

## Setup

```bash
git clone https://github.com/AlexanderCollins/TurboDRF.git
cd TurboDRF
python -m venv venv && source venv/bin/activate
pip install -e ".[dev]"
```

## Tests

```bash
python -m pytest                  # whole suite, in parallel (-n auto in pyproject.toml)
python -m pytest -n0              # one process, e.g. for pdb
python -m pytest tests/unit -q    # unit tests only (a few seconds)
python -m pytest -k <name>        # matching tests
```

Each xdist worker gets its own SQLite database and cache, so tests don't
interfere. Tests run against `tests/settings.py` and the models in
`tests/test_app/`; keep `tests/test_app/models.py` in step when you add a
framework-level feature, since it's the reference downstream apps mirror.
`tests.test_app.apps.set_test_brokerage(user, brokerage)` gives a test user
a tenant, and `user._test_roles = [...]` its roles.

## Lint

The lint tools are pinned in the `dev` extra, and CI uses the same pins.

```bash
make lint      # black --check, isort --check-only, flake8
make format    # black, isort
```

## Docs

The docs are Markdown files in `docs/`, published at
<https://turbodrf.com/docs/> along with `AGENTS.md` and `CHANGELOG.md`. Link
between pages with relative `page.md#anchor` links. CI also builds them with
Sphinx, failing on any warning:

```bash
pip install -e ".[docs]"
sphinx-build -W --keep-going -b html docs docs/_build/html
```

## Releasing

1. Set the new version in `pyproject.toml` and `turbodrf/__init__.py`, and
   add its entry to `CHANGELOG.md`.
2. Run the tests and lint, then commit (`release: vX.Y.Z — summary`) and
   push to `main`. Wait for CI to pass.
3. Tag and publish the release:

   ```bash
   git tag vX.Y.Z && git push origin vX.Y.Z
   gh release create vX.Y.Z --title "vX.Y.Z" --notes "..."
   ```

Publishing the GitHub release runs `.github/workflows/publish.yml`, which
runs the tests, checks that the tag matches the `pyproject.toml` version,
builds and checks the package, and uploads it to PyPI.
