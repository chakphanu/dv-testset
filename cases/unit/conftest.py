# SPDX-License-Identifier: MIT
"""Everything under cases/unit is offline (loopback at most): mark unit."""
import pytest


def pytest_collection_modifyitems(items):
    for item in items:
        if "/unit/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.unit)
