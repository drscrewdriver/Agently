# 多平台沙箱扩展框架设计草案

> **状态**: Draft · 2026-07-15  
> **目标**: 以 Seatbelt (macOS) 为参考实现，定义跨平台本地沙箱 Provider 扩展规范，并对接 E2B 商业设施。

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

## 2. 架构总览

```
ExecutionResourceManager
├── kind="docker"       DockerExecutionResourceProvider (runc / runsc)
│   ├── runtime="runc"  标准 Docker namespace 隔离
│   └── runtime="runsc" gVisor 用户态内核隔离
├── kind="seatbelt"     SeatbeltExecutionResourceProvider  ← macOS
├── kind="bwrap"        BwrapExecutionResourceProvider     ← Linux (Bubblewrap)
├── kind="landlock"     LandlockExecutionResourceProvider  ← Linux (Landlock LSM)
├── kind="winrt"        WinRestrictedTokenProvider         ← Windows
├── kind="winac"        WinAppContainerProvider            ← Windows
├── kind="winsbox"      WinSandboxProvider                 ← Windows Sandbox
├── kind="e2b"          E2BExecutionResourceProvider       ← 远程商业沙箱
└── kind="python"       PythonExecutionResourceProvider    (trusted_local)
```

---

## 3. Provider 扩展协议

每个新 Provider 必须实现以下接口：

```python
class SandboxExecutionResourceProvider:
    name: str                          # 插件名
    kind: str                          # 路由键
    DEFAULT_SETTINGS: dict = {}

    @staticmethod
    def _on_register(): ...

    @staticmethod
    def _on_unregister(): ...

    async def async_ensure(
        self,
        *, requirement: ExecutionResourceRequirement,
        policy: ExecutionResourcePolicy,
        existing_handle: ExecutionResourceHandle | None = None,
    ) -> ExecutionResourceHandle: ...

    async def async_health_check(
        self, handle: ExecutionResourceHandle
    ) -> ExecutionResourceStatus: ...

    async def async_release(
        self, handle: ExecutionResourceHandle
    ) -> None: ...
```

**附加要求**：

- 必须提供 `inspect_*_availability()` 静态方法，返回 `{available: bool, reason: str, ...}`
- 必须在平台不可用时返回 `available=False`，不抛异常
- `async_ensure` 中若平台不可用，抛 `ExecutionResourceError(code="execution_resource.<kind>_unavailable")`

---

## 4. 各平台实现规范

### 4.1 macOS — Seatbelt (已实现)

| 维度 | 值 |
|------|---|
| **kind** | `"seatbelt"` |
| **机制** | `sandbox-exec` + SBPL profile |
| **隔离强度** | 内核级 syscall 过滤，强于 Docker namespace |
| **限制** | 无网络 namespace、无 PID namespace |
| **检测** | `platform.system() == "Darwin"` + `shutil.which("sandbox-exec")` |
| **文件** | `SeatbeltExecutionResourceProvider.py` |

**SBPL 策略生成**：
- 默认 deny-all，显式 allow 读/写路径
- 网络隔离通过移除 `network-outbound` 规则实现
- 支持 `extra_sbpl_rules` 自定义追加规则

---

### 4.2 Linux — Bubblewrap (bwrap)

| 维度 | 值 |
|------|---|
| **kind** | `"bwrap"` |
| **机制** | `bwrap` 命令 + namespace 绑定 |
| **隔离强度** | 用户 namespace + mount namespace + seccomp |
| **限制** | 需要内核支持 user namespaces（部分发行版默认禁用） |
| **检测** | `shutil.which("bwrap")` + `unshare --user true` 测试 |

**接口设计**：

```python
class BwrapExecutionResource:
    def __init__(
        self,
        *,
        timeout: int = 60,
        network: bool = False,          # --unshare-net
        read_only_paths: list[str] = [],  # --ro-bind
        write_paths: list[str] = [],      # --bind
        dev_paths: list[str] = ["/dev/null", "/dev/zero"],  # --dev-bind
        seccomp_profile: str | None = None,  # --seccomp 9
        extra_args: list[str] = [],
    ): ...
```

**bwrap 命令构造**：
```bash
bwrap \
  --unshare-all \
  --unshare-net \            # 当 network=False
  --ro-bind /usr /usr \
  --ro-bind /lib /lib \
  --bind /tmp /tmp \
  --dev /dev \
  --die-with-parent \
  --new-session \
  --seccomp 9 <fd> \         # 可选 seccomp 过滤
  -- python3 /tmp/user_code.py
```

---

### 4.3 Linux — Landlock

| 维度 | 值 |
|------|---|
| **kind** | `"landlock"` |
| **机制** | Landlock LSM (Linux 5.13+) 通过 Python `landlock` 模块 |
| **隔离强度** | 内核级文件系统访问控制，叠加在现有 DAC 之上 |
| **限制** | 仅文件系统，无网络/PID 隔离；需要 Linux ≥5.13 |
| **检测** | `platform.system() == "Linux"` + 内核版本检查 + `/sys/kernel/security/lsm` 包含 `landlock` |

**接口设计**：

```python
class LandlockExecutionResource:
    def __init__(
        self,
        *,
        timeout: int = 60,
        read_paths: list[str] = [],
        write_paths: list[str] = [],
        execute_paths: list[str] = [],
    ): ...
```

**实现方式**：
- 使用 `python-landlock` 库（或 `ctypes` 直接调 syscall）
- 在子进程入口通过 `LD_PRELOAD` 或 `prctl(PR_SET_NO_NEW_PRIVS)` 激活
- 与 `subprocess` 配合，在子进程启动前设置 Landlock 规则

---

### 4.4 Windows — Restricted Token

| 维度 | 值 |
|------|---|
| **kind** | `"winrt"` |
| **机制** | `CreateRestrictedToken` API，移除敏感 SID/Privilege |
| **隔离强度** | 降权 token，无法访问高完整性级别资源 |
| **限制** | 无文件系统/网络隔离，仅降权 |
| **检测** | `platform.system() == "Windows"` |

**接口设计**：

```python
class WinRestrictedTokenExecutionResource:
    def __init__(
        self,
        *,
        timeout: int = 60,
        disable_sids: list[str] = ["S-1-5-32-544"],  # Administrators
        remove_privileges: bool = True,
        deny_sids: list[str] = [],
    ): ...
```

---

### 4.5 Windows — AppContainer

| 维度 | 值 |
|------|---|
| **kind** | `"winac"` |
| **机制** | `CreateAppContainerProfile` + `DeriveAppContainerSid` |
| **隔离强度** | 文件系统/注册表/网络全隔离，强于 Restricted Token |
| **限制** | 需要 Windows 8+，配置复杂 |
| **检测** | `platform.system() == "Windows"` + `CheckTokenMembership` 测试 |

**接口设计**：

```python
class WinAppContainerExecutionResource:
    def __init__(
        self,
        *,
        timeout: int = 60,
        capabilities: list[str] = [],  # SID capabilities
        appcontainer_name: str = "agently_sandbox",
    ): ...
```

---

### 4.6 Windows — Windows Sandbox

| 维度 | 值 |
|------|---|
| **kind** | `"winsbox"` |
| **机制** | `WindowsSandbox` (WSB) 轻量虚拟机 |
| **隔离强度** | 硬件级隔离（Hyper-V VM），最强 |
| **限制** | 需要 Windows 10/11 Pro+、Hyper-V 启用、启动慢（~5s） |
| **检测** | `platform.system() == "Windows"` + `Get-WindowsOptionalFeature -FeatureName Containers-DisposableClientVM` |

**接口设计**：

```python
class WinSandboxExecutionResource:
    def __init__(
        self,
        *,
        timeout: int = 120,
        vgpu: bool = True,
        networking: bool = False,
        mapped_folders: list[dict] = [],  # [{host_path, sandbox_path, read_only}]
        logon_command: str = "",
    ): ...
```

**WSB 配置文件生成**：
```xml
<Configuration>
  <VGpu>{Enabled|Disabled}</VGpu>
  <Networking>{Default|Disable}</Networking>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>C:\temp\input</HostFolder>
      <SandboxFolder>C:\sandbox</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>python C:\sandbox\run.py</Command>
  </LogonCommand>
</Configuration>
```

---

### 4.7 E2B — 远程商业沙箱

| 维度 | 值 |
|------|---|
| **kind** | `"e2b"` |
| **机制** | E2B SDK（`e2b` Python 包），远程 microVM 沙箱 |
| **隔离强度** | Firecracker microVM，硬件级隔离 |
| **限制** | 需要网络 + API Key，有延迟和成本 |
| **检测** | `shutil.which("e2b")` 或 `import e2b` + API key 配置检查 |

**接口设计**：

```python
class E2BExecutionResource:
    def __init__(
        self,
        *,
        timeout: int = 60,
        api_key: str | None = None,       # 或从环境变量 E2B_API_KEY 读取
        template: str = "base",           # E2B sandbox template
        domain: str | None = None,        # 自定义域名（私有部署）
        metadata: dict[str, str] = {},
    ): ...
```

**E2B 使用模式**：
```python
from e2b import Sandbox

async with Sandbox(template="base", api_key=self.api_key) as sandbox:
    result = await sandbox.commands.run("python3 -c 'print(1+1)'")
    # result.stdout == "2\n"
```

**与本地 Provider 的差异**：
- 生命周期：E2B sandbox 可持久化（分钟级），非一次性
- 网络：默认有网络，通过 `network_enabled=False` 禁用
- 文件：通过 `sandbox.files.write()` / `sandbox.files.read()` 传输
- 成本：每次执行有 API 调用成本，需配置 rate limiting

---

## 5. 自动选择策略（sandbox="auto"）

当 `sandbox="auto"` 时，按以下优先级选择后端：

```python
SANDBOX_AUTO_PRIORITY = {
    "Darwin": ["seatbelt", "docker"],
    "Linux":  ["bwrap", "landlock", "docker"],
    "Windows": ["winsbox", "winac", "winrt", "docker"],
}

REMOTE_PRIORITY = ["e2b"]  # 仅在本地全部不可用时使用
```

**选择逻辑**：
1. 按平台优先级遍历本地后端
2. 调用 `inspect_*_availability()` 检查可用性
3. 首个 `available=True` 的后端被选中
4. 若本地全部不可用，尝试 E2B（需配置 API key）
5. 若 E2B 也不可用，fallback 到 `trusted_local`（发出警告）

---

## 6. 实现路线图

| 阶段 | Provider | 平台 | 优先级 |
|------|---------|------|--------|
| Phase 1 | `SeatbeltExecutionResourceProvider` | macOS | ✅ 已实现 |
| Phase 2 | `BwrapExecutionResourceProvider` | Linux | 高 |
| Phase 3 | `E2BExecutionResourceProvider` | 跨平台 | 高 |
| Phase 4 | `LandlockExecutionResourceProvider` | Linux 5.13+ | 中 |
| Phase 5 | `WinRestrictedTokenProvider` | Windows | 中 |
| Phase 6 | `WinAppContainerProvider` | Windows 8+ | 低 |
| Phase 7 | `WinSandboxExecutionResourceProvider` | Windows 10/11 Pro+ | 低 |

---

## 7. 测试策略

每个 Provider 需要以下测试：

1. **可用性检测测试**：`inspect_*_availability()` 在目标平台返回正确结果
2. **隔离验证测试**：确认沙箱内无法访问沙箱外资源
3. **超时测试**：确认 `timeout` 参数生效
4. **网络隔离测试**：确认 `network=False` 时无法访问网络
5. **降级测试**：首选后端不可用时，`auto` 模式正确 fallback

**跨平台 CI 矩阵**：

```yaml
# .github/workflows/sandbox-tests.yml
strategy:
  matrix:
    os: [ubuntu-latest, macos-latest, windows-latest]
    sandbox: [auto, docker, seatbelt, bwrap, e2b]
    exclude:
      - os: windows-latest
        sandbox: seatbelt
      - os: windows-latest
        sandbox: bwrap
      - os: ubuntu-latest
        sandbox: seatbelt
      - os: macos-latest
        sandbox: bwrap
```

---

## 8. 安全考量

| 后端 | syscall 过滤 | 文件系统隔离 | 网络隔离 | PID 隔离 |
|------|-------------|------------|---------|---------|
| Docker (runc) | seccomp | namespace | namespace | namespace |
| Docker (runsc/gVisor) | 用户态内核拦截 | namespace | namespace | namespace |
| Seatbelt | SBPL 规则 | 部分（路径限制） | SBPL 规则 | ❌ |
| bwrap | seccomp | namespace | namespace | namespace |
| Landlock | ❌ | LSM 强制访问控制 | ❌ | ❌ |
| Win Restricted Token | ❌ | 完整性级别 | ❌ | ❌ |
| Win AppContainer | ❌ | 隔离容器 | 能力控制 | ❌ |
| Win Sandbox | Hyper-V VM | VM 隔离 | VM 隔离 | VM 隔离 |
| E2B | Firecracker microVM | VM 隔离 | VM 隔离 | VM 隔离 |

**建议**：对安全性要求高的场景，优先选择 gVisor / bwrap / E2B / Win Sandbox；对开发环境快速验证，Seatbelt / Landlock / Restricted Token 足够。
