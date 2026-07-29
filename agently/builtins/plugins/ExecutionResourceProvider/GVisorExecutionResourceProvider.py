# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at:
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
GVisorExecutionResourceProvider — Linux gVisor (runsc) standalone sandbox backend.

Uses ``runsc`` (gVisor) directly to provide user-space kernel sandboxing.
Unlike the Docker-based approach, this provider runs runsc without Docker daemon,
using OCI bundle format for container execution.

This provider conforms to the Agently 4.1.4.2 ExecutionResourceProvider
contract: it registers under ``kind="code_execution"`` and implements
``async_probe`` / ``async_ensure`` / ``async_health_check`` / ``async_release``
/ ``async_execute_code``.

Only functional on Linux with runsc installed. On other platforms or when
runsc is unavailable, the provider reports itself as unavailable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from agently.types.data import (
    CodeExecutionBundle,
    TaskWorkspaceAccessGrant,
    TaskWorkspaceExecutionManifest,
    resolve_code_execution_workspace_uri,
)
from agently.types.data.code_execution import extract_code_toolchain_version


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def is_linux() -> bool:
    return platform.system() == "Linux"


# ---------------------------------------------------------------------------
# OCI Runtime Spec helpers
# ---------------------------------------------------------------------------

def _generate_oci_config(
    *,
    argv: list[str],
    cwd: str,
    env: dict[str, str],
    rootfs: str,
    network: bool = False,
    writable_paths: list[str] | None = None,
    readonly_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Generate OCI runtime spec (config.json) for runsc."""
    writable_paths = writable_paths or []
    readonly_paths = readonly_paths or []

    # Build mounts
    mounts: list[dict[str, Any]] = [
        {"destination": "/proc", "type": "proc", "source": "proc"},
        {"destination": "/dev", "type": "tmpfs", "source": "tmpfs", "options": ["nosuid", "strictatime", "mode=755", "size=65536k"]},
        {"destination": "/dev/pts", "type": "devpts", "source": "devpts", "options": ["nosuid", "noexec", "newinstance", "ptmxmode=0666", "mode=0620"]},
        {"destination": "/dev/shm", "type": "tmpfs", "source": "shm", "options": ["nosuid", "noexec", "nodev", "mode=1777", "size=65536k"]},
        {"destination": "/sys", "type": "sysfs", "source": "sysfs", "options": ["nosuid", "noexec", "nodev", "ro"]},
    ]

    # Build Linux mounts for workspace
    linux_mounts: list[dict[str, Any]] = []

    # Add readonly bind mounts
    for host_path in readonly_paths:
        if Path(host_path).exists():
            container_path = f"/workspace/ro/{Path(host_path).name}"
            mounts.append({
                "destination": container_path,
                "type": "bind",
                "source": host_path,
                "options": ["bind", "ro"],
            })

    # Add writable bind mounts
    for host_path in writable_paths:
        if Path(host_path).exists():
            container_path = f"/workspace/rw/{Path(host_path).name}"
            mounts.append({
                "destination": container_path,
                "type": "bind",
                "source": host_path,
                "options": ["bind", "rw"],
            })

    # Environment variables
    env_list = [f"{k}={v}" for k, v in env.items()]
    env_list.extend(["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME=/root"])

    config: dict[str, Any] = {
        "ociVersion": "1.0.2",
        "process": {
            "terminal": False,
            "user": {"uid": 0, "gid": 0},
            "args": argv,
            "env": env_list,
            "cwd": cwd if cwd.startswith("/") else f"/workspace/{cwd}",
            "capabilities": {
                "bounding": [],
                "effective": [],
                "inheritable": [],
                "permitted": [],
            },
            "rlimits": [
                {"type": "RLIMIT_NOFILE", "hard": 1024, "soft": 1024},
                {"type": "RLIMIT_NPROC", "hard": 256, "soft": 256},
            ],
            "noNewPrivileges": True,
        },
        "root": {
            "path": rootfs,
            "readonly": False,
        },
        "hostname": "gvisor-sandbox",
        "mounts": mounts,
        "linux": {
            "namespaces": [
                {"type": "pid"},
                {"type": "ipc"},
                {"type": "uts"},
                {"type": "mount"},
            ],
            "resources": {
                "memory": {"limit": 536870912},  # 512MB
                "cpu": {"quota": 100000, "period": 100000},  # 1 CPU
                "pids": {"limit": 256},
            },
            "maskedPaths": [
                "/proc/kcore", "/proc/latency_stats", "/proc/timer_list",
                "/proc/timer_stats", "/proc/sched_debug", "/sys/firmware",
            ],
            "readonlyPaths": [
                "/proc/asound", "/proc/bus", "/proc/fs", "/proc/irq",
                "/proc/sys", "/proc/sysrq-trigger",
            ],
        },
    }

    # Network namespace
    if not network:
        config["linux"]["namespaces"].append({"type": "network"})

    return config


# ---------------------------------------------------------------------------
# Availability probe
# ---------------------------------------------------------------------------

def inspect_gvisor_availability() -> dict[str, Any]:
    """Check whether gVisor (runsc) is available on the host."""
    if not is_linux():
        return {"available": False, "reason": "not_linux"}
    runsc_path = shutil.which("runsc")
    if runsc_path is None:
        return {"available": False, "reason": "runsc_binary_missing"}
    try:
        result = subprocess.run(
            ["runsc", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        version_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else "unknown"
    except Exception as error:
        return {"available": False, "reason": "runsc_version_failed", "error": str(error)}
    return {
        "available": True,
        "platform": "linux",
        "runsc_path": runsc_path,
        "version": version_line,
    }


# ---------------------------------------------------------------------------
# GVisorCodeExecutionResource
# ---------------------------------------------------------------------------

class GVisorCodeExecutionResource:
    """Execute code with gVisor (runsc) sandbox isolation.

    Uses runsc directly without Docker daemon, following OCI runtime spec.
    Follows the same bundle/manifest/grant validation pattern as
    TrustedLocalCodeExecutionResource.
    """

    def __init__(
        self,
        *,
        grant: TaskWorkspaceAccessGrant,
        max_output_bytes: int = 20000,
        network: bool = False,
        writable_paths: list[str] | None = None,
        readonly_paths: list[str] | None = None,
        runsc_binary: str = "runsc",
        rootfs_image: str | None = None,
    ) -> None:
        self.grant = grant
        self.max_output_bytes = max(1, int(max_output_bytes))
        self.network = network
        self.writable_paths = [str(p) for p in (writable_paths or [])]
        self.readonly_paths = [str(p) for p in (readonly_paths or [])]
        self.runsc_binary = runsc_binary
        self.rootfs_image = rootfs_image  # Path to rootfs tarball or directory
        self._active_executions: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._state_dir = Path(tempfile.mkdtemp(prefix="gvisor-state-"))
        self._state_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _sha256(path: Path) -> str:
        return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"

    def _validate_materialization(
        self,
        *,
        bundle: CodeExecutionBundle,
        manifest: TaskWorkspaceExecutionManifest,
        grant: TaskWorkspaceAccessGrant,
    ) -> Path:
        if grant != self.grant:
            raise PermissionError("GVisor resource is bound to another Workspace grant.")
        if (
            manifest.grant_id != grant.grant_id
            or manifest.bundle_id != bundle.bundle_id
            or manifest.bundle_digest != bundle.bundle_digest
        ):
            raise PermissionError("Code execution manifest does not match the bound bundle and grant.")
        area = Path(grant.execution_area).resolve()
        manifest_files = {Path(item.host_path).resolve(): item for item in manifest.files}
        for item in bundle.files:
            target = (area / "source" / Path(item.path)).resolve()
            if area not in target.parents or target.is_symlink() or not target.is_file():
                raise PermissionError("Materialized bundle file escaped or is unavailable.")
            recorded = manifest_files.get(target)
            if recorded is None or recorded.sha256 != item.sha256:
                raise PermissionError("Materialized bundle file is absent from the Workspace manifest.")
            if self._sha256(target) != item.sha256:
                raise PermissionError("Materialized bundle file digest changed before execution.")
        return area

    async def _prepare_rootfs(self, area: Path) -> Path:
        """Prepare rootfs for OCI bundle."""
        rootfs_dir = self._state_dir / "rootfs"
        if rootfs_dir.exists():
            return rootfs_dir

        rootfs_dir.mkdir(parents=True, exist_ok=True)

        # If rootfs_image is a directory, use it directly
        if self.rootfs_image and Path(self.rootfs_image).is_dir():
            # Create symlink or copy minimal structure
            (rootfs_dir / "usr").mkdir(exist_ok=True)
            (rootfs_dir / "bin").mkdir(exist_ok=True)
            (rootfs_dir / "lib").mkdir(exist_ok=True)
            (rootfs_dir / "lib64").mkdir(exist_ok=True)
            (rootfs_dir / "etc").mkdir(exist_ok=True)
            return rootfs_dir

        # Create minimal rootfs structure
        for d in ["bin", "sbin", "usr/bin", "usr/sbin", "usr/lib", "lib", "lib64", "etc", "tmp", "workspace"]:
            (rootfs_dir / d).mkdir(parents=True, exist_ok=True)

        # Copy essential binaries if available
        for binary in ["/usr/bin/python3", "/usr/bin/python", "/bin/sh", "/bin/bash"]:
            if Path(binary).exists():
                target_dir = rootfs_dir / Path(binary).parent
                target_dir.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(binary, target_dir / Path(binary).name)
                except Exception:
                    pass  # Skip if copy fails (permissions, etc.)

        return rootfs_dir

    async def _run_with_runsc(
        self,
        argv: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        timeout: int,
        area: Path,
    ) -> dict[str, Any]:
        """Run command in gVisor sandbox using runsc."""
        # Prepare OCI bundle
        bundle_dir = self._state_dir / f"bundle-{uuid.uuid4().hex[:8]}"
        bundle_dir.mkdir(parents=True, exist_ok=True)

        rootfs = await self._prepare_rootfs(area)

        # Copy workspace files to bundle
        workspace_dir = bundle_dir / "workspace"
        if workspace_dir.exists():
            shutil.rmtree(workspace_dir)
        shutil.copytree(area, workspace_dir, symlinks=False)

        # Generate OCI config
        oci_config = _generate_oci_config(
            argv=argv,
            cwd=cwd,
            env=env,
            rootfs=str(rootfs),
            network=self.network,
            writable_paths=[str(workspace_dir)] + self.writable_paths,
            readonly_paths=self.readonly_paths,
        )

        config_path = bundle_dir / "config.json"
        config_path.write_text(json.dumps(oci_config, indent=2))

        container_id = f"gvisor-{uuid.uuid4().hex[:12]}"

        # Run with runsc
        cmd = [
            self.runsc_binary,
            "--root", str(self._state_dir / "runsc-state"),
            "run",
            "--bundle", str(bundle_dir),
            container_id,
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(bundle_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                # Kill the container
                await asyncio.create_subprocess_exec(
                    self.runsc_binary, "--root", str(self._state_dir / "runsc-state"),
                    "kill", container_id, "SIGKILL",
                )
                await proc.wait()
                return {
                    "ok": False,
                    "status": "error",
                    "returncode": -1,
                    "stdout": "",
                    "stderr": f"execution timed out after {timeout} seconds",
                    "stdout_truncated": False,
                    "stderr_truncated": True,
                }
        except Exception as error:
            return {
                "ok": False,
                "status": "error",
                "returncode": -1,
                "stdout": "",
                "stderr": f"failed to start gVisor container: {error}",
                "stdout_truncated": False,
                "stderr_truncated": False,
            }
        finally:
            # Cleanup container
            try:
                await asyncio.create_subprocess_exec(
                    self.runsc_binary, "--root", str(self._state_dir / "runsc-state"),
                    "delete", container_id,
                )
            except Exception:
                pass

        stdout_bytes = stdout or b""
        stderr_bytes = stderr or b""

        # Truncate output
        stdout_truncated = len(stdout_bytes) > self.max_output_bytes
        stderr_truncated = len(stderr_bytes) > self.max_output_bytes
        if stdout_truncated:
            stdout_bytes = stdout_bytes[:self.max_output_bytes]
        if stderr_truncated:
            stderr_bytes = stderr_bytes[:self.max_output_bytes]

        return {
            "ok": proc.returncode == 0,
            "status": "success" if proc.returncode == 0 else "error",
            "returncode": proc.returncode,
            "stdout": stdout_bytes.decode("utf-8", errors="replace"),
            "stderr": stderr_bytes.decode("utf-8", errors="replace"),
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }

    async def _run(
        self,
        *,
        bundle: CodeExecutionBundle,
        area: Path,
        timeout: int,
    ) -> dict[str, Any]:
        logs_root = area / "logs"
        logs_root.mkdir(parents=True, exist_ok=True)
        steps = (*bundle.build_steps, bundle.run_step)
        final_stdout = ""
        final_stderr = ""
        returncode = 0
        log_refs: list[str] = []
        for index, step in enumerate(steps):
            cwd = (area / Path(step.cwd)).resolve()
            if area not in cwd.parents or not cwd.is_dir() or cwd.is_symlink():
                raise PermissionError("Execution step cwd escaped its Workspace grant.")
            stdout_path = logs_root / f"{index:02d}-{step.role}.stdout.log"
            stderr_path = logs_root / f"{index:02d}-{step.role}.stderr.log"
            environment = dict(os.environ)
            workspace_roots = {
                root.role: root.host_path
                for root in self.grant.roots
                if root.role in {"source", "build", "output", "logs"}
            }
            environment.update(
                {
                    key: resolve_code_execution_workspace_uri(
                        value,
                        roots=workspace_roots,
                    )
                    for key, value in step.env.items()
                }
            )
            result = await self._run_with_runsc(
                list(step.argv),
                cwd=str(cwd),
                env=environment,
                timeout=timeout,
                area=area,
            )
            returncode = result["returncode"]
            final_stdout = result["stdout"]
            final_stderr = result["stderr"]
            stdout_path.write_text(final_stdout, encoding="utf-8")
            stderr_path.write_text(final_stderr, encoding="utf-8")
            log_refs.extend(
                [
                    f"logs/{stdout_path.name}",
                    f"logs/{stderr_path.name}",
                ]
            )
            if returncode != 0:
                break
        outputs = [
            path
            for path in bundle.expected_outputs
            if (area / Path(path)).is_file() and not (area / Path(path)).is_symlink()
        ]
        return {
            "ok": returncode == 0,
            "status": "success" if returncode == 0 else "error",
            "returncode": returncode,
            "stdout": final_stdout,
            "stderr": final_stderr,
            "stdout_truncated": result.get("stdout_truncated", False),
            "stderr_truncated": result.get("stderr_truncated", False),
            "outputs": outputs,
            "log_refs": log_refs,
        }

    async def async_execute_code(
        self,
        *,
        bundle: CodeExecutionBundle,
        manifest: TaskWorkspaceExecutionManifest,
        grant: TaskWorkspaceAccessGrant,
        timeout: int,
    ) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("GVisor execution resource is closed.")
        area = self._validate_materialization(
            bundle=bundle,
            manifest=manifest,
            grant=grant,
        )
        task = asyncio.current_task()
        if task is not None:
            self._active_executions.add(task)
        try:
            return await self._run(
                bundle=bundle,
                area=area,
                timeout=timeout,
            )
        finally:
            if task is not None:
                self._active_executions.discard(task)

    async def async_close(self) -> None:
        self._closed = True
        current = asyncio.current_task()
        active = [task for task in self._active_executions if task is not current]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        # Cleanup state directory
        try:
            if self._state_dir.exists():
                shutil.rmtree(self._state_dir)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# GVisorExecutionResourceProvider
# ---------------------------------------------------------------------------

class GVisorExecutionResourceProvider:
    """Provider that creates gVisor (runsc) sandboxed execution resources.

    Conforms to the 4.1.4.2 ExecutionResourceProvider contract:
    - ``provider_id = "gvisor"``
    - ``supported_kinds = ("code_execution",)``
    - Implements ``async_probe`` / ``async_ensure`` / ``async_health_check``
      / ``async_release``

    Uses runsc directly without Docker daemon for maximum isolation.
    """

    name = "GVisorExecutionResourceProvider"
    provider_id = "gvisor"
    supported_kinds = ("code_execution",)

    @staticmethod
    def _on_register() -> None:
        return None

    @staticmethod
    def _on_unregister() -> None:
        return None

    @staticmethod
    def _tool_facts() -> dict[str, dict[str, Any]]:
        commands = {
            "python": ("python3", ("--version",)),
        }
        facts: dict[str, dict[str, Any]] = {}
        for language, (tool, command_args) in commands.items():
            binary = shutil.which(tool)
            fact: dict[str, Any] = {
                "tool": tool,
                "available": binary is not None,
                "binary": binary or "",
                "version": "",
                "raw_version": "",
            }
            if binary is not None:
                try:
                    completed = subprocess.run(
                        [binary, *command_args],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    raw_version = str(completed.stdout or completed.stderr).strip()[:300]
                    fact["raw_version"] = raw_version
                    fact["version"] = extract_code_toolchain_version(raw_version)
                    fact["available"] = completed.returncode == 0
                except Exception as error:
                    fact.update(available=False, error=str(error)[:300])
            facts[language] = fact
        return facts

    async def async_probe(self, *, requirement, policy):
        _ = requirement, policy
        availability = await asyncio.to_thread(inspect_gvisor_availability)
        available = bool(availability.get("available"))
        reason = str(availability.get("reason", "available")) if not available else "gvisor available"
        facts = await asyncio.to_thread(self._tool_facts) if available else {}
        languages = [language for language, fact in facts.items() if fact["available"]]
        toolchains = {
            str(fact["tool"]): {
                "available": bool(fact["available"]),
                "version": str(fact.get("version", "")),
                "raw_version": str(fact.get("raw_version", "")),
                "binary": str(fact.get("binary", "")),
            }
            for fact in facts.values()
        }
        return {
            "provider_id": self.provider_id,
            "available": available and bool(languages),
            "supported_kinds": list(self.supported_kinds),
            "capabilities": {
                "languages": languages,
                "toolchains": toolchains,
                "isolation": {
                    "process_contained": True,
                    "host_filesystem_restricted": True,
                    "privilege_escalation_blocked": True,
                    "syscalls_restricted": True,
                    "mechanism": "gvisor",
                    "version": availability.get("version", "unknown"),
                },
                "workspace_access_modes": ["snapshot", "read_only", "read_write"],
                "network": "configurable",
                "safety_class": "isolated",
            },
            "reason": reason,
            "meta": {"availability": availability, "toolchains": facts},
        }

    async def async_ensure(self, *, requirement, policy):
        from agently.core import ExecutionResourceError

        config = requirement.get("config", {})
        config = config if isinstance(config, dict) else {}
        grant = requirement.get("task_workspace_access_grant")
        if str(requirement.get("kind", "")) == "code_execution":
            if not isinstance(grant, TaskWorkspaceAccessGrant):
                raise ExecutionResourceError(
                    "GVisor code execution requires a TaskWorkspace access grant.",
                    code="execution_resource.workspace_grant_required",
                    payload={"provider_id": self.provider_id},
                )

        availability = await asyncio.to_thread(inspect_gvisor_availability)
        if not availability.get("available"):
            raise ExecutionResourceError(
                f"gVisor is not available: {availability.get('reason', 'unknown')}",
                code="execution_resource.gvisor_unavailable",
                payload={"provider_id": self.provider_id, "availability": availability},
            )

        return {
            "handle_id": f"gvisor:{uuid.uuid4().hex}",
            "resource": GVisorCodeExecutionResource(
                grant=grant,
                max_output_bytes=int(policy.get("max_output_bytes", 20000)),
                network=bool(config.get("network", False)),
                writable_paths=[str(p) for p in config.get("writable_paths", [])],
                readonly_paths=[str(p) for p in config.get("readonly_paths", [])],
                runsc_binary=str(config.get("runsc_binary", "runsc")),
                rootfs_image=config.get("rootfs_image"),
            ),
            "status": "ready",
            "meta": {
                "provider": self.name,
                "available": True,
                "platform": "linux",
                "runsc_path": availability.get("runsc_path", "runsc"),
                "grant_id": grant.grant_id if isinstance(grant, TaskWorkspaceAccessGrant) else None,
            },
        }

    async def async_health_check(self, handle):
        return "ready" if isinstance(
            handle.get("resource"), GVisorCodeExecutionResource
        ) else "unhealthy"

    async def async_release(self, handle) -> None:
        resource = handle.get("resource")
        if isinstance(resource, GVisorCodeExecutionResource):
            await resource.async_close()


__all__ = [
    "GVisorCodeExecutionResource",
    "GVisorExecutionResourceProvider",
    "inspect_gvisor_availability",
]
