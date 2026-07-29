# Bubblewrap ExecutionResourceProvider

> Branch: `feature/bubblewrap-provider`
> Based on: Agently main (`1317da67`)
> Changes: 2 files, +530 lines

---

## Overview

This branch adds `BubblewrapExecutionResourceProvider`, using Linux `bwrap` (bubblewrap) to provide user namespace sandbox isolation.

Bubblewrap is the sandbox tool used by Flatpak, available in most Linux distributions, providing lightweight process isolation.

---

## Architecture

### Conforms to 4.1.4.2 Contract

```
provider_id = "bubblewrap"
supported_kinds = ("code_execution",)
```

Implements all required interfaces:
- `async_probe` — Detect bwrap availability
- `async_ensure` — Create execution resource
- `async_health_check` — Health check
- `async_release` — Release resource
- `async_execute_code` — Execute code (bundle/manifest/grant validation pattern)

### Conditional Loading

Only loaded on Linux systems:

```python
# __init__.py
if platform.system() == "Linux":
    from .BubblewrapExecutionResourceProvider import BubblewrapExecutionResourceProvider
```

---

## Configuration

### Via `code_execution.providers`

```python
settings.set("code_execution.providers", [
    {"provider_id": "bubblewrap", "config": {
        "unshare_all": True,
        "share_net": False,
        "writable_paths": ["/tmp/work"],
    }},
    {"provider_id": "docker", "config": {}},  # fallback
])
```

### Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `unshare_all` | bool | `True` | Isolate all namespaces |
| `share_net` | bool | `False` | Share network namespace |
| `writable_paths` | list[str] | `[]` | Writable path list |
| `readonly_paths` | list[str] | `[]` | Read-only path list |
| `tmpfs_paths` | list[str] | `[]` | tmpfs mount paths |
| `bwrap_binary` | str | `"bwrap"` | bwrap executable path |

---

## Isolation Capabilities

| Capability | Support |
|------------|---------|
| Process isolation | ✅ (PID namespace) |
| Filesystem isolation | ✅ (mount namespace + bind mounts) |
| Network isolation | ✅ (network namespace, configurable) |
| User isolation | ✅ (user namespace) |
| Resource limits | ⚠️ (depends on cgroup config) |

---

## Comparison with Docker Provider

| Feature | Bubblewrap | Docker |
|---------|------------|--------|
| Startup speed | Fast (no daemon) | Slower (requires daemon) |
| Memory overhead | Low | Higher |
| Isolation strength | User namespace | Full container |
| Image support | ❌ | ✅ |
| Cross-platform | Linux only | Linux/macOS/Windows |

---

## System Requirements

- Linux kernel 3.8+ (user namespaces)
- `bwrap` installed (`apt install bubblewrap` or `dnf install bubblewrap`)
- Kernel parameter `kernel.unprivileged_userns_clone=1` (Debian/Ubuntu)

---

## File List

| File | Description |
|------|-------------|
| `BubblewrapExecutionResourceProvider.py` | Complete provider implementation (530 lines) |
| `__init__.py` | Linux conditional import |
