"""The Windows pytest ACL workaround stays explicitly scoped."""

import os
import tempfile
from pathlib import Path

import pytest

from tests.windows_sandbox_temp import inherited_mode


def test_private_mode_is_relaxed_only_beneath_selected_root():
    root = Path("C:/Temp/hive-pytest-sandbox-example").resolve(strict=False)
    assert inherited_mode(root / "run" / "case", 0o700, root) == 0o777
    assert inherited_mode(root.parent / "other", 0o700, root) == 0o700
    assert inherited_mode(root / "run", 0o755, root) == 0o755
    assert inherited_mode(root / "run", 0o700, root, dir_fd=1) == 0o700
    assert inherited_mode(b"raw-path", 0o700, root) == 0o700


def test_sandbox_tempfile_can_reopen_its_private_directory():
    if not os.environ.get("HIVE_PYTEST_SANDBOX_TEMP_ROOT"):
        pytest.skip("requires the opt-in Windows sandbox temp root")
    with tempfile.TemporaryDirectory() as folder:
        assert list(Path(folder).iterdir()) == []
