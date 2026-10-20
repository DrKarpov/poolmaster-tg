"""Prevent tests from loading developer or bot runtime secrets from .env."""

import os


os.environ["PYTHON_DOTENV_DISABLED"] = "1"
