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

import asyncio
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable, Literal, TYPE_CHECKING, ParamSpec, TypeAlias, TypeVar, cast
from typing_extensions import Self

from agently.core import BaseAgent
from agently.core.runtime.RuntimeContext import get_current_agent_execution_context
from agently.utils import DeprecationWarnings, FunctionShifter
from agently.builtins.actions.Cmd import DEFAULT_SAFE_CMD_PREFIXES

if TYPE_CHECKING:
    from agently.core import Prompt
    from agently.core.operation.Action import ToolCommand, ToolExecutionRecord
    from agently.types.data import ActionCall, ActionResult, AgentlyModelResult, KwargsType, MCPConfigs, ReturnType
    from agently.types.plugins import AgentExecution

from agently.base import action as global_action

P = ParamSpec("P")
R = TypeVar("R")
CapabilityDescMode: TypeAlias = Literal["append", "override", "default"]
CodeSandboxMode: TypeAlias = Literal["auto", "docker", "gvisor", "trusted_local"]
DependencyPolicyMode: TypeAlias = Literal["deny", "request", "install"]
ProvisioningProfileMode: TypeAlias = Literal["strict", "developer", "ci"]
ImagePullPolicyMode: TypeAlias = Literal["never", "request", "if_missing", "always"]
_WORKSPACE_ROOT_UNSET = object()


class ActionExtension(BaseAgent):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        self.action = type(global_action)(self.plugin_manager, self.settings)
        self.tool = self.action

        self.use_action = self.use_actions
        self.use_tool = self.use_tools
        self.use_mcp = FunctionShifter.syncify(self.async_use_mcp)
        self.use_sandbox = self.use_action_sandbox
        self.use_python = self.enable_python
        self.use_shell = self.enable_shell
        self.use_nodejs = self.enable_nodejs
        self.use_code_runtime = self.enable_code_runtime
        self.use_sqlite = self.enable_sqlite
        self.use_docker = self.enable_docker
        self.use_workspace_file_actions = self.enable_workspace_file_actions

        self.settings.setdefault("action.loop.max_rounds", None, inherit=True)
        self.settings.setdefault("action.loop.concurrency", None, inherit=True)
        self.settings.setdefault("action.loop.timeout", None, inherit=True)
        self.settings.setdefault("action.loop.max_consecutive_failed_rounds_per_action", 2, inherit=True)
        self.settings.setdefault("action.loop.enabled", True, inherit=True)
        self.settings.setdefault("tool.loop.max_rounds", None, inherit=True)
        self.settings.setdefault("tool.loop.concurrency", None, inherit=True)
        self.settings.setdefault("tool.loop.timeout", None, inherit=True)
        self.settings.setdefault("tool.loop.max_consecutive_failed_rounds_per_action", 2, inherit=True)
        self.settings.setdefault("tool.loop.enabled", True, inherit=True)
        self.settings.setdefault("execution_resource.owner_id", self.name, inherit=False)

        self.__action_logs: list[ActionResult] = []
        self.__prepared_action_results: dict[str, Any] | None = None
        self.__action_planning_handler = None
        self.__action_execution_handler = None
        self.__required_action_ids: list[str] = []

        self.extension_handlers.append("request_prefixes", self.__request_prefix)
        self.extension_handlers.append("broadcast_prefixes", self.__broadcast_prefix)

    def __import_global_action(self, action_id: str):
        if not isinstance(action_id, str) or action_id.strip() == "":
            return
        local_registry = getattr(self.action, "action_registry", None)
        if local_registry is not None and local_registry.has(action_id):
            return
        global_registry = getattr(global_action, "action_registry", None)
        if global_registry is None or not global_registry.has(action_id):
            return

        spec = global_registry.get_spec(action_id)
        executor = global_registry.get_executor(action_id)
        if spec is None or executor is None:
            return

        copied_spec = dict(spec)
        for key in ("kwargs", "default_policy", "meta"):
            if isinstance(copied_spec.get(key), dict):
                copied_spec[key] = dict(copied_spec[key])
        if isinstance(copied_spec.get("tags"), list):
            copied_spec["tags"] = list(copied_spec["tags"])
        if isinstance(copied_spec.get("execution_resources"), list):
            copied_spec["execution_resources"] = [dict(item) for item in copied_spec["execution_resources"]]

        self.action.register_action(
            action_id=str(copied_spec.get("action_id", action_id)),
            desc=str(copied_spec.get("desc", "")),
            kwargs=copied_spec.get("kwargs", {}),
            func=global_registry.get_func(action_id),
            executor=executor,
            returns=copied_spec.get("returns"),
            tags=copied_spec.get("tags", []),
            default_policy=copied_spec.get("default_policy", {}),
            side_effect_level=copied_spec.get("side_effect_level", "read"),
            approval_required=bool(copied_spec.get("approval_required", False)),
            sandbox_required=bool(copied_spec.get("sandbox_required", False)),
            replay_safe=bool(copied_spec.get("replay_safe", True)),
            expose_to_model=bool(copied_spec.get("expose_to_model", True)),
            execution_resources=copied_spec.get("execution_resources", []),
            meta=copied_spec.get("meta", {}),
        )

    def register_action(
        self,
        *,
        name: str,
        desc: str,
        kwargs: "KwargsType",
        func: Callable,
        returns: "ReturnType | None" = None,
    ) -> Self:
        self.action.register_action(
            action_id=name,
            desc=desc,
            kwargs=kwargs,
            func=func,
            tags=[f"agent-{ self.name }"],
            returns=returns,
        )
        return self

    def register_tool(
        self,
        *,
        name: str,
        desc: str,
        kwargs: "KwargsType",
        func: Callable,
        returns: "ReturnType | None" = None,
    ) -> Self:
        return self.register_action(name=name, desc=desc, kwargs=kwargs, func=func, returns=returns)

    def action_func(self, func: Callable[P, R]) -> Callable[P, R]:
        self.action.action_func(func)
        name = func.__name__
        self.action.tag([name], [f"agent-{ self.name }"])
        return func

    def tool_func(self, func: Callable[P, R]) -> Callable[P, R]:
        return self.action_func(func)

    @staticmethod
    def _normalize_action_items(actions: Any):
        if isinstance(actions, str) or callable(actions) or hasattr(actions, "register_actions"):
            return [actions]
        if isinstance(actions, (list, tuple, set)):
            return list(actions)
        return [actions]

    @staticmethod
    def _normalize_registered_action_ids(value: Any):
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value if str(item)]
        return []

    def _register_action_items(self, actions: Callable | str | list[str | Callable] | Any) -> list[str]:
        names: list[str] = []
        local_registry = getattr(self.action, "action_registry", None)
        agent_tag = f"agent-{ self.name }"
        for action_item in self._normalize_action_items(actions):
            register_actions = getattr(action_item, "register_actions", None)
            if callable(register_actions):
                language_policy = self.settings.get("agent.language_policy", None)
                apply_language_policy = getattr(action_item, "apply_language_policy", None)
                if isinstance(language_policy, Mapping) and callable(apply_language_policy):
                    apply_language_policy(language_policy)
                names.extend(self._normalize_registered_action_ids(register_actions(self.action, tags=[agent_tag])))
                continue
            if isinstance(action_item, str):
                self.__import_global_action(action_item)
                names.append(action_item)
            else:
                action_name = getattr(action_item, "__name__", "")
                if not action_name:
                    raise TypeError("use_actions() expects action names, callables, or built-in action packages.")
                if action_name not in self.action.tool_funcs and (local_registry is None or not local_registry.has(action_name)):
                    self.action_func(action_item)
                names.append(action_name)
        if names:
            self.action.tag(names, agent_tag)
        return names

    def use_actions(
        self,
        actions: Callable | str | list[str | Callable] | Any,
        *,
        always: bool = False,
    ) -> "Self | AgentExecution":
        if not always:
            return self.create_execution().use_actions(actions)
        self._register_action_items(actions)
        return self

    def use_acp(
        self,
        *,
        root: str | Path | None = None,
        agent_ids: list[str] | tuple[str, ...] | str | None = None,
        provider: Any | None = None,
        on_missing: Literal["skip", "error"] = "skip",
        timeout_seconds: float | None = 600,
        action_prefix: str = "",
    ) -> Self:
        from agently.builtins.actions import ACP

        resolved_root = root
        if resolved_root is None:
            workspace = getattr(self, "workspace", None)
            files_root = getattr(workspace, "files_root", None)
            if files_root is not None:
                resolved_root = files_root
                ensure_files_guide = getattr(workspace, "ensure_files_guide", None)
                if callable(ensure_files_guide):
                    ensure_files_guide()
                else:
                    Path(str(files_root)).mkdir(parents=True, exist_ok=True)
        acp = ACP(
            root=resolved_root,
            agent_ids=agent_ids,
            provider=provider,
            on_missing=on_missing,
            timeout_seconds=timeout_seconds,
            action_prefix=action_prefix,
        )
        self._register_action_items(acp)
        diagnostics = acp.list_agents().get("diagnostics", [])
        if diagnostics:
            self.settings.set("agent.acp.diagnostics", cast(Any, diagnostics))
        return self

    def require_actions(
        self,
        actions: Callable | str | list[str | Callable] | Any,
        *,
        always: bool = False,
    ) -> "Self | AgentExecution":
        if not always:
            return self.create_execution().require_actions(actions)
        for name in self._register_action_items(actions):
            if name not in self.__required_action_ids:
                self.__required_action_ids.append(name)
        return self

    def _collect_required_action_ids(self) -> list[str]:
        return list(self.__required_action_ids)

    @staticmethod
    def _action_item_id(item: dict[str, Any]) -> str:
        return str(item.get("action_id") or item.get("name") or "").strip()

    def _get_scoped_action_list(self) -> list[dict[str, Any]]:
        action_list = self.action.get_action_list(tags=[f"agent-{ self.name }"])
        execution_context = get_current_agent_execution_context()
        scoped_action_ids = getattr(execution_context, "scoped_action_ids", None)
        raw_allowed_ids = scoped_action_ids() if callable(scoped_action_ids) else None
        allowed_ids = (
            {str(item).strip() for item in raw_allowed_ids if str(item).strip()}
            if isinstance(raw_allowed_ids, set)
            else set()
        )
        if not allowed_ids:
            scoped_list = action_list
        else:
            scoped_list = [
            item
            for item in action_list
            if self._action_item_id(item) in allowed_ids
            ]
        recall_records = getattr(execution_context, "scoped_action_artifact_recall_records", None)
        if callable(recall_records):
            scoped_list = self.action._with_action_artifact_recall_action(
                scoped_list,
                cast(list["ActionResult"], recall_records()),
            )
        return scoped_list

    def use_tools(self, tools: Callable | str | list[str | Callable] | Any) -> "Self | AgentExecution":
        return self.use_actions(tools)

    @staticmethod
    def _build_capability_desc(
        default_desc: str,
        desc: str | None = None,
        *,
        mode: CapabilityDescMode = "append",
    ) -> str:
        extra = desc.strip() if isinstance(desc, str) else ""
        if not extra or mode == "default":
            return default_desc
        if mode == "override":
            return extra
        if mode != "append":
            raise ValueError("desc_mode must be one of: 'append', 'override', 'default'.")
        return f"{ default_desc }\n\nAdditional guidance: { extra }"

    async def async_use_mcp(
        self,
        transport: "MCPConfigs | str | Any",
        *,
        headers: dict[str, str] | None = None,
    ) -> Self:
        await self.action.async_use_mcp(transport, headers=headers, tags=[f"agent-{ self.name }"])
        return self

    def use_action_sandbox(
        self,
        sandbox: str,
        *,
        action_id: str | None = None,
        expose_to_model: bool = True,
        sandbox_mode: CodeSandboxMode | None = None,
        **kwargs: Any,
    ) -> Self:
        sandbox_name = sandbox.strip().lower() if isinstance(sandbox, str) else ""
        if sandbox_name in {"python", "python_sandbox"}:
            resolved_action_id = action_id or "python_sandbox"
            kwargs.setdefault("sandbox", sandbox_mode or "auto")
            self.action.register_python_sandbox_action(
                action_id=resolved_action_id,
                tags=[f"agent-{ self.name }"],
                expose_to_model=expose_to_model,
                **kwargs,
            )
            return self
        if sandbox_name in {"bash", "shell", "bash_sandbox"}:
            resolved_action_id = action_id or "bash_sandbox"
            kwargs.setdefault("sandbox", sandbox_mode or "auto")
            self.action.register_bash_sandbox_action(
                action_id=resolved_action_id,
                tags=[f"agent-{ self.name }"],
                expose_to_model=expose_to_model,
                **kwargs,
            )
            return self
        raise ValueError("sandbox must be one of: 'python', 'bash'.")

    def enable_python(
        self,
        *,
        action_id: str = "run_python",
        desc: str | None = None,
        desc_mode: CapabilityDescMode = "append",
        expose_to_model: bool = True,
        preset_objects: dict[str, object] | None = None,
        base_vars: dict[str, Any] | None = None,
        allowed_return_types: list[type] | None = None,
        sandbox: CodeSandboxMode = "auto",
        docker_image: str = "python:3.12-slim",
        docker_binary: str = "docker",
        docker_default_args: list[str] | None = None,
        dependency_policy: DependencyPolicyMode | dict[str, Any] | None = None,
        provisioning_profile: ProvisioningProfileMode = "strict",
        image_pull_policy: ImagePullPolicyMode | None = None,
        timeout: int = 60,
    ) -> Self:
        if sandbox == "trusted_local":
            default_desc = (
                "Run Python code through an explicitly trusted local in-process execution resource for "
                "deterministic calculation or small data shaping. Assign the final value to `result`."
            )
        else:
            default_desc = (
                "Run Python code in a Docker-backed sandbox when a local Docker service is available. "
                "Use this for deterministic calculation or small data shaping and assign the final value "
                "to `result`. Dependency installation is controlled by the host resource policy, not by "
                "model-visible action inputs; if Docker is unavailable this action fails closed."
            )
        self.action.register_python_sandbox_action(
            action_id=action_id,
            desc=self._build_capability_desc(default_desc, desc, mode=desc_mode),
            tags=[f"agent-{ self.name }"],
            expose_to_model=expose_to_model,
            preset_objects=preset_objects,
            base_vars=base_vars,
            allowed_return_types=allowed_return_types,
            sandbox=sandbox,
            docker_image=docker_image,
            docker_binary=docker_binary,
            docker_default_args=docker_default_args,
            dependency_policy=dependency_policy,
            provisioning_profile=provisioning_profile,
            image_pull_policy=image_pull_policy,
            timeout=timeout,
        )
        return self

    def enable_shell(
        self,
        *,
        root: str | Path | None = None,
        commands: list[str] | None = None,
        action_id: str = "run_bash",
        desc: str | None = None,
        desc_mode: CapabilityDescMode = "append",
        expose_to_model: bool = True,
        timeout: int = 20,
        env: dict[str, str] | None = None,
        max_output_chars: int = 20000,
        sandbox: CodeSandboxMode = "auto",
        docker_image: str = "python:3.12-slim",
        docker_binary: str = "docker",
        docker_default_args: list[str] | None = None,
        dependency_policy: DependencyPolicyMode | dict[str, Any] | None = None,
        provisioning_profile: ProvisioningProfileMode = "strict",
        image_pull_policy: ImagePullPolicyMode | None = None,
    ) -> Self:
        workspace = getattr(self, "workspace", None)
        if root is None and workspace is not None:
            root = getattr(workspace, "files_root", getattr(workspace, "content_root", None))
        root_path = Path(root).expanduser().resolve() if root is not None else None
        roots: list[str | Path] | None = [str(root_path)] if root_path is not None else None
        resolved_commands = list(commands) if commands is not None else list(DEFAULT_SAFE_CMD_PREFIXES)
        output_artifact_dir = str(root_path / "artifacts" / "shell") if root_path is not None else None
        boundary_text = (
            "Docker-backed workspace boundary"
            if sandbox != "trusted_local"
            else "explicitly trusted local workspace boundary"
        )
        default_desc = (
            f"Run an allowlisted shell command inside a { boundary_text } for tests, builds, "
            "git status inspection, and read-only diagnostics. Prefer dedicated Workspace actions "
            "`read_file`, `glob_files`, `grep_files`, `edit_file`, `apply_patch`, and `write_file` for "
            "file reading, searching, editing, and writing. Do not start background long-running "
            "commands; each command is bounded by timeout and output preview limits. Dependency installation "
            "is controlled by the host resource policy, not by model-visible action inputs."
        )
        self.action.register_bash_sandbox_action(
            action_id=action_id,
            desc=self._build_capability_desc(default_desc, desc, mode=desc_mode),
            tags=[f"agent-{ self.name }"],
            expose_to_model=expose_to_model,
            allowed_cmd_prefixes=resolved_commands,
            allowed_workdir_roots=roots,
            timeout=timeout,
            env=env,
            max_output_chars=max_output_chars,
            output_artifact_dir=output_artifact_dir,
            sandbox=sandbox,
            docker_image=docker_image,
            docker_binary=docker_binary,
            docker_default_args=docker_default_args,
            dependency_policy=dependency_policy,
            provisioning_profile=provisioning_profile,
            image_pull_policy=image_pull_policy,
        )
        return self

    def enable_nodejs(
        self,
        *,
        action_id: str = "run_nodejs",
        desc: str | None = None,
        desc_mode: CapabilityDescMode = "append",
        expose_to_model: bool = True,
        node_binary: str = "node",
        cwd: str | None = None,
        timeout: int = 20,
        env: dict[str, str] | None = None,
        sandbox: CodeSandboxMode = "auto",
        docker_image: str = "node:22-slim",
        docker_binary: str = "docker",
        docker_default_args: list[str] | None = None,
        dependency_policy: DependencyPolicyMode | dict[str, Any] | None = None,
        provisioning_profile: ProvisioningProfileMode = "strict",
        image_pull_policy: ImagePullPolicyMode | None = None,
    ) -> Self:
        workspace = getattr(self, "workspace", None)
        if cwd is None and workspace is not None:
            cwd = str(getattr(workspace, "files_root", getattr(workspace, "content_root")))
        if sandbox == "trusted_local":
            default_desc = "Run JavaScript with Node.js inside an explicitly trusted local execution resource."
        else:
            default_desc = (
                "Run JavaScript with Node.js inside a Docker-backed execution resource when a local Docker "
                "service is available. Dependency installation is controlled by the host resource policy, "
                "not by model-visible action inputs; if Docker is unavailable this action fails closed."
            )
        self.action.register_nodejs_action(
            action_id=action_id,
            desc=self._build_capability_desc(default_desc, desc, mode=desc_mode),
            tags=[f"agent-{ self.name }"],
            expose_to_model=expose_to_model,
            node_binary=node_binary,
            cwd=cwd,
            timeout=timeout,
            env=env,
            sandbox=sandbox,
            docker_image=docker_image,
            docker_binary=docker_binary,
            docker_default_args=docker_default_args,
            dependency_policy=dependency_policy,
            provisioning_profile=provisioning_profile,
            image_pull_policy=image_pull_policy,
        )
        return self

    def enable_code_runtime(
        self,
        *,
        language: str,
        action_id: str | None = None,
        desc: str | None = None,
        desc_mode: CapabilityDescMode = "append",
        expose_to_model: bool = True,
        docker_image: str | None = None,
        docker_binary: str = "docker",
        docker_default_args: list[str] | None = None,
        dependency_policy: DependencyPolicyMode | dict[str, Any] | None = None,
        provisioning_profile: ProvisioningProfileMode = "strict",
        image_pull_policy: ImagePullPolicyMode | None = None,
        timeout: int = 60,
    ) -> Self:
        from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import (
            get_code_runtime_profile,
        )

        profile = get_code_runtime_profile(language, image=docker_image)
        display_names = {
            "nodejs": "JavaScript/Node.js",
            "typescript": "TypeScript",
            "cpp": "C++",
            "csharp": "C#/.NET",
            "r": "R",
        }
        language_name = display_names.get(profile["language"], str(profile["language"]).capitalize())
        default_desc = (
            f"Run { language_name } code inside a Docker-backed code runtime sandbox. "
            "The host-selected runtime profile owns image pull and dependency preparation; "
            "the model can provide source files and arguments but not arbitrary compiler or package-manager commands."
        )
        self.action.register_code_runtime_action(
            language=profile["language"],
            action_id=action_id,
            desc=self._build_capability_desc(default_desc, desc, mode=desc_mode),
            tags=[f"agent-{ self.name }"],
            expose_to_model=expose_to_model,
            docker_image=str(profile["image"]),
            docker_binary=docker_binary,
            docker_default_args=docker_default_args,
            dependency_policy=dependency_policy,
            provisioning_profile=provisioning_profile,
            image_pull_policy=image_pull_policy,
            timeout=timeout,
        )
        return self

    def enable_sqlite(
        self,
        *,
        database: str = ":memory:",
        action_id: str = "query_sqlite",
        read_only: bool = True,
        desc: str | None = None,
        desc_mode: CapabilityDescMode = "append",
        expose_to_model: bool = True,
        uri: bool = False,
    ) -> Self:
        default_desc = "Query a SQLite database through a managed execution resource."
        self.action.register_sqlite_action(
            action_id=action_id,
            desc=self._build_capability_desc(default_desc, desc, mode=desc_mode),
            tags=[f"agent-{ self.name }"],
            expose_to_model=expose_to_model,
            database=database,
            read_only=read_only,
            uri=uri,
        )
        return self

    def enable_docker(
        self,
        *,
        action_id: str = "run_docker",
        image: str | None = None,
        desc: str | None = None,
        desc_mode: CapabilityDescMode = "append",
        expose_to_model: bool = False,
        timeout: int = 60,
        docker_binary: str = "docker",
        default_args: list[str] | None = None,
    ) -> Self:
        default_desc = "Run a command in a Docker container through a managed execution resource."
        self.action.register_docker_action(
            action_id=action_id,
            desc=self._build_capability_desc(default_desc, desc, mode=desc_mode),
            tags=[f"agent-{ self.name }"],
            expose_to_model=expose_to_model,
            image=image,
            timeout=timeout,
            docker_binary=docker_binary,
            default_args=default_args,
        )
        return self

    def enable_workspace_file_actions(
        self,
        *,
        root: str | Path | object = _WORKSPACE_ROOT_UNSET,
        isolated: bool = False,
        read: bool = True,
        write: bool = False,
        search: bool = True,
        list_files: bool = True,
        export: bool = False,
        action_prefix: str = "",
        expose_to_model: bool = True,
        max_file_bytes: int = 20000,
        max_search_file_bytes: int = 200000,
        desc: str | None = None,
        desc_mode: CapabilityDescMode = "append",
        coding_agent: bool = False,
    ) -> Self:
        workspace = getattr(self, "workspace", None)
        if isolated and root is _WORKSPACE_ROOT_UNSET:
            root = tempfile.mkdtemp(prefix="agently-workspace-action-")
        elif root is _WORKSPACE_ROOT_UNSET and workspace is not None:
            root = getattr(workspace, "files_root", getattr(workspace, "content_root"))
        elif root is _WORKSPACE_ROOT_UNSET:
            DeprecationWarnings.warn_deprecated_once(
                "ActionExtension.enable_workspace.default_root_without_foundation_workspace",
                "`agent.enable_workspace_file_actions()` without an Agent Workspace binding "
                "defaults to the current directory. Standard Agents include a lazy Workspace; "
                "pass an explicit `root=` or call `agent.use_workspace(...)` to override it.",
                stacklevel=2,
            )
            root = "."
        root_path = Path(str(root)).expanduser().resolve()
        agent_tag = f"agent-{ self.name }"
        prefix = action_prefix.strip()
        workspace_for_actions = None
        if workspace is not None:
            workspace_root = Path(str(getattr(workspace, "files_root", ""))).expanduser().resolve()
            if workspace_root == root_path:
                workspace_for_actions = workspace

        from agently.base import workspace as global_workspace

        manager = getattr(workspace_for_actions, "manager", global_workspace)

        def action_name(name: str):
            return f"{ prefix }{ name }" if prefix else name

        def has_action(name: str):
            registry = getattr(self.action, "action_registry", None)
            return bool(registry is not None and registry.has(action_name(name)))

        def resolve_workspace_path(path: str | Path = "."):
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = root_path / candidate
            resolved = candidate.expanduser().resolve()
            try:
                resolved.relative_to(root_path)
            except ValueError as error:
                raise ValueError(f"Path is outside workspace root: { path }") from error
            return resolved

        def is_hidden(path: Path):
            try:
                relative_parts = path.relative_to(root_path).parts
            except ValueError:
                return True
            return any(part.startswith(".") for part in relative_parts)

        def iter_workspace_files(
            path: str = ".",
            pattern: str = "*",
            max_results: int = 200,
            include_hidden: bool = False,
        ):
            base = resolve_workspace_path(path)
            if base.is_file():
                candidates = [base]
            elif base.exists():
                candidates = base.rglob(pattern)
            else:
                candidates = []
            collected: list[Path] = []
            for candidate in candidates:
                if len(collected) >= max_results:
                    break
                if not candidate.is_file():
                    continue
                if not include_hidden and is_hidden(candidate):
                    continue
                collected.append(candidate)
            return collected

        def relative_path(path: Path):
            return str(path.relative_to(root_path))

        def file_result_action_output(result: Any):
            return {
                "status": "success",
                "ok": True,
                "data": result,
                "result": result,
            }

        read_state: dict[str, dict[str, Any]] = {}

        def inspect_workspace_file(path: str | Path):
            target = resolve_workspace_path(path)
            return manager.inspect_file_path(target, relative_path=relative_path(target))

        def remember_read(path: str | Path, result: Mapping[str, Any], *, offset: int = 0):
            if not coding_agent:
                return
            if offset != 0 or result.get("truncated") or not result.get("ok", result.get("readable")):
                return
            path_text = str(result.get("path") or relative_path(resolve_workspace_path(path)))
            sha256 = str(result.get("sha256") or "")
            if sha256:
                read_state[path_text] = {"sha256": sha256}

        def remember_write(path: str | Path, result: Mapping[str, Any]):
            if not coding_agent:
                return
            path_text = str(result.get("path") or relative_path(resolve_workspace_path(path)))
            sha256 = str(result.get("sha256") or "")
            if sha256:
                read_state[path_text] = {"sha256": sha256}

        def require_fresh_for_write(path: str | Path, expected_sha256: str | None = None):
            if not coding_agent:
                return
            info = inspect_workspace_file(path)
            path_text = str(info.get("path") or relative_path(resolve_workspace_path(path)))
            current_sha = str(info.get("sha256") or "")
            if not info.get("exists"):
                if expected_sha256 not in (None, ""):
                    raise ValueError("Workspace file does not exist; expected_sha256 cannot be satisfied.")
                return
            if expected_sha256:
                if current_sha != str(expected_sha256):
                    raise ValueError("Workspace file has changed since the expected sha256.")
                return
            state = read_state.get(path_text)
            if not state:
                raise PermissionError(
                    "File has not been read through this coding-agent action set; read it first or pass expected_sha256."
                )
            if str(state.get("sha256") or "") != current_sha:
                raise ValueError("File has been modified since it was read; read it again before writing.")

        async def manager_read_file(path: str, max_bytes: int = max_file_bytes, offset: int = 0):
            if workspace_for_actions is not None:
                return await workspace_for_actions.read_file(path, max_bytes=max_bytes, offset=offset)
            target = resolve_workspace_path(path)
            if not target.is_file():
                raise FileNotFoundError(f"Workspace file not found: { path }")
            return await manager.read_file_path(
                target,
                relative_path=relative_path(target),
                max_bytes=max_bytes,
                offset=offset,
            )

        async def manager_write_file(path: str, content: str, append: bool = False):
            if workspace_for_actions is not None:
                return await workspace_for_actions.write_file(path, content, append=append)
            target = resolve_workspace_path(path)
            return await manager.write_file_path(
                target,
                relative_path=relative_path(target),
                content=content,
                append=append,
            )

        async def manager_edit_file(
            path: str,
            old_string: str,
            new_string: str,
            *,
            replace_all: bool = False,
            expected_sha256: str | None = None,
        ):
            require_fresh_for_write(path, expected_sha256)
            if workspace_for_actions is not None:
                result = await workspace_for_actions.edit_file(
                    path,
                    old_string,
                    new_string,
                    replace_all=replace_all,
                    expected_sha256=expected_sha256,
                )
            else:
                info = inspect_workspace_file(path)
                if not info.get("exists"):
                    if old_string != "":
                        raise FileNotFoundError(f"Workspace file not found: { path }")
                    result = await manager_write_file(path, new_string, append=False)
                else:
                    read_result = await manager_read_file(path, max_bytes=int(info.get("bytes") or 0) + 1)
                    content = str(read_result.get("content") or "")
                    if old_string == new_string:
                        raise ValueError("old_string and new_string are identical; no edit was applied.")
                    if old_string == "":
                        if content:
                            raise ValueError("Cannot create a file with edit_file because the target already exists.")
                        new_content = new_string
                        replacements = 1
                    else:
                        replacements = content.count(old_string)
                        if replacements <= 0:
                            raise ValueError("old_string was not found in the Workspace file.")
                        if replacements > 1 and not replace_all:
                            raise ValueError(
                                "old_string matched multiple locations; set replace_all=True or provide more context."
                            )
                        new_content = (
                            content.replace(old_string, new_string)
                            if replace_all
                            else content.replace(old_string, new_string, 1)
                        )
                    result = dict(await manager_write_file(path, new_content, append=False))
                    result["replacements"] = replacements
            remember_write(path, result)
            return result

        def patch_paths(patch: str) -> list[str]:
            paths: list[str] = []

            def add(raw_path: str):
                text = raw_path.strip()
                if not text or text == "/dev/null":
                    return
                if text.startswith("a/") or text.startswith("b/"):
                    text = text[2:]
                if "\t" in text:
                    text = text.split("\t", 1)[0]
                target = resolve_workspace_path(text)
                normalized = relative_path(target)
                if normalized not in paths:
                    paths.append(normalized)

            for line in str(patch or "").splitlines():
                if line.startswith("diff --git "):
                    parts = line.split()
                    if len(parts) >= 4:
                        add(parts[2])
                        add(parts[3])
                    continue
                if line.startswith("+++ ") or line.startswith("--- "):
                    add(line[4:])
            return paths

        async def manager_apply_patch(patch: str, expected_files: list[str] | None = None):
            paths = patch_paths(patch)
            if not paths:
                raise ValueError("Patch did not declare any file paths.")
            if expected_files is not None:
                expected = [relative_path(resolve_workspace_path(item)) for item in expected_files]
                if sorted(paths) != sorted(dict.fromkeys(expected)):
                    raise ValueError("Patch file set does not match expected_files.")
            for path in paths:
                if inspect_workspace_file(path).get("exists"):
                    require_fresh_for_write(path)
            if workspace_for_actions is not None:
                result = await workspace_for_actions.apply_patch(patch, expected_files=expected_files)
            else:
                git_path = shutil.which("git")
                if git_path is None:
                    raise RuntimeError("git executable is required for apply_patch.")
                completed = await asyncio.to_thread(
                    subprocess.run,
                    [git_path, "apply", "--whitespace=nowarn"],
                    cwd=str(root_path),
                    input=str(patch or ""),
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=30,
                )
                if completed.returncode != 0:
                    raise ValueError(str(completed.stderr or completed.stdout or "git apply failed").strip())
                result = {
                    "ok": True,
                    "status": "success",
                    "paths": paths,
                    "file_infos": [inspect_workspace_file(path) for path in paths],
                }
            for path in paths:
                info = inspect_workspace_file(path)
                if info.get("exists"):
                    read_state[str(info.get("path") or path)] = {"sha256": str(info.get("sha256") or "")}
            return result

        async def manager_export_file(
            source_path: str,
            output_path: str,
            export_kind: str,
            options: dict[str, Any] | None = None,
        ):
            if workspace_for_actions is not None:
                return await workspace_for_actions.export_file(
                    source_path,
                    output_path,
                    export_kind=export_kind,
                    options=options,
                )
            source = resolve_workspace_path(source_path)
            if not source.is_file():
                raise FileNotFoundError(f"Workspace source file not found: { source_path }")
            output = resolve_workspace_path(output_path)
            return await manager.export_file_path(
                source,
                output,
                source_relative_path=relative_path(source),
                output_relative_path=relative_path(output),
                export_kind=export_kind,
                options=options,
            )

        if read and list_files and not has_action("list_files"):

            def list_workspace_files(
                path: str = ".",
                pattern: str = "*",
                max_results: int = 200,
                include_hidden: bool = False,
            ):
                files = iter_workspace_files(path, pattern, max_results, include_hidden)
                return [str(file.relative_to(root_path)) for file in files]

            self.action.register_action(
                action_id=action_name("list_files"),
                desc=self._build_capability_desc(
                    f"List files under the workspace root { root_path }.",
                    desc,
                    mode=desc_mode,
                ),
                kwargs={
                    "path": (str, "Workspace-relative directory or file path. Default: '.'."),
                    "pattern": (str, "Glob pattern. Default: '*'."),
                    "max_results": (int, "Maximum files to return. Default: 200."),
                    "include_hidden": (bool, "Whether to include hidden paths. Default: False."),
                },
                func=list_workspace_files,
                tags=[agent_tag],
                side_effect_level="read",
                expose_to_model=expose_to_model,
                meta={"component": "workspace", "root": str(root_path)},
            )

        if read and not has_action("read_file"):

            async def read_file(path: str, max_bytes: int = max_file_bytes, offset: int = 0):
                result = await manager_read_file(path, max_bytes=max_bytes, offset=offset)
                remember_read(path, result, offset=offset)
                return file_result_action_output(result)

            self.action.register_action(
                action_id=action_name("read_file"),
                desc=self._build_capability_desc(
                    f"Read a file under the workspace root { root_path } through registered Workspace file IO handlers.",
                    desc,
                    mode=desc_mode,
                ),
                kwargs={
                    "path": (str, "Workspace-relative file path."),
                    "max_bytes": (int, f"Maximum bytes to read. Default: { max_file_bytes }."),
                    "offset": (int, "Byte offset to start reading from. Default: 0."),
                },
                func=read_file,
                tags=[agent_tag],
                side_effect_level="read",
                expose_to_model=expose_to_model,
                meta={"component": "workspace", "root": str(root_path)},
            )

        if read and coding_agent and not has_action("glob_files"):

            async def glob_files(
                pattern: str,
                path: str = ".",
                max_results: int = 200,
                include_hidden: bool = False,
            ):
                if workspace_for_actions is not None:
                    return await workspace_for_actions.glob_files(
                        pattern,
                        path=path,
                        max_results=max_results,
                        include_hidden=include_hidden,
                    )
                files = iter_workspace_files(path, pattern, max_results=max_results, include_hidden=include_hidden)
                matches = [relative_path(file) for file in files]
                return {
                    "pattern": pattern,
                    "path": path,
                    "matches": matches,
                    "count": len(matches),
                    "truncated": len(matches) >= max_results,
                    "max_results": max_results,
                }

            self.action.register_action(
                action_id=action_name("glob_files"),
                desc=self._build_capability_desc(
                    (
                        f"Find files under the workspace root { root_path } by glob. "
                        "Use this instead of shell find/ls when looking for files."
                    ),
                    desc,
                    mode=desc_mode,
                ),
                kwargs={
                    "pattern": (str, "Glob pattern such as '*.py' or '**/*.md'."),
                    "path": (str, "Workspace-relative directory or file path. Default: '.'."),
                    "max_results": (int, "Maximum files to return. Default: 200."),
                    "include_hidden": (bool, "Whether to include hidden paths. Default: False."),
                },
                func=glob_files,
                tags=[agent_tag],
                side_effect_level="read",
                expose_to_model=expose_to_model,
                meta={"component": "workspace", "root": str(root_path), "coding_agent": True},
            )

        if read and coding_agent and not has_action("grep_files"):

            async def grep_files(
                pattern: str,
                path: str = ".",
                regex: bool = True,
                glob: str | None = None,
                context_lines: int = 0,
                max_results: int = 50,
                include_hidden: bool = False,
            ):
                if workspace_for_actions is not None:
                    return await workspace_for_actions.grep_files(
                        pattern,
                        path=path,
                        regex=regex,
                        glob=glob,
                        context_lines=context_lines,
                        max_results=max_results,
                        include_hidden=include_hidden,
                        max_file_bytes=max_search_file_bytes,
                    )
                compiled = re.compile(pattern) if regex else None
                results: list[dict[str, Any]] = []
                files = iter_workspace_files(path, glob or "**/*", max_results=1000, include_hidden=include_hidden)
                for file in files:
                    if len(results) >= max_results:
                        break
                    if file.stat().st_size > max_search_file_bytes:
                        continue
                    read_result = await manager_read_file(relative_path(file), max_bytes=max_search_file_bytes)
                    text = str(read_result.get("content") or "")
                    lines = text.splitlines()
                    for line_index, line in enumerate(lines):
                        matched = bool(compiled.search(line)) if compiled is not None else str(pattern) in line
                        if not matched:
                            continue
                        start = max(0, line_index - max(0, context_lines))
                        end = min(len(lines), line_index + max(0, context_lines) + 1)
                        results.append(
                            {
                                "path": relative_path(file),
                                "line": line_index + 1,
                                "text": line,
                                "snippet": "\n".join(lines[start:end]),
                                "line_start": start + 1,
                                "line_end": end,
                                "truncated": False,
                            }
                        )
                        break
                return {
                    "pattern": pattern,
                    "regex": regex,
                    "glob": glob or "**/*",
                    "path": path,
                    "matches": results,
                    "count": len(results),
                    "truncated": len(results) >= max_results,
                    "max_results": max_results,
                }

            self.action.register_action(
                action_id=action_name("grep_files"),
                desc=self._build_capability_desc(
                    (
                        f"Search file contents under the workspace root { root_path } with regex or fixed text. "
                        "Use this instead of shell grep/rg when looking inside files."
                    ),
                    desc,
                    mode=desc_mode,
                ),
                kwargs={
                    "pattern": (str, "Regex or fixed text pattern to search for."),
                    "path": (str, "Workspace-relative directory or file path. Default: '.'."),
                    "regex": (bool, "Treat pattern as a regular expression. Default: True."),
                    "glob": (str, "Optional file glob such as '*.py' or '**/*.md'. Default: None."),
                    "context_lines": (int, "Number of surrounding lines to include. Default: 0."),
                    "max_results": (int, "Maximum matches to return. Default: 50."),
                    "include_hidden": (bool, "Whether to include hidden paths. Default: False."),
                },
                func=grep_files,
                tags=[agent_tag],
                side_effect_level="read",
                expose_to_model=expose_to_model,
                meta={"component": "workspace", "root": str(root_path), "coding_agent": True},
            )

        if read and search and not has_action("search_files"):

            async def search_files_action(
                query: str,
                path: str = ".",
                pattern: str = "*",
                max_results: int = 50,
                include_hidden: bool = False,
            ):
                if workspace_for_actions is not None:
                    return await workspace_for_actions.search_files(
                        query,
                        path=path,
                        pattern=pattern,
                        max_results=max_results,
                        include_hidden=include_hidden,
                        max_file_bytes=max_search_file_bytes,
                    )
                results: list[dict[str, Any]] = []
                files = iter_workspace_files(path, pattern, max_results=1000, include_hidden=include_hidden)
                for file in files:
                    if len(results) >= max_results:
                        break
                    file_size = file.stat().st_size
                    if file_size > max_search_file_bytes:
                        continue
                    result = await manager_read_file(
                        relative_path(file),
                        max_bytes=max_search_file_bytes,
                    )
                    if not result.get("readable") or result.get("content_kind") != "text":
                        continue
                    text = str(result.get("content", ""))
                    for line_no, line in enumerate(text.splitlines(), start=1):
                        if query in line:
                            path_text = relative_path(file)
                            search_scope = {
                                "path": path,
                                "pattern": pattern,
                                "include_hidden": include_hidden,
                                "max_results": max_results,
                            }
                            locator_ref = {
                                "role": "locator_ref",
                                "content_state": "ref_only",
                                "source": "workspace.search_files",
                                "query": query,
                                "scope": search_scope,
                                "path": path_text,
                                "bytes": file_size,
                            }
                            results.append(
                                {
                                    "path": path_text,
                                    "line": line_no,
                                    "text": line,
                                    "role": "evidence_snippet",
                                    "content_state": "bounded_readback_available",
                                    "source": "workspace.search_files",
                                    "query": query,
                                    "scope": search_scope,
                                    "locator_ref": locator_ref,
                                    "snippet": line,
                                    "snippet_chars": len(line),
                                    "snippet_bytes": len(line.encode("utf-8")),
                                    "line_start": line_no,
                                    "line_end": line_no,
                                    "bytes": file_size,
                                }
                            )
                            break
                return results

            self.action.register_action(
                action_id=action_name("search_files"),
                desc=self._build_capability_desc(
                    f"Search UTF-8 text files under the workspace root { root_path }.",
                    desc,
                    mode=desc_mode,
                ),
                kwargs={
                    "query": (str, "Exact text to search for."),
                    "path": (str, "Workspace-relative directory or file path. Default: '.'."),
                    "pattern": (str, "Glob pattern. Default: '*'."),
                    "max_results": (int, "Maximum matching files to return. Default: 50."),
                    "include_hidden": (bool, "Whether to include hidden paths. Default: False."),
                },
                func=search_files_action,
                tags=[agent_tag],
                side_effect_level="read",
                expose_to_model=expose_to_model,
                meta={"component": "workspace", "root": str(root_path)},
            )

        if write and not has_action("write_file"):

            async def write_file(
                path: str,
                content: str,
                append: bool = False,
                expected_sha256: str | None = None,
            ):
                require_fresh_for_write(path, expected_sha256)
                result = await manager_write_file(path, content, append=append)
                remember_write(path, result)
                return file_result_action_output(result)

            self.action.register_action(
                action_id=action_name("write_file"),
                desc=self._build_capability_desc(
                    f"Write a plain text file under the workspace root { root_path } through registered Workspace file IO handlers.",
                    desc,
                    mode=desc_mode,
                ),
                kwargs={
                    "path": (str, "Workspace-relative file path."),
                    "content": (str, "Text content to write."),
                    "append": (bool, "Append instead of overwrite. Default: False."),
                    "expected_sha256": (
                        str,
                        "Optional current file sha256. In coding-agent mode, existing files require this or a prior full read.",
                    ),
                },
                func=write_file,
                tags=[agent_tag],
                side_effect_level="write",
                expose_to_model=expose_to_model,
                meta={"component": "workspace", "root": str(root_path), "write": True},
            )

        if write and coding_agent and not has_action("edit_file"):

            async def edit_file(
                path: str,
                old_string: str,
                new_string: str,
                replace_all: bool = False,
                expected_sha256: str | None = None,
            ):
                return file_result_action_output(
                    await manager_edit_file(
                        path,
                        old_string,
                        new_string,
                        replace_all=replace_all,
                        expected_sha256=expected_sha256,
                    )
                )

            self.action.register_action(
                action_id=action_name("edit_file"),
                desc=self._build_capability_desc(
                    (
                        f"Edit a text file under the workspace root { root_path } by exact string replacement. "
                        "Existing files require a prior full read or expected_sha256; ambiguous replacements fail closed."
                    ),
                    desc,
                    mode=desc_mode,
                ),
                kwargs={
                    "path": (str, "Workspace-relative file path."),
                    "old_string": (str, "Exact string to replace. Use empty string only to create a new file."),
                    "new_string": (str, "Replacement text."),
                    "replace_all": (bool, "Replace every occurrence. Default: False."),
                    "expected_sha256": (str, "Optional current file sha256."),
                },
                func=edit_file,
                tags=[agent_tag],
                side_effect_level="write",
                expose_to_model=expose_to_model,
                meta={"component": "workspace", "root": str(root_path), "write": True, "coding_agent": True},
            )

        if write and coding_agent and not has_action("apply_patch"):

            async def apply_patch(patch: str, expected_files: list[str] | None = None):
                return file_result_action_output(await manager_apply_patch(patch, expected_files=expected_files))

            self.action.register_action(
                action_id=action_name("apply_patch"),
                desc=self._build_capability_desc(
                    (
                        f"Apply a unified diff patch under the workspace root { root_path }. "
                        "Existing patched files require a prior full read; expected_files must match the patch file set when provided."
                    ),
                    desc,
                    mode=desc_mode,
                ),
                kwargs={
                    "patch": (str, "Unified diff patch to apply."),
                    "expected_files": ([str], "Optional exact list of Workspace-relative files expected in the patch."),
                },
                func=apply_patch,
                tags=[agent_tag],
                side_effect_level="write",
                expose_to_model=expose_to_model,
                meta={"component": "workspace", "root": str(root_path), "write": True, "coding_agent": True},
            )

        if write and export and not has_action("export_file"):

            async def export_file(
                source_path: str,
                output_path: str,
                export_kind: str,
                options: dict[str, Any] | None = None,
            ):
                return file_result_action_output(
                    await manager_export_file(
                        source_path,
                        output_path,
                        export_kind,
                        options=options,
                    )
                )

            self.action.register_action(
                action_id=action_name("export_file"),
                desc=self._build_capability_desc(
                    f"Export a Workspace file under { root_path } using registered Workspace file IO handlers.",
                    desc,
                    mode=desc_mode,
                ),
                kwargs={
                    "source_path": (str, "Workspace-relative source file path."),
                    "output_path": (str, "Workspace-relative output file path."),
                    "export_kind": (
                        str,
                        "Export kind such as 'html_pdf', 'markdown_pdf', or 'html_screenshot'.",
                    ),
                    "options": (dict, "Optional handler-specific export options. Default: None."),
                },
                func=export_file,
                tags=[agent_tag],
                side_effect_level="write",
                expose_to_model=expose_to_model,
                meta={"component": "workspace", "root": str(root_path), "write": True, "export": True},
            )

        return self

    def enable_coding_agent_actions(
        self,
        *,
        root: str | Path | object = _WORKSPACE_ROOT_UNSET,
        isolated: bool = False,
        read: bool = True,
        write: bool = True,
        search: bool = True,
        list_files: bool = True,
        export: bool = False,
        action_prefix: str = "",
        expose_to_model: bool = True,
        max_file_bytes: int = 20000,
        max_search_file_bytes: int = 200000,
        desc: str | None = None,
        desc_mode: CapabilityDescMode = "append",
    ) -> Self:
        default_desc = (
            "Coding-agent Workspace file actions. Use read_file/glob_files/grep_files for inspection, "
            "edit_file/apply_patch for targeted edits, and write_file only for deliberate full-file writes. "
            "Existing file writes require a prior full read through this action set or expected_sha256."
        )
        return self.enable_workspace_file_actions(
            root=root,
            isolated=isolated,
            read=read,
            write=write,
            search=search,
            list_files=list_files,
            export=export,
            action_prefix=action_prefix,
            expose_to_model=expose_to_model,
            max_file_bytes=max_file_bytes,
            max_search_file_bytes=max_search_file_bytes,
            desc=self._build_capability_desc(default_desc, desc, mode=desc_mode),
            desc_mode="append",
            coding_agent=True,
        )

    def enable_workspace(
        self,
        *,
        root: str | Path | object = _WORKSPACE_ROOT_UNSET,
        isolated: bool = False,
        read: bool = True,
        write: bool = False,
        search: bool = True,
        list_files: bool = True,
        export: bool = False,
        action_prefix: str = "",
        expose_to_model: bool = True,
        max_file_bytes: int = 20000,
        max_search_file_bytes: int = 200000,
        desc: str | None = None,
        desc_mode: CapabilityDescMode = "append",
    ) -> Self:
        DeprecationWarnings.warn_deprecated_once(
            "ActionExtension.enable_workspace.renamed_to_enable_workspace_file_actions",
            "`agent.enable_workspace(...)` is kept as a compatibility alias for "
            "`agent.enable_workspace_file_actions(...)`. Standard Agents include a lazy "
            "Workspace binding, and `agent.use_workspace(...)` overrides its root, mode, "
            "or provider. Use `enable_workspace_file_actions(...)` when you want to expose "
            "Workspace file list/search/read/write actions.",
            stacklevel=2,
        )
        return self.enable_workspace_file_actions(
            root=root,
            isolated=isolated,
            read=read,
            write=write,
            search=search,
            list_files=list_files,
            export=export,
            action_prefix=action_prefix,
            expose_to_model=expose_to_model,
            max_file_bytes=max_file_bytes,
            max_search_file_bytes=max_search_file_bytes,
            desc=desc,
            desc_mode=desc_mode,
        )

    def set_action_loop(
        self,
        *,
        enabled: bool | None = None,
        max_rounds: int | None = None,
        concurrency: int | None = None,
        timeout: float | None = None,
    ) -> Self:
        if enabled is not None:
            self.settings.set("action.loop.enabled", bool(enabled))
            self.settings.set("tool.loop.enabled", bool(enabled))
        if max_rounds is not None:
            if not isinstance(max_rounds, int) or max_rounds < 0:
                raise ValueError("max_rounds must be an integer >= 0.")
            self.settings.set("action.loop.max_rounds", max_rounds)
            self.settings.set("tool.loop.max_rounds", max_rounds)
        if concurrency is not None:
            if not isinstance(concurrency, int) or concurrency <= 0:
                raise ValueError("concurrency must be an integer > 0.")
            self.settings.set("action.loop.concurrency", concurrency)
            self.settings.set("tool.loop.concurrency", concurrency)
        if timeout is not None:
            if not isinstance(timeout, (int, float)) or timeout <= 0:
                raise ValueError("timeout must be a number > 0.")
            self.settings.set("action.loop.timeout", float(timeout))
            self.settings.set("tool.loop.timeout", float(timeout))
        return self

    def set_tool_loop(
        self,
        *,
        enabled: bool | None = None,
        max_rounds: int | None = None,
        concurrency: int | None = None,
        timeout: float | None = None,
    ) -> Self:
        return self.set_action_loop(
            enabled=enabled,
            max_rounds=max_rounds,
            concurrency=concurrency,
            timeout=timeout,
        )

    def register_action_planning_handler(self, handler: Any) -> Self:
        self.__action_planning_handler = handler
        return self

    def register_tool_plan_analysis_handler(self, handler: Any) -> Self:
        return self.register_action_planning_handler(handler)

    def register_action_execution_handler(self, handler: Any) -> Self:
        self.__action_execution_handler = handler
        return self

    def register_tool_execution_handler(self, handler: Any) -> Self:
        return self.register_action_execution_handler(handler)

    async def async_generate_action_call(
        self,
        prompt: "Prompt | None" = None,
        *,
        done_plans: list["ActionResult"] | None = None,
        last_round_records: list["ActionResult"] | None = None,
        round_index: int = 0,
        max_rounds: int | None = None,
        planning_protocol: str | None = None,
    ) -> list["ActionCall"]:
        target_prompt = prompt if prompt is not None else self.request.prompt
        action_list = self._get_scoped_action_list()
        return await self.action.async_generate_action_call(
            prompt=target_prompt,
            settings=self.settings,
            action_list=action_list,
            agent_name=self.name,
            planning_handler=self.__action_planning_handler,
            done_plans=done_plans,
            last_round_records=last_round_records,
            round_index=round_index,
            max_rounds=max_rounds,
            planning_protocol=planning_protocol,
        )

    def generate_action_call(
        self,
        prompt: "Prompt | None" = None,
        *,
        done_plans: list["ActionResult"] | None = None,
        last_round_records: list["ActionResult"] | None = None,
        round_index: int = 0,
        max_rounds: int | None = None,
        planning_protocol: str | None = None,
    ) -> list["ActionCall"]:
        return FunctionShifter.syncify(self.async_generate_action_call)(
            prompt=prompt,
            done_plans=done_plans,
            last_round_records=last_round_records,
            round_index=round_index,
            max_rounds=max_rounds,
            planning_protocol=planning_protocol,
        )

    async def async_get_action_result(
        self,
        prompt: "Prompt | None" = None,
        *,
        max_rounds: int | None = None,
        concurrency: int | None = None,
        timeout: float | None = None,
        planning_protocol: str | None = None,
        store_for_reply: bool = True,
    ) -> list["ActionResult"]:
        target_prompt = prompt if prompt is not None else self.request.prompt
        action_list = self._get_scoped_action_list()
        if len(action_list) == 0:
            return []

        records = await self.action.async_plan_and_execute(
            prompt=target_prompt,
            settings=self.settings,
            action_list=action_list,
            agent_name=self.name,
            planning_handler=self.__action_planning_handler,
            action_execution_handler=self.__action_execution_handler,
            max_rounds=max_rounds,
            concurrency=concurrency,
            timeout=timeout,
            planning_protocol=planning_protocol,
        )
        if store_for_reply:
            action_results = self.action.to_action_results(records)
            target_prompt.set("action_results", action_results)
            if len(records) > 0:
                target_prompt.set("extra_instruction", self.action.ACTION_RESULT_QUOTE_NOTICE)
            self.__action_logs = records
            self.__prepared_action_results = action_results
        return records

    def get_action_result(
        self,
        prompt: "Prompt | None" = None,
        *,
        max_rounds: int | None = None,
        concurrency: int | None = None,
        timeout: float | None = None,
        planning_protocol: str | None = None,
        store_for_reply: bool = True,
    ) -> list["ActionResult"]:
        return FunctionShifter.syncify(self.async_get_action_result)(
            prompt=prompt,
            max_rounds=max_rounds,
            concurrency=concurrency,
            timeout=timeout,
            planning_protocol=planning_protocol,
            store_for_reply=store_for_reply,
        )

    async def async_generate_tool_command(
        self,
        prompt: "Prompt | None" = None,
        *,
        done_plans: list["ToolExecutionRecord"] | None = None,
        last_round_records: list["ToolExecutionRecord"] | None = None,
        round_index: int = 0,
        max_rounds: int | None = None,
    ) -> list["ToolCommand"]:
        target_prompt = prompt if prompt is not None else self.request.prompt
        tool_list = self.tool.get_tool_list(tags=[f"agent-{ self.name }"])
        return await self.tool.async_generate_tool_command(
            prompt=target_prompt,
            settings=self.settings,
            tool_list=tool_list,
            agent_name=self.name,
            plan_analysis_handler=self.__action_planning_handler,
            done_plans=done_plans,
            last_round_records=last_round_records,
            round_index=round_index,
            max_rounds=max_rounds,
        )

    def generate_tool_command(
        self,
        prompt: "Prompt | None" = None,
        *,
        done_plans: list["ToolExecutionRecord"] | None = None,
        last_round_records: list["ToolExecutionRecord"] | None = None,
        round_index: int = 0,
        max_rounds: int | None = None,
    ) -> list["ToolCommand"]:
        return FunctionShifter.syncify(self.async_generate_tool_command)(
            prompt=prompt,
            done_plans=done_plans,
            last_round_records=last_round_records,
            round_index=round_index,
            max_rounds=max_rounds,
        )

    async def async_must_call(
        self,
        prompt: "Prompt | None" = None,
        *,
        done_plans: list["ToolExecutionRecord"] | None = None,
        last_round_records: list["ToolExecutionRecord"] | None = None,
        round_index: int = 0,
        max_rounds: int | None = None,
    ) -> list["ToolCommand"]:
        DeprecationWarnings.warn_deprecated_once(
            "ActionExtension.async_must_call",
            "Method .async_must_call() is deprecated and will be removed in future version, "
            "please use .async_generate_tool_command() instead.",
            stacklevel=2,
        )
        return await self.async_generate_tool_command(
            prompt=prompt,
            done_plans=done_plans,
            last_round_records=last_round_records,
            round_index=round_index,
            max_rounds=max_rounds,
        )

    def must_call(
        self,
        prompt: "Prompt | None" = None,
        *,
        done_plans: list["ToolExecutionRecord"] | None = None,
        last_round_records: list["ToolExecutionRecord"] | None = None,
        round_index: int = 0,
        max_rounds: int | None = None,
    ) -> list["ToolCommand"]:
        DeprecationWarnings.warn_deprecated_once(
            "ActionExtension.must_call",
            "Method .must_call() is deprecated and will be removed in future version, "
            "please use .generate_tool_command() instead.",
            stacklevel=2,
        )
        return self.generate_tool_command(
            prompt=prompt,
            done_plans=done_plans,
            last_round_records=last_round_records,
            round_index=round_index,
            max_rounds=max_rounds,
        )

    async def __request_prefix(self, prompt: "Prompt", _settings):
        settings = _settings if _settings is not None else self.settings
        missing = object()
        existing_action_results = prompt.get("action_results", default=missing)
        if existing_action_results is not missing:
            if self.__prepared_action_results is not None and existing_action_results == self.__prepared_action_results:
                self.__prepared_action_results = None
            else:
                self.__action_logs = []
            has_action_results = bool(existing_action_results)
            if has_action_results and prompt.get("extra_instruction", default=missing) is missing:
                prompt.set("extra_instruction", self.action.ACTION_RESULT_QUOTE_NOTICE)
            return

        self.__action_logs = []
        if settings.get("action.loop.enabled", settings.get("tool.loop.enabled", True)) is not True:
            return

        action_list = self._get_scoped_action_list()
        if len(action_list) == 0:
            return

        records = await self.action.async_plan_and_execute(
            prompt=prompt,
            settings=settings,
            action_list=action_list,
            agent_name=self.name,
            planning_handler=self.__action_planning_handler,
            action_execution_handler=self.__action_execution_handler,
            max_rounds=settings.get("action.loop.max_rounds", settings.get("tool.loop.max_rounds", None)),  # type: ignore[arg-type]
            concurrency=settings.get("action.loop.concurrency", settings.get("tool.loop.concurrency", None)),  # type: ignore[arg-type]
            timeout=settings.get("action.loop.timeout", settings.get("tool.loop.timeout", None)),  # type: ignore[arg-type]
        )

        if len(records) > 0:
            prompt.set("action_results", self.action.to_action_results(records))
            prompt.set("extra_instruction", self.action.ACTION_RESULT_QUOTE_NOTICE)
            self.__action_logs = records

    async def __broadcast_prefix(self, full_result_data: "AgentlyModelResult", _):
        if len(self.__action_logs) == 0:
            return

        tool_logs = [log for log in self.__action_logs if log.get("expose_to_model", True)]

        for action_log in self.__action_logs:
            yield "action", action_log
        for tool_log in tool_logs:
            yield "tool", tool_log

        if "extra" not in full_result_data:
            full_result_data["extra"] = {}
        if isinstance(full_result_data["extra"], dict) and "action_logs" not in full_result_data["extra"]:
            full_result_data["extra"]["action_logs"] = []
        if isinstance(full_result_data["extra"], dict) and "tool_logs" not in full_result_data["extra"]:
            full_result_data["extra"]["tool_logs"] = []
        if (
            "extra" in full_result_data
            and isinstance(full_result_data["extra"], dict)
            and isinstance(full_result_data["extra"].get("action_logs"), list)
        ):
            full_result_data["extra"]["action_logs"].extend(self.__action_logs)
        if (
            "extra" in full_result_data
            and isinstance(full_result_data["extra"], dict)
            and isinstance(full_result_data["extra"].get("tool_logs"), list)
        ):
            full_result_data["extra"]["tool_logs"].extend(tool_logs)
        self.__action_logs = []
