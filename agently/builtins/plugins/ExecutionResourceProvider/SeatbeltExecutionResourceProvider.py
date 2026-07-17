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
SeatbeltExecutionResourceProvider — macOS Seatbelt sandbox backend.

Uses `sandbox-exec` with SBPL (Seatbelt Profile) to provide kernel-level
syscall filtering on macOS, as an alternative to Docker-based isolation.

This provider is only available on macOS. On other platforms it reports
itself as unavailable and the ExecutionResourceManager will refuse to
create handles of kind="seatbelt".
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agently.types.data import (
        ExecutionResourceHandle,
        ExecutionResourcePolicy,
        ExecutionResourceRequirement,
        ExecutionResourceStatus,
    )


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def is_macos() -> bool:
    """Return True if the current platform is macOS / Darwin."""
    return platform.system() == "Darwin"


def inspect_seatbelt_availability() -> dict[str, Any]:
    """Check whether Seatbelt (sandbox-exec) is available on this host."""
    if not is_macos():
        return {
            "available": False,
            "reason": "not_macos",
            "platform": platform.system(),
        }
    binary = shutil.which("sandbox-exec")
    if binary is None:
        return {
            "available": False,
            "reason": "sandbox_exec_missing",
            "platform": "Darwin",
        }
    return {
        "available": True,
        "reason": "ready",
        "platform": "Darwin",
        "binary": binary,
    }


# ---------------------------------------------------------------------------
# SBPL profile generation
# ---------------------------------------------------------------------------

# Default SBPL profile: deny everything, allow minimal reads + stdout/stderr.
_DEFAULT_SBPL_TEMPLATE = """\
(version 1)
(deny default)
(allow process-exec)
(allow signal)
(allow sysctl-read)
(allow mach-lookup
    (global-name "com.apple.system.opendirectoryd.libinfo")
    (global-name "com.apple.system.dnssd"))
(allow file-read*
    (subpath "/usr/lib")
    (subpath "/usr/share/zoneinfo")
    (subpath "/System/Library")
    (subpath "/Library/Caches")
    (literal "/dev/null")
    (literal "/dev/zero")
    (literal "/dev/random")
    (literal "/dev/urandom"))
(allow file-read*
    (subpath "{read_paths}"))
(allow file-write*
    (subpath "{write_paths}"))
(allow network-outbound
    (remote unix-socket (path-regex #"^/private/var/run/mDNSResponder$")))
(allow ipc-posix-shm-read*
    (prefix "apple."))
"""


def _build_sbpl_profile(
    *,
    network: bool = False,
    read_paths: list[str] | None = None,
    write_paths: list[str] | None = None,
    extra_rules: str = "",
) -> str:
    """Generate an SBPL profile string."""
    read_str = "\n    ".join(
        f'(subpath "{p}")' for p in (read_paths or ["/tmp"])
    )
    write_str = "\n    ".join(
        f'(subpath "{p}")' for p in (write_paths or ["/tmp"])
    )
    profile = _DEFAULT_SBPL_TEMPLATE.format(
        read_paths=read_str,
        write_paths=write_str,
    )
    if not network:
        # Remove network-outbound rule when network is disabled.
        profile = profile.replace(
            '(allow network-outbound\n    (remote unix-socket (path-regex #"^/private/var/run/mDNSResponder$")))',
            "(deny network*)",
        )
    if extra_rules:
        profile += f"\n{extra_rules}\n"
    return profile


# ---------------------------------------------------------------------------
# SeatbeltExecutionResource
# ---------------------------------------------------------------------------

class SeatbeltExecutionResource:
    """Wraps sandbox-exec for running commands inside a Seatbelt profile."""

    def __init__(
        self,
        *,
        timeout: int = 60,
        network: bool = False,
        read_paths: list[str] | None = None,
        write_paths: list[str] | None = None,
        extra_sbpl_rules: str = "",
    ):
        self.timeout = timeout
        self.network = network
        self.read_paths = read_paths or ["/tmp"]
        self.write_paths = write_paths or ["/tmp"]
        self.extra_sbpl_rules = extra_sbpl_rules
        self._sbpl_cache: str | None = None

    # -- availability -------------------------------------------------------

    @staticmethod
    def is_available() -> bool:
        return inspect_seatbelt_availability().get("available", False)

    def ensure_available(self) -> dict[str, Any]:
        info = inspect_seatbelt_availability()
        if not info.get("available"):
            from agently.core import ExecutionResourceError
            raise ExecutionResourceError(
                f"Seatbelt sandbox is unavailable: {info.get('reason', 'unknown')}. "
                "Seatbelt requires macOS with sandbox-exec installed.",
                code="execution_resource.seatbelt_unavailable",
                payload=info,
            )
        return info

    # -- SBPL ---------------------------------------------------------------

    def _get_sbpl_profile(self) -> str:
        if self._sbpl_cache is None:
            self._sbpl_cache = _build_sbpl_profile(
                network=self.network,
                read_paths=self.read_paths,
                write_paths=self.write_paths,
                extra_rules=self.extra_sbpl_rules,
            )
        return self._sbpl_cache

    # -- execution ----------------------------------------------------------

    async def run_command(
        self,
        *,
        cmd: list[str],
        env: dict[str, str] | None = None,
        workdir: str | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Run a command inside a Seatbelt sandbox."""
        self.ensure_available()
        sbpl = self._get_sbpl_profile()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".sbpl", prefix="agently-seatbelt-", delete=False
        ) as f:
            f.write(sbpl)
            sbpl_path = f.name

        try:
            args = ["sandbox-exec", "-f", sbpl_path]
            args.extend(cmd)

            run_env: dict[str, str] | None = None
            if env:
                import os
                run_env = dict(os.environ)
                run_env.update(env)

            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout,
                cwd=workdir,
                env=run_env,
            )
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else str(error.stdout or "")
            stderr = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else str(error.stderr or "")
            return {
                "ok": False,
                "status": "timed_out",
                "timeout_seconds": timeout or self.timeout,
                "stdout": stdout,
                "stderr": stderr,
            }
        finally:
            Path(sbpl_path).unlink(missing_ok=True)

        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    async def run_python_code(
        self,
        *,
        python_code: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Execute Python code inside a Seatbelt sandbox."""
        import json as _json

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", prefix="agently-sb-py-", delete=False
        ) as f:
            f.write(python_code)
            code_path = f.name

        try:
            wrapper = (
                "import json, pathlib, traceback, sys\n"
                "scope = {}\n"
                f"code = pathlib.Path({code_path!r}).read_text(encoding='utf-8')\n"
                "try:\n"
                "    exec(compile(code, 'user_code.py', 'exec'), scope, scope)\n"
                "    if 'result' in scope:\n"
                "        print('__AGENTLY_RESULT_JSON__' + json.dumps(scope['result'], ensure_ascii=False, default=str))\n"
                "except Exception:\n"
                "    traceback.print_exc()\n"
                "    raise\n"
            )
            result = await self.run_command(
                cmd=["python3", "-c", wrapper],
                timeout=timeout,
            )
        finally:
            Path(code_path).unlink(missing_ok=True)

        # Extract result JSON from stdout.
        stdout = str(result.get("stdout", ""))
        result_value = None
        visible_lines: list[str] = []
        for line in stdout.splitlines():
            if line.startswith("__AGENTLY_RESULT_JSON__"):
                try:
                    result_value = _json.loads(line.removeprefix("__AGENTLY_RESULT_JSON__"))
                except _json.JSONDecodeError:
                    result_value = line.removeprefix("__AGENTLY_RESULT_JSON__")
                continue
            visible_lines.append(line)
        result["stdout"] = "\n".join(visible_lines)
        if result_value is not None:
            result["result"] = result_value
        return result


# ---------------------------------------------------------------------------
# SeatbeltExecutionResourceProvider (plugin entry point)
# ---------------------------------------------------------------------------

class SeatbeltExecutionResourceProvider:
    """
    Plugin entry point for Seatbelt-based sandbox execution on macOS.

    Registers as kind="seatbelt" in the ExecutionResourceManager.
    Only available on macOS; on other platforms it reports unavailability.
    """

    name = "SeatbeltExecutionResourceProvider"
    DEFAULT_SETTINGS = {}
    kind = "seatbelt"

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    async def async_ensure(
        self,
        *,
        requirement: "ExecutionResourceRequirement",
        policy: "ExecutionResourcePolicy",
        existing_handle: "ExecutionResourceHandle | None" = None,
    ) -> "ExecutionResourceHandle":
        _ = existing_handle
        config = requirement.get("config", {})
        resource = SeatbeltExecutionResource(
            timeout=int(policy.get("timeout_seconds", config.get("timeout", 60))),
            network=bool(config.get("network", False)),
            read_paths=config.get("read_paths"),
            write_paths=config.get("write_paths"),
            extra_sbpl_rules=str(config.get("extra_sbpl_rules", "")),
        )
        availability = resource.ensure_available()
        return {
            "handle_id": f"seatbelt:{uuid.uuid4().hex}",
            "resource": resource,
            "status": "ready",
            "meta": {
                "provider": self.name,
                "platform": "Darwin",
                "available": True,
                "availability": availability,
            },
        }

    async def async_health_check(
        self, handle: "ExecutionResourceHandle"
    ) -> "ExecutionResourceStatus":
        resource = handle.get("resource")
        if resource is None or not hasattr(resource, "run_command"):
            return "unhealthy"
        if not resource.is_available():
            return "unhealthy"
        return "ready"

    async def async_release(self, handle: "ExecutionResourceHandle") -> None:
        _ = handle
        return None
