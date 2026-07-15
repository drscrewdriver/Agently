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

from typing import Any


class DockerActionExecutor:
    name = "DockerActionExecutor"
    DEFAULT_SETTINGS = {
        "$global": {
            "sandbox.docker_binary": "docker",
            "sandbox.default_timeout": 60,
            "sandbox.default_image": "python:3.12-slim",
            "sandbox.network_mode": "disabled",
            "sandbox.memory": "512m",
            "sandbox.cpus": "1",
        },
    }

    kind = "docker"
    sandboxed = True

    def __init__(
        self,
        *,
        image: str | None = None,
        timeout: int = 60,
        docker_binary: str = "docker",
        network_mode: str = "disabled",
        memory: str = "512m",
        cpus: str = "1",
    ):
        self.image = image
        self.timeout = timeout
        self.docker_binary = docker_binary
        self.network_mode = network_mode
        self.memory = memory
        self.cpus = cpus

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    async def execute(self, *, spec, action_call, policy, settings) -> Any:
        action_input = action_call.get("action_input", {})
        if not isinstance(action_input, dict):
            action_input = {}
        image = str(action_input.get("image") or self.image or "")
        command = action_input.get("cmd", action_input.get("command", []))
        if isinstance(command, str):
            cmd = command
        elif isinstance(command, list):
            cmd = [str(item) for item in command]
        else:
            cmd = str(command)
        action_id = str(spec.get("action_id", "run_docker"))
        timeout = int(policy.get("timeout_seconds", self.timeout))

        # Path 1: Use ExecutionResourceProvider if available (existing behaviour)
        environment_resources = action_call.get("execution_resource_resources", {})
        if isinstance(environment_resources, dict):
            docker_resource = environment_resources.get(action_id) or environment_resources.get("docker")
            if docker_resource is not None and hasattr(docker_resource, "run"):
                return await docker_resource.run(
                    image=image,
                    cmd=cmd,
                    workdir=action_input.get("workdir"),
                    env=action_input.get("env"),
                    timeout=timeout,
                )

        # Path 2: Fallback — directly manage Docker containers via subprocess
        from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import (
            DockerExecutionResource,
        )

        docker_binary = str(settings.get("sandbox.docker_binary", self.docker_binary)) if settings else self.docker_binary
        network_mode = str(settings.get("sandbox.network_mode", self.network_mode)) if settings else self.network_mode
        memory = str(settings.get("sandbox.memory", self.memory)) if settings else self.memory
        cpus = str(settings.get("sandbox.cpus", self.cpus)) if settings else self.cpus

        fallback_resource = DockerExecutionResource(
            docker_binary=docker_binary,
            timeout=timeout,
            runtime_profile={
                "network_mode": network_mode,
                "memory": memory,
                "cpus": cpus,
                "provisioning_profile": "strict",
            },
        )
        if not fallback_resource.is_binary_available():
            return {
                "ok": False,
                "error": (
                    f"Docker execution resource is not available and Docker "
                    f"binary '{docker_binary}' not found."
                ),
            }

        # Detect python_code input and route to run_python_code
        python_code = action_input.get("python_code")
        if python_code and not action_input.get("cmd") and not action_input.get("command"):
            return await fallback_resource.run_python_code(
                python_code=str(python_code),
                timeout=timeout,
            )

        # Default: resolve image from settings if not specified
        if not image:
            default_image = str(settings.get("sandbox.default_image", "python:3.12-slim")) if settings else "python:3.12-slim"
            image = default_image

        return await fallback_resource.run(
            image=image,
            cmd=cmd,
            workdir=action_input.get("workdir"),
            env=action_input.get("env"),
            timeout=timeout,
        )
