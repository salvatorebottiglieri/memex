"""Unit tests for memex.resolver."""
from __future__ import annotations

import os
import subprocess
from unittest.mock import patch

import pytest

from memex.resolve.browsers import (
    Resolver,
    PiResolver,
    _CustomResolver,
    detect_resolver,
    ResolverError,
)


class TestResolverABC:
    """Resolver is an abstract base class."""

    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            Resolver()  # type: ignore[abstract]

    def test_available_is_abstract(self):
        # Verify the contract — subclass without available() fails
        with pytest.raises(TypeError):

            class Bad(Resolver):
                def resolve(self, url: str) -> str:
                    return url

            Bad()  # type: ignore[abstract]


class TestPiResolver:
    """PiResolver dispatches to the `pi` CLI."""

    def test_available_false_when_pi_not_found(self):
        with patch("shutil.which", return_value=None):
            assert PiResolver.available() is False

    def test_available_true_when_pi_found(self):
        with patch("shutil.which", return_value="/usr/local/bin/pi"):
            assert PiResolver.available() is True

    def test_resolve_returns_url_on_success(self):
        resolver = PiResolver()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["pi", "prompt"],
                returncode=0,
                stdout="https://example.com/article\n",
                stderr="",
            )
            result = resolver.resolve("https://x.com/user/123")
            assert result == "https://example.com/article"
            # Verify the prompt was constructed with the URL
            call_args = mock_run.call_args.args[0]
            assert call_args[0] == "pi"
            assert "https://x.com/user/123" in call_args[2]

    def test_resolve_raises_on_non_url_output(self):
        resolver = PiResolver()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["pi", "prompt"],
                returncode=0,
                stdout="I couldn't find anything\n",
                stderr="",
            )
            with pytest.raises(ResolverError, match="did not return a valid URL"):
                resolver.resolve("https://x.com/user/123")

    def test_resolve_raises_on_empty_output(self):
        resolver = PiResolver()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["pi", "prompt"],
                returncode=0,
                stdout="  \n",
                stderr="",
            )
            with pytest.raises(ResolverError, match="did not return a valid URL"):
                resolver.resolve("https://x.com/user/123")

    def test_resolve_raises_on_timeout(self):
        resolver = PiResolver()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pi", timeout=120)):
            with pytest.raises(ResolverError, match="timed out"):
                resolver.resolve("https://x.com/user/123")

    def test_resolve_raises_on_file_not_found(self):
        resolver = PiResolver()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(ResolverError, match="not found"):
                resolver.resolve("https://x.com/user/123")


class TestDetectResolver:
    """detect_resolver returns the first available resolver."""

    def test_detect_none_when_no_resolver(self):
        with patch("shutil.which", return_value=None), \
             patch("memex.resolve.browsers.PlaywrightResolver.available", return_value=False), \
             patch.dict(os.environ, {}, clear=True):
            r = detect_resolver()
            assert r is None

    def test_detect_pi_when_available(self):
        with patch("shutil.which", return_value="/usr/local/bin/pi"), \
             patch("memex.resolve.browsers.PlaywrightResolver.available", return_value=False), \
             patch.dict(os.environ, {}, clear=True):
            r = detect_resolver()
            assert r is not None
            assert isinstance(r, PiResolver)

    def test_detect_custom_cmd_from_env(self):
        with patch.dict(os.environ, {"MEMEX_RESOLVER_CMD": "claude run"}, clear=True):
            r = detect_resolver()
            assert r is not None
            assert isinstance(r, _CustomResolver)

    def test_custom_cmd_takes_precedence_over_pi(self):
        with patch("shutil.which", return_value="/usr/local/bin/pi"), \
             patch("memex.resolve.browsers.PlaywrightResolver.available", return_value=False), \
             patch.dict(os.environ, {"MEMEX_RESOLVER_CMD": "claude run"}, clear=True):
            r = detect_resolver()
            assert isinstance(r, _CustomResolver)

    def test_register_order_first_wins(self):
        with patch("shutil.which", side_effect=[True, False]), \
             patch("memex.resolve.browsers.PlaywrightResolver.available", return_value=False), \
             patch.dict(os.environ, {}, clear=True):
            r = detect_resolver()
            assert isinstance(r, PiResolver)


class TestResolveAgentCLI:
    """Subprocess tests for `memex resolve-agent`."""

    def test_resolve_agent_missing_url_errors(self, run_memex):
        proc = run_memex(["resolve-agent"])
        assert proc.returncode != 0
        import json
        data = json.loads(proc.stderr)
        assert "error" in data

    def test_resolve_agent_no_resolver_errors(self, run_memex):
        """Verify resolve-agent errors when no resolver is available."""
        import os
        env = {**os.environ, "MEMEX_RESOLVER_CMD": "/nonexistent/resolver"}
        proc = run_memex(["resolve-agent", "https://x.com/user/123"], env=env)
        assert proc.returncode != 0
        import json
        data = json.loads(proc.stderr)
        assert "error" in data


class TestCustomResolver:
    """Custom resolver from MEMEX_RESOLVER_CMD."""

    def test_available_always_true(self):
        assert _CustomResolver("some-cmd").available() is True

    def test_resolve_returns_url_on_success(self):
        resolver = _CustomResolver("my-resolver")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["my-resolver", "https://x.com/foo"],
                returncode=0,
                stdout="https://example.com/article\n",
                stderr="",
            )
            result = resolver.resolve("https://x.com/foo")
            assert result == "https://example.com/article"
            # URL is appended to the custom command
            assert mock_run.call_args.args[0] == ["my-resolver", "https://x.com/foo"]

    def test_resolve_raises_on_invalid_output(self):
        resolver = _CustomResolver("my-resolver")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["my-resolver", "https://x.com/foo"],
                returncode=0,
                stdout="nope\n",
                stderr="",
            )
            with pytest.raises(ResolverError, match="did not return a valid URL"):
                resolver.resolve("https://x.com/foo")

    def test_resolve_raises_on_timeout(self):
        resolver = _CustomResolver("my-resolver")
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="my-resolver", timeout=120)):
            with pytest.raises(ResolverError, match="timed out"):
                resolver.resolve("https://x.com/foo")
