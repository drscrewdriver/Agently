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
Docker sandbox backend.

Provides real container-based isolation using Docker CLI (subprocess).
Container pool management is inspired by graph-flow's DockerSandboxManager
(acquire/release pattern) but adapted for Agently's async-first architecture.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .protocol import IsolationLevel, SandboxConfig, SandboxResult


# ---------------------------------------------------------------------------
# Container handle — tracks a single running container
# ---------------------------------------------------------------------------

@dataclass
class _ContainerHandle:
    """Internal handle for a running sandbox container."""

    container_id: str
    image: str
    session_id: str
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    is_healthy: bool = True


# ---------------------------------------------------------------------------
# DockerSandboxSession — one isolated execution session
# ---------------------------------------------------------------------------

class DockerSandboxSession:
    """
    A single Docker sandbox session wrapping one running container.

    Given a container_id and Docker binary
    When execute() is called
    Then the command runs inside the isolated container via ``docker exec``
    """

    def __init__(
        self,
        *,
        session_id: str,
        container_id: str,
        docker_binary: str,
        image: str,
        config: SandboxConfig,
    ):
        self.session_id = session_id
        self.backend_name = "DockerSandbox"
        self.isolation_level = IsolationLevel.CONTAINER
        self._container_id = container_id
        self._docker_binary = docker_binary
        self._image = image
        self._config = config

    async def execute(self, command: str, *, timeout: int | None = None) -> SandboxResult:
        """Execute a shell command inside the container."""
        effective_timeout = timeout or self._config.timeout
        start = time.monotonic()

        escaped = command.replace("\\", "\\\\").replace('"', '\\"')
        shell_cmd = f'sh -c "{escaped}"'

        result = await asyncio.to_thread(
            self._run_docker_exec,
            shell_cmd,
            effective_timeout,
        )
        elapsed_ms = (time.monotonic() - start) * 1000

        return SandboxResult(
            exit_code=result.get("exit_code", 1),
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
            execution_time_ms=elapsed_ms,
            sandbox_id=self._container_id,
            truncated=result.get("truncated", False),
        )

    async def execute_python(self, code: str, *, timeout: int | None = None) -> SandboxResult:
        """Execute Python code inside the container."""
        effective_timeout = timeout or self._config.timeout
        start = time.monotonic()

        # Write code to a temp file inside the container and execute it
        wrapped_cmd = (
            f"python -c {shlex.quote(code)}"
        )

        result = await asyncio.to_thread(
            self._run_docker_exec,
            f'sh -c {shlex.quote(wrapped_cmd)}',
            effective_timeout,
        )
        elapsed_ms = (time.monotonic() - start) * 1000

        return SandboxResult(
            exit_code=result.get("exit_code", 1),
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
            execution_time_ms=elapsed_ms,
            sandbox_id=self._container_id,
            truncated=result.get("truncated", False),
        )

    async def read_file(self, path: str) -> bytes:
        """Read a file from the container."""
        result = await asyncio.to_thread(
            self._run_docker_exec,
            f'cat {shlex.quote(path)}',
            10,
        )
        if result.get("exit_code", 1) != 0:
            raise FileNotFoundError(f"Cannot read {path}: {result.get('stderr', '')}")
        return result.get("stdout", "").encode("utf-8")

    async def write_file(self, path: str, content: bytes) -> None:
        """Write a file to the container via docker exec."""
        import base64

        b64 = base64.b64encode(content).decode("ascii")
        cmd = f'echo {b64} | base64 -d > {shlex.quote(path)}'
        result = await asyncio.to_thread(self._run_docker_exec, cmd, 10)
        if result.get("exit_code", 1) != 0:
            raise OSError(f"Cannot write {path}: {result.get('stderr', '')}")

    async def list_files(self, path: str) -> list[dict[str, Any]]:
        """List files in a directory inside the container."""
        cmd = f'ls -la --json {shlex.quote(path)} 2>/dev/null || ls -la {shlex.quote(path)}'
        result = await asyncio.to_thread(self._run_docker_exec, cmd, 10)
        stdout = result.get("stdout", "")
        # Parse simple ls output into list of dicts
        entries: list[dict[str, Any]] = []
        for line in stdout.strip().splitlines():
            if line.startswith("total "):
                continue
            entries.append({"raw": line})
        return entries

    async def get_status(self) -> dict[str, Any]:
        """Get container status."""
        result = await asyncio.to_thread(
            self._run_docker_cmd,
            ["inspect", "--format", "{{.State.Status}}", self._container_id],
            5,
        )
        return {
            "session_id": self.session_id,
            "container_id": self._container_id,
            "status": result.get("stdout", "unknown").strip(),
            "backend": self.backend_name,
        }

    async def close(self) -> None:
        """Stop and remove the container."""
        await asyncio.to_thread(
            self._run_docker_cmd,
            ["stop", "-t", "5", self._container_id],
            15,
        )
        await asyncio.to_thread(
            self._run_docker_cmd,
            ["rm", "-f", self._container_id],
            10,
        )

    # -- internal helpers --

    def _run_docker_exec(self, cmd: str, timeout: int) -> dict[str, Any]:
        """Synchronous docker exec (runs in thread)."""
        import subprocess

        args = [
            self._docker_binary, "exec",
            self._container_id,
            "sh", "-c", cmd,
        ]
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
            return {
                "exit_code": -1,
                "stdout": stdout,
                "stderr": stderr or "Command timed out",
                "truncated": True,
            }
        except Exception as exc:
            return {
                "exit_code": 1,
                "stdout": "",
                "stderr": str(exc),
            }

    def _run_docker_cmd(self, sub_args: list[str], timeout: int) -> dict[str, Any]:
        """Synchronous docker CLI call (runs in thread)."""
        import subprocess

        args = [self._docker_binary, *sub_args]
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
        except Exception as exc:
            return {
                "exit_code": 1,
                "stdout": "",
                "stderr": str(exc),
            }


# ---------------------------------------------------------------------------
# DockerSandboxBackend — manages sessions and container pool
# ---------------------------------------------------------------------------

class DockerSandboxBackend:
    """
    Docker sandbox backend with container pool management.

    Given a SandboxConfig
    When create_session is called
    Then an existing idle container is reused from the pool, or a new one is created
    When destroy_session is called
    Then the container is returned to the pool or destroyed

    Pool strategy (inspired by graph-flow DockerSandboxManager):
    - _pool: idle containers available for reuse
    - _active: containers currently in use
    - pool_min / pool_max / idle_timeout control sizing
    """

    name = "DockerSandbox"
    supported_isolation_levels = [IsolationLevel.CONTAINER]

    def __init__(
        self,
        *,
        docker_binary: str = "docker",
        pool_min: int = 1,
        pool_max: int = 10,
        idle_timeout: int = 300,
    ):
        self._docker_binary = docker_binary
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._idle_timeout = idle_timeout

        self._pool: list[_ContainerHandle] = []
        self._active: dict[str, _ContainerHandle] = {}
        self._lock = asyncio.Lock()
        self._docker_available: bool | None = None

    # -- SandboxBackend protocol --

    async def create_session(self, config: SandboxConfig) -> DockerSandboxSession:
        """Create or reuse a sandbox session."""
        if self._docker_available is None:
            self._docker_available = await self._check_docker()
        if not self._docker_available:
            raise RuntimeError(
                f"Docker binary '{self._docker_binary}' is not available. "
                "Ensure Docker is installed and the daemon is running."
            )

        session_id = f"sandbox-{uuid.uuid4().hex[:12]}"

        async with self._lock:
            # Try to reuse an idle container from the pool
            handle = self._find_idle_container(config.image)
            if handle is not None:
                handle.session_id = session_id
                handle.last_used = time.time()
                self._active[session_id] = handle
            else:
                # Create a new container
                if len(self._active) + len(self._pool) >= self._pool_max:
                    await self._shrink_pool()
                container_id = await self._create_container(config, session_id)
                handle = _ContainerHandle(
                    container_id=container_id,
                    image=config.image,
                    session_id=session_id,
                )
                self._active[session_id] = handle

        return DockerSandboxSession(
            session_id=session_id,
            container_id=handle.container_id,
            docker_binary=self._docker_binary,
            image=config.image,
            config=config,
        )

    async def destroy_session(self, session_id: str) -> None:
        """Destroy a sandbox session — return container to pool or destroy."""
        async with self._lock:
            handle = self._active.pop(session_id, None)
            if handle is None:
                return

            handle.session_id = ""
            handle.last_used = time.time()

            if len(self._pool) < self._pool_min:
                # Return to pool for reuse
                self._pool.append(handle)
            else:
                # Pool is full, destroy the container
                await self._destroy_container(handle)

    async def list_sessions(self) -> list[str]:
        """List active session IDs."""
        async with self._lock:
            return list(self._active.keys())

    async def health_check(self) -> dict[str, Any]:
        """Check backend health."""
        available = await self._check_docker()
        async with self._lock:
            return {
                "backend": self.name,
                "healthy": available,
                "docker_available": available,
                "active_sessions": len(self._active),
                "idle_containers": len(self._pool),
                "pool_max": self._pool_max,
            }

    def capabilities(self) -> dict[str, Any]:
        """Return backend capabilities."""
        return {
            "backend": self.name,
            "isolation_levels": [lvl.value for lvl in self.supported_isolation_levels],
            "network_control": True,
            "gpu_support": False,
            "read_only_fs": True,
            "resource_limits": True,
        }

    # -- internal methods --

    async def _check_docker(self) -> bool:
        """Check if Docker is available."""
        def _check() -> bool:
            if shutil.which(self._docker_binary) is None:
                return False
            import subprocess
            try:
                result = subprocess.run(
                    [self._docker_binary, "version", "--format", "{{.Server.Version}}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                return result.returncode == 0
            except Exception:
                return False

        return await asyncio.to_thread(_check)

    def _find_idle_container(self, image: str) -> _ContainerHandle | None:
        """Find an idle container matching the requested image."""
        for handle in self._pool:
            if handle.image == image and handle.is_healthy:
                self._pool.remove(handle)
                return handle
        return None

    async def _create_container(self, config: SandboxConfig, session_id: str) -> str:
        """Create and start a new Docker container with security hardening."""
        container_name = f"agently-sandbox-{session_id}"

        args = [
            self._docker_binary, "run", "-d",
            "--name", container_name,
            "--label", "agently.sandbox=true",
            "--label", f"agently.session={session_id}",
        ]

        # Resource limits
        args.extend(["--memory", config.memory_limit])
        cpu_period = 100000
        cpu_quota = int(cpu_period * config.cpu_limit)
        args.extend(["--cpu-period", str(cpu_period), "--cpu-quota", str(cpu_quota)])

        # Security hardening
        args.extend(["--security-opt", "no-new-privileges:true"])
        if config.drop_capabilities:
            args.extend(["--cap-drop", "ALL"])
        args.extend(["--user", f"{config.run_as_user}:{config.run_as_user}"])

        # Filesystem
        if config.read_only_fs:
            args.append("--read-only")
        args.extend([
            "--tmpfs", f"/tmp:size={config.writable_tmp_size},noexec,nosuid",
        ])

        # Network
        if not config.network_enabled:
            args.extend(["--network", "none"])

        # GPU support
        if config.gpu_enabled and config.gpu_device_ids:
            device_ids = ",".join(str(i) for i in config.gpu_device_ids)
            args.extend([
                "--gpus", f'"device={device_ids}"',
            ])

        # Volume mounts
        for host_path, container_path in config.mount_volumes.items():
            args.extend(["-v", f"{host_path}:{container_path}"])

        # Image and keep-alive command
        args.append(config.image)
        args.extend(["sleep", "infinity"])

        result = await asyncio.to_thread(self._subprocess_run, args, 30)
        if result.get("exit_code", 1) != 0:
            raise RuntimeError(
                f"Failed to create sandbox container: {result.get('stderr', 'unknown error')}"
            )

        container_id = result.get("stdout", "").strip()
        if not container_id:
            raise RuntimeError("Failed to get container ID after creation")
        return container_id[:12]

    async def _destroy_container(self, handle: _ContainerHandle) -> None:
        """Stop and remove a container."""
        await asyncio.to_thread(
            self._subprocess_run,
            [self._docker_binary, "stop", "-t", "5", handle.container_id],
            15,
        )
        await asyncio.to_thread(
            self._subprocess_run,
            [self._docker_binary, "rm", "-f", handle.container_id],
            10,
        )

    async def _shrink_pool(self) -> None:
        """Remove expired idle containers from the pool."""
        now = time.time()
        expired = [
            h for h in self._pool
            if now - h.last_used > self._idle_timeout
        ]
        for handle in expired:
            if len(self._pool) > self._pool_min:
                self._pool.remove(handle)
                await self._destroy_container(handle)

    @staticmethod
    def _subprocess_run(args: list[str], timeout: int) -> dict[str, Any]:
        """Run a subprocess command synchronously."""
        import subprocess

        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
        except subprocess.TimeoutExpired:
            return {"exit_code": -1, "stdout": "", "stderr": "Command timed out"}
        except Exception as exc:
            return {"exit_code": 1, "stdout": "", "stderr": str(exc)}
