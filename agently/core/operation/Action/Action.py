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

# ── Action module responsibilities ────────────────────────────────────────────
# This module owns the Action runtime surface: registration, execution dispatch,
# artifact management, sandbox bootstrapping, MCP integration, decision
# normalization, and tool-compat aliases. It is intentionally large because
# Action is the single first-class executable abstraction.
#
# When adding new functionality, prefer:
#   - ActionDispatcher    for execution, policy merge, environment provisioning
#   - ActionRegistry      for spec/executor registration and tag indexing
#   - A new focused module under core/Action/ when the concern is independent
#     (e.g. artifact lifecycle, redaction/compaction, MCP transport).
#
# DO NOT add more tool-compat aliases — the tool surface is deprecated and
# exists only for migration compatibility.
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Callable,
    Coroutine,
    Literal,
    ParamSpec,
    TypeVar,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

from agently.types.data import (
    ActionArtifact,
    ActionCall,
    ActionDecision,
    ActionExecutionRequest,
    ActionPlanningRequest,
    ActionPolicy,
    ActionResult,
    ActionRunContext,
    ActionSpec,
    ExecutionResourcePolicy,
    ExecutionResourceRequirement,
)
from agently.types.plugins import (
    ActionExecutionHandler,
    ActionPlanningHandler,
    StandardActionExecutionHandler,
    StandardActionPlanningHandler,
)
from agently.utils import DeprecationWarnings, FunctionShifter, Settings, SettingsNamespace
from agently.utils import DataFormatter

from .ActionArtifactManager import ActionArtifactManager
from .ActionDispatcher import ActionDispatcher
from .ActionFlowController import ActionFlowController
from .ActionMetadata import sanitize_action_spec_for_metadata, summarize_action_records
from .ActionResourceRegistrar import ActionResourceRegistrar
from .ActionNormalization import (
    is_execution_error_result,
    is_next_action_path,
    normalize_action_call,
    normalize_action_decision,
    normalize_execution_record,
    normalize_native_action_calls,
    parse_native_arguments,
    should_continue,
    to_action_results,
)
from .ActionRegistry import ActionRegistry

if TYPE_CHECKING:
    from agently.core import PluginManager, Prompt
    from agently.types.data import MCPConfigs, KwargsType, ReturnType

P = ParamSpec("P")
R = TypeVar("R")


class _DeprecatedActionManagerProxy:
    def __init__(self, action: "Action", name: str):
        self._action = action
        self._name = name

    def __getattr__(self, item: str):
        DeprecationWarnings.warn_deprecated_once(
            f"Action.{ self._name }.proxy",
            f"Action.{ self._name } is deprecated. Use Action directly; `tool` remains only as a public surface alias.",
            stacklevel=2,
        )
        return getattr(self._action, item)


class Action:
    ACTION_RESULT_QUOTE_NOTICE = (
        "NOTICE: MUST QUOTE KEY INFO OR MARK SOURCE (PREFER URL INCLUDED) FROM {action_results} "
        "IN REPLY IF YOU USE {action_results} TO IMPROVE REPLY!"
    )
    TOOL_RESULT_QUOTE_NOTICE = ACTION_RESULT_QUOTE_NOTICE

    def __init__(
        self,
        plugin_manager: "PluginManager",
        parent_settings: "Settings",
    ):
        self.plugin_manager = plugin_manager
        self.settings = Settings(
            name="Action-Settings",
            parent=parent_settings,
        )
        self.action_settings = SettingsNamespace(self.settings, "action")
        self.action_settings.setdefault("loop.max_rounds", None)
        self.action_settings.setdefault("loop.concurrency", None)
        self.action_settings.setdefault("loop.timeout", None)
        self.action_settings.setdefault("loop.max_consecutive_failed_rounds_per_action", 2)
        self.action_settings.setdefault("protocol", "structured_plan")
        self.action_settings.setdefault("planning_model_key", None)
        self.action_settings.setdefault("policy.global", {})
        self.action_settings.setdefault("policy.agent", {})

        self.tool_settings = SettingsNamespace(self.settings, "tool")
        self.tool_settings.setdefault("loop.max_rounds", None)
        self.tool_settings.setdefault("loop.concurrency", None)
        self.tool_settings.setdefault("loop.timeout", None)
        self.tool_settings.setdefault("loop.max_consecutive_failed_rounds_per_action", 2)

        self.action_registry = ActionRegistry(name="ActionRegistry")
        self.action_dispatcher = ActionDispatcher(self.action_registry, self.settings)
        self.action_funcs: dict[str, Callable[..., Any]] = {}
        self.tool_funcs = self.action_funcs
        self._artifact_manager = ActionArtifactManager(registry=self.action_registry)
        self._resource_registrar = ActionResourceRegistrar(self)
        self._flow_controller = ActionFlowController(self)
        self._action_artifacts = self._artifact_manager._artifacts  # backward-compat alias
        self._deprecated_action_manager = _DeprecatedActionManagerProxy(self, "action_manager")
        self._deprecated_tool_manager = _DeprecatedActionManagerProxy(self, "tool_manager")

        self.action_runtime = self._create_action_runtime()
        self.runtime = self.action_runtime
        self.action_flow = self._create_action_flow()
        self.flow = self.action_flow
        self._register_action_artifact_recall_action()

        self.plan_and_execute = FunctionShifter.syncify(self.async_plan_and_execute)
        self.generate_action_call = FunctionShifter.syncify(self.async_generate_action_call)
        self.generate_tool_command = FunctionShifter.syncify(self.async_generate_tool_command)
        self.use_action_mcp = FunctionShifter.syncify(self.async_use_action_mcp)
        self.use_mcp = FunctionShifter.syncify(self.async_use_mcp)
        self.read_action_artifact = FunctionShifter.syncify(self.async_read_action_artifact)

    def _register_action_artifact_recall_action(self):
        self.register_action(
            action_id="read_action_artifact",
            desc=(
                "Read full raw input, output, code, command, SQL, page, or log content "
                "from a previous Action call by artifact reference when the execution "
                "digest is not enough."
            ),
            kwargs={
                "artifact_id": (str, "Artifact id from a previous Action execution digest."),
                "action_call_id": (str, "Optional Action call id that should own this artifact."),
            },
            func=self.async_read_action_artifact,
            side_effect_level="read",
            expose_to_model=False,
            meta={
                "component": "action_loop_execution_recall",
                "auto_exposed_after_artifact": True,
            },
        )
        return self

    async def async_read_action_artifact(
        self,
        artifact_id: str,
        action_call_id: str | None = None,
    ) -> dict[str, Any]:
        from agently.base import async_emit_runtime
        from agently.types.data import ObservationEvent

        artifact = self._action_artifacts.get(str(artifact_id))
        if artifact is None:
            result = {
                "ok": False,
                "status": "not_found",
                "artifact_id": str(artifact_id),
                "error": "Action artifact was not found or is no longer retained.",
            }
        elif action_call_id and str(artifact.get("action_call_id", "")) != str(action_call_id):
            result = {
                "ok": False,
                "status": "forbidden",
                "artifact_id": str(artifact_id),
                "action_call_id": str(action_call_id),
                "error": "Action artifact does not belong to the requested action_call_id.",
            }
        else:
            value = artifact.get("value")
            result = {
                "ok": True,
                "status": "success",
                "artifact_id": str(artifact_id),
                "action_call_id": artifact.get("action_call_id", ""),
                "artifact_type": artifact.get("artifact_type", ""),
                "label": artifact.get("label", ""),
                "media_type": artifact.get("media_type", ""),
                "value": value,
                "data": value,
                "result": value,
                "meta": artifact.get("meta", {}),
            }

        await async_emit_runtime(
            ObservationEvent(
                event_type="action.artifact_read",
                source="ActionRuntime",
                level="INFO" if result.get("ok") else "WARNING",
                message=f"Action artifact '{ artifact_id }' read.",
                payload={
                    "artifact_id": str(artifact_id),
                    "action_call_id": action_call_id,
                    "status": result.get("status"),
                    "ok": result.get("ok"),
                },
            )
        )
        return result

    @property
    def action_manager(self):
        DeprecationWarnings.warn_deprecated_once(
            "Action.action_manager",
            "Action.action_manager is deprecated. Use Action directly.",
            stacklevel=2,
        )
        return self._deprecated_action_manager

    @property
    def tool_manager(self):
        DeprecationWarnings.warn_deprecated_once(
            "Action.tool_manager",
            "Action.tool_manager is deprecated. Use Action directly; `tool` remains a public surface alias.",
            stacklevel=2,
        )
        return self._deprecated_tool_manager

    @staticmethod
    def _normalize_tags(tags: str | list[str] | None):
        if tags is None:
            return []
        if isinstance(tags, str):
            return [tags]
        return [str(tag) for tag in tags]

    def create_action_executor(self, plugin_name: str, **kwargs) -> Any:
        plugin_class = cast(type[Any], self.plugin_manager.get_plugin("ActionExecutor", plugin_name))
        return plugin_class(**kwargs)

    def _create_executor(self, plugin_name: str, **kwargs) -> Any:
        return self.create_action_executor(plugin_name, **kwargs)

    @staticmethod
    def _sanitize_action_spec(
        *,
        action_id: str,
        desc: str | None,
        kwargs: "KwargsType | None",
        returns: "ReturnType | None",
        tags: list[str],
        default_policy: "ActionPolicy | None",
        side_effect_level: str,
        approval_required: bool,
        sandbox_required: bool,
        replay_safe: bool,
        expose_to_model: bool,
        executor_type: str,
        execution_resources: list[ExecutionResourceRequirement] | None,
        meta: dict[str, Any] | None,
    ) -> "ActionSpec":
        spec = cast(ActionSpec, {
            "action_id": action_id,
            "name": action_id,
            "desc": desc if desc is not None else "",
            "kwargs": kwargs if kwargs is not None else {},
            "tags": tags,
            "default_policy": default_policy if default_policy is not None else {},
            "side_effect_level": side_effect_level,
            "approval_required": approval_required,
            "sandbox_required": sandbox_required,
            "replay_safe": replay_safe,
            "expose_to_model": expose_to_model,
            "executor_type": executor_type,
            "execution_resources": execution_resources if execution_resources is not None else [],
            "meta": meta if meta is not None else {},
        })
        if returns is not None:
            spec["returns"] = returns
        return spec

    def register_action(
        self,
        *,
        action_id: str,
        desc: str | None,
        kwargs: "KwargsType | None",
        func: Callable[..., Any] | None = None,
        executor=None,
        returns: "ReturnType | None" = None,
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        side_effect_level: Literal["read", "write", "exec"] = "read",
        approval_required: bool = False,
        sandbox_required: bool = False,
        replay_safe: bool = True,
        expose_to_model: bool = True,
        execution_resources: list[ExecutionResourceRequirement] | None = None,
        meta: dict[str, Any] | None = None,
    ):
        if executor is None:
            if func is None:
                raise ValueError("register_action() requires either func or executor.")
            executor = self._create_executor("LocalFunctionActionExecutor", func=func)
        normalized_tags = self._normalize_tags(tags)
        executor_type = str(getattr(executor, "kind", "function"))
        spec = self._sanitize_action_spec(
            action_id=action_id,
            desc=desc,
            kwargs=kwargs,
            returns=returns,
            tags=normalized_tags,
            default_policy=default_policy,
            side_effect_level=side_effect_level,
            approval_required=approval_required,
            sandbox_required=sandbox_required,
            replay_safe=replay_safe,
            expose_to_model=expose_to_model,
            executor_type=executor_type,
            execution_resources=execution_resources,
            meta=meta,
        )
        self.action_registry.register(spec, executor, func=func)
        if func is not None:
            self.action_funcs[action_id] = func
        return self

    def register(
        self,
        *,
        name: str | None = None,
        action_id: str | None = None,
        desc: str | None,
        kwargs: "KwargsType | None",
        func: Callable[..., Any],
        returns: "ReturnType | None" = None,
        tags: str | list[str] | None = None,
    ):
        resolved_name = action_id if isinstance(action_id, str) and action_id.strip() != "" else name
        if not isinstance(resolved_name, str) or resolved_name.strip() == "":
            raise ValueError("register() requires either name or action_id.")
        self.register_action(
            action_id=resolved_name,
            desc=desc,
            kwargs=kwargs,
            func=func,
            returns=returns,
            tags=tags,
        )
        return self

    def tag(self, action_ids: str | list[str], tags: str | list[str]):
        self.action_registry.tag(action_ids, tags)
        return self

    def unregister_action(self, action_ids: str | list[str]) -> list[str]:
        """Unregister one or more actions, returning the ids actually removed.

        Reverses scoped registrations (e.g. capability mounts) so a one-time
        registration does not persist on the host.
        """
        ids = self._normalize_registered_action_ids(action_ids)
        removed: list[str] = []
        for action_id in ids:
            if self.action_registry.unregister(action_id):
                self.action_funcs.pop(action_id, None)
                removed.append(action_id)
        return removed

    def action_func(self, func: Callable[P, R]) -> Callable[P, R]:
        action_id = func.__name__
        desc = inspect.getdoc(func) or func.__name__
        signature = inspect.signature(func)
        type_hints = get_type_hints(func)
        returns = None
        if "return" in type_hints:
            returns = DataFormatter.sanitize(type_hints["return"], remain_type=True)
        kwargs_signature = {}
        for param_name, param in signature.parameters.items():
            annotated_type = param.annotation
            if get_origin(annotated_type) is Annotated:
                base_type, *annotations = get_args(annotated_type)
            else:
                base_type = annotated_type
                annotations = []
            if param.default != inspect.Parameter.empty:
                annotations.append(f"Default: { param.default }")
            kwargs_signature[param_name] = (base_type, ";".join(annotations))
        self.register_action(
            action_id=action_id,
            desc=desc,
            kwargs=kwargs_signature,
            func=func,
            returns=returns,
        )
        return func

    def tool_func(self, func: Callable[P, R]) -> Callable[P, R]:
        return self.action_func(func)

    @staticmethod
    def _normalize_action_items(actions: Any) -> list[Any]:
        if isinstance(actions, str) or callable(actions) or hasattr(actions, "register_actions"):
            return [actions]
        if isinstance(actions, (list, tuple, set)):
            return list(actions)
        return [actions]

    @staticmethod
    def _normalize_registered_action_ids(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value if str(item)]
        return []

    def use_actions(
        self,
        actions: Callable | str | list[str | Callable] | Any,
        *,
        tags: str | list[str] | None = None,
    ):
        action_ids: list[str] = []
        for action_item in self._normalize_action_items(actions):
            register_actions = getattr(action_item, "register_actions", None)
            if callable(register_actions):
                action_ids.extend(self._normalize_registered_action_ids(register_actions(self, tags=tags)))
                continue
            if isinstance(action_item, str):
                if not self.action_registry.has(action_item):
                    raise ValueError(f"Can not find action named '{ action_item }'")
                action_ids.append(action_item)
                continue
            if callable(action_item):
                action_name = getattr(action_item, "__name__", "")
                if not action_name:
                    raise ValueError("Callable action must have a __name__.")
                if action_name not in self.action_funcs and not self.action_registry.has(action_name):
                    self.action_func(action_item)
                action_ids.append(action_name)
                continue
            raise TypeError("use_actions() expects action names, callables, or built-in action packages.")
        if tags and action_ids:
            self.tag(action_ids, tags)
        return self

    def use_tools(self, tools: Callable | str | list[str | Callable] | Any, *, tags: str | list[str] | None = None):
        return self.use_actions(tools, tags=tags)

    _RECALL_ACTION_ID = "read_action_artifact"
    _INSTRUCTION_HEAVY_EXECUTOR_TYPES = {
        "bash_sandbox",
        "python_sandbox",
        "nodejs",
        "docker",
        "sqlite",
        "browse",
        "search",
    }
    _INSTRUCTION_HEAVY_KWARGS = {
        "cmd",
        "command",
        "python_code",
        "js_code",
        "code",
        "query",
        "sql",
        "url",
    }
    _SENSITIVE_KEYWORDS = {
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "header",
        "password",
        "secret",
        "token",
    }

    @classmethod
    def _is_sensitive_key(cls, key: Any):
        return ActionArtifactManager._is_sensitive_key(key)

    @staticmethod
    def _compact_text(value: Any, *, limit: int = 4000):
        return ActionArtifactManager._compact_text(value, limit=limit)

    @classmethod
    def _compact_value(cls, value: Any, *, limit: int = 4000, depth: int = 0):
        return ActionArtifactManager._compact_value(value, limit=limit, depth=depth)

    @classmethod
    def _redaction_report_for_value(cls, value: Any, *, path: str = "") -> list[str]:
        return ActionArtifactManager._redaction_report_for_value(value, path=path)

    @classmethod
    def _redact_value(cls, value: Any):
        return ActionArtifactManager._redact_value(value)

    @classmethod
    def _safe_json_size(cls, value: Any):
        return ActionArtifactManager._safe_json_size(value)

    def _register_execution_artifact(
        self,
        *,
        action_call_id: str,
        artifact_type: str,
        label: str,
        value: Any,
        media_type: str = "application/json",
        meta: dict[str, Any] | None = None,
    ) -> ActionArtifact:
        return self._artifact_manager.register_execution_artifact(
            action_call_id=action_call_id,
            artifact_type=artifact_type,
            label=label,
            value=value,
            media_type=media_type,
            meta=meta,
        )

    def _is_instruction_heavy_record(self, record: "ActionResult"):
        return self._artifact_manager._is_instruction_heavy_record(record)

    def _summarize_action_instruction(self, record: "ActionResult"):
        return self._artifact_manager._summarize_action_instruction(record)

    def _build_execution_digest(self, record: "ActionResult", *, artifact_refs: list[ActionArtifact], redaction_report: list[str]) -> dict[str, Any]:
        return self._artifact_manager._build_execution_digest(record, artifact_refs=artifact_refs, redaction_report=redaction_report)

    def _finalize_action_result(self, result: Any) -> "ActionResult":
        return self._artifact_manager.finalize_action_result(result)

    @classmethod
    def _to_model_visible_record(cls, record: "ActionResult") -> "ActionResult":
        return ActionArtifactManager._to_model_visible_record(record)

    @classmethod
    def to_model_visible_records(cls, records: list["ActionResult"] | None):
        return ActionArtifactManager.to_model_visible_records(records)

    def _with_action_artifact_recall_action(self, action_list: list[dict[str, Any]], records: list["ActionResult"] | None):
        return self._artifact_manager.with_action_artifact_recall_action(action_list, records)

    def _iter_action_ids(self, tags: str | list[str] | None = None, *, expose_only: bool = True):
        if tags is None:
            action_ids = self.action_registry.list_action_ids()
            collected = []
            for action_id in action_ids:
                spec = self.action_registry.get_spec(action_id)
                if spec is None:
                    continue
                if expose_only and spec.get("expose_to_model", True) is not True:
                    continue
                collected.append(action_id)
            return collected

        action_ids = self.action_registry.list_action_ids(tags)
        collected = []
        for action_id in action_ids:
            spec = self.action_registry.get_spec(action_id)
            if spec is None:
                continue
            if expose_only and spec.get("expose_to_model", True) is not True:
                continue
            collected.append(action_id)
        return collected

    def get_action_info(self, tags: str | list[str] | None = None):
        action_info: dict[str, dict[str, Any]] = {}
        for action_id in self._iter_action_ids(tags, expose_only=True):
            spec = self.action_registry.get_spec(action_id)
            if spec is None:
                continue
            action_info[action_id] = sanitize_action_spec_for_metadata(spec)
        return action_info

    def summarize_records(
        self,
        records: list["ActionResult"] | list[dict[str, Any]] | None,
        *,
        validation_command_markers: list[str] | tuple[str, ...] | None = None,
        validation_command_predicate: Callable[[str, dict[str, Any]], bool] | None = None,
    ):
        return summarize_action_records(
            records,
            validation_command_markers=validation_command_markers,
            validation_command_predicate=validation_command_predicate,
        )

    def get_tool_info(self, tags: str | list[str] | None = None):
        tool_info: dict[str, dict[str, Any]] = {}
        for action_id, spec in self.get_action_info(tags).items():
            tool_spec = {
                "name": spec.get("name", action_id),
                "desc": spec.get("desc", ""),
                "kwargs": spec.get("kwargs", {}),
            }
            if "returns" in spec:
                tool_spec["returns"] = spec["returns"]
            tool_info[action_id] = tool_spec
        return tool_info

    def get_action_list(self, tags: str | list[str] | None = None):
        return list(self.get_action_info(tags).values())

    def get_tool_list(self, tags: str | list[str] | None = None):
        return list(self.get_tool_info(tags).values())

    def get_action_func(
        self,
        name: str,
        *,
        shift: Literal["sync", "async"] | None = None,
    ) -> Callable[..., Coroutine] | Callable[..., Any] | None:
        action_func = self.action_funcs[name] if name in self.action_funcs else None
        if action_func is None and self.action_registry.has(name):

            async def _call_action(**kwargs):
                return await self.async_call_action(name, kwargs)

            action_func = _call_action
        if action_func is None:
            return None
        match shift:
            case "sync":
                return FunctionShifter.syncify(action_func)
            case "async":
                return FunctionShifter.asyncify(action_func)
            case None:
                return action_func

    def get_tool_func(
        self,
        name: str,
        *,
        shift: Literal["sync", "async"] | None = None,
    ) -> Callable[..., Coroutine] | Callable[..., Any] | None:
        return self.get_action_func(name, shift=shift)

    def _legacy_error(self, name: str, *, as_tool: bool):
        subject = "tool" if as_tool else "action"
        return f"Can not find { subject } named '{ name }'"

    def _legacy_result(self, result: Any, *, as_tool: bool):
        if not isinstance(result, dict):
            return None
        status = str(result.get("status", "success"))
        data = result.get("data", result.get("result"))
        error = str(result.get("error", ""))
        if status in {"success", "partial_success"}:
            return data
        if isinstance(data, dict) and "error" in data:
            return data
        if status in {"approval_required", "blocked"}:
            return {
                "status": status,
                "error": error or f"{'Tool' if as_tool else 'Action'} execution is blocked.",
                "approval": result.get("approval", {}),
            }
        label = "Tool" if as_tool else "Action"
        return f"Error: { error if error else f'{ label } execution failed.' }"

    async def async_execute_action(
        self,
        name: str,
        kwargs: dict[str, Any],
        *,
        settings: "Settings | None" = None,
        purpose: str | None = None,
        policy_override: "ActionPolicy | None" = None,
        trusted_policy_override: "ActionPolicy | None" = None,
        source_protocol: str = "direct",
        todo_suggestion: str = "",
        next_value: str = "",
    ):
        result = await self.action_dispatcher.async_execute(
            name,
            kwargs,
            settings=settings,
            purpose=purpose,
            policy_override=policy_override,
            trusted_policy_override=trusted_policy_override,
            source_protocol=source_protocol,
            todo_suggestion=todo_suggestion,
            next_value=next_value,
        )
        return self._finalize_action_result(result)

    def execute_action(self, name: str, kwargs: dict[str, Any], **kwargs_options):
        return FunctionShifter.syncify(self.async_execute_action)(name, kwargs, **kwargs_options)

    async def async_call_action(self, name: str, kwargs: dict[str, Any]) -> Any:
        if not self.action_registry.has(name):
            return self._legacy_error(name, as_tool=False)
        result = await self.async_execute_action(name, kwargs)
        return self._legacy_result(result, as_tool=False)

    def call_action(self, name: str, kwargs: dict[str, Any]) -> Any:
        return FunctionShifter.syncify(self.async_call_action)(name, kwargs)

    async def async_call_tool(self, name: str, kwargs: dict[str, Any]) -> Any:
        if not self.action_registry.has(name):
            return self._legacy_error(name, as_tool=True)
        result = await self.async_execute_action(name, kwargs)
        return self._legacy_result(result, as_tool=True)

    def call_tool(self, name: str, kwargs: dict[str, Any]) -> Any:
        return FunctionShifter.syncify(self.async_call_tool)(name, kwargs)

    async def async_use_action_mcp(
        self,
        transport: "MCPConfigs | str | Any",
        *,
        headers: dict[str, str] | None = None,
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        side_effect_level: Literal["read", "write", "exec"] = "read",
        approval_required: bool = False,
        sandbox_required: bool = False,
        replay_safe: bool = True,
        expose_to_model: bool = True,
    ):
        return await self._resource_registrar.async_use_action_mcp(
            transport,
            headers=headers,
            tags=tags,
            default_policy=default_policy,
            side_effect_level=side_effect_level,
            approval_required=approval_required,
            sandbox_required=sandbox_required,
            replay_safe=replay_safe,
            expose_to_model=expose_to_model,
        )

    async def async_use_mcp(
        self,
        transport: "MCPConfigs | str | Any",
        *,
        headers: dict[str, str] | None = None,
        tags: str | list[str] | None = None,
    ):
        return await self._resource_registrar.async_use_mcp(transport, headers=headers, tags=tags)

    def register_python_sandbox_action(
        self,
        *,
        action_id: str = "python_sandbox",
        desc: str = "Execute Python code through an explicitly trusted local Python execution resource.",
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        preset_objects: dict[str, object] | None = None,
        base_vars: dict[str, Any] | None = None,
        allowed_return_types: list[type] | None = None,
        sandbox: Literal["auto", "docker", "gvisor", "trusted_local"] = "trusted_local",
        docker_image: str = "python:3.12-slim",
        docker_binary: str = "docker",
        docker_default_args: list[str] | None = None,
        dependency_policy: Literal["deny", "request", "install"] | dict[str, Any] | str | None = None,
        provisioning_profile: Literal["strict", "developer", "ci"] | str = "strict",
        image_pull_policy: Literal["never", "request", "if_missing", "always"] | str | None = None,
        timeout: int = 60,
    ):
        return self._resource_registrar.register_python_sandbox_action(
            action_id=action_id,
            desc=desc,
            tags=tags,
            default_policy=default_policy,
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

    def register_bash_sandbox_action(
        self,
        *,
        action_id: str = "bash_sandbox",
        desc: str = "Execute a shell command inside a constrained sandbox.",
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        allowed_cmd_prefixes: list[str] | None = None,
        allowed_workdir_roots: list[str | Path] | None = None,
        timeout: int = 20,
        env: dict[str, str] | None = None,
        max_output_chars: int = 20000,
        output_artifact_dir: str | Path | None = None,
        sandbox: Literal["auto", "docker", "gvisor", "trusted_local"] = "trusted_local",
        docker_image: str = "python:3.12-slim",
        docker_binary: str = "docker",
        docker_default_args: list[str] | None = None,
        dependency_policy: Literal["deny", "request", "install"] | dict[str, Any] | str | None = None,
        provisioning_profile: Literal["strict", "developer", "ci"] | str = "strict",
        image_pull_policy: Literal["never", "request", "if_missing", "always"] | str | None = None,
    ):
        return self._resource_registrar.register_bash_sandbox_action(
            action_id=action_id,
            desc=desc,
            tags=tags,
            default_policy=default_policy,
            expose_to_model=expose_to_model,
            allowed_cmd_prefixes=allowed_cmd_prefixes,
            allowed_workdir_roots=allowed_workdir_roots,
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

    def register_nodejs_action(
        self,
        *,
        action_id: str = "run_nodejs",
        desc: str = "Execute JavaScript with Node.js inside a managed execution resource.",
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        node_binary: str = "node",
        cwd: str | None = None,
        timeout: int = 20,
        env: dict[str, str] | None = None,
        sandbox: Literal["auto", "docker", "gvisor", "trusted_local"] = "trusted_local",
        docker_image: str = "node:22-slim",
        docker_binary: str = "docker",
        docker_default_args: list[str] | None = None,
        dependency_policy: Literal["deny", "request", "install"] | dict[str, Any] | str | None = None,
        provisioning_profile: Literal["strict", "developer", "ci"] | str = "strict",
        image_pull_policy: Literal["never", "request", "if_missing", "always"] | str | None = None,
    ):
        return self._resource_registrar.register_nodejs_action(
            action_id=action_id,
            desc=desc,
            tags=tags,
            default_policy=default_policy,
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

    def register_code_runtime_action(
        self,
        *,
        language: str,
        action_id: str | None = None,
        desc: str | None = None,
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        docker_image: str | None = None,
        docker_binary: str = "docker",
        docker_default_args: list[str] | None = None,
        dependency_policy: Literal["deny", "request", "install"] | dict[str, Any] | str | None = None,
        provisioning_profile: Literal["strict", "developer", "ci"] | str = "strict",
        image_pull_policy: Literal["never", "request", "if_missing", "always"] | str | None = None,
        timeout: int = 60,
    ):
        return self._resource_registrar.register_code_runtime_action(
            language=language,
            action_id=action_id,
            desc=desc,
            tags=tags,
            default_policy=default_policy,
            expose_to_model=expose_to_model,
            docker_image=docker_image,
            docker_binary=docker_binary,
            docker_default_args=docker_default_args,
            dependency_policy=dependency_policy,
            provisioning_profile=provisioning_profile,
            image_pull_policy=image_pull_policy,
            timeout=timeout,
        )

    def register_docker_action(
        self,
        *,
        action_id: str = "run_docker",
        desc: str = "Run a command in a Docker container through a managed execution resource.",
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        image: str | None = None,
        timeout: int = 60,
        docker_binary: str = "docker",
        default_args: list[str] | None = None,
    ):
        return self._resource_registrar.register_docker_action(
            action_id=action_id,
            desc=desc,
            tags=tags,
            default_policy=default_policy,
            expose_to_model=expose_to_model,
            image=image,
            timeout=timeout,
            docker_binary=docker_binary,
            default_args=default_args,
        )

    def register_sqlite_action(
        self,
        *,
        action_id: str = "query_sqlite",
        desc: str = "Query a SQLite database through a managed execution resource.",
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        database: str = ":memory:",
        read_only: bool = True,
        uri: bool = False,
    ):
        return self._resource_registrar.register_sqlite_action(
            action_id=action_id,
            desc=desc,
            tags=tags,
            default_policy=default_policy,
            expose_to_model=expose_to_model,
            database=database,
            read_only=read_only,
            uri=uri,
        )

    def _create_action_runtime(self, plugin_name: str | None = None):
        return self._flow_controller.create_action_runtime(plugin_name)

    def create_action_runtime(self, plugin_name: str, **kwargs):
        return self._flow_controller.create_named_action_runtime(plugin_name, **kwargs)

    def _create_action_flow(self, plugin_name: str | None = None):
        return self._flow_controller.create_action_flow(plugin_name)

    def create_action_flow(self, plugin_name: str, **kwargs):
        return self._flow_controller.create_named_action_flow(plugin_name, **kwargs)

    def set_loop_options(
        self,
        *,
        max_rounds: int | None = None,
        concurrency: int | None = None,
        timeout: float | None = None,
    ):
        return self._flow_controller.set_loop_options(
            max_rounds=max_rounds,
            concurrency=concurrency,
            timeout=timeout,
        )

    def register_action_planning_handler(self, handler: "ActionPlanningHandler | None"):
        return self._flow_controller.register_action_planning_handler(handler)

    def register_plan_analysis_handler(self, handler: "ActionPlanningHandler | None"):
        return self.register_action_planning_handler(handler)

    def register_action_execution_handler(self, handler: "ActionExecutionHandler | None"):
        return self._flow_controller.register_action_execution_handler(handler)

    def register_tool_execution_handler(self, handler: "ActionExecutionHandler | None"):
        return self.register_action_execution_handler(handler)

    def _resolve_planning_protocol(self, settings: "Settings", planning_protocol: str | None = None):
        return self._flow_controller.resolve_planning_protocol(settings, planning_protocol)

    async def _default_structured_planning_handler(self, context: ActionRunContext, request: ActionPlanningRequest):
        return await self._flow_controller.default_structured_planning_handler(context, request)

    async def _default_native_tool_call_planning_handler(self, context: ActionRunContext, request: ActionPlanningRequest):
        return await self._flow_controller.default_native_tool_call_planning_handler(context, request)

    async def _default_planning_handler(self, context: ActionRunContext, request: ActionPlanningRequest):
        return await self._flow_controller.default_planning_handler(context, request)

    async def _default_plan_analysis_handler(self, context: ActionRunContext, request: ActionPlanningRequest):
        return await self._flow_controller.default_structured_planning_handler(context, request)

    async def _default_action_execution_handler(self, context: ActionRunContext, request: ActionExecutionRequest):
        return await self._flow_controller.default_action_execution_handler(context, request)

    async def _default_tool_execution_handler(self, context: ActionRunContext, request: ActionExecutionRequest):
        return await self._flow_controller.default_action_execution_handler(context, request)

    @staticmethod
    def _is_next_action_path(path: Any) -> bool:
        return is_next_action_path(path)

    @staticmethod
    async def _try_close_response_stream(response: Any):
        from agently.utils.GeneratorConsumer import GeneratorConsumer

        result = getattr(response, "result", None)
        parser = getattr(result, "_response_parser", None)
        consumer = getattr(parser, "_response_consumer", None)
        if isinstance(consumer, GeneratorConsumer):
            return
        close = getattr(consumer, "close", None)
        if callable(close):
            maybe_coroutine = close()
            if asyncio.iscoroutine(maybe_coroutine):
                await maybe_coroutine

    @staticmethod
    def _parse_native_arguments(raw_arguments: Any):
        return parse_native_arguments(raw_arguments)

    @classmethod
    def _normalize_native_action_calls(cls, tool_call_chunks: list[Any]) -> list[ActionCall]:
        return normalize_native_action_calls(tool_call_chunks)

    async def async_generate_action_call(
        self,
        *,
        prompt: "Prompt",
        settings: "Settings",
        action_list: list[dict[str, Any]],
        agent_name: str = "Manual",
        planning_handler: "ActionPlanningHandler | None" = None,
        done_plans: list[ActionResult] | None = None,
        last_round_records: list[ActionResult] | None = None,
        round_index: int = 0,
        max_rounds: int | None = None,
        planning_protocol: str | None = None,
    ) -> list[ActionCall]:
        return await self._flow_controller.async_generate_action_call(
            prompt=prompt,
            settings=settings,
            action_list=action_list,
            agent_name=agent_name,
            planning_handler=planning_handler,
            done_plans=done_plans,
            last_round_records=last_round_records,
            round_index=round_index,
            max_rounds=max_rounds,
            planning_protocol=planning_protocol,
        )

    async def async_generate_tool_command(
        self,
        *,
        prompt: "Prompt",
        settings: "Settings",
        tool_list: list[dict[str, Any]],
        agent_name: str = "Manual",
        plan_analysis_handler: "ActionPlanningHandler | None" = None,
        done_plans: list[ActionResult] | None = None,
        last_round_records: list[ActionResult] | None = None,
        round_index: int = 0,
        max_rounds: int | None = None,
    ) -> list[ActionCall]:
        return await self._flow_controller.async_generate_tool_command(
            prompt=prompt,
            settings=settings,
            tool_list=tool_list,
            agent_name=agent_name,
            plan_analysis_handler=plan_analysis_handler,
            done_plans=done_plans,
            last_round_records=last_round_records,
            round_index=round_index,
            max_rounds=max_rounds,
        )

    @staticmethod
    def is_execution_error_result(result: Any):
        return is_execution_error_result(result)

    @staticmethod
    def _normalize_action_call(command: Any, *, fallback_next: str | None = None) -> "ActionCall | None":
        return normalize_action_call(command, fallback_next=fallback_next)

    def _normalize_action_decision(self, decision: Any) -> "ActionDecision":
        return normalize_action_decision(decision)

    def _normalize_execution_record(self, record: Any, command: "ActionCall | None", index: int) -> "ActionResult":
        return normalize_execution_record(record, command, index)

    def _normalize_execution_records(self, records: Any, commands: list["ActionCall"]) -> list["ActionResult"]:
        return self._artifact_manager.normalize_execution_records(records, commands)

    @staticmethod
    def to_action_results(records: list["ActionResult"]):
        return to_action_results(records)

    @staticmethod
    def _should_continue(decision: "ActionDecision", *, round_index: int, max_rounds: int | None):
        return should_continue(decision, round_index=round_index, max_rounds=max_rounds)

    async def _async_emit_action_flow_observation(self, observation: dict[str, Any]):
        await self._flow_controller.async_emit_action_flow_observation(observation)

    async def async_plan_and_execute(
        self,
        *,
        prompt: "Prompt",
        settings: "Settings",
        action_list: list[dict[str, Any]] | None = None,
        tool_list: list[dict[str, Any]] | None = None,
        agent_name: str = "Manual",
        parent_run_context=None,
        planning_handler: "ActionPlanningHandler | None" = None,
        plan_analysis_handler: "ActionPlanningHandler | None" = None,
        action_execution_handler: "ActionExecutionHandler | None" = None,
        tool_execution_handler: "ActionExecutionHandler | None" = None,
        max_rounds: int | None = None,
        concurrency: int | None = None,
        timeout: float | None = None,
        planning_protocol: str | None = None,
    ) -> list["ActionResult"]:
        return await self._flow_controller.async_plan_and_execute(
            prompt=prompt,
            settings=settings,
            action_list=action_list,
            tool_list=tool_list,
            agent_name=agent_name,
            parent_run_context=parent_run_context,
            planning_handler=planning_handler,
            plan_analysis_handler=plan_analysis_handler,
            action_execution_handler=action_execution_handler,
            tool_execution_handler=tool_execution_handler,
            max_rounds=max_rounds,
            concurrency=concurrency,
            timeout=timeout,
            planning_protocol=planning_protocol,
        )


ToolCommand = ActionCall
ToolPlanDecision = ActionDecision
ToolExecutionRecord = ActionResult
ToolPlanAnalysisHandler = ActionPlanningHandler
StandardToolPlanAnalysisHandler = StandardActionPlanningHandler
ToolExecutionHandler = ActionExecutionHandler
StandardToolExecutionHandler = StandardActionExecutionHandler
Tool = Action
