"""Coverage for kaos_tabular/serve.py — argparse + import-error paths."""

from __future__ import annotations

from io import StringIO
from unittest import mock

import pytest

from kaos_tabular import serve


def test_serve_help_exits_clean(capsys: pytest.CaptureFixture[str]) -> None:
    """``kaos-tabular-serve --help`` prints usage and exits 0."""
    with pytest.raises(SystemExit) as exc_info:
        serve.main(["--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "kaos-tabular-serve" in captured.out
    assert "--http" in captured.out
    assert "--port" in captured.out


def test_serve_missing_mcp_extra_emits_install_hint() -> None:
    """When kaos-mcp isn't installed, serve.py exits 1 with an install hint."""
    fake_stderr = StringIO()
    with (
        mock.patch.dict("sys.modules", {"kaos_mcp": None}),
        mock.patch("sys.stderr", fake_stderr),
        pytest.raises(SystemExit) as exc_info,
    ):
        serve.main([])
    assert exc_info.value.code == 1
    msg = fake_stderr.getvalue()
    assert "mcp" in msg
    assert "kaos-tabular[mcp]" in msg
