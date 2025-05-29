from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="turbodrf",
    version="0.1.1",
    author="Your Name",
    author_email="your.email@example.com",
    description="Dead simple Django REST API generator with role-based permissions",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/yourusername/turbodrf",
    packages=find_packages(),
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: Internet :: WWW/HTTP :: Dynamic Content",
        "Topic :: Internet :: WWW/HTTP :: HTTP Servers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Framework :: Django",
        "Framework :: Django :: 3.2",
        "Framework :: Django :: 4.0",
        "Framework :: Django :: 4.1",
        "Framework :: Django :: 4.2",
        "Framework :: Django :: 5.0",
    ],
    python_requires=">=3.8",
    install_requires=[
        "Django>=3.2",
        "djangorestframework>=3.12.0",
        "drf-yasg>=1.20.0",
        "django-filter>=2.0",
    ],
    extras_require={
        "dev": [
            "pytest>=6.0",
            "pytest-django>=4.0",
            "pytest-cov>=2.0",
            "black>=22.0",
            "flake8>=4.0",
            "isort>=5.0",
            "mypy>=0.9",
        ],
    },
    include_package_data=True,
    package_data={
        "turbodrf": ["templates/turbodrf/*.html"],
    },
)
