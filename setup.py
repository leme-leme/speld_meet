# Frappe v15's bench `get-app` still expects a setup.py at the repo root for
# editable installs; flit's PEP 621 metadata in pyproject.toml is the source
# of truth, this file is only the shim.
from setuptools import setup

setup()
