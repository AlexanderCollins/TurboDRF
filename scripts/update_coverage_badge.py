#!/usr/bin/env python3
"""
Update coverage badge in README.md with current coverage percentage.

This script:
1. Runs tests with coverage
2. Extracts the coverage percentage
3. Updates the README.md badge

Usage:
    python scripts/update_coverage_badge.py
"""

import json
import re
import subprocess
import sys
from pathlib import Path


def run_coverage():
    """Run pytest with coverage and generate JSON report."""
    print("Running tests with coverage...")
    result = subprocess.run(
        ["pytest", "--cov=turbodrf", "--cov-report=json", "--cov-report=term"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print("Tests failed!")
        print(result.stdout)
        print(result.stderr)
        sys.exit(1)

    print("Tests passed!")
    return result


def get_coverage_percentage():
    """Extract coverage percentage from coverage.json."""
    coverage_file = Path("coverage.json")

    if not coverage_file.exists():
        print("Error: coverage.json not found!")
        sys.exit(1)

    with open(coverage_file) as f:
        coverage_data = json.load(f)

    percentage = coverage_data["totals"]["percent_covered"]
    return round(percentage, 2)


def get_badge_color(percentage):
    """Get badge color based on coverage percentage."""
    if percentage >= 90:
        return "brightgreen"
    elif percentage >= 80:
        return "green"
    elif percentage >= 70:
        return "yellowgreen"
    elif percentage >= 60:
        return "yellow"
    elif percentage >= 50:
        return "orange"
    else:
        return "red"


def update_readme(percentage):
    """Update README.md with new coverage percentage."""
    readme_path = Path("README.md")

    if not readme_path.exists():
        print("Error: README.md not found!")
        sys.exit(1)

    with open(readme_path) as f:
        content = f.read()

    color = get_badge_color(percentage)

    # Pattern to match the coverage badge
    # Matches: [![Coverage](https://img.shields.io/badge/coverage-XX%25-color)]...
    pattern = r'\[!\[Coverage\]\(https://img\.shields\.io/badge/coverage-[\d.]+%25-\w+\)\]'

    # New badge
    new_badge = f"[![Coverage](https://img.shields.io/badge/coverage-{percentage}%25-{color})]"

    # Check if badge exists
    if re.search(pattern, content):
        # Update existing badge
        content = re.sub(pattern, new_badge, content)
        print(f"Updated existing coverage badge to {percentage}%")
    else:
        # Badge doesn't exist, look for commented badge
        comment_pattern = r'<!-- \[!\[Coverage\].*?\) -->'
        if re.search(comment_pattern, content):
            # Replace commented badge
            content = re.sub(comment_pattern, new_badge, content)
            print(f"Uncommented and set coverage badge to {percentage}%")
        else:
            print("Warning: Could not find coverage badge in README.md")
            print(f"Please add this badge manually:\n{new_badge}")
            return False

    # Write updated content
    with open(readme_path, "w") as f:
        f.write(content)

    print(f"README.md updated with coverage: {percentage}% ({color})")
    return True


def main():
    """Main function."""
    print("=" * 60)
    print("Updating Coverage Badge")
    print("=" * 60)

    # Run coverage
    run_coverage()

    # Get percentage
    percentage = get_coverage_percentage()
    print(f"\nCurrent coverage: {percentage}%")

    # Update README
    success = update_readme(percentage)

    if success:
        print("\n✅ Coverage badge updated successfully!")
        print(f"Coverage: {percentage}%")
    else:
        print("\n⚠️  Could not update badge automatically")
        sys.exit(1)


if __name__ == "__main__":
    main()
