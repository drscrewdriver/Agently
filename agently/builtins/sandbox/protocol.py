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
Sandbox backend protocol definitions.

Defines the abstract interface for pluggable sandbox backends (Docker, gVisor,
containerd, etc.) that provide real isolation for Agent code execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class IsolationLevel(str, Enum):
    """Isolation level — maps to sandbox technology tiers."""

    PROCESS = "process"  # Process-level (restricted exec, legacy level)
    CONTAINER = "container"  # Container-level (Docker namespace+cgroup)
    ENHANCED_CONTAINER = "enhanced"  # Enhanced container (gVisor syscall interception)
    MICROVM = "microvm"  # Micro-VM (Firecracker independent kernel)


@dataclass
class SandboxConfig:
    """Sandbox session configuration covering five security dimensions."""

    image: str = "python:3.12-slim"
    isolation_level: IsolationLevel = IsolationLevel.CONTAINER

    # Resource limits
    memory_limit: str = "512m"
    cpu_limit: float = 1.0
    disk_limit: str = "1g"
    timeout: int = 30

    # Network policy
    network_enabled: bool = False
    network_allowlist: list[str] = field(default_factory=list)
    block_cloud_metadata: bool = True

    # Filesystem
    read_only_fs: bool = True
    writable_tmp_size: str = "100m"
    mount_volumes: dict[str, str] = field(default_factory=dict)

    # Permissions
    drop_capabilities: bool = True
    no_new_privileges: bool = True
    run_as_user: int = 1000

    # GPU (gVisor scenario)
    gpu_enabled: bool = False
    gpu_device_ids: list[int] = field(default_factory=list)

    # Container pool
    pool_min: int = 1
    pool_max: int = 10
    idle_timeout: int = 300

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "image": self.image,
            "isolation_level": self.isolation_level.value,
            "memory_limit": self.memory_limit,
            "cpu_limit": self.cpu_limit,
            "timeout": self.timeout,
            "network_enabled": self.network_enabled,
            "read_only_fs": self.read_only_fs,
            "gpu_enabled": self.gpu_enabled,
        }


@dataclass
class SandboxResult:
    """Sandbox execution result."""

    exit_code: int
    stdout: str
    stderr: str
    execution_time_ms: float
    sandbox_id: str
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        """Whether the execution succeeded."""
        return self.exit_code == 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "execution_time_ms": self.execution_time_ms,
            "sandbox_id": self.sandbox_id,
            "truncated": self.truncated,
            "metadata": self.metadata,
        }


@runtime_checkable
class SandboxSession(Protocol):
    """Sandbox session — lifecycle of one isolated execution environment."""

    session_id: str
    backend_name: str
    isolation_level: IsolationLevel

    async def execute(self, command: str, *, timeout: int | None = None) -> SandboxResult: ...
    async def execute_python(self, code: str, *, timeout: int | None = None) -> SandboxResult: ...
    async def read_file(self, path: str) -> bytes: ...
    async def write_file(self, path: str, content: bytes) -> None: ...
    async def list_files(self, path: str) -> list[dict[str, Any]]: ...
    async def get_status(self) -> dict[str, Any]: ...
    async def close(self) -> None: ...


@runtime_checkable
class SandboxBackend(Protocol):
    """Sandbox backend — manages creation and destruction of sandbox sessions."""

    name: str
    supported_isolation_levels: list[IsolationLevel]

    async def create_session(self, config: SandboxConfig) -> SandboxSession: ...
    async def destroy_session(self, session_id: str) -> None: ...
    async def list_sessions(self) -> list[str]: ...
    async def health_check(self) -> dict[str, Any]: ...
    def capabilities(self) -> dict[str, Any]: ...
