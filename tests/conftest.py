import os
import tempfile

# Must be set before any app module imports so pydantic-settings picks it up.
_TEST_DATA_DIR = tempfile.mkdtemp()
os.environ.setdefault("DATA_DIR", _TEST_DATA_DIR)
