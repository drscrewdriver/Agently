# Multi-Platform Sandbox Extension Framework — Design Draft

> **Status**: Draft · 2026-07-15
> **Goal**: Define a cross-platform sandbox provider extension specification using Seatbelt (macOS) as the reference implementation, with Linux (bwrap/landlock), Windows (Restricted-token/AppContainer/Sandbox), and E2B commercial integration.

---

## 1. Design Goals

| Goal | Description |
|------|-------------|
| **Platform coverage** | Linux, macOS, Windows each have a native sandbox backend |
| **Unified interface** | All providers implement `ExecutionResourceProvider` protocol, routed by `kind` |
| **Self-detectable** | Every provider must implement `inspect_*_availability()` |
| **Graceful degradation** | Framework auto-selects best available backend via fallback chain |
| **Commercial integration** | E2B as remote sandbox standard, alongside local backends |

---

## 2. Sandbox Mode Hierarchy

`register_*_action(sandbox=...)` supports 5 levels:

| Mode | Meaning | Behavior |
|------|---------|----------|
| `trusted_local` | No isolation | Direct exec, zero isolation |
| `local` | OS-native sandbox | **Select best local backend by OS** (seatbelt/bwrap/landlock) |
| `docker` | Docker container | runc runtime |
| `gvisor` | Docker + gVisor | runsc runtime (user-space kernel) |
| `auto` | Auto-selection | **Container-first**: gvisor > docker > local > trusted_local |

### 2.1 `auto` Resolution Order

```
auto →
  1. gvisor (if runsc available + registered in Docker daemon)
  2. docker  (if docker binary available)
  3. local   (OS-native: seatbelt on macOS, bwrap/landlock on Linux)
  4. trusted_local (fallback, no isolation)
```

### 2.2 `local` Resolution by OS

| OS | Backend | Detection |
|----|---------|-----------|
| macOS | Seatbelt | `sandbox-exec` binary exists |
| Linux | Bubblewrap | `bwrap` binary exists |
| Linux | Landlock | Kernel ≥ 5.13 + `landlock` syscall available |
| Windows | (future) | Restricted-token / AppContainer / Windows Sandbox |

---

## 3. Architecture Overview

```
ExecutionResourceManager
├── kind="docker"        DockerExecutionResourceProvider
│   ├── runtime="runc"   Standard Docker namespace isolation
│   └── runtime="runsc"  gVisor user-space kernel isolation
│
├── kind="seatbelt"      SeatbeltExecutionResourceProvider    [macOS]
│   └── sandbox-exec + SBPL profile
│
├── kind="bwrap"         BwrapExecutionResourceProvider       [Linux]
│   └── bubblewrap namespace isolation
│
├── kind="landlock"      LandlockExecutionResourceProvider    [Linux 5.13+]
│   └── Landlock LSM filesystem access control
│
├── kind="winrt"         WindowsRuntimeExecutionResourceProvider [Windows, future]
│   ├── Restricted-token
│   ├── AppContainer
│   └── Windows Sandbox (WinSxS)
│
└── kind="e2b"           E2BExecutionResourceProvider         [Remote, commercial]
    └── E2B API sandbox-as-a-service
```

---

## 4. Provider Extension Protocol

Every new sandbox provider must implement:

```python
class XxxExecutionResourceProvider(ExecutionResourceProvider):
    @property
    def name(self) -> str: ...       # e.g. "seatbelt"

    @property
    def kind(self) -> str: ...       # e.g. "seatbelt", "bwrap"

    def inspect_availability(self) -> dict[str, Any]:
        """Return {"available": bool, ...}"""
        ...

    def create_handle(self, *, config, policy) -> dict[str, Any]:
        """Create an execution resource handle."""
        ...
```

### 4.1 Availability Detection Pattern

```python
def inspect_xxx_availability() -> dict[str, Any]:
    # 1. Platform check
    if platform.system() != "ExpectedOS":
        return {"available": False, "reason": "wrong_platform"}
    # 2. Binary/capability check
    if shutil.which("required_binary") is None:
        return {"available": False, "reason": "binary_missing"}
    # 3. Kernel version check (if needed)
    if kernel_version < minimum:
        return {"available": False, "reason": "kernel_too_old"}
    return {"available": True, "binary": path, ...}
```

---

## 5. Platform-Specific Backends

### 5.1 macOS: Seatbelt

- **Binary**: `sandbox-exec` (ships with macOS)
- **Mechanism**: SBPL (Seatbelt Profile Language) → kernel-level syscall filtering
- **Isolation level**: Filesystem + network + IPC
- **Reference**: `SeatbeltExecutionResourceProvider` (see `feature/seatbelt-provider` branch)

### 5.2 Linux: Bubblewrap (bwrap)

- **Binary**: `bwrap` (package: `bubblewrap`)
- **Mechanism**: Linux namespaces (user, mount, pid, net, ipc, uts)
- **Isolation level**: Full namespace isolation
- **Example**:
  ```bash
  bwrap --ro-bind /usr /usr --proc /proc --dev /dev \
        --unshare-pid --unshare-net \
        python3 -c "print('isolated')"
  ```

### 5.3 Linux: Landlock

- **Kernel**: Linux 5.13+
- **Mechanism**: Landlock LSM — filesystem access control at kernel level
- **Isolation level**: Filesystem only (no network/process isolation)
- **Note**: Complementary to bwrap; use bwrap for full isolation, Landlock for lightweight FS control

### 5.4 Windows (Future)

| Backend | Mechanism | Isolation Level |
|---------|-----------|-----------------|
| Restricted-token | `CreateRestrictedToken()` API | Limit privileges/SIDs |
| AppContainer | `CreateProcess` with `SECURITY_CAPABILITIES` | File/registry/network namespace |
| Windows Sandbox | `WindowsSandbox.wsb` config file | Full VM-like isolation (Win10/11 Pro+) |

### 5.5 E2B (Remote, Commercial)

- **Service**: [E2B](https://e2b.dev) — sandbox-as-a-service
- **Mechanism**: REST API → remote Firecracker microVM
- **Integration**:
  ```python
  class E2BExecutionResourceProvider(ExecutionResourceProvider):
      kind = "e2b"
      # Uses E2B SDK: e2b_code_interpreter.Sandbox()
      # API key from config["e2b_api_key"]
  ```
- **Advantage**: No local Docker/kernel dependency; works on any platform

---

## 6. Integration with Upstream 4.1.4.1

Upstream 4.1.4.1 introduced `DockerExecutionResourceProvider` as the primary sandbox.
This framework extends it in three orthogonal directions:

| Branch | Direction | Scope |
|--------|-----------|-------|
| `feature/gvisor-docker-runtime` | gVisor as Docker runtime | Modify existing Docker provider |
| `feature/seatbelt-provider` | Seatbelt as alternative provider | New provider alongside Docker |
| This branch | Unified extension framework | Design spec + future providers |

### 6.1 Merge Strategy

1. **Phase 1**: Merge `feature/gvisor-docker-runtime` — adds `runtime` param to Docker provider
2. **Phase 2**: Merge `feature/seatbelt-provider` — adds `kind="seatbelt"` provider
3. **Phase 3**: Implement bwrap/landlock providers following the same pattern
4. **Phase 4**: Add E2B provider for commercial use case
5. **Phase 5**: Implement `sandbox="auto"` with full fallback chain

---

## 7. OS Detection Matrix

| Provider | macOS | Linux | Windows | Detection Method |
|----------|-------|-------|---------|------------------|
| Docker (runc) | ✅ | ✅ | ✅ | `shutil.which("docker")` |
| Docker (gVisor) | ✅ | ✅ | ✅ | `shutil.which("runsc")` + daemon check |
| Seatbelt | ✅ | ❌ | ❌ | `platform.system() == "Darwin"` + `sandbox-exec` |
| Bubblewrap | ❌ | ✅ | ❌ | `shutil.which("bwrap")` |
| Landlock | ❌ | ✅ (5.13+) | ❌ | Kernel version + syscall probe |
| Windows RT | ❌ | ❌ | ✅ | `platform.system() == "Windows"` + API probe |
| E2B | ✅ | ✅ | ✅ | API key present + network check |

---

## 8. Fallback Chain Implementation

```python
def _resolve_auto_sandbox() -> str:
    """Container-first auto-selection."""
    # 1. gVisor
    gvisor = DockerExecutionResource.inspect_gvisor_availability()
    if gvisor.get("available"):
        return "gvisor"
    # 2. Docker
    if shutil.which("docker") is not None:
        return "docker"
    # 3. Local OS-native
    system = platform.system()
    if system == "Darwin" and shutil.which("sandbox-exec"):
        return "seatbelt"
    if system == "Linux":
        if shutil.which("bwrap"):
            return "bwrap"
        if _check_landlock_available():
            return "landlock"
    # 4. Fallback
    return "trusted_local"
```

---

## 9. Future Work

- [ ] Implement `BwrapExecutionResourceProvider`
- [ ] Implement `LandlockExecutionResourceProvider`
- [ ] Implement `E2BExecutionResourceProvider`
- [ ] Windows `WindowsRuntimeExecutionResourceProvider` (3 backends)
- [ ] `sandbox="auto"` full integration test matrix
- [ ] Provider priority configuration via `agently.yaml`
- [ ] Hot-reload provider registration

---

## 10. Related Branches

- `feature/gvisor-docker-runtime` — gVisor runtime in Docker provider
- `feature/seatbelt-provider` — macOS Seatbelt provider
- Issue #312 — Docker sandbox primary approach
- Issue #324 — Framework-level sandbox abstraction
