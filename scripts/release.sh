#!/bin/bash
# TurboDRF Quick Release Script
# Usage: ./scripts/release.sh [patch|minor|major|version]

set -e  # Exit on error

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get current version
CURRENT_VERSION=$(python -c "import turbodrf; print(turbodrf.__version__)")
echo -e "${GREEN}Current version: ${CURRENT_VERSION}${NC}"

# Determine new version
if [ -z "$1" ]; then
    echo "Usage: $0 [patch|minor|major|version]"
    echo "Example: $0 patch"
    echo "Example: $0 0.1.12"
    exit 1
fi

# Use Python script for version calculation
NEW_VERSION=$(python -c "
import sys
current = '$CURRENT_VERSION'
bump = '$1'
major, minor, patch = map(int, current.split('.'))

if bump == 'major':
    print(f'{major + 1}.0.0')
elif bump == 'minor':
    print(f'{major}.{minor + 1}.0')
elif bump == 'patch':
    print(f'{major}.{minor}.{patch + 1}')
else:
    print(bump)
")

echo -e "${GREEN}New version: ${NEW_VERSION}${NC}"

# Confirmation
read -p "Continue with release? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    exit 1
fi

# Update version files
echo -e "${YELLOW}Updating version files...${NC}"
sed -i.bak "s/__version__ = \".*\"/__version__ = \"$NEW_VERSION\"/" turbodrf/__init__.py
sed -i.bak "s/version=\".*\"/version=\"$NEW_VERSION\"/" setup.py
sed -i.bak "s/version = \".*\"/version = \"$NEW_VERSION\"/" pyproject.toml

# Clean up backup files
rm -f turbodrf/__init__.py.bak setup.py.bak pyproject.toml.bak

# Git operations
echo -e "${YELLOW}Committing changes...${NC}"
git add turbodrf/__init__.py setup.py pyproject.toml
git commit -m "chore: Bump version to $NEW_VERSION"

echo -e "${YELLOW}Creating tag...${NC}"
git tag -a "v$NEW_VERSION" -m "Release version $NEW_VERSION"

echo -e "${YELLOW}Pushing to GitHub...${NC}"
git push origin main
git push origin "v$NEW_VERSION"

echo -e "${GREEN}✅ Release preparation complete!${NC}"
echo
echo "To complete the release:"
echo "1. Go to: https://github.com/alexandercollins/turbodrf/releases/new"
echo "2. Select tag: v$NEW_VERSION"
echo "3. Add release notes"
echo "4. Click 'Publish release'"
echo
echo "Or use GitHub CLI:"
echo "gh release create v$NEW_VERSION --title \"TurboDRF v$NEW_VERSION\" --generate-notes"