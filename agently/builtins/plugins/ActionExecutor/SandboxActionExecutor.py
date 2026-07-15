# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
SandboxActionExecutor — unified sandbox ActionExecutor plugin.

Replaces the legacy PythonSandboxActionExecutor and BashSandboxActionExecutor
with a real isolated sandbox backend (Docker / gVisor).

This executor integrates with Agently's plugin system via the ActionExecutor
protocol and provides pluggable backend selection through settings.
"""

from __future__ import annotations

import logging
from typing import Any

from agently.builtins.sandbox.protocol import IsolationLevel, SandboxConfig, SandboxResult

logger = logging.getLogger(__name__)


class SandboxActionExecutor:
    """
    Unified sandbox ActionExecutor plugin with pluggable backends.

    This replaces the fake sandbox executors (PythonSandbox, BashSandbox) with
    real container isolation.  The backend is selected via settings:

    - ``sandbox.backend``: ``"docker"`` (default) or ``"gvisor"``
    - ``sandbox.network_enabled``: ``False`` (default)
    - ``sandbox.block_cloud_metadata``: ``True`` (default)

    The executor first checks ``execution_resource_resources`` for backward
    compatibility with the existing ExecutionResourceProvider chain.  If no
    external resource is available, it falls back to its own backend management.
    """

    name = "SandboxActionExecutor"
    DEFAULT_SETTINGS = {
        "$global": {
            "sandbox.backend": "docker",
            "sandbox.default_isolation": "container",
            "sandbox.default_timeout": 30,
            "sandbox.default_memory_limit": "512m",
            "sandbox.default_image": "python:3.12-slim",
            "sandbox.network_enabled": False,
            "sandbox.block_cloud_metadata": True,
            "sandbox.docker_binary": "docker",
            "sandbox.pool_min": 1,
            "sandbox.pool_max": 10,
        },
    }

    kind = "sandbox"
    sandboxed = True  # This time it's real!

    def __init__(
        self,
        *,
        backend: str = "docker",
        docker_binary: str = "docker",
        default_image: str = "python:3.12-slim",
        default_timeout: int = 30,
        network_enabled: bool = False,
        block_cloud_metadata: bool = True,
        pool_min: int = 1,
        pool_max: int = 10,
    ):
        self._backend_name = backend
        self._docker_binary = docker_binary
        self._default_image = default_image
        self._default_timeout = default_timeout
        self._network_enabled = network_enabled
        self._block_cloud_metadata = block_cloud_metadata
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._backend: Any = None
        self._sessions: dict[str, Any] = {}

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    def _get_backend(self) -> Any:
        """Lazy-initialize the sandbox backend."""
        if self._backend is not None:
            return self._backend

        if self._backend_name == "gvisor":
            from agently.builtins.sandbox.gvisor_backend import GVisorSandboxBackend

            self._backend = GVisorSandboxBackend(
                docker_binary=self._docker_binary,
                pool_min=self._pool_min,
                pool_max=self._pool_max,
            )
        else:
            from agently.builtins.sandbox.docker_backend import DockerSandboxBackend

            self._backend = DockerSandboxBackend(
                docker_binary=self._docker_binary,
                pool_min=self._pool_min,
                pool_max=self._pool_max,
            )
        return self._backend

    def _build_config(self, policy: dict[str, Any], settings: Any) -> SandboxConfig:
        """Merge settings and policy into a SandboxConfig."""
        image = self._default_image
        timeout = self._default_timeout
        memory_limit = "512m"
        network_enabled = self._network_enabled

        if settings:
            image = str(settings.get("sandbox.default_image", image))
            timeout = int(settings.get("sandbox.default_timeout", timeout))
            memory_limit = str(settings.get("sandbox.default_memory_limit", memory_limit))
            network_enabled = bool(settings.get("sandbox.network_enabled", network_enabled))

        # Policy overrides
        if isinstance(policy, dict):
            timeout = int(policy.get("timeout_seconds", timeout))
            image = str(policy.get("sandbox_image", image))
            memory_limit = str(policy.get("sandbox_memory", memory_limit))
            if "network_enabled" in policy:
                network_enabled = bool(policy["network_enabled"])

        return SandboxConfig(
            image=image,
            timeout=timeout,
            memory_limit=memory_limit,
            network_enabled=network_enabled,
            block_cloud_metadata=self._block_cloud_metadata,
        )

    async def execute(self, *, spec, action_call, policy, settings) -> Any:
        """
        Execute an action inside a real sandbox container.

        Flow:
        1. Check execution_resource_resources (backward compat)
        2. Build SandboxConfig from settings + policy
        3. Create or reuse sandbox session
        4. Route to execute() or execute_python() based on action_input
        5. Return standardized result
        """
        action_input = action_call.get("action_input", {})
        if not isinstance(action_input, dict):
            action_input = {}

        action_id = str(spec.get("action_id", "sandbox"))
        timeout = int(policy.get("timeout_seconds", self._default_timeout)) if isinstance(policy, dict) else self._default_timeout

        # Path 1: backward-compatible ExecutionResourceProvider delegation
        environment_resources = action_call.get("execution_resource_resources", {})
        if isinstance(environment_resources, dict):
            resource = environment_resources.get(action_id) or environment_resources.get("sandbox") or environment_resources.get("docker")
            if resource is not None and hasattr(resource, "run"):
                image = str(action_input.get("image") or self._default_image)
                command = action_input.get("cmd", action_input.get("command", []))
                return await resource.run(
                    image=image,
                    cmd=command,
                    workdir=action_input.get("workdir"),
                    env=action_input.get("env"),
                    timeout=timeout,
                )

        # Path 2: native sandbox backend
        config = self._build_config(policy if isinstance(policy, dict) else {}, settings)
        config.timeout = timeout

        backend = self._get_backend()

        # Get or create session (reuse for same action_id within the agent run)
        session_key = action_id
        session = self._sessions.get(session_key)
        if session is None:
            try:
                session = await backend.create_session(config)
                self._sessions[session_key] = session
            except RuntimeError as exc:
                return {
                    "ok": False,
                    "error": str(exc),
                    "sandbox_backend": self._backend_name,
                }

        # Route based on action_input type
        python_code = action_input.get("python_code")
        if python_code and not action_input.get("cmd") and not action_input.get("command"):
            result = await session.execute_python(
                str(python_code),
                timeout=timeout,
            )
        else:
            command = action_input.get("cmd", action_input.get("command", ""))
            if isinstance(command, list):
                command = " ".join(str(item) for item in command)
            result = await session.execute(
                str(command),
                timeout=timeout,
            )

        return {
            "ok": result.success,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "execution_time_ms": result.execution_time_ms,
            "sandbox_id": result.sandbox_id,
            "sandbox_backend": self._backend_name,
            "truncated": result.truncated,
        }
