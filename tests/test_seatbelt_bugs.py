"""
TDD tests for SeatbeltExecutionResourceProvider — locks down BUG-1/2/3 structure.

These tests verify:
- BUG-2: SBPL mach(*) syntax must be mach*
- BUG-3: SBPL (deny network) must be (deny network-outbound)
- BUG-1: sandbox-exec must use temp file, not stdin (-f -)
- Regression: writable_paths, protected_paths, deny_read_paths, extra_rules
"""

import platform
import sys
from unittest.mock import patch, MagicMock

import pytest

from agently.builtins.plugins.ExecutionResourceProvider.SeatbeltExecutionResourceProvider import (
    SeatbeltExecutionResource,
    _build_sbpl_profile,
    _realpath,
)


# ── BUG-2: mach syntax ────────────────────────────────────────

class TestSBPLMachSyntax:
    """BUG-2: (allow mach(*)) is invalid SBPL, must be (allow mach*)"""

    def test_sbpl_mach_syntax(self):
        profile = _build_sbpl_profile()
        assert "(allow mach*)" in profile, "Profile must contain (allow mach*)"
        assert "(allow mach(*))" not in profile, "Profile must NOT contain invalid (allow mach(*))"


# ── BUG-3: network deny syntax ────────────────────────────────

class TestSBPLNetworkSyntax:
    """BUG-3: (deny network) is invalid SBPL, must be (deny network-outbound)"""

    def test_sbpl_deny_network_syntax(self):
        profile = _build_sbpl_profile(network=False)
        assert "(deny network-outbound)" in profile, "Profile must contain (deny network-outbound)"
        # Ensure bare (deny network) is NOT present (but (deny network-outbound) is OK)
        lines = profile.splitlines()
        for line in lines:
            stripped = line.strip()
            if stripped == "(deny network)":
                pytest.fail(f"Profile must NOT contain bare '(deny network)', found: {stripped}")

    def test_sbpl_allow_network_outbound(self):
        profile = _build_sbpl_profile(network=True)
        assert "(allow network-outbound)" in profile, "Profile must contain (allow network-outbound) when network=True"
        assert "(deny network)" not in profile.replace("(deny network-outbound)", ""), \
            "Profile must NOT contain bare (deny network)"


# ── BUG-1: sandbox-exec invocation ────────────────────────────

class TestSandboxExecInvocation:
    """BUG-1: sandbox-exec -f - doesn't support stdin, must use temp file"""

    @pytest.mark.skipif(platform.system() != "Darwin", reason="sandbox-exec is macOS only")
    def test_run_python_code_uses_file_not_stdin(self):
        resource = SeatbeltExecutionResource(
            timeout=10,
            network=False,
            writable_paths=["/tmp"],
        )

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "OK"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            import asyncio
            result = asyncio.run(
                resource.run_python_code(python_code="print('OK')")
            )

        # Verify subprocess.run was called
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        cmd = call_args[0][0] if call_args[0] else call_args[1].get("args", [])

        # The command should be: sandbox-exec -f <temp_file> python3 -c "print('OK')"
        assert cmd[0] == "sandbox-exec", f"First arg should be sandbox-exec, got {cmd[0]}"
        assert cmd[1] == "-f", f"Second arg should be -f, got {cmd[1]}"
        assert cmd[2] != "-", f"Third arg must NOT be '-' (stdin), got: {cmd[2]}"
        # cmd[2] should be a temp file path
        assert cmd[2].endswith(".sb"), f"Temp file should have .sb suffix, got: {cmd[2]}"

        # Verify 'input' parameter is NOT passed (no stdin)
        if "input" in call_args.kwargs:
            pytest.fail("subprocess.run should NOT receive 'input' parameter (stdin)")

    @pytest.mark.skipif(platform.system() != "Darwin", reason="sandbox-exec is macOS only")
    def test_run_python_code_cleans_up_temp_file(self):
        """Verify temp file is cleaned up even on success"""
        import os
        resource = SeatbeltExecutionResource(timeout=10)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        captured_path = None

        original_run = __import__("subprocess").run

        def capture_run(cmd, *args, **kwargs):
            nonlocal captured_path
            if cmd[0] == "sandbox-exec" and cmd[1] == "-f":
                captured_path = cmd[2]
            return mock_result

        with patch("subprocess.run", side_effect=capture_run):
            import asyncio
            asyncio.run(
                resource.run_python_code(python_code="pass")
            )

        if captured_path:
            assert not os.path.exists(captured_path), \
                f"Temp file {captured_path} should be cleaned up after execution"


# ── Regression tests ──────────────────────────────────────────

class TestSBPLRegression:
    """Regression tests for SBPL profile generation"""

    def test_sbpl_writable_paths_realpath(self):
        """writable_paths should be resolved via _realpath"""
        profile = _build_sbpl_profile(writable_paths=["/tmp/mywork"])
        real_path = _realpath("/tmp/mywork")
        expected = f'(allow file-write* (subpath "{real_path}"))'
        assert expected in profile, f"Expected '{expected}' in profile"

    def test_sbpl_protected_paths_order(self):
        """deny rules for protected_paths must appear AFTER allow rules (last-match-wins)"""
        profile = _build_sbpl_profile(
            writable_paths=["/tmp/mywork"],
            protected_paths=["/tmp/mywork/.git"],
        )
        real_work = _realpath("/tmp/mywork")
        real_git = _realpath("/tmp/mywork/.git")
        allow_line = f'(allow file-write* (subpath "{real_work}"))'
        deny_line = f'(deny file-write* (subpath "{real_git}"))'
        assert allow_line in profile
        assert deny_line in profile
        assert profile.index(allow_line) < profile.index(deny_line), \
            "deny rule must appear AFTER allow rule (last-match-wins)"

    def test_sbpl_deny_read_paths(self):
        """deny_read_paths should generate both deny read and deny write"""
        profile = _build_sbpl_profile(deny_read_paths=["/etc/secrets"])
        real_secrets = _realpath("/etc/secrets")
        assert f'(deny file-read* (subpath "{real_secrets}"))' in profile
        assert f'(deny file-write* (subpath "{real_secrets}"))' in profile

    def test_sbpl_extra_rules_appended(self):
        """extra_rules should be appended verbatim"""
        profile = _build_sbpl_profile(extra_rules="(deny iokit-open)")
        assert "(deny iokit-open)" in profile

    def test_sbpl_default_deny(self):
        """Profile should start with (deny default)"""
        profile = _build_sbpl_profile()
        assert "(deny default)" in profile

    def test_sbpl_temp_dirs_always_writable(self):
        """Temp directories should always be writable"""
        profile = _build_sbpl_profile()
        assert '(allow file-write* (subpath "/private/tmp"))' in profile
