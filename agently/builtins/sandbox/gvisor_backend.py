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
gVisor enhanced sandbox backend.

Extends DockerSandboxBackend by using the gVisor (runsc) OCI runtime for
system-call interception.  Provides stronger isolation than plain Docker
while retaining GPU support via nvproxy.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from .docker_backend import DockerSandboxBackend, _ContainerHandle
from .protocol import IsolationLevel, SandboxConfig, SandboxResult


class GVisorSandboxSession:
    """
    A gVisor sandbox session — identical to DockerSandboxSession but running
    inside a container backed by the ``runsc`` runtime.
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
        self.backend_name = "GVisorSandbox"
        self.isolation_level = IsolationLevel.ENHANCED_CONTAINER
        self._container_id = container_id
        self._docker_binary = docker_binary
        self._image = image
        self._config = config

    async def execute(self, command: str, *, timeout: int | None = None) -> SandboxResult:
        effective_timeout = timeout or self._config.timeout
        start = time.monotonic()

        escaped = command.replace("\\", "\\\\").replace('"', '\\"')
        shell_cmd = f'sh -c "{escaped}"'

        result = await asyncio.to_thread(
            self._run_docker_exec, shell_cmd, effective_timeout,
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
        import shlex as _shlex

        effective_timeout = timeout or self._config.timeout
        start = time.monotonic()

        wrapped_cmd = f"python -c {_shlex.quote(code)}"
        result = await asyncio.to_thread(
            self._run_docker_exec,
            f'sh -c {_shlex.quote(wrapped_cmd)}',
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
        result = await asyncio.to_thread(
            self._run_docker_exec, f'cat "{path}"', 10,
        )
        if result.get("exit_code", 1) != 0:
            raise FileNotFoundError(f"Cannot read {path}: {result.get('stderr', '')}")
        return result.get("stdout", "").encode("utf-8")

    async def write_file(self, path: str, content: bytes) -> None:
        import base64

        b64 = base64.b64encode(content).decode("ascii")
        cmd = f'echo {b64} | base64 -d > "{path}"'
        result = await asyncio.to_thread(self._run_docker_exec, cmd, 10)
        if result.get("exit_code", 1) != 0:
            raise OSError(f"Cannot write {path}: {result.get('stderr', '')}")

    async def list_files(self, path: str) -> list[dict[str, Any]]:
        cmd = f'ls -la "{path}"'
        result = await asyncio.to_thread(self._run_docker_exec, cmd, 10)
        entries: list[dict[str, Any]] = []
        for line in result.get("stdout", "").strip().splitlines():
            if line.startswith("total "):
                continue
            entries.append({"raw": line})
        return entries

    async def get_status(self) -> dict[str, Any]:
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
            "runtime": "runsc",
        }

    async def close(self) -> None:
        await asyncio.to_thread(
            self._run_docker_cmd, ["stop", "-t", "5", self._container_id], 15,
        )
        await asyncio.to_thread(
            self._run_docker_cmd, ["rm", "-f", self._container_id], 10,
        )

    # -- internal helpers (same as DockerSandboxSession) --

    def _run_docker_exec(self, cmd: str, timeout: int) -> dict[str, Any]:
        import subprocess

        args = [self._docker_binary, "exec", self._container_id, "sh", "-c", cmd]
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
            return {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
            return {"exit_code": -1, "stdout": stdout, "stderr": stderr or "Command timed out", "truncated": True}
        except Exception as exc:
            return {"exit_code": 1, "stdout": "", "stderr": str(exc)}

    def _run_docker_cmd(self, sub_args: list[str], timeout: int) -> dict[str, Any]:
        import subprocess

        args = [self._docker_binary, *sub_args]
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
            return {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
        except Exception as exc:
            return {"exit_code": 1, "stdout": "", "stderr": str(exc)}


class GVisorSandboxBackend(DockerSandboxBackend):
    """
    gVisor enhanced sandbox backend.

    Inherits container pool management from DockerSandboxBackend but overrides
    container creation to use the ``runsc`` OCI runtime for system-call
    interception.

    Key differences from DockerSandboxBackend:
    - ``--runtime=runsc`` on container creation
    - ``RUNSC_FLAGS=--platform=systrap`` for syscall interception mode
    - GPU support via nvproxy (multi-card visible, no MIG slicing)
    - ~70-80% syscall compatibility vs 100% for plain Docker
    """

    name = "GVisorSandbox"
    supported_isolation_levels = [IsolationLevel.ENHANCED_CONTAINER]

    async def create_session(self, config: SandboxConfig) -> GVisorSandboxSession:
        """Create a gVisor-backed sandbox session."""
        if self._docker_available is None:
            self._docker_available = await self._check_docker()
        if not self._docker_available:
            raise RuntimeError(
                f"Docker binary '{self._docker_binary}' is not available. "
                "Ensure Docker is installed and the daemon is running."
            )

        session_id = f"gvisor-{uuid.uuid4().hex[:12]}"

        async with self._lock:
            handle = self._find_idle_container(config.image)
            if handle is not None:
                handle.session_id = session_id
                handle.last_used = time.time()
                self._active[session_id] = handle
            else:
                if len(self._active) + len(self._pool) >= self._pool_max:
                    await self._shrink_pool()
                container_id = await self._create_gvisor_container(config, session_id)
                handle = _ContainerHandle(
                    container_id=container_id,
                    image=config.image,
                    session_id=session_id,
                )
                self._active[session_id] = handle

        return GVisorSandboxSession(
            session_id=session_id,
            container_id=handle.container_id,
            docker_binary=self._docker_binary,
            image=config.image,
            config=config,
        )

    async def health_check(self) -> dict[str, Any]:
        """Check gVisor backend health including runsc runtime availability."""
        base = await super().health_check()
        base["backend"] = self.name
        base["runsc_available"] = await self._check_runsc()
        base["healthy"] = base["healthy"] and base["runsc_available"]
        return base

    def capabilities(self) -> dict[str, Any]:
        caps = super().capabilities()
        caps.update({
            "backend": self.name,
            "isolation_levels": [lvl.value for lvl in self.supported_isolation_levels],
            "syscall_interception": True,
            "gpu_support": True,
            "gpu_mig_support": False,  # gVisor supports multi-card but NOT MIG
            "kernel_isolation": True,
        })
        return caps

    # -- gVisor-specific方法 --

    async def _create_gvisor_container(self, config: SandboxConfig, session_id: str) -> str:
        """Create a container with gVisor (runsc) runtime."""
        container_name = f"agently-gvisor-{session_id}"

        args = [
            self._docker_binary, "run", "-d",
            "--name", container_name,
            "--runtime", "runsc",
            "--label", "agently.sandbox=true",
            "--label", "agently.sandbox.runtime=runsc",
            "--label", f"agently.session={session_id}",
        ]

        # gVisor platform configuration
        args.extend(["-e", "RUNSC_FLAGS=--platform=systrap"])

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

        # GPU support via nvproxy
        if config.gpu_enabled and config.gpu_device_ids:
            device_ids = ",".join(str(i) for i in config.gpu_device_ids)
            args.extend(["--gpus", f'"device={device_ids}"'])

        # Volume mounts
        for host_path, container_path in config.mount_volumes.items():
            args.extend(["-v", f"{host_path}:{container_path}"])

        # Image and keep-alive
        args.append(config.image)
        args.extend(["sleep", "infinity"])

        result = await asyncio.to_thread(self._subprocess_run, args, 30)
        if result.get("exit_code", 1) != 0:
            stderr = result.get("stderr", "")
            if "runtime" in stderr.lower() or "runsc" in stderr.lower():
                raise RuntimeError(
                    f"gVisor runtime 'runsc' is not properly configured. "
                    f"Install gVisor and register the runsc runtime with Docker. "
                    f"Original error: {stderr}"
                )
            raise RuntimeError(f"Failed to create gVisor sandbox container: {stderr}")

        container_id = result.get("stdout", "").strip()
        if not container_id:
            raise RuntimeError("Failed to get container ID after gVisor container creation")
        return container_id[:12]

    async def _check_runsc(self) -> bool:
        """Check if gVisor runsc runtime is available."""
        def _check() -> bool:
            import subprocess

            try:
                result = subprocess.run(
                    [self._docker_binary, "info", "--format", "{{.Runtimes}}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                return "runsc" in result.stdout
            except Exception:
                return False

        return await asyncio.to_thread(_check)
