import os
import tempfile

# Must be set before any app module imports so pydantic-settings picks these up.
_TEST_DATA_DIR = tempfile.mkdtemp()
os.environ.setdefault("DATA_DIR", _TEST_DATA_DIR)
os.environ.setdefault("ADMIN_PASSWORD", "testpassword123")  # >= 12 chars, used by bootstrap_admin
