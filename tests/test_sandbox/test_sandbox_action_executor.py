# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for SandboxActionExecutor plugin."""

import pytest

from agently.builtins.plugins.ActionExecutor.SandboxActionExecutor import (
    SandboxActionExecutor,
)


class TestSandboxActionExecutorProtocol:
    """Verify SandboxActionExecutor conforms to the ActionExecutor protocol."""

    def test_has_name(self):
        assert SandboxActionExecutor.name == "SandboxActionExecutor"

    def test_has_kind(self):
        executor = SandboxActionExecutor()
        assert executor.kind == "sandbox"

    def test_has_sandboxed_true(self):
        executor = SandboxActionExecutor()
        assert executor.sandboxed is True

    def test_has_default_settings(self):
        assert isinstance(SandboxActionExecutor.DEFAULT_SETTINGS, dict)
        assert "$global" in SandboxActionExecutor.DEFAULT_SETTINGS
        global_settings = SandboxActionExecutor.DEFAULT_SETTINGS["$global"]
        assert "sandbox.backend" in global_settings
        assert "sandbox.default_timeout" in global_settings
        assert "sandbox.network_enabled" in global_settings

    def test_has_on_register(self):
        assert hasattr(SandboxActionExecutor, "_on_register")

    def test_has_on_unregister(self):
        assert hasattr(SandboxActionExecutor, "_on_unregister")

    def test_has_execute_method(self):
        executor = SandboxActionExecutor()
        assert hasattr(executor, "execute")


class TestSandboxActionExecutorInit:
    def test_default_init(self):
        executor = SandboxActionExecutor()
        assert executor._backend_name == "docker"
        assert executor._default_image == "python:3.12-slim"
        assert executor._default_timeout == 30
        assert executor._network_enabled is False

    def test_custom_init(self):
        executor = SandboxActionExecutor(
            backend="gvisor",
            default_image="node:22-slim",
            default_timeout=60,
            network_enabled=True,
        )
        assert executor._backend_name == "gvisor"
        assert executor._default_image == "node:22-slim"
        assert executor._default_timeout == 60
        assert executor._network_enabled is True


class TestSandboxActionExecutorConfig:
    def test_build_config_defaults(self):
        executor = SandboxActionExecutor()
        config = executor._build_config({}, None)
        assert config.image == "python:3.12-slim"
        assert config.timeout == 30
        assert config.network_enabled is False

    def test_build_config_policy_override(self):
        executor = SandboxActionExecutor()
        config = executor._build_config(
            {"timeout_seconds": 60, "sandbox_image": "custom:latest"},
            None,
        )
        assert config.timeout == 60
        assert config.image == "custom:latest"


class TestSandboxActionExecutorBackendSelection:
    def test_get_docker_backend(self):
        executor = SandboxActionExecutor(backend="docker")
        backend = executor._get_backend()
        assert backend.name == "DockerSandbox"

    def test_get_gvisor_backend(self):
        executor = SandboxActionExecutor(backend="gvisor")
        backend = executor._get_backend()
        assert backend.name == "GVisorSandbox"

    def test_backend_is_cached(self):
        executor = SandboxActionExecutor(backend="docker")
        backend1 = executor._get_backend()
        backend2 = executor._get_backend()
        assert backend1 is backend2
