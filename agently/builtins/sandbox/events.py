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
Sandbox event emission.

Emits RuntimeEvents for sandbox lifecycle and execution, integrating with
Agently's EventCenter for observability and traceability.
"""

from __future__ import annotations

from typing import Any

from agently.types.data.event import RuntimeEvent


# ---------------------------------------------------------------------------
# Sandbox event types
# ---------------------------------------------------------------------------

SANDBOX_EVENT_TYPES: dict[str, str] = {
    "sandbox.session_created": "Sandbox session created",
    "sandbox.execution_started": "Command execution started",
    "sandbox.execution_completed": "Command execution completed",
    "sandbox.execution_failed": "Command execution failed",
    "sandbox.execution_timeout": "Command execution timed out",
    "sandbox.policy_violation": "Security policy violation (command blocked)",
    "sandbox.network_blocked": "Network request blocked",
    "sandbox.session_destroyed": "Sandbox session destroyed",
}


# ---------------------------------------------------------------------------
# Event emission helpers
# ---------------------------------------------------------------------------

async def emit_sandbox_event(
    event_type: str,
    *,
    session_id: str,
    backend_name: str,
    isolation_level: str = "container",
    command: str | None = None,
    result: Any | None = None,
    policy_violation: str | None = None,
    extra: dict[str, Any] | None = None,
) -> RuntimeEvent:
    """
    Create a sandbox RuntimeEvent.

    Returns the event object.  Callers are responsible for dispatching it
    through the EventCenter (or logging it directly).

    Args:
        event_type: One of the SANDBOX_EVENT_TYPES keys.
        session_id: The sandbox session identifier.
        backend_name: The sandbox backend name (e.g. "DockerSandbox").
        isolation_level: The isolation level string.
        command: The command that was executed (if applicable).
        result: The SandboxResult (if applicable).
        policy_violation: Description of the policy violation (if applicable).
        extra: Additional payload data.

    Returns:
        A RuntimeEvent ready for dispatch.
    """
    payload: dict[str, Any] = {
        "session_id": session_id,
        "isolation_level": isolation_level,
    }

    if command is not None:
        payload["command"] = command

    if result is not None:
        if hasattr(result, "to_dict"):
            payload["result"] = result.to_dict()
        elif isinstance(result, dict):
            payload["result"] = result
        else:
            payload["result"] = str(result)

    if policy_violation is not None:
        payload["policy_violation"] = policy_violation

    if extra:
        payload.update(extra)

    level = "INFO"
    if "failed" in event_type or "violation" in event_type or "blocked" in event_type:
        level = "WARNING"
    elif "timeout" in event_type:
        level = "WARNING"

    return RuntimeEvent(
        event_type=event_type,
        source=f"SandboxBackend:{backend_name}",
        level=level,
        message=SANDBOX_EVENT_TYPES.get(event_type, event_type),
        payload=payload,
        meta={
            "sandbox_backend": backend_name,
            "sandboxed": True,
        },
    )


async def emit_session_created(
    *,
    session_id: str,
    backend_name: str,
    isolation_level: str = "container",
    image: str = "",
) -> RuntimeEvent:
    """Emit a session-created event."""
    return await emit_sandbox_event(
        "sandbox.session_created",
        session_id=session_id,
        backend_name=backend_name,
        isolation_level=isolation_level,
        extra={"image": image},
    )


async def emit_execution_completed(
    *,
    session_id: str,
    backend_name: str,
    isolation_level: str = "container",
    command: str | None = None,
    result: Any | None = None,
) -> RuntimeEvent:
    """Emit an execution-completed event."""
    return await emit_sandbox_event(
        "sandbox.execution_completed",
        session_id=session_id,
        backend_name=backend_name,
        isolation_level=isolation_level,
        command=command,
        result=result,
    )


async def emit_policy_violation(
    *,
    session_id: str,
    backend_name: str,
    violation: str,
    command: str | None = None,
) -> RuntimeEvent:
    """Emit a policy-violation event."""
    return await emit_sandbox_event(
        "sandbox.policy_violation",
        session_id=session_id,
        backend_name=backend_name,
        command=command,
        policy_violation=violation,
    )


async def emit_session_destroyed(
    *,
    session_id: str,
    backend_name: str,
    isolation_level: str = "container",
) -> RuntimeEvent:
    """Emit a session-destroyed event."""
    return await emit_sandbox_event(
        "sandbox.session_destroyed",
        session_id=session_id,
        backend_name=backend_name,
        isolation_level=isolation_level,
    )
