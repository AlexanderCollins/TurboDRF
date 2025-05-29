# Contributing to TurboDRF 🚀

First off, thank you for considering contributing to TurboDRF! It's people like you that make TurboDRF such a great tool. We welcome contributions from everyone, regardless of experience level.

## 📋 Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [How Can I Contribute?](#how-can-i-contribute)
- [Development Setup](#development-setup)
- [Development Workflow](#development-workflow)
- [Testing Guidelines](#testing-guidelines)
- [Code Style](#code-style)
- [Documentation](#documentation)
- [Pull Request Process](#pull-request-process)
- [Release Process](#release-process)

## 🤝 Code of Conduct

By participating in this project, you agree to abide by our Code of Conduct:

- **Be respectful** - Disagreements happen, but respect differing viewpoints
- **Be inclusive** - Welcome and support people of all backgrounds
- **Be constructive** - Provide helpful feedback and accept it gracefully
- **Be responsible** - Take ownership of your mistakes and learn from them

## 🚀 Getting Started

1. **Star the repository** ⭐ - If you haven't already!
2. **Fork the repository** - Click the Fork button in the top right
3. **Join our community** - Discussions happen in GitHub Issues and Pull Requests

## 💡 How Can I Contribute?

### 🐛 Reporting Bugs

Before creating bug reports, please check existing issues to avoid duplicates. When creating a bug report, include:

- **Clear, descriptive title**
- **Steps to reproduce** (be specific!)
- **Expected behavior**
- **Actual behavior**
- **Screenshots** (if applicable)
- **Environment details:**
  ```
  - TurboDRF version: 
  - Python version:
  - Django version:
  - DRF version:
  - OS:
  ```

**Example Bug Report:**
```markdown
Title: Field permissions not applied to nested serializers

Description:
When using nested field notation (author__name), field-level permissions 
are not being enforced correctly.

Steps to reproduce:
1. Define a model with nested fields in turbodrf()
2. Set field permission 'books.book.author__name.read' for admin role only
3. Access API as viewer role
4. The author__name field is visible (should be hidden)

Expected: Field should not be visible to viewer role
Actual: Field is visible to all authenticated users

Environment:
- TurboDRF: 0.1.0
- Python: 3.11
- Django: 4.2
- DRF: 3.14
```

### ✨ Suggesting Enhancements

Enhancement suggestions are tracked as GitHub issues. When creating an enhancement suggestion, include:

- **Use case** - Why is this needed?
- **Current behavior** - What happens now?
- **Desired behavior** - What should happen?
- **Alternative solutions** - Have you considered other approaches?
- **Additional context** - Screenshots, mockups, examples

### 🔧 Your First Code Contribution

Unsure where to begin? Look for these labels:

- `good first issue` - Simple issues perfect for beginners
- `help wanted` - Issues where we need community help
- `documentation` - Help improve our docs

## 💻 Development Setup

### Prerequisites

- Python 3.8 or higher
- Git
- Virtual environment tool (venv, virtualenv, conda)

### Setup Steps

1. **Clone your fork:**
   ```bash
   git clone https://github.com/alexandercollins/turbodrf.git
   cd turbodrf
   ```

2. **Add upstream remote:**
   ```bash
   git remote add upstream https://github.com/alexandercollins/turbodrf.git
   ```

3. **Create virtual environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

4. **Install in development mode:**
   ```bash
   pip install -e ".[dev]"
   ```

5. **Install pre-commit hooks:**
   ```bash
   pre-commit install
   ```

6. **Verify setup:**
   ```bash
   pytest  # Should run all tests successfully
   ```

## 🔄 Development Workflow

### 1. Create a Branch

```bash
# Update main branch
git checkout main
git pull upstream main

# Create feature branch
git checkout -b feature/your-feature-name
# Or for bugs: git checkout -b fix/bug-description
```

### 2. Make Your Changes

- Write clean, readable code
- Add docstrings to all functions/classes
- Update tests for new functionality
- Update documentation if needed

### 3. Test Your Changes

```bash
# Run all tests
pytest

# Run specific test file
pytest tests/test_permissions.py

# Run with coverage
pytest --cov=turbodrf --cov-report=html

# Check code style
black --check turbodrf/
flake8 turbodrf/
isort --check-only turbodrf/
```

### 4. Commit Your Changes

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```bash
# Format: <type>(<scope>): <subject>

git commit -m "feat(permissions): add field-level permission caching"
git commit -m "fix(router): handle models without turbodrf method"
git commit -m "docs(readme): update installation instructions"
git commit -m "test(views): add edge case for empty queryset"
```

**Types:**
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation only
- `style`: Code style changes (formatting, etc)
- `refactor`: Code change that neither fixes a bug nor adds a feature
- `perf`: Performance improvement
- `test`: Adding or updating tests
- `chore`: Changes to build process or auxiliary tools

## 🧪 Testing Guidelines

### Writing Tests

1. **Test file naming:** `test_<module_name>.py`
2. **Test class naming:** `Test<ClassName>`
3. **Test method naming:** `test_<specific_scenario>`

### Test Structure

```python
class TestTurboDRFPermission(TestCase):
    """Test cases for TurboDRF permissions."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Create test data
        pass
    
    def test_admin_can_perform_all_operations(self):
        """Test that admin users have full CRUD access."""
        # Arrange
        user = self.create_admin_user()
        
        # Act
        response = self.client.get('/api/books/')
        
        # Assert
        self.assertEqual(response.status_code, 200)
    
    def test_viewer_can_only_read(self):
        """Test that viewer users have read-only access."""
        # Test implementation
        pass
```

### Test Categories

- **Unit Tests** (`tests/unit/`) - Test individual components
- **Integration Tests** (`tests/integration/`) - Test component interactions
- **End-to-End Tests** - Test complete user workflows

### Coverage Requirements

- New features must have >90% test coverage
- Bug fixes must include regression tests
- Run `pytest --cov=turbodrf` to check coverage

## 🎨 Code Style

### Python Style Guide

We use several tools to maintain consistent code style:

1. **Black** - Code formatting (line length: 88)
2. **isort** - Import sorting
3. **flake8** - Linting (max line length: 88)

### Docstring Format

We use Google-style docstrings:

```python
def calculate_permissions(user, model):
    """
    Calculate effective permissions for a user on a model.
    
    This function aggregates permissions from all user roles and returns
    the complete set of permissions applicable to the given model.
    
    Args:
        user: User instance with 'roles' property
        model: Django model class with TurboDRFMixin
        
    Returns:
        set: Set of permission strings (e.g., {'books.book.read', 'books.book.update'})
        
    Raises:
        AttributeError: If user doesn't have 'roles' property
        
    Example:
        >>> user = User.objects.get(username='admin')
        >>> perms = calculate_permissions(user, Book)
        >>> 'books.book.read' in perms
        True
    """
    # Implementation
```

### Import Order

```python
# Standard library imports
import os
import sys
from datetime import datetime

# Third-party imports
import django
from django.db import models
from rest_framework import serializers

# Local imports
from .mixins import TurboDRFMixin
from .permissions import TurboDRFPermission
```

## 📚 Documentation

### Types of Documentation

1. **Code Documentation** - Docstrings in source code
2. **API Documentation** - Technical reference in `API.md`
3. **User Documentation** - README.md and other markdown files
4. **Examples** - Working examples in `example_project/`

### Documentation Standards

- Use clear, simple language
- Include code examples
- Explain the "why" not just the "how"
- Keep it up to date with code changes

## 🔀 Pull Request Process

### Before Submitting

- [ ] Tests pass locally (`pytest`)
- [ ] Code style checks pass (`make lint`)
- [ ] Documentation is updated
- [ ] Commit messages follow convention
- [ ] Branch is up to date with main

### PR Title Format

Follow the same convention as commits:
- `feat(scope): add amazing new feature`
- `fix(scope): resolve issue with X`

### PR Description Template

```markdown
## Description
Brief description of changes

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Breaking change
- [ ] Documentation update

## Testing
- [ ] Unit tests pass
- [ ] Integration tests pass
- [ ] Manual testing completed

## Checklist
- [ ] My code follows the project style
- [ ] I've added tests for my changes
- [ ] I've updated documentation
- [ ] I've added myself to CONTRIBUTORS.md

## Related Issues
Fixes #123
```

### Review Process

1. **Automated checks** must pass
2. **Code review** by at least one maintainer
3. **Testing** in multiple environments
4. **Documentation review** if applicable
5. **Final approval** and merge

## 🚢 Release Process

We use semantic versioning (MAJOR.MINOR.PATCH):

- **MAJOR**: Breaking changes
- **MINOR**: New features (backward compatible)
- **PATCH**: Bug fixes (backward compatible)

### Release Steps

1. Update version in `turbodrf/__init__.py`
2. Update CHANGELOG.md
3. Create release PR
4. Tag release after merge
5. Build and publish to PyPI
6. Update documentation

## 🎉 Recognition

Contributors are recognized in:
- CONTRIBUTORS.md file
- Release notes
- Project documentation

## 💬 Getting Help

- **GitHub Issues** - For bugs and features
- **Discussions** - For questions and ideas
- **Email** - For security issues only

## 🙏 Thank You!

Your contributions make TurboDRF better for everyone. We appreciate your time and effort in improving the project!

---

**Happy Coding!** 🚀