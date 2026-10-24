# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

import os
import sys
from importlib import metadata
from pathlib import Path

# autodoc imports turbodrf, which needs configured Django settings. Use the
# test suite's settings, with the repository root on the path.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")

import django  # noqa: E402

django.setup()

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

project = "TurboDRF"
copyright = "2025, TurboDRF Team"
author = "TurboDRF Team"
try:
    release = metadata.version("turbodrf")
except metadata.PackageNotFoundError:  # a checkout that isn't installed
    import turbodrf

    release = turbodrf.__version__
version = ".".join(release.split(".")[:2])

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.viewcode",
    "sphinx.ext.napoleon",
    "sphinx_rtd_theme",
    "myst_parser",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]

# Napoleon settings
napoleon_google_docstring = True
napoleon_numpy_docstring = True
napoleon_include_init_with_doc = True
napoleon_include_private_with_doc = False
napoleon_include_special_with_doc = True
napoleon_use_admonition_for_examples = False
napoleon_use_admonition_for_notes = False
napoleon_use_admonition_for_references = False
# Attributes sections as :ivar: fields, so they don't duplicate the
# attributes autodoc documents.
napoleon_use_ivar = True
napoleon_use_param = True
napoleon_use_rtype = True
napoleon_preprocess_types = False
napoleon_type_aliases = None
napoleon_attr_annotations = True

# MyST settings
myst_enable_extensions = [
    "deflist",
    "html_image",
]
# Generate heading anchors so `page.md#section` links resolve.
myst_heading_anchors = 3

# Autodoc settings
autodoc_default_options = {
    "members": True,
    "member-order": "bysource",
    "special-members": "__init__",
    "undoc-members": True,
    "exclude-members": "__weakref__",
}


# Several turbodrf docstrings are plain text that isn't valid reST; autodoc
# still renders them, but docutils reports their markup. The build runs with
# -W, so skip those reports (Sphinx's own warnings still fail it).
suppress_warnings = ["docutils"]
