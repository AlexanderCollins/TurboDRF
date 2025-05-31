#!/usr/bin/env python3
"""
TurboDRF Release Automation Script

This script automates the release process:
1. Bumps version in all required files
2. Commits the changes
3. Creates a git tag
4. Pushes to GitHub
5. Creates a GitHub release (triggers PyPI deployment)

Usage:
    python scripts/release.py patch  # 0.1.0 -> 0.1.1
    python scripts/release.py minor  # 0.1.0 -> 0.1.11
    python scripts/release.py major  # 0.1.0 -> 1.0.0
    python scripts/release.py 0.1.11  # Specific version
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path


def get_current_version():
    """Extract current version from __init__.py"""
    init_file = Path("turbodrf/__init__.py")
    content = init_file.read_text()
    match = re.search(r'__version__ = ["\']([^"\']+)["\']', content)
    if match:
        return match.group(1)
    raise ValueError("Could not find version in turbodrf/__init__.py")


def parse_version(version):
    """Parse version string into tuple of integers"""
    return tuple(map(int, version.split(".")))


def bump_version(current_version, bump_type):
    """Calculate new version based on bump type"""
    major, minor, patch = parse_version(current_version)

    if bump_type == "major":
        return f"{major + 1}.0.0"
    elif bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    elif bump_type == "patch":
        return f"{major}.{minor}.{patch + 1}"
    else:
        # Assume it's a specific version
        return bump_type


def update_version_in_file(file_path, old_version, new_version, pattern=None):
    """Update version in a specific file"""
    path = Path(file_path)
    content = path.read_text()

    if pattern:
        # Use custom pattern
        new_content = re.sub(
            pattern.format(old_version), pattern.format(new_version), content
        )
    else:
        # Simple string replacement
        new_content = content.replace(old_version, new_version)

    if new_content != content:
        path.write_text(new_content)
        print(f"✅ Updated {file_path}: {old_version} -> {new_version}")
    else:
        print(f"⚠️  No changes in {file_path}")


def run_command(cmd, check=True):
    """Run a shell command and return output"""
    print(f"🏃 Running: {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)

    if check and result.returncode != 0:
        print(f"❌ Command failed: {cmd}")
        print(f"Error: {result.stderr}")
        sys.exit(1)

    return result


def main():
    parser = argparse.ArgumentParser(description="Automate TurboDRF releases")
    parser.add_argument(
        "version",
        help="Version bump type (major/minor/patch) or specific version (e.g., 0.1.11)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--no-push", action="store_true", help="Don't push changes to GitHub"
    )

    args = parser.parse_args()

    # Get current version
    current_version = get_current_version()
    print(f"📌 Current version: {current_version}")

    # Calculate new version
    if args.version in ["major", "minor", "patch"]:
        new_version = bump_version(current_version, args.version)
    else:
        new_version = args.version

    print(f"🚀 New version: {new_version}")

    if args.dry_run:
        print("\n🔍 DRY RUN - No changes will be made")
        return

    # Update version in all files
    print("\n📝 Updating version files...")

    # Update __init__.py
    update_version_in_file(
        "turbodrf/__init__.py", current_version, new_version, '__version__ = "{}"'
    )

    # Update setup.py
    update_version_in_file("setup.py", current_version, new_version, 'version="{}"')

    # Update pyproject.toml
    update_version_in_file(
        "pyproject.toml", current_version, new_version, 'version = "{}"'
    )

    # Run tests to ensure everything is working
    print("\n🧪 Running tests...")
    result = run_command("pytest tests/unit/test_package.py -v", check=False)
    if result.returncode != 0:
        print("⚠️  Tests failed, but continuing...")

    # Git operations
    print("\n📦 Committing changes...")
    run_command("git add turbodrf/__init__.py setup.py pyproject.toml")
    run_command(f'git commit -m "chore: Bump version to {new_version}"')

    print("\n🏷️  Creating tag...")
    run_command(f'git tag -a v{new_version} -m "Release version {new_version}"')

    if not args.no_push:
        print("\n📤 Pushing to GitHub...")
        run_command("git push origin main")
        run_command(f"git push origin v{new_version}")

        print("\n🎉 Release preparation complete!")
        print("\n📋 Next steps:")
        print("1. Go to: https://github.com/alexandercollins/turbodrf/releases/new")
        print(f"2. Select the tag 'v{new_version}'")
        print(f"3. Set release title: 'TurboDRF v{new_version}'")
        print("4. Add release notes describing the changes")
        print("5. Click 'Publish release' to trigger PyPI deployment")
        print("\nOr use GitHub CLI:")
        print(
            f'gh release create v{new_version} --title "TurboDRF v{new_version}" '
            '--notes "Add release notes here"'
        )
    else:
        print("\n✅ Changes committed locally (not pushed)")
        print("Run these commands when ready:")
        print("  git push origin main")
        print(f"  git push origin v{new_version}")


if __name__ == "__main__":
    main()
