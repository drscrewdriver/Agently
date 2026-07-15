# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for gVisor sandbox backend with mocked Docker CLI."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from agently.builtins.sandbox.gvisor_backend import (
    GVisorSandboxBackend,
    GVisorSandboxSession,
)
from agently.builtins.sandbox.protocol import IsolationLevel, SandboxConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> SandboxConfig:
    defaults = dict(
        image="python:3.12-slim",
        timeout=30,
        memory_limit="512m",
        cpu_limit=1.0,
    )
    defaults.update(overrides)
    return SandboxConfig(**defaults)


def _mock_subprocess_ok(stdout="abc123def456"):
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = stdout
    mock_proc.stderr = ""
    return mock_proc


# ---------------------------------------------------------------------------
# GVisorSandboxSession
# ---------------------------------------------------------------------------

class TestGVisorSandboxSession:
    @pytest.fixture
    def session(self):
        return GVisorSandboxSession(
            session_id="gvisor-session",
            container_id="ctr-gv-123",
            docker_binary="docker",
            image="python:3.12-slim",
            config=_make_config(),
        )

    def test_session_attributes(self, session):
        assert session.session_id == "gvisor-session"
        assert session.backend_name == "GVisorSandbox"
        assert session.isolation_level == IsolationLevel.ENHANCED_CONTAINER

    def test_execute_success(self, session):
        mock_proc = _mock_subprocess_ok(stdout="hello from gvisor\n")
        with patch("subprocess.run", return_value=mock_proc):
            result = asyncio.run(session.execute("echo hello"))
        assert result.exit_code == 0
        assert "hello from gvisor" in result.stdout
        assert result.sandbox_id == "ctr-gv-123"

    def test_execute_python(self, session):
        mock_proc = _mock_subprocess_ok(stdout="99\n")
        with patch("subprocess.run", return_value=mock_proc):
            result = asyncio.run(session.execute_python("print(99)"))
        assert result.exit_code == 0
        assert "99" in result.stdout

    def test_read_file(self, session):
        mock_proc = _mock_subprocess_ok(stdout="gvisor data")
        with patch("subprocess.run", return_value=mock_proc):
            data = asyncio.run(session.read_file("/tmp/test.txt"))
        assert data == b"gvisor data"

    def test_write_file(self, session):
        mock_proc = _mock_subprocess_ok(stdout="")
        with patch("subprocess.run", return_value=mock_proc):
            asyncio.run(session.write_file("/tmp/out.txt", b"gv_data"))

    def test_get_status_includes_runtime(self, session):
        mock_proc = _mock_subprocess_ok(stdout="running\n")
        with patch("subprocess.run", return_value=mock_proc):
            status = asyncio.run(session.get_status())
        assert status["backend"] == "GVisorSandbox"
        assert status["runtime"] == "runsc"

    def test_close(self, session):
        mock_proc = _mock_subprocess_ok(stdout="")
        with patch("subprocess.run", return_value=mock_proc):
            asyncio.run(session.close())


# ---------------------------------------------------------------------------
# GVisorSandboxBackend
# ---------------------------------------------------------------------------

class TestGVisorSandboxBackend:
    def test_name(self):
        backend = GVisorSandboxBackend()
        assert backend.name == "GVisorSandbox"

    def test_supported_isolation_levels(self):
        backend = GVisorSandboxBackend()
        assert IsolationLevel.ENHANCED_CONTAINER in backend.supported_isolation_levels

    def test_capabilities(self):
        backend = GVisorSandboxBackend()
        caps = backend.capabilities()
        assert caps["backend"] == "GVisorSandbox"
        assert caps["syscall_interception"] is True
        assert caps["gpu_support"] is True
        assert caps["gpu_mig_support"] is False
        assert caps["kernel_isolation"] is True

    def test_health_check_no_docker(self):
        backend = GVisorSandboxBackend(docker_binary="nonexistent_docker_bin")
        result = asyncio.run(backend.health_check())
        assert result["healthy"] is False
        assert result["backend"] == "GVisorSandbox"

    def test_health_check_no_runsc(self):
        """When docker is available but runsc is not, health should be False."""
        backend = GVisorSandboxBackend()
        call_count = [0]

        def mock_subprocess(args, **kwargs):
            call_count[0] += 1
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            # docker version check succeeds
            if "version" in args:
                mock_proc.stdout = "24.0.0"
            # docker info shows no runsc runtime
            elif "info" in args:
                mock_proc.stdout = "map[runc:]"
            else:
                mock_proc.stdout = ""
            mock_proc.stderr = ""
            return mock_proc

        with patch("subprocess.run", side_effect=mock_subprocess):
            with patch("shutil.which", return_value="/usr/bin/docker"):
                result = asyncio.run(backend.health_check())
        assert result["runsc_available"] is False
        assert result["healthy"] is False

    def test_create_session_docker_not_available(self):
        backend = GVisorSandboxBackend(docker_binary="nonexistent_docker_bin")
        with pytest.raises(RuntimeError, match="not available"):
            asyncio.run(backend.create_session(_make_config()))

    def test_create_session_success(self):
        backend = GVisorSandboxBackend()
        mock_proc = _mock_subprocess_ok(stdout="gv123def45678901234567890123456")
        with patch("subprocess.run", return_value=mock_proc):
            with patch("shutil.which", return_value="/usr/bin/docker"):
                session = asyncio.run(backend.create_session(_make_config()))
        assert isinstance(session, GVisorSandboxSession)
        assert session.session_id.startswith("gvisor-")

    def test_gvisor_runtime_arg_in_create(self):
        """Verify that container creation includes --runtime runsc."""
        backend = GVisorSandboxBackend()
        captured_args = []

        def capture_subprocess(args, **kwargs):
            captured_args.append(args)
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = "gv123def45678901234567890123456"
            mock_proc.stderr = ""
            return mock_proc

        with patch("subprocess.run", side_effect=capture_subprocess):
            with patch("shutil.which", return_value="/usr/bin/docker"):
                asyncio.run(backend.create_session(_make_config()))

        # The second call is container creation (first is docker version check)
        create_call = captured_args[1]
        assert "--runtime" in create_call
        assert "runsc" in create_call
        assert "RUNSC_FLAGS=--platform=systrap" in " ".join(create_call)

    def test_create_failure_with_runsc_error(self):
        """When runsc is not configured, should raise a helpful error."""
        backend = GVisorSandboxBackend()

        call_count = [0]

        def mock_subprocess(args, **kwargs):
            call_count[0] += 1
            mock_proc = MagicMock()
            if call_count[0] == 1:
                # docker version check succeeds
                mock_proc.returncode = 0
                mock_proc.stdout = "24.0.0"
                mock_proc.stderr = ""
            else:
                # container creation fails with runsc error
                mock_proc.returncode = 125
                mock_proc.stdout = ""
                mock_proc.stderr = "docker: Error response from daemon: runtime runsc not found"
            return mock_proc

        with patch("subprocess.run", side_effect=mock_subprocess):
            with patch("shutil.which", return_value="/usr/bin/docker"):
                with pytest.raises(RuntimeError, match="gVisor runtime 'runsc' is not properly configured"):
                    asyncio.run(backend.create_session(_make_config()))
