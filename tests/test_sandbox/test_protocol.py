# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for sandbox protocol definitions."""

import pytest

from agently.builtins.sandbox.protocol import (
    IsolationLevel,
    SandboxBackend,
    SandboxConfig,
    SandboxResult,
    SandboxSession,
)


class TestIsolationLevel:
    def test_enum_values(self):
        assert IsolationLevel.PROCESS == "process"
        assert IsolationLevel.CONTAINER == "container"
        assert IsolationLevel.ENHANCED_CONTAINER == "enhanced"
        assert IsolationLevel.MICROVM == "microvm"

    def test_enum_is_str(self):
        assert isinstance(IsolationLevel.CONTAINER, str)


class TestSandboxConfig:
    def test_defaults(self):
        config = SandboxConfig()
        assert config.image == "python:3.12-slim"
        assert config.isolation_level == IsolationLevel.CONTAINER
        assert config.memory_limit == "512m"
        assert config.cpu_limit == 1.0
        assert config.timeout == 30
        assert config.network_enabled is False
        assert config.block_cloud_metadata is True
        assert config.read_only_fs is True
        assert config.drop_capabilities is True
        assert config.no_new_privileges is True
        assert config.run_as_user == 1000
        assert config.gpu_enabled is False

    def test_custom_values(self):
        config = SandboxConfig(
            image="node:22-slim",
            memory_limit="1g",
            cpu_limit=2.0,
            network_enabled=True,
            network_allowlist=["api.example.com"],
        )
        assert config.image == "node:22-slim"
        assert config.memory_limit == "1g"
        assert config.cpu_limit == 2.0
        assert config.network_enabled is True
        assert config.network_allowlist == ["api.example.com"]

    def test_to_dict(self):
        config = SandboxConfig()
        d = config.to_dict()
        assert d["image"] == "python:3.12-slim"
        assert d["isolation_level"] == "container"
        assert d["network_enabled"] is False
        assert "memory_limit" in d


class TestSandboxResult:
    def test_success_result(self):
        result = SandboxResult(
            exit_code=0,
            stdout="hello world",
            stderr="",
            execution_time_ms=100.0,
            sandbox_id="abc123",
        )
        assert result.success is True
        assert result.exit_code == 0
        assert result.stdout == "hello world"

    def test_failure_result(self):
        result = SandboxResult(
            exit_code=1,
            stdout="",
            stderr="command not found",
            execution_time_ms=50.0,
            sandbox_id="abc123",
        )
        assert result.success is False

    def test_to_dict(self):
        result = SandboxResult(
            exit_code=0,
            stdout="output",
            stderr="",
            execution_time_ms=10.0,
            sandbox_id="test-id",
            truncated=True,
        )
        d = result.to_dict()
        assert d["exit_code"] == 0
        assert d["stdout"] == "output"
        assert d["truncated"] is True
        assert d["sandbox_id"] == "test-id"


class TestProtocolCompliance:
    def test_sandbox_session_is_runtime_checkable(self):
        """SandboxSession protocol should be runtime-checkable."""
        assert isinstance(SandboxSession, type)

    def test_sandbox_backend_is_runtime_checkable(self):
        """SandboxBackend protocol should be runtime-checkable."""
        assert isinstance(SandboxBackend, type)
