"""Keep the suite off any local IMPPAT download.

The cache is CC BY-NC-ND 4.0 and is not part of the test fixtures. Tests that
exercise the lookup pass a temporary directory of synthetic rows.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _imppat_disabled_unless_a_test_opts_in(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERBENZO_IMPPAT_DIR", "off")
