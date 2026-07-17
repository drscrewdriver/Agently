# gVisor Docker Runtime Extension

> Branch: `feature/gvisor-docker-runtime`
> Based on: Agently 4.1.4.1 (`bbcd6335`)
> Change scope: 2 files, +54 / -4 lines

---

## Overview

This branch injects gVisor (`runsc`) runtime support into the upstream `DockerExecutionResourceProvider` **without adding any external dependencies**.

gVisor is a Google-developed user-space kernel that intercepts system calls inside containers and implements them in application layer, providing stronger isolation than standard Docker (runc + seccomp/AppArmor).

---

## Differences from Upstream

### Changed Files

| File | Changes | Description |
|------|---------|-------------|
| `DockerExecutionResourceProvider.py` | +48 lines | New `runtime` parameter, `SUPPORTED_RUNTIMES` constant, `inspect_gvisor_availability()` method |
| `ActionResourceRegistrar.py` | +10 / -4 lines | `_normalize_code_sandbox` adds `"gvisor"` value; `_docker_runtime_requirement` adds `docker_runtime` parameter |

### Invasiveness Analysis

**Zero breaking changes** — all modifications are backward-compatible additions:

1. **Default behavior unchanged**: `runtime` parameter defaults to `"runc"`, identical to upstream when not specified
2. **No base class modifications**: `ExecutionResource` / `ExecutionResourceProvider` base classes untouched
3. **`create_handle` signature unchanged**: `runtime` passed via `config` dict, no new positional arguments
4. **`sandbox` enum parsing unchanged**: `"gvisor"` is just a new optional value

---

## Configuration

### 1. Via `sandbox` Parameter

```python
agent = Agent("sandbox_agent")

# Standard Docker sandbox (default, same as upstream)
agent \
    .register_docker_python_action(
        action_id="safe_run",
        sandbox="docker",
    )

# gVisor sandbox (new value)
agent \
    .register_docker_python_action(
        action_id="gvisor_run",
        sandbox="gvisor",  # ← new value
    )
```

### 2. Via `docker_runtime` Parameter

```python
agent \
    .register_docker_python_action(
        action_id="custom_runtime",
        docker_runtime="runsc",  # directly specify underlying runtime
    )
```

### 3. Via `config.yaml` Globally

```yaml
execution_resources:
  providers:
    docker:
      config:
        runtime: runsc          # "runc" (default) or "runsc"
        docker_binary: docker
        timeout: 60
```

---

## Runtime Detection

### Automatic gVisor Availability Check

```python
from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import (
    DockerExecutionResource,
)

info = DockerExecutionResource.inspect_gvisor_availability()
print(info)
# {
#     "available": True,
#     "runsc_path": "/usr/local/bin/runsc",
#     "runsc_version": "runsc version 20240408.0",
#     "registered_in_docker": True
# }
```

### Detection Logic

`inspect_gvisor_availability()` checks in order:

1. Whether `runsc` binary exists in `$PATH`
2. Whether `runsc --version` executes successfully
3. Whether Docker daemon has registered the `runsc` runtime (`docker info --format '{{json .Runtimes}}'`)

Only returns `"available": True` when all three checks pass.

---

## Implementation Details

### Runtime Parameter Propagation Chain

```
config["runtime"] → DockerExecutionResourceProvider.create_handle()
    → DockerExecutionResource(runtime="runsc")
        → _container_base_args() injects --runtime runsc
            → docker run --rm --runtime runsc ...
```

### Duplicate Injection Prevention

```python
if self.runtime != "runc" and "--runtime" not in " ".join(self.default_args):
    args.extend(["--runtime", self.runtime])
```

If user already passed `--runtime` via `default_args`, it won't be injected again.

### Invalid Runtime Fallback

```python
self.runtime = runtime if runtime in self.SUPPORTED_RUNTIMES else "runc"
```

Unsupported runtime values silently fall back to `"runc"` without crashing.

---

## Prerequisites

### Install gVisor

```bash
# Download runsc
ARCH=x86_64
sudo curl -L -o /usr/local/bin/runsc \
    "https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}/runsc"
sudo chmod +x /usr/local/bin/runsc

# Verify
runsc --version
```

### Register with Docker Daemon

Edit `/etc/docker/daemon.json`:

```json
{
    "runtimes": {
        "runsc": {
            "path": "/usr/local/bin/runsc"
        }
    }
}
```

Restart Docker:

```bash
sudo systemctl restart docker
```

### Verify Registration

```bash
docker info --format '{{json .Runtimes}}'
# Should include "runsc"
```

---

## Security Layer Comparison

| Layer | Standard Docker (runc) | Docker + gVisor (runsc) |
|-------|----------------------|------------------------|
| System calls | Direct to host kernel | runsc user-space interception |
| `/proc` `/sys` | Partial seccomp filtering | Full virtualization |
| Kernel exploit surface | Larger | Minimal |
| Performance overhead | ~0% | ~5-15% for I/O-intensive workloads |
| Startup latency | <1s after image pull | Same |
| Extra installation | No | Requires runsc binary |

---

## Relationship with `sandbox-extension-framework`

This branch contains **code changes only**, no design drafts.

For multi-platform sandbox extension design discussion (including gVisor, Seatbelt, bwrap, landlock, etc.), refer to the design documents in the `feature/sandbox-extension-framework` branch.

---

## Merge Recommendations

1. **Independently mergeable**: This branch has no dependencies on other feature branches
2. **Backward compatible**: Default `runtime="runc"` preserves upstream behavior
3. **Suggested order**: Merge this branch first, then `sandbox-extension-framework` design docs
