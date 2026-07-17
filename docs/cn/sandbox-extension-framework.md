# 多平台沙箱扩展框架设计草案

> **状态**: 草案 · 2026-07-15  
> **目标**: 以 Seatbelt (macOS) 为参考实现，定义跨平台本地沙箱 Provider 扩展规范，并对接 Linux (bwrap/landlock)、Windows (Restricted-token/AppContainer/Sandbox) 及 E2B 商业设施。

---

## 1. 设计目标

| 目标 | 说明 |
|------|------|
| **平台覆盖** | Linux、macOS、Windows 均有对应本地沙箱后端 |
| **统一接口** | 所有 Provider 实现 `ExecutionResourceProvider` 协议，`kind` 字段路由 |
| **可检测** | 每个 Provider 必须实现 `inspect_*_availability()` 自检 |
| **可降级** | 首选后端不可用时，框架可自动 fallback 到次选后端 |
| **商业对接** | E2B 作为远程沙箱标准，与本地后端并列 |

---

## 2. Sandbox 模式层级

`register_*_action(sandbox=...)` 支持 5 级模式：

| 模式 | 含义 | 行为 |
|------|------|------|
| `trusted_local` | 无隔离 | 直接 exec，零隔离 |
| `local` | 本机沙箱 | **按 OS 选择最佳本地后端** (seatbelt/bwrap/landlock) |
| `docker` | Docker 容器 | runc 运行时 |
| `gvisor` | Docker + gVisor | runsc 运行时（用户态内核） |
| `auto` | 自动选择 | **容器优先**: gvisor > docker > local > trusted_local |

### 2.1 `auto` 解析顺序

```
auto →
  1. gvisor (若 runsc 可用 + 已在 Docker daemon 注册)
  2. docker  (若 docker 二进制可用)
  3. local   (OS 原生: macOS 上 seatbelt, Linux 上 bwrap/landlock)
  4. trusted_local (兜底，无隔离)
```

### 2.2 `local` 按 OS 解析

| OS | 后端 | 检测方式 |
|----|------|----------|
| macOS | Seatbelt | `sandbox-exec` 二进制存在 |
| Linux | Bubblewrap | `bwrap` 二进制存在 |
| Linux | Landlock | 内核 ≥ 5.13 + `landlock` 系统调用可用 |
| Windows | (未来) | Restricted-token / AppContainer / Windows Sandbox |

---

## 3. 架构总览

```
ExecutionResourceManager
├── kind="docker"        DockerExecutionResourceProvider
│   ├── runtime="runc"   标准 Docker namespace 隔离
│   └── runtime="runsc"  gVisor 用户态内核隔离
│
├── kind="seatbelt"      SeatbeltExecutionResourceProvider    [macOS]
│   └── sandbox-exec + SBPL profile
│
├── kind="bwrap"         BwrapExecutionResourceProvider       [Linux]
│   └── bubblewrap namespace 隔离
│
├── kind="landlock"      LandlockExecutionResourceProvider    [Linux 5.13+]
│   └── Landlock LSM 文件系统访问控制
│
├── kind="winrt"         WindowsRuntimeExecutionResourceProvider [Windows, 未来]
│   ├── Restricted-token
│   ├── AppContainer
│   └── Windows Sandbox (WinSxS)
│
└── kind="e2b"           E2BExecutionResourceProvider         [远程, 商业]
    └── E2B API 沙箱即服务
```

---

## 4. Provider 扩展协议

每个新沙箱 Provider 必须实现：

```python
class XxxExecutionResourceProvider(ExecutionResourceProvider):
    @property
    def name(self) -> str: ...       # 例如 "seatbelt"

    @property
    def kind(self) -> str: ...       # 例如 "seatbelt", "bwrap"

    def inspect_availability(self) -> dict[str, Any]:
        """返回 {"available": bool, ...}"""
        ...

    def create_handle(self, *, config, policy) -> dict[str, Any]:
        """创建执行资源句柄。"""
        ...
```

### 4.1 可用性检测模式

```python
def inspect_xxx_availability() -> dict[str, Any]:
    # 1. 平台检查
    if platform.system() != "ExpectedOS":
        return {"available": False, "reason": "wrong_platform"}
    # 2. 二进制/能力检查
    if shutil.which("required_binary") is None:
        return {"available": False, "reason": "binary_missing"}
    # 3. 内核版本检查（如需要）
    if kernel_version < minimum:
        return {"available": False, "reason": "kernel_too_old"}
    return {"available": True, "binary": path, ...}
```

---

## 5. 平台特定后端

### 5.1 macOS: Seatbelt

- **二进制**: `sandbox-exec` (macOS 自带)
- **机制**: SBPL (Seatbelt Profile Language) → 内核级 syscall 过滤
- **隔离级别**: 文件系统 + 网络 + IPC
- **参考**: `SeatbeltExecutionResourceProvider` (见 `feature/seatbelt-provider` 分支)

### 5.2 Linux: Bubblewrap (bwrap)

- **二进制**: `bwrap` (包名: `bubblewrap`)
- **机制**: Linux namespaces (user, mount, pid, net, ipc, uts)
- **隔离级别**: 完整 namespace 隔离
- **示例**:
  ```bash
  bwrap --ro-bind /usr /usr --proc /proc --dev /dev \
        --unshare-pid --unshare-net \
        python3 -c "print('isolated')"
  ```

### 5.3 Linux: Landlock

- **内核**: Linux 5.13+
- **机制**: Landlock LSM — 内核级文件系统访问控制
- **隔离级别**: 仅文件系统（无网络/进程隔离）
- **说明**: 与 bwrap 互补；bwrap 用于完整隔离，Landlock 用于轻量级 FS 控制

### 5.4 Windows (未来)

| 后端 | 机制 | 隔离级别 |
|------|------|----------|
| Restricted-token | `CreateRestrictedToken()` API | 限制权限/SID |
| AppContainer | `CreateProcess` + `SECURITY_CAPABILITIES` | 文件/注册表/网络 namespace |
| Windows Sandbox | `WindowsSandbox.wsb` 配置文件 | 完整 VM 级隔离 (Win10/11 Pro+) |

### 5.5 E2B (远程, 商业)

- **服务**: [E2B](https://e2b.dev) — 沙箱即服务
- **机制**: REST API → 远程 Firecracker microVM
- **集成**:
  ```python
  class E2BExecutionResourceProvider(ExecutionResourceProvider):
      kind = "e2b"
      # 使用 E2B SDK: e2b_code_interpreter.Sandbox()
      # API key 来自 config["e2b_api_key"]
  ```
- **优势**: 无需本地 Docker/内核依赖；跨平台可用

---

## 6. 与上游 4.1.4.1 的整合

上游 4.1.4.1 引入 `DockerExecutionResourceProvider` 作为主要沙箱。
本框架从三个正交方向扩展：

| 分支 | 方向 | 范围 |
|------|------|------|
| `feature/gvisor-docker-runtime` | gVisor 作为 Docker 运行时 | 修改现有 Docker provider |
| `feature/seatbelt-provider` | Seatbelt 作为替代 provider | Docker 之外的新 provider |
| 本分支 | 统一扩展框架 | 设计规格 + 未来 providers |

### 6.1 合并策略

1. **阶段 1**: 合并 `feature/gvisor-docker-runtime` — 为 Docker provider 添加 `runtime` 参数
2. **阶段 2**: 合并 `feature/seatbelt-provider` — 添加 `kind="seatbelt"` provider
3. **阶段 3**: 按相同模式实现 bwrap/landlock providers
4. **阶段 4**: 添加 E2B provider 用于商业场景
5. **阶段 5**: 实现 `sandbox="auto"` 完整降级链

---

## 7. OS 检测矩阵

| Provider | macOS | Linux | Windows | 检测方式 |
|----------|-------|-------|---------|----------|
| Docker (runc) | ✅ | ✅ | ✅ | `shutil.which("docker")` |
| Docker (gVisor) | ✅ | ✅ | ✅ | `shutil.which("runsc")` + daemon 检查 |
| Seatbelt | ✅ | ❌ | ❌ | `platform.system() == "Darwin"` + `sandbox-exec` |
| Bubblewrap | ❌ | ✅ | ❌ | `shutil.which("bwrap")` |
| Landlock | ❌ | ✅ (5.13+) | ❌ | 内核版本 + syscall 探测 |
| Windows RT | ❌ | ❌ | ✅ | `platform.system() == "Windows"` + API 探测 |
| E2B | ✅ | ✅ | ✅ | API key 存在 + 网络检查 |

---

## 8. 降级链实现

```python
def _resolve_auto_sandbox() -> str:
    """容器优先自动选择。"""
    # 1. gVisor
    gvisor = DockerExecutionResource.inspect_gvisor_availability()
    if gvisor.get("available"):
        return "gvisor"
    # 2. Docker
    if shutil.which("docker") is not None:
        return "docker"
    # 3. 本地 OS 原生
    system = platform.system()
    if system == "Darwin" and shutil.which("sandbox-exec"):
        return "seatbelt"
    if system == "Linux":
        if shutil.which("bwrap"):
            return "bwrap"
        if _check_landlock_available():
            return "landlock"
    # 4. 兜底
    return "trusted_local"
```

---

## 9. 后续工作

- [ ] 实现 `BwrapExecutionResourceProvider`
- [ ] 实现 `LandlockExecutionResourceProvider`
- [ ] 实现 `E2BExecutionResourceProvider`
- [ ] Windows `WindowsRuntimeExecutionResourceProvider` (3 个后端)
- [ ] `sandbox="auto"` 完整集成测试矩阵
- [ ] 通过 `agently.yaml` 配置 Provider 优先级
- [ ] 热重载 Provider 注册

---

## 10. 相关分支

- `feature/gvisor-docker-runtime` — Docker provider 中的 gVisor 运行时
- `feature/seatbelt-provider` — macOS Seatbelt provider
- Issue #312 — Docker 沙箱优先方案
- Issue #324 — 框架级沙箱抽象

---

> 语言：[English](../../en/sandbox-extension-framework.md) · **中文**
