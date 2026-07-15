# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for Docker sandbox backend with mocked Docker CLI."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from agently.builtins.sandbox.docker_backend import (
    DockerSandboxBackend,
    DockerSandboxSession,
    _ContainerHandle,
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
    """Return a mock that simulates a successful subprocess.run."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = stdout
    mock_proc.stderr = ""
    return mock_proc


# ---------------------------------------------------------------------------
# _ContainerHandle
# ---------------------------------------------------------------------------

class TestContainerHandle:
    def test_defaults(self):
        h = _ContainerHandle(container_id="abc", image="python:3.12-slim", session_id="s1")
        assert h.container_id == "abc"
        assert h.is_healthy is True
        assert h.session_id == "s1"


# ---------------------------------------------------------------------------
# DockerSandboxSession
# ---------------------------------------------------------------------------

class TestDockerSandboxSession:
    @pytest.fixture
    def session(self):
        return DockerSandboxSession(
            session_id="test-session",
            container_id="ctr-123",
            docker_binary="docker",
            image="python:3.12-slim",
            config=_make_config(),
        )

    def test_session_attributes(self, session):
        assert session.session_id == "test-session"
        assert session.backend_name == "DockerSandbox"
        assert session.isolation_level == IsolationLevel.CONTAINER

    def test_execute_success(self, session):
        mock_proc = _mock_subprocess_ok(stdout="hello world\n")
        with patch("subprocess.run", return_value=mock_proc):
            result = asyncio.run(session.execute("echo hello"))
        assert result.exit_code == 0
        assert "hello world" in result.stdout
        assert result.sandbox_id == "ctr-123"

    def test_execute_failure(self, session):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "command not found"
        with patch("subprocess.run", return_value=mock_proc):
            result = asyncio.run(session.execute("bad_cmd"))
        assert result.exit_code == 1
        assert "command not found" in result.stderr

    def test_execute_timeout(self, session):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=1)):
            result = asyncio.run(session.execute("sleep 999", timeout=1))
        assert result.exit_code == -1
        assert result.truncated is True

    def test_execute_python(self, session):
        mock_proc = _mock_subprocess_ok(stdout="42\n")
        with patch("subprocess.run", return_value=mock_proc):
            result = asyncio.run(session.execute_python("print(42)"))
        assert result.exit_code == 0
        assert "42" in result.stdout

    def test_read_file(self, session):
        mock_proc = _mock_subprocess_ok(stdout="file content")
        with patch("subprocess.run", return_value=mock_proc):
            data = asyncio.run(session.read_file("/tmp/test.txt"))
        assert data == b"file content"

    def test_read_file_not_found(self, session):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "No such file"
        with patch("subprocess.run", return_value=mock_proc):
            with pytest.raises(FileNotFoundError):
                asyncio.run(session.read_file("/nonexistent"))

    def test_write_file(self, session):
        mock_proc = _mock_subprocess_ok(stdout="")
        with patch("subprocess.run", return_value=mock_proc):
            asyncio.run(session.write_file("/tmp/out.txt", b"data"))

    def test_write_file_error(self, session):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "Permission denied"
        with patch("subprocess.run", return_value=mock_proc):
            with pytest.raises(OSError):
                asyncio.run(session.write_file("/readonly", b"data"))

    def test_list_files(self, session):
        mock_proc = _mock_subprocess_ok(stdout="total 8\n-rw-r--r-- 1 root root 100 Jan 1 file.txt\n")
        with patch("subprocess.run", return_value=mock_proc):
            entries = asyncio.run(session.list_files("/tmp"))
        assert len(entries) >= 1

    def test_get_status(self, session):
        mock_proc = _mock_subprocess_ok(stdout="running\n")
        with patch("subprocess.run", return_value=mock_proc):
            status = asyncio.run(session.get_status())
        assert status["session_id"] == "test-session"
        assert status["backend"] == "DockerSandbox"
        assert "running" in status["status"]

    def test_close(self, session):
        mock_proc = _mock_subprocess_ok(stdout="")
        with patch("subprocess.run", return_value=mock_proc):
            asyncio.run(session.close())


# ---------------------------------------------------------------------------
# DockerSandboxBackend
# ---------------------------------------------------------------------------

class TestDockerSandboxBackend:
    def test_name(self):
        backend = DockerSandboxBackend()
        assert backend.name == "DockerSandbox"

    def test_supported_isolation_levels(self):
        backend = DockerSandboxBackend()
        assert IsolationLevel.CONTAINER in backend.supported_isolation_levels

    def test_capabilities(self):
        backend = DockerSandboxBackend()
        caps = backend.capabilities()
        assert caps["backend"] == "DockerSandbox"
        assert caps["read_only_fs"] is True
        assert caps["gpu_support"] is False
        assert caps["resource_limits"] is True

    def test_health_check_no_docker(self):
        backend = DockerSandboxBackend(docker_binary="nonexistent_docker_bin")
        result = asyncio.run(backend.health_check())
        assert result["healthy"] is False
        assert result["docker_available"] is False

    def test_list_sessions_empty(self):
        backend = DockerSandboxBackend()
        result = asyncio.run(backend.list_sessions())
        assert result == []

    def test_create_session_docker_not_available(self):
        backend = DockerSandboxBackend(docker_binary="nonexistent_docker_bin")
        with pytest.raises(RuntimeError, match="not available"):
            asyncio.run(backend.create_session(_make_config()))

    def test_create_session_success(self):
        backend = DockerSandboxBackend()
        mock_proc = _mock_subprocess_ok(stdout="abc123def45678901234567890123456")
        with patch("subprocess.run", return_value=mock_proc):
            with patch("shutil.which", return_value="/usr/bin/docker"):
                session = asyncio.run(backend.create_session(_make_config()))
        assert isinstance(session, DockerSandboxSession)
        assert session.session_id.startswith("sandbox-")

    def test_destroy_session_returns_to_pool(self):
        backend = DockerSandboxBackend(pool_min=1, pool_max=5)
        mock_proc = _mock_subprocess_ok(stdout="abc123def45678901234567890123456")
        with patch("subprocess.run", return_value=mock_proc):
            with patch("shutil.which", return_value="/usr/bin/docker"):
                session = asyncio.run(backend.create_session(_make_config()))
                sid = session.session_id
                assert len(asyncio.run(backend.list_sessions())) == 1
                asyncio.run(backend.destroy_session(sid))
                assert len(asyncio.run(backend.list_sessions())) == 0
                assert len(backend._pool) == 1

    def test_destroy_session_unknown_id(self):
        backend = DockerSandboxBackend()
        asyncio.run(backend.destroy_session("nonexistent-session"))

    def test_pool_reuse(self):
        backend = DockerSandboxBackend(pool_min=1, pool_max=5)
        mock_proc = _mock_subprocess_ok(stdout="abc123def45678901234567890123456")
        with patch("subprocess.run", return_value=mock_proc):
            with patch("shutil.which", return_value="/usr/bin/docker"):
                s1 = asyncio.run(backend.create_session(_make_config()))
                sid1 = s1.session_id
                asyncio.run(backend.destroy_session(sid1))
                assert len(backend._pool) == 1
                s2 = asyncio.run(backend.create_session(_make_config()))
                assert len(backend._pool) == 0
                assert s2.session_id != sid1

    def test_security_args_in_create(self):
        """Verify that container creation includes security hardening args."""
        backend = DockerSandboxBackend()
        captured_args = []

        def capture_subprocess(args, **kwargs):
            captured_args.append(args)
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = "abc123def45678901234567890"
            mock_proc.stderr = ""
            return mock_proc

        with patch("subprocess.run", side_effect=capture_subprocess):
            with patch("shutil.which", return_value="/usr/bin/docker"):
                asyncio.run(backend.create_session(_make_config()))

        create_call = captured_args[1]
        assert "--cap-drop" in create_call
        assert "ALL" in create_call
        assert "--read-only" in create_call
        assert "--network" in create_call
        assert "none" in create_call
        assert "--security-opt" in create_call
        assert "no-new-privileges:true" in create_call
        assert "--label" in create_call
        assert "agently.sandbox=true" in create_call
