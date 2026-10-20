import os


def test_pytest_disables_local_dotenv_loading():
    assert os.environ.get("PYTHON_DOTENV_DISABLED") == "1"
