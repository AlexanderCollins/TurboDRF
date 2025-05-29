# 📦 TurboDRF PyPI Deployment Guide

This guide covers how to build, test, and deploy TurboDRF to PyPI (Python Package Index).

## Prerequisites

1. **PyPI Account**
   - Register at [PyPI](https://pypi.org/account/register/)
   - Register at [Test PyPI](https://test.pypi.org/account/register/) for testing

2. **Install Build Tools**
   ```bash
   pip install --upgrade pip
   pip install --upgrade build twine
   ```

3. **Configure PyPI Publishing**
   
   TurboDRF uses OpenID Connect (OIDC) for secure PyPI publishing via GitHub Actions.
   No API tokens or passwords are needed - authentication happens automatically
   through GitHub's trusted publisher setup.

## Building the Package

1. **Clean Previous Builds**
   ```bash
   rm -rf dist/ build/ *.egg-info/
   ```

2. **Update Version Number**
   
   Update version in:
   - `setup.py`
   - `pyproject.toml`
   - `turbodrf/__init__.py`

3. **Build Distribution Files**
   ```bash
   python -m build
   ```

   This creates:
   - `dist/turbodrf-0.1.0.tar.gz` (source distribution)
   - `dist/turbodrf-0.1.0-py3-none-any.whl` (wheel)

## Testing Before Deployment

1. **Run All Tests**
   ```bash
   # Run unit tests
   pytest tests/

   # Run with coverage
   pytest --cov=turbodrf --cov-report=html

   # Run linting
   black --check turbodrf/
   flake8 turbodrf/
   isort --check-only turbodrf/
   ```

2. **Test Installation Locally**
   ```bash
   # Create a virtual environment
   python -m venv test_env
   source test_env/bin/activate  # On Windows: test_env\Scripts\activate

   # Install from local build
   pip install dist/turbodrf-0.1.0-py3-none-any.whl

   # Test import
   python -c "import turbodrf; print(turbodrf.__version__)"
   ```

3. **Upload to Test PyPI**
   ```bash
   twine upload --repository testpypi dist/*
   ```

4. **Install from Test PyPI**
   ```bash
   pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ turbodrf
   ```

## Production Deployment

1. **Final Checks**
   - [ ] All tests pass
   - [ ] Documentation is updated
   - [ ] CHANGELOG.md is updated
   - [ ] Version numbers are correct
   - [ ] Example project works

2. **Tag the Release**
   ```bash
   git tag -a v0.1.0 -m "Release version 0.1.0"
   git push origin v0.1.0
   ```

3. **Upload to PyPI**
   ```bash
   twine upload dist/*
   ```

4. **Verify Installation**
   ```bash
   pip install turbodrf
   ```

## Post-Deployment

1. **Create GitHub Release**
   - Go to GitHub releases page
   - Create release from tag
   - Add release notes from CHANGELOG.md
   - Upload wheel and tarball files

2. **Update Documentation**
   - Update README badges
   - Update installation instructions
   - Post announcement

## Troubleshooting

### Common Issues

1. **"Invalid distribution file"**
   - Ensure `setup.py` and `pyproject.toml` are properly formatted
   - Check that all required files are included in MANIFEST.in

2. **"Version already exists"**
   - Increment version number
   - Version numbers cannot be reused on PyPI

3. **Missing files in package**
   - Check MANIFEST.in
   - Ensure package_data is correctly specified

### Package Structure Verification

```bash
# Check what files are included
tar -tzf dist/turbodrf-0.1.0.tar.gz

# Check wheel contents
unzip -l dist/turbodrf-0.1.0-py3-none-any.whl
```

## Automation with GitHub Actions

### Setting up OIDC Publishing

1. **Configure PyPI Trusted Publisher**
   - Go to your PyPI project settings
   - Add a new trusted publisher:
     - Publisher: GitHub
     - Repository: alexandercollins/turbodrf
     - Workflow: publish.yml
     - Environment: pypi (optional)

2. **GitHub Actions Workflow**

Create `.github/workflows/publish.yml`:

```yaml
name: Publish to PyPI

on:
  release:
    types: [published]

jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions:
      id-token: write  # Required for OIDC
    steps:
    - uses: actions/checkout@v3
    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.x'
    - name: Install dependencies
      run: |
        python -m pip install --upgrade pip
        pip install build
    - name: Build package
      run: python -m build
    - name: Publish to PyPI
      uses: pypa/gh-action-pypi-publish@release/v1
      # No credentials needed - uses OIDC!
```

### Benefits of OIDC Publishing

- **No API tokens to manage** - Authentication via GitHub identity
- **More secure** - No long-lived credentials that can be compromised
- **Easier setup** - No secrets to configure in GitHub
- **Automatic** - Works immediately when trusted publisher is configured

## Maintenance

### Version Bumping

Follow semantic versioning:
- MAJOR: Breaking changes
- MINOR: New features (backwards compatible)
- PATCH: Bug fixes

### Deprecation Policy

1. Mark deprecated features in code
2. Add deprecation warnings
3. Document in CHANGELOG.md
4. Remove after 2 minor versions

### Security Updates

Monitor dependencies:
```bash
pip list --outdated
safety check
```

## Resources

- [PyPI Documentation](https://packaging.python.org/)
- [Python Packaging User Guide](https://packaging.python.org/tutorials/packaging-projects/)
- [Twine Documentation](https://twine.readthedocs.io/)
- [Semantic Versioning](https://semver.org/)