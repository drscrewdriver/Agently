# gVisor Standalone ExecutionResourceProvider

> Branch: `feature/gvisor-standalone-provider`
> Based on: Agently main (`1317da67`)
> Changes: 2 files, +698 lines

---

## Overview

This branch adds `GVisorExecutionResourceProvider`, using `runsc` (gVisor) directly to provide user-space kernel sandbox isolation, **without Docker daemon dependency**.

gVisor is a user-space kernel open-sourced by Google that intercepts container system calls and implements them in application space, providing stronger isolation than regular Docker. This provider uses OCI runtime spec to call runsc directly, without Docker.

---

## Comparison with Docker Variant

| Feature | Standalone (this branch) | Docker Variant |
|---------|--------------------------|----------------|
| Dependency | runsc only | Docker daemon + runsc |
| provider_id | `"gvisor"` | `"docker"` (reused) |
| Configuration | `provider_id: "gvisor"` | `config.runtime: "runsc"` |
| Isolation strength | Full OCI container | Docker container |
| Image support | Manual rootfs | Docker images |
| Startup overhead | Low (no daemon) | Higher |

---

## Architecture

### Conforms to 4.1.4.2 Contract

```
provider_id = "gvisor"
supported_kinds = ("code_execution",)
```

Implements all required interfaces:
- `async_probe` — Detect runsc availability
- `async_ensure` — Create execution resource
- `async_health_check` — Health check
- `async_release` — Release resource
- `async_execute_code` — Execute code (bundle/manifest/grant validation pattern)

### Conditional Loading

Only loaded on Linux systems:

```python
# __init__.py
if platform.system() == "Linux":
    from .GVisorExecutionResourceProvider import GVisorExecutionResourceProvider
```

---

## Configuration

### Via `code_execution.providers`

```python
settings.set("code_execution.providers", [
    {"provider_id": "gvisor", "config": {
        "network": False,
        "writable_paths": ["/tmp/work"],
        "readonly_paths": ["/usr/lib"],
    }},
    {"provider_id": "docker", "config": {}},  # fallback
])
```

### Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `network` | bool | `False` | Allow network access |
| `writable_paths` | list[str] | `[]` | Writable path list |
| `readonly_paths` | list[str] | `[]` | Read-only path list |
| `runsc_binary` | str | `"runsc"` | runsc executable path |
| `rootfs_image` | str | `None` | rootfs path (directory or tarball) |

---

## Isolation Capabilities

| Capability | Support |
|------------|---------|
| Process isolation | ✅ (PID namespace) |
| Filesystem isolation | ✅ (mount namespace + OCI mounts) |
| Network isolation | ✅ (network namespace, configurable) |
| IPC isolation | ✅ (IPC namespace) |
| Syscall filtering | ✅ (gVisor Sentry) |
| Resource limits | ✅ (memory/cpu/pids) |

---

## OCI Bundle Generation

This provider automatically generates OCI runtime spec:

```json
{
  "ociVersion": "1.0.2",
  "process": {
    "args": ["python3", "script.py"],
    "cwd": "/workspace",
    "capabilities": {},
    "noNewPrivileges": true
  },
  "root": { "path": "rootfs" },
  "linux": {
    "namespaces": ["pid", "ipc", "uts", "mount", "network"],
    "resources": {
      "memory": { "limit": 536870912 },
      "cpu": { "quota": 100000 },
      "pids": { "limit": 256 }
    }
  }
}
```

---

## System Requirements

- Linux kernel 4.14+ (5.0+ recommended)
- `runsc` installed
- KVM optional (improves performance)

### Installing runsc

```bash
# Download latest version
curl -LO https://storage.googleapis.com/gvisor/releases/release/latest/x86_64/runsc
chmod +x runsc
sudo mv runsc /usr/local/bin/
```

---

## File List

| File | Description |
|------|-------------|
| `GVisorExecutionResourceProvider.py` | Complete provider implementation (693 lines) |
| `__init__.py` | Linux conditional import |
