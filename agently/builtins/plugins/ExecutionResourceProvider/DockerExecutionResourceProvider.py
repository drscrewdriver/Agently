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

import json
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from agently.types.data import (
        ExecutionResourceHandle,
        ExecutionResourcePolicy,
        ExecutionResourceRequirement,
        ExecutionResourceStatus,
    )

CODE_RUNTIME_ALIASES: dict[str, str] = {
    "py": "python",
    "python3": "python",
    "js": "nodejs",
    "javascript": "nodejs",
    "node": "nodejs",
    "node.js": "nodejs",
    "ts": "typescript",
    "c++": "cpp",
    "cxx": "cpp",
    "cc": "cpp",
    "cs": "csharp",
    "c#": "csharp",
    "dotnet": "csharp",
    "net": "csharp",
    "shell": "bash",
    "sh": "bash",
    "rscript": "r",
}

CODE_RUNTIME_PROFILES: dict[str, dict[str, str]] = {
    "python": {
        "image": "python:3.12-slim",
        "source_file": "main.py",
        "entrypoint": "sh /sandbox/run.sh",
    },
    "nodejs": {
        "image": "node:22-slim",
        "source_file": "main.js",
        "entrypoint": "sh /sandbox/run.sh",
    },
    "typescript": {
        "image": "denoland/deno:alpine",
        "source_file": "main.ts",
        "entrypoint": "sh /sandbox/run.sh",
    },
    "c": {
        "image": "gcc:14",
        "source_file": "main.c",
        "entrypoint": "sh /sandbox/run.sh",
    },
    "cpp": {
        "image": "gcc:14",
        "source_file": "main.cpp",
        "entrypoint": "sh /sandbox/run.sh",
    },
    "go": {
        "image": "golang:1",
        "source_file": "main.go",
        "entrypoint": "sh /sandbox/run.sh",
    },
    "rust": {
        "image": "rust:1",
        "source_file": "main.rs",
        "entrypoint": "sh /sandbox/run.sh",
    },
    "java": {
        "image": "maven:3-eclipse-temurin-21",
        "source_file": "Main.java",
        "entrypoint": "sh /sandbox/run.sh",
    },
    "csharp": {
        "image": "mcr.microsoft.com/dotnet/sdk:8.0",
        "source_file": "Program.cs",
        "entrypoint": "sh /sandbox/run.sh",
    },
    "php": {
        "image": "php:8.3-cli",
        "source_file": "main.php",
        "entrypoint": "sh /sandbox/run.sh",
    },
    "ruby": {
        "image": "ruby:3.3",
        "source_file": "main.rb",
        "entrypoint": "sh /sandbox/run.sh",
    },
    "perl": {
        "image": "perl:5.40",
        "source_file": "main.pl",
        "entrypoint": "sh /sandbox/run.sh",
    },
    "r": {
        "image": "r-base:4.4",
        "source_file": "main.R",
        "entrypoint": "sh /sandbox/run.sh",
    },
    "lua": {
        "image": "nickblah/lua:5.4",
        "source_file": "main.lua",
        "entrypoint": "sh /sandbox/run.sh",
    },
    "bash": {
        "image": "bash:5",
        "source_file": "main.sh",
        "entrypoint": "sh /sandbox/run.sh",
    },
}


def normalize_code_runtime_language(language: str) -> str:
    normalized = str(language or "").strip().lower().replace("-", "_")
    normalized = CODE_RUNTIME_ALIASES.get(normalized, normalized)
    if normalized not in CODE_RUNTIME_PROFILES:
        supported = ", ".join(sorted(CODE_RUNTIME_PROFILES))
        raise ValueError(f"Unsupported code runtime language '{ language }'. Supported languages: { supported }.")
    return normalized


def get_code_runtime_profile(language: str, *, image: str | None = None) -> dict[str, str]:
    canonical = normalize_code_runtime_language(language)
    profile = dict(CODE_RUNTIME_PROFILES[canonical])
    profile["language"] = canonical
    if image:
        profile["image"] = str(image)
    return profile


class DockerExecutionResource:
    # Supported runtime backends for Docker-based sandbox execution.
    # "runc" is the standard Docker runtime; "runsc" is gVisor (user-space kernel).
    SUPPORTED_RUNTIMES = ("runc", "runsc")

    def __init__(
        self,
        *,
        docker_binary: str = "docker",
        timeout: int = 60,
        default_args: list[str] | None = None,
        runtime_profile: dict[str, Any] | None = None,
        runtime: str = "runc",
    ):
        self.docker_binary = docker_binary
        self.timeout = timeout
        self.default_args = default_args or []
        self.runtime_profile = dict(runtime_profile or {})
        self.runtime = runtime if runtime in self.SUPPORTED_RUNTIMES else "runc"
        self._prepared_images: dict[str, dict[str, Any]] = {}

    def is_binary_available(self):
        return shutil.which(self.docker_binary) is not None

    def inspect_availability(self) -> dict[str, Any]:
        if not self.is_binary_available():
            return {
                "available": False,
                "reason": "binary_missing",
                "docker_binary": self.docker_binary,
            }
        try:
            result = subprocess.run(
                [self.docker_binary, "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                text=True,
                timeout=min(max(self.timeout, 1), 10),
            )
        except Exception as error:
            return {
                "available": False,
                "reason": "daemon_unavailable",
                "docker_binary": self.docker_binary,
                "error": str(error),
            }
        if result.returncode != 0:
            return {
                "available": False,
                "reason": "daemon_unavailable",
                "docker_binary": self.docker_binary,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        return {
            "available": True,
            "reason": "ready",
            "docker_binary": self.docker_binary,
            "server_version": result.stdout.strip(),
        }

    def is_available(self):
        return bool(self.inspect_availability().get("available", False))

    def ensure_available(self) -> dict[str, Any]:
        availability = self.inspect_availability()
        if availability.get("available"):
            return availability
        from agently.core import ExecutionResourceError

        reason = str(availability.get("reason", "docker_unavailable"))
        message = (
            f"Docker execution resource is unavailable: { reason }. "
            f"Check Docker binary '{ self.docker_binary }' and local Docker daemon status."
        )
        raise ExecutionResourceError(
            message,
            code="execution_resource.docker_unavailable",
            payload=availability,
        )

    @staticmethod
    def _normalize_dependency_policy(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            mode = str(value.get("mode", "deny")).strip().lower() or "deny"
            normalized = dict(value)
            normalized["mode"] = mode
            return normalized
        mode = str(value or "deny").strip().lower() or "deny"
        return {"mode": mode}

    @staticmethod
    def _normalize_provisioning_profile(value: Any) -> str:
        mode = str(value or "strict").strip().lower().replace("-", "_") or "strict"
        if mode not in {"strict", "developer", "ci"}:
            raise ValueError("provisioning_profile must be one of: 'strict', 'developer', 'ci'.")
        return mode

    @staticmethod
    def _normalize_image_pull_policy(value: Any, *, provisioning_profile: str) -> str:
        if value is None or str(value).strip() == "":
            return "if_missing" if provisioning_profile in {"developer", "ci"} else "never"
        mode = str(value).strip().lower().replace("-", "_")
        if mode not in {"never", "request", "if_missing", "always"}:
            raise ValueError("image_pull_policy must be one of: 'never', 'request', 'if_missing', 'always'.")
        return mode

    def _profile(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        profile = dict(self.runtime_profile)
        if overrides:
            profile.update(overrides)
        provisioning_profile = self._normalize_provisioning_profile(profile.get("provisioning_profile", "strict"))
        profile["provisioning_profile"] = provisioning_profile
        if "dependency_policy" in profile:
            dependency_policy = self._normalize_dependency_policy(profile.get("dependency_policy", "deny"))
        else:
            dependency_policy = {"mode": "install"} if provisioning_profile in {"developer", "ci"} else {"mode": "deny"}
        profile["dependency_policy"] = dependency_policy
        profile["image_pull_policy"] = self._normalize_image_pull_policy(
            profile.get("image_pull_policy", None),
            provisioning_profile=provisioning_profile,
        )
        if "network_mode" not in profile:
            profile["network_mode"] = (
                "bridge"
                if provisioning_profile in {"developer", "ci"} and dependency_policy.get("mode") == "install"
                else "disabled"
            )
        profile.setdefault("cpus", "1")
        profile.setdefault("memory", "512m")
        return profile

    @staticmethod
    def _default_image(language: str) -> str:
        if language == "nodejs":
            return "node:22-slim"
        if language in CODE_RUNTIME_PROFILES:
            return CODE_RUNTIME_PROFILES[language]["image"]
        return "python:3.12-slim"

    @staticmethod
    def _safe_relative_path(relative_path: str) -> Path:
        raw = str(relative_path).strip().replace("\\", "/")
        if not raw or raw.startswith("/") or raw.startswith("../") or "/../" in raw or raw == "..":
            raise ValueError(f"Unsafe sandbox file path: { relative_path }")
        path = Path(raw)
        if any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError(f"Unsafe sandbox file path: { relative_path }")
        return path

    def inspect_image(self, image: str) -> dict[str, Any]:
        result = subprocess.run(
            [self.docker_binary, "image", "inspect", image, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            timeout=min(max(self.timeout, 1), 30),
        )
        return {
            "exists": result.returncode == 0,
            "image": image,
            "image_id": result.stdout.strip() if result.returncode == 0 else "",
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    def pull_image(self, image: str, *, timeout: int | None = None) -> dict[str, Any]:
        try:
            result = subprocess.run(
                [self.docker_binary, "pull", image],
                capture_output=True,
                text=True,
                timeout=timeout or max(self.timeout, 300),
            )
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else str(error.stdout or "")
            stderr = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else str(error.stderr or "")
            return {
                "ok": False,
                "status": "timed_out",
                "image": image,
                "timeout_seconds": timeout or max(self.timeout, 300),
                "stdout": stdout,
                "stderr": stderr,
            }
        return {
            "ok": result.returncode == 0,
            "image": image,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    def ensure_image_ready(self, image: str, *, profile: dict[str, Any] | None = None) -> dict[str, Any]:
        active_profile = self._profile(profile)
        image_pull_policy = str(active_profile.get("image_pull_policy", "never"))
        cache_key = f"{ image }::{ image_pull_policy }"
        if image_pull_policy != "always" and cache_key in self._prepared_images:
            return self._prepared_images[cache_key]
        from agently.core import ExecutionResourceError

        first_inspect = self.inspect_image(image)
        if first_inspect.get("exists") and image_pull_policy != "always":
            prepared = {
                "status": "local",
                "image": image,
                "image_id": first_inspect.get("image_id", ""),
                "image_pull_policy": image_pull_policy,
            }
            self._prepared_images[cache_key] = prepared
            return prepared
        if image_pull_policy == "never":
            raise ExecutionResourceError(
                f"Docker image '{ image }' is not available locally and image_pull_policy='never'.",
                code="execution_resource.docker_image_missing",
                payload={
                    "image": image,
                    "image_pull_policy": image_pull_policy,
                    "inspect": first_inspect,
                },
            )
        if image_pull_policy == "request":
            raise ExecutionResourceError(
                f"Docker image '{ image }' requires approval before pulling.",
                code="execution_resource.docker_image_pull_approval_required",
                payload={
                    "image": image,
                    "image_pull_policy": image_pull_policy,
                    "inspect": first_inspect,
                },
            )
        pull_timeout = int(active_profile.get("image_pull_timeout_seconds", 300) or 300)
        pull = self.pull_image(image, timeout=pull_timeout)
        if not pull.get("ok"):
            raise ExecutionResourceError(
                f"Docker image pull failed for '{ image }'.",
                code="execution_resource.docker_image_pull_failed",
                payload={
                    "image": image,
                    "image_pull_policy": image_pull_policy,
                    "pull": pull,
                    "inspect": first_inspect,
                },
            )
        second_inspect = self.inspect_image(image)
        if not second_inspect.get("exists"):
            raise ExecutionResourceError(
                f"Docker image '{ image }' was pulled but could not be inspected.",
                code="execution_resource.docker_image_pull_failed",
                payload={
                    "image": image,
                    "image_pull_policy": image_pull_policy,
                    "pull": pull,
                    "inspect": second_inspect,
                },
            )
        prepared = {
            "status": "pulled" if not first_inspect.get("exists") else "refreshed",
            "image": image,
            "image_id": second_inspect.get("image_id", ""),
            "image_pull_policy": image_pull_policy,
            "pull": pull,
        }
        self._prepared_images[cache_key] = prepared
        return prepared

    @staticmethod
    def _normalize_cmd(cmd: str | Sequence[str]) -> list[str]:
        if isinstance(cmd, str):
            return shlex.split(cmd)
        return [str(item) for item in cmd]

    @staticmethod
    def _cmd_allowed(args: list[str], allowed_cmd_prefixes: Sequence[str] | None) -> bool:
        if not args:
            return False
        allowed = list(allowed_cmd_prefixes or [])
        if not allowed:
            return False
        base = Path(args[0]).name
        for raw_prefix in allowed:
            prefix = shlex.split(str(raw_prefix))
            if not prefix:
                continue
            if len(prefix) == 1:
                if base == prefix[0] or args[0] == prefix[0]:
                    return True
                continue
            if len(args) < len(prefix):
                continue
            first_matches = base == prefix[0] or args[0] == prefix[0]
            if first_matches and args[1 : len(prefix)] == prefix[1:]:
                return True
        return False

    @staticmethod
    def _resolve_workdir(
        workdir: str | Path | None,
        allowed_workdir_roots: Sequence[str | Path] | None,
    ) -> tuple[Path | None, Path | None, str | None]:
        roots = [Path(root).expanduser().resolve() for root in (allowed_workdir_roots or [])]
        if workdir is not None:
            workdir_path = Path(workdir).expanduser().resolve()
        elif roots:
            workdir_path = roots[0]
        else:
            return None, None, None
        for root in roots:
            try:
                relative = workdir_path.relative_to(root)
            except ValueError:
                continue
            container_workdir = "/workspace" if str(relative) == "." else f"/workspace/{ relative.as_posix() }"
            return workdir_path, root, container_workdir
        return workdir_path, None, None

    def _container_base_args(
        self,
        *,
        profile: dict[str, Any],
        workdir: str = "/sandbox",
        extra_mounts: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        args = [self.docker_binary, "run", "--rm", *self.default_args]
        # Inject --runtime flag when using a non-default runtime (e.g. gVisor/runsc).
        if self.runtime != "runc" and f"--runtime" not in " ".join(self.default_args):
            args.extend(["--runtime", self.runtime])
        network_mode = str(profile.get("network_mode", "disabled"))
        if network_mode == "disabled":
            args.extend(["--network", "none"])
        elif network_mode and network_mode not in {"inherit", "enabled"}:
            args.extend(["--network", network_mode])
        cpus = profile.get("cpus", "1")
        memory = profile.get("memory", "512m")
        if cpus is not None:
            args.extend(["--cpus", str(cpus)])
        if memory is not None:
            args.extend(["--memory", str(memory)])
        for mount in extra_mounts or []:
            args.extend(["-v", mount])
        args.extend(["-w", workdir])
        if isinstance(env, dict):
            for key, value in env.items():
                args.extend(["-e", f"{ key }={ value }"])
        return args

    async def _run_container(
        self,
        *,
        image: str,
        cmd: list[str],
        files: dict[str, str] | None = None,
        profile: dict[str, Any] | None = None,
        workdir: str = "/sandbox",
        env: dict[str, str] | None = None,
        timeout: int | None = None,
        extra_mounts: list[str] | None = None,
    ) -> dict[str, Any]:
        if not image:
            return {"ok": False, "error": "Docker image is required."}
        if not self.is_binary_available():
            return {"ok": False, "error": f"Docker binary not found: { self.docker_binary }"}
        active_profile = self._profile(profile)
        self.ensure_image_ready(image, profile=active_profile)
        dependency_policy = self._normalize_dependency_policy(active_profile.get("dependency_policy", "deny"))
        if dependency_policy.get("mode") == "request":
            return {
                "ok": False,
                "status": "approval_required",
                "reason": "dependency_policy_requires_prepare_approval",
                "diagnostics": [{"code": "docker_runtime.dependency_policy_requires_prepare_approval"}],
            }
        with tempfile.TemporaryDirectory(prefix="agently-docker-runtime-") as temp_dir:
            temp_path = Path(temp_dir)
            for relative_path, content in (files or {}).items():
                target = temp_path / self._safe_relative_path(relative_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            mounts = [f"{ temp_path }:/sandbox:ro", *(extra_mounts or [])]
            args = self._container_base_args(
                profile=active_profile,
                workdir=workdir,
                extra_mounts=mounts,
                env=env,
            )
            args.append(image)
            args.extend(cmd)
            try:
                result = subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=timeout or self.timeout,
                )
            except subprocess.TimeoutExpired as error:
                stdout = error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else str(error.stdout or "")
                stderr = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else str(error.stderr or "")
                return {
                    "ok": False,
                    "status": "timed_out",
                    "reason": "container_timeout",
                    "timeout_seconds": timeout or self.timeout,
                    "stdout": stdout,
                    "stderr": stderr,
                    "diagnostics": [
                        {
                            "code": "docker_runtime.container_timeout",
                            "timeout_seconds": timeout or self.timeout,
                        }
                    ],
                }
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "diagnostics": [],
        }

    @staticmethod
    def inspect_gvisor_availability() -> dict[str, Any]:
        """Check whether gVisor (runsc) is available on the host."""
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
        # Also verify Docker daemon recognises the runsc runtime.
        try:
            probe = subprocess.run(
                ["docker", "info", "--format", "{{json .Runtimes}}"],
                capture_output=True, text=True, timeout=5,
            )
            runsc_in_daemon = "runsc" in (probe.stdout or "")
        except Exception:
            runsc_in_daemon = False
        return {
            "available": runsc_in_daemon,
            "runsc_path": runsc_path,
            "runsc_version": version_line,
            "registered_in_docker": runsc_in_daemon,
        }

    async def run_python_code(self, *, python_code: str, timeout: int | None = None) -> dict[str, Any]:
        profile = self._profile({"language": "python"})
        image = str(profile.get("image") or self._default_image("python"))
        wrapper = (
            "import json\n"
            "import pathlib\n"
            "import traceback\n"
            "scope = {}\n"
            "code = pathlib.Path('/sandbox/user_code.py').read_text(encoding='utf-8')\n"
            "try:\n"
            "    exec(compile(code, '/sandbox/user_code.py', 'exec'), scope, scope)\n"
            "    if 'result' in scope:\n"
            "        print('__AGENTLY_RESULT_JSON__' + json.dumps(scope['result'], ensure_ascii=False, default=str))\n"
            "except Exception:\n"
            "    traceback.print_exc()\n"
            "    raise\n"
        )
        output = await self._run_container(
            image=image,
            cmd=["python", "/sandbox/main.py"],
            files={"main.py": wrapper, "user_code.py": str(python_code)},
            profile=profile,
            timeout=timeout,
        )
        stdout = str(output.get("stdout", ""))
        result_value = None
        visible_lines: list[str] = []
        for line in stdout.splitlines():
            if line.startswith("__AGENTLY_RESULT_JSON__"):
                try:
                    result_value = json.loads(line.removeprefix("__AGENTLY_RESULT_JSON__"))
                except json.JSONDecodeError:
                    result_value = line.removeprefix("__AGENTLY_RESULT_JSON__")
                continue
            visible_lines.append(line)
        output["stdout"] = "\n".join(visible_lines)
        if stdout.endswith("\n") and output["stdout"]:
            output["stdout"] = f"{ output['stdout'] }\n"
        if result_value is not None:
            output["result"] = result_value
        return output

    async def run_nodejs_code(
        self,
        *,
        js_code: str,
        args: list[str] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        profile = self._profile({"language": "nodejs"})
        image = str(profile.get("image") or self._default_image("nodejs"))
        return await self._run_container(
            image=image,
            cmd=["node", "/sandbox/main.js", *[str(arg) for arg in (args or [])]],
            files={"main.js": str(js_code)},
            profile=profile,
            timeout=timeout,
        )

    async def run_shell_command(
        self,
        *,
        cmd: str | Sequence[str],
        workdir: str | Path | None = None,
        allow_unsafe: bool = False,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        profile = self._profile({"language": "shell"})
        args = self._normalize_cmd(cmd)
        allowed_cmd_prefixes = profile.get("allowed_cmd_prefixes")
        allowed_roots = profile.get("allowed_workdir_roots")
        workdir_path, root, container_workdir = self._resolve_workdir(workdir, allowed_roots)
        if workdir_path is None:
            return {
                "ok": False,
                "status": "blocked",
                "need_approval": True,
                "reason": "workspace_boundary_required",
                "diagnostics": [{"code": "shell.workspace_boundary_required"}],
            }
        if root is None or container_workdir is None:
            return {
                "ok": False,
                "status": "blocked",
                "need_approval": True,
                "reason": "workdir_not_allowed",
                "workdir": str(workdir_path),
                "diagnostics": [{"code": "shell.workdir_not_allowed", "workdir": str(workdir_path)}],
            }
        if not allow_unsafe and not self._cmd_allowed(args, allowed_cmd_prefixes):
            return {
                "ok": False,
                "status": "blocked",
                "need_approval": True,
                "reason": "cmd_not_allowed",
                "cmd": args,
                "diagnostics": [{"code": "shell.cmd_not_allowed", "cmd": args}],
            }
        image = str(profile.get("image") or self._default_image("shell"))
        return await self._run_container(
            image=image,
            cmd=args,
            profile=profile,
            workdir=container_workdir,
            timeout=timeout,
            extra_mounts=[f"{ root }:/workspace"],
            env=profile.get("env") if isinstance(profile.get("env"), dict) else None,
        )

    @staticmethod
    def _code_runtime_script(language: str) -> str:
        scripts = {
            "python": (
                "set -e\n"
                "if [ -f /sandbox/requirements.txt ] && [ \"${AGENTLY_DEPENDENCY_POLICY}\" = \"install\" ]; then\n"
                "  python -m pip install --disable-pip-version-check --target /tmp/agently-python-packages -r /sandbox/requirements.txt\n"
                "  export PYTHONPATH=\"/tmp/agently-python-packages${PYTHONPATH:+:$PYTHONPATH}\"\n"
                "fi\n"
                "exec python /sandbox/main.py \"$@\"\n"
            ),
            "nodejs": (
                "set -e\n"
                "if [ -f /sandbox/package.json ] && [ \"${AGENTLY_DEPENDENCY_POLICY}\" = \"install\" ]; then\n"
                "  mkdir -p /tmp/agently-node\n"
                "  cp /sandbox/package.json /tmp/agently-node/package.json\n"
                "  if [ -f /sandbox/package-lock.json ]; then cp /sandbox/package-lock.json /tmp/agently-node/package-lock.json; fi\n"
                "  cd /tmp/agently-node\n"
                "  if [ -f package-lock.json ]; then npm ci --omit=dev; else npm install --omit=dev; fi\n"
                "  export NODE_PATH=/tmp/agently-node/node_modules\n"
                "fi\n"
                "cd /sandbox\n"
                "exec node /sandbox/main.js \"$@\"\n"
            ),
            "typescript": (
                "set -e\n"
                "export DENO_DIR=/tmp/agently-deno\n"
                "exec deno run --quiet /sandbox/main.ts \"$@\"\n"
            ),
            "c": (
                "set -e\n"
                "cc /sandbox/main.c -o /tmp/agently-code-runtime-app\n"
                "exec /tmp/agently-code-runtime-app \"$@\"\n"
            ),
            "cpp": (
                "set -e\n"
                "c++ /sandbox/main.cpp -o /tmp/agently-code-runtime-app\n"
                "exec /tmp/agently-code-runtime-app \"$@\"\n"
            ),
            "go": (
                "set -e\n"
                "export GOCACHE=/tmp/go-build\n"
                "export GOMODCACHE=/tmp/go-mod\n"
                "cd /sandbox\n"
                "if [ -f go.mod ]; then go mod download; fi\n"
                "go build -o /tmp/agently-code-runtime-app ./main.go\n"
                "exec /tmp/agently-code-runtime-app \"$@\"\n"
            ),
            "rust": (
                "set -e\n"
                "if [ -f /sandbox/Cargo.toml ]; then\n"
                "  mkdir -p /tmp/agently-rust\n"
                "  cp -R /sandbox/. /tmp/agently-rust/\n"
                "  cd /tmp/agently-rust\n"
                "  exec cargo run --quiet -- \"$@\"\n"
                "fi\n"
                "rustc /sandbox/main.rs -o /tmp/agently-code-runtime-app\n"
                "exec /tmp/agently-code-runtime-app \"$@\"\n"
            ),
            "java": (
                "set -e\n"
                "mkdir -p /tmp/agently-java\n"
                "javac -d /tmp/agently-java /sandbox/Main.java\n"
                "exec java -cp /tmp/agently-java Main \"$@\"\n"
            ),
            "csharp": (
                "set -e\n"
                "mkdir -p /tmp/agently-dotnet\n"
                "if find /sandbox -maxdepth 1 -name '*.csproj' | grep -q .; then\n"
                "  cp -R /sandbox/. /tmp/agently-dotnet/\n"
                "else\n"
                "  dotnet new console --output /tmp/agently-dotnet --force >/dev/null\n"
                "  cp /sandbox/Program.cs /tmp/agently-dotnet/Program.cs\n"
                "fi\n"
                "cd /tmp/agently-dotnet\n"
                "exec dotnet run -- \"$@\"\n"
            ),
            "php": "set -e\nexec php /sandbox/main.php \"$@\"\n",
            "ruby": (
                "set -e\n"
                "if [ -f /sandbox/Gemfile ] && [ \"${AGENTLY_DEPENDENCY_POLICY}\" = \"install\" ]; then\n"
                "  mkdir -p /tmp/agently-ruby\n"
                "  cp /sandbox/Gemfile /tmp/agently-ruby/Gemfile\n"
                "  if [ -f /sandbox/Gemfile.lock ]; then cp /sandbox/Gemfile.lock /tmp/agently-ruby/Gemfile.lock; fi\n"
                "  cd /tmp/agently-ruby\n"
                "  bundle install --path vendor/bundle\n"
                "  export BUNDLE_GEMFILE=/tmp/agently-ruby/Gemfile\n"
                "fi\n"
                "cd /sandbox\n"
                "exec ruby /sandbox/main.rb \"$@\"\n"
            ),
            "perl": "set -e\nexec perl /sandbox/main.pl \"$@\"\n",
            "r": "set -e\nexec Rscript /sandbox/main.R \"$@\"\n",
            "lua": "set -e\nexec lua /sandbox/main.lua \"$@\"\n",
            "bash": "set -e\nexec bash /sandbox/main.sh \"$@\"\n",
        }
        return scripts[language]

    def _code_runtime_files(
        self,
        *,
        language: str,
        source_code: str,
        files: dict[str, str] | None,
        profile: dict[str, Any],
    ) -> dict[str, str]:
        runtime_files = {str(key): str(value) for key, value in (files or {}).items()}
        source_file = str(profile.get("source_file") or get_code_runtime_profile(language)["source_file"])
        if source_code or source_file not in runtime_files:
            runtime_files[source_file] = str(source_code)
        runtime_files["run.sh"] = self._code_runtime_script(language)
        return runtime_files

    async def run_code(
        self,
        *,
        language: str,
        source_code: str,
        files: dict[str, str] | None = None,
        args: list[str] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        canonical_language = normalize_code_runtime_language(language)
        catalog_profile = get_code_runtime_profile(canonical_language)
        profile = self._profile({
            "language": canonical_language,
            "source_file": catalog_profile["source_file"],
            "entrypoint": catalog_profile["entrypoint"],
        })
        image = str(profile.get("image") or catalog_profile["image"])
        dependency_policy = self._normalize_dependency_policy(profile.get("dependency_policy", "deny"))
        return await self._run_container(
            image=image,
            cmd=["sh", "/sandbox/run.sh", *[str(arg) for arg in (args or [])]],
            files=self._code_runtime_files(
                language=canonical_language,
                source_code=source_code,
                files=files,
                profile=profile,
            ),
            profile=profile,
            timeout=timeout,
            env={
                "AGENTLY_DEPENDENCY_POLICY": str(dependency_policy.get("mode", "deny")),
            },
        )

    async def run(
        self,
        *,
        image: str,
        cmd: str | list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ):
        if not image:
            return {"ok": False, "error": "Docker image is required."}
        if not self.is_binary_available():
            return {"ok": False, "error": f"Docker binary not found: { self.docker_binary }"}
        args = [self.docker_binary, "run", "--rm", *self.default_args]
        if workdir:
            args.extend(["-w", str(workdir)])
        if isinstance(env, dict):
            for key, value in env.items():
                args.extend(["-e", f"{ key }={ value }"])
        args.append(image)
        if isinstance(cmd, str):
            args.extend(shlex.split(cmd))
        else:
            args.extend([str(item) for item in cmd])
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout or self.timeout,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }


class DockerExecutionResourceProvider:
    name = "DockerExecutionResourceProvider"
    DEFAULT_SETTINGS = {}
    kind = "docker"

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
        default_args = config.get("default_args", [])
        if not isinstance(default_args, list):
            default_args = []
        runtime_profile = config.get("runtime_profile", {})
        if not isinstance(runtime_profile, dict):
            runtime_profile = {}
        runtime = str(config.get("runtime", "runc")).strip().lower()
        if runtime not in DockerExecutionResource.SUPPORTED_RUNTIMES:
            runtime = "runc"
        resource = DockerExecutionResource(
            docker_binary=str(config.get("docker_binary", "docker")),
            timeout=int(policy.get("timeout_seconds", config.get("timeout", 60))),
            default_args=[str(item) for item in default_args],
            runtime_profile=runtime_profile,
            runtime=runtime,
        )
        availability = resource.ensure_available()
        active_profile = resource._profile()
        image_preparation: dict[str, Any] | None = None
        image = str(active_profile.get("image", ""))
        if image:
            image_preparation = resource.ensure_image_ready(image, profile=active_profile)
        gvisor_info = {}
        if resource.runtime == "runsc":
            gvisor_info = resource.inspect_gvisor_availability()
        return {
            "handle_id": f"docker:{ uuid.uuid4().hex }",
            "resource": resource,
            "status": "ready",
            "meta": {
                "provider": self.name,
                "docker_binary": resource.docker_binary,
                "runtime": resource.runtime,
                "available": True,
                "availability": availability,
                "runtime_profile": active_profile,
                "image_preparation": image_preparation,
                **({"gvisor": gvisor_info} if gvisor_info else {}),
            },
        }

    async def async_health_check(self, handle: "ExecutionResourceHandle") -> "ExecutionResourceStatus":
        resource = handle.get("resource")
        return "ready" if resource is not None and hasattr(resource, "run") else "unhealthy"

    async def async_release(self, handle: "ExecutionResourceHandle") -> None:
        _ = handle
        return None
