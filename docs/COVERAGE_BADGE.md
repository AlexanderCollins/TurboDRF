# Coverage Badge Setup

TurboDRF automatically updates the coverage badge in the README using GitHub Actions.

## How It Works

1. **Automated Updates (GitHub Actions)**:
   - The `.github/workflows/update-coverage-badge.yml` workflow runs on every push to `main`
   - It runs tests with coverage, extracts the percentage, and updates the README badge
   - The badge color changes based on coverage:
     - 90%+ = bright green
     - 80-89% = green
     - 70-79% = yellow-green
     - 60-69% = yellow
     - 50-59% = orange
     - <50% = red

2. **Manual Updates (Local)**:
   ```bash
   # Run the update script locally
   python scripts/update_coverage_badge.py
   ```

## Badge Format

The badge in README.md looks like:
```markdown
[![Coverage](https://img.shields.io/badge/coverage-XX.XX%25-color)](https://github.com/alexandercollins/turbodrf)
```

## Alternative: Gist-Based Dynamic Badge

If you prefer a dynamic badge using GitHub Gist (no commits required):

1. **Create a GitHub Gist**:
   - Go to https://gist.github.com
   - Create a new public gist named `turbodrf-coverage.json`
   - Add any content (it will be overwritten)
   - Note the Gist ID from the URL

2. **Create a Personal Access Token**:
   - Go to GitHub Settings → Developer settings → Personal access tokens
   - Generate new token with `gist` scope
   - Copy the token

3. **Add GitHub Secrets**:
   - Go to your repo → Settings → Secrets and variables → Actions
   - Add two secrets:
     - `GIST_SECRET`: Your personal access token
     - `GIST_ID`: Your gist ID

4. **Update README badge**:
   ```markdown
   [![Coverage](https://img.shields.io/endpoint?url=https://gist.githubusercontent.com/USERNAME/GIST_ID/raw/turbodrf-coverage.json)](https://github.com/alexandercollins/turbodrf)
   ```

The gist-based approach uses the code already in `.github/workflows/ci.yml`.

## Current Coverage

Run `python scripts/update_coverage_badge.py` to see the current coverage percentage.
