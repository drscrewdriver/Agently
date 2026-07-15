---
title: Sandbox Plugin
description: Agently Sandbox Plugin System — Container-level Isolated Execution Environment.
keywords: Agently, sandbox, Docker, gVisor, container, isolation, security
---

# Sandbox Plugin

> Language: **English** · [中文](../../cn/sandbox/README.md)

The Sandbox Plugin provides container-level isolated execution environments for Agent actions.

## Overview

The sandbox plugin system offers two isolation levels:

| Isolation Level | Backend | Isolation Mechanism | Use Case |
|-----------------|---------|---------------------|----------|
| `CONTAINER` | `DockerSandboxBackend` | Linux namespace + cgroup | General sandbox tasks |
| `ENHANCED_CONTAINER` | `GVisorSandboxBackend` | User-space kernel syscall interception | High-security scenarios |

## Module Structure

```text
agently/builtins/sandbox/
├── protocol.py          # Core protocol type definitions
├── docker_backend.py    # Docker runtime backend
├── gvisor_backend.py    # gVisor runtime backend
├── network_policy.py    # Network policy engine
└── events.py            # Event emitter
```

## Core Protocol

### IsolationLevel

```python
from agently.builtins.sandbox.protocol import IsolationLevel

IsolationLevel.CONTAINER           # Standard container isolation
IsolationLevel.ENHANCED_CONTAINER  # gVisor enhanced isolation
```

### SandboxConfig

```python
from agently.builtins.sandbox.protocol import SandboxConfig

config = SandboxConfig(
    # Container configuration
    image="python:3.12-slim",
    isolation_level=IsolationLevel.CONTAINER,
    
    # Resource limits
    memory_limit="512m",
    cpu_limit=1.0,
    timeout=30,
    
    # Network policy
    network_enabled=False,
    network_allowlist=[],
    block_cloud_metadata=True,
    
    # Filesystem
    read_only_fs=True,
    writable_tmp_size="100m",
    
    # Permissions
    drop_capabilities=True,
    no_new_privileges=True,
)
```

### SandboxResult

```python
from agently.builtins.sandbox.protocol import SandboxResult

result: SandboxResult = await session.execute("echo hello")
print(result.exit_code)   # 0
print(result.stdout)      # "hello\n"
print(result.success)     # True
```

## Backend Usage

### DockerSandboxBackend

```python
import asyncio
from agently.builtins.sandbox.docker_backend import DockerSandboxBackend
from agently.builtins.sandbox.protocol import SandboxConfig

async def main():
    backend = DockerSandboxBackend()
    config = SandboxConfig(timeout=30)
    
    # Create session
    session = await backend.create_session(config)
    print(f"Session: {session.session_id}")
    
    # Execute command
    result = await session.execute("echo hello")
    print(result.stdout)
    
    # Execute Python code
    result = await session.execute_python("print(2 + 2)")
    
    # Cleanup
    await session.close()
    await backend.destroy_session(session.session_id)

asyncio.run(main())
```

### GVisorSandboxBackend

```python
import asyncio
from agently.builtins.sandbox.gvisor_backend import GVisorSandboxBackend
from agently.builtins.sandbox.protocol import SandboxConfig

async def main():
    backend = GVisorSandboxBackend()
    config = SandboxConfig(timeout=30)
    
    # Create gVisor session (uses runsc runtime)
    session = await backend.create_session(config)
    print(f"Isolation: {session.isolation_level}")  # ENHANCED_CONTAINER
    
    # Execute command (inside gVisor sandbox)
    result = await session.execute("echo hello from gVisor")
    
    # Cleanup
    await session.close()
    await backend.destroy_session(session.session_id)

asyncio.run(main())
```

## Network Policy

```python
from agently.builtins.sandbox.network_policy import NetworkPolicyEngine
from agently.builtins.sandbox.protocol import SandboxConfig

engine = NetworkPolicyEngine()

# Network disabled configuration
config = SandboxConfig(network_enabled=False)
policy = engine.evaluate(config)
print(policy.allowed)  # False

# Network allowlist configuration
config = SandboxConfig(
    network_enabled=True,
    network_allowlist=["pypi.org", "github.com"],
)
policy = engine.evaluate(config)
print(policy.allowed_domains)  # ["pypi.org", "github.com"]
```

## SandboxActionExecutor

Unified sandbox executor compliant with Agently Action Executor protocol:

```python
from agently.builtins.plugins.ActionExecutor.SandboxActionExecutor import SandboxActionExecutor

executor = SandboxActionExecutor(
    backend="docker",           # "docker" or "gvisor"
    default_image="python:3.12-slim",
    network_enabled=False,
)

# Protocol attributes
print(f"kind: {executor.kind}")          # "sandbox"
print(f"sandboxed: {executor.sandboxed}")  # True

# Execute action
result = await executor.execute(
    spec={"action_id": "sandbox"},
    action_call={"action_input": {"cmd": "echo hello"}},
    policy={"timeout_seconds": 60},
    settings=None,
)
```

## Event System

```python
from agently.builtins.sandbox.events import (
    emit_session_created,
    emit_execution_completed,
    emit_policy_violation,
)

# Events are emitted via Agently event bus, subscribable at Agent layer
```

## Environment Requirements

### Docker Mode
- Docker >= 20.x
- No additional configuration required

### gVisor Mode
- Docker >= 20.x
- gVisor (runsc) installed and registered with Docker

Installing gVisor:

```bash
# Download runsc
wget -O runsc https://storage.googleapis.com/gvisor/releases/release/latest/x86_64/runsc
sudo install -m 0755 runsc /usr/local/bin/runsc

# Configure Docker daemon
sudo tee /etc/docker/daemon.json > /dev/null << 'EOF'
{
  "runtimes": {
    "runsc": {
      "path": "/usr/local/bin/runsc",
      "runtimeArgs": []
    }
  }
}
EOF

# Restart Docker
sudo systemctl restart docker

# Verify
runsc --version
docker run --runtime=runsc --rm hello-world
```

## Identifying Runtime

```bash
# Check container runtime
docker inspect <container_id> --format '{{.HostConfig.Runtime}}'
# runc = Docker mode, runsc = gVisor mode

# Check kernel
docker exec <container_id> uname -r
# Host kernel = Docker, 4.19.0-gvisor = gVisor
```

## Related Documentation

- [Actions](../actions/README.md) — Action execution architecture
- [Execution Resource](../actions/execution-environment.md) — Managed execution resource layer
- [Architecture](../architecture/README.md) — System architecture overview
