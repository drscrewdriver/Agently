# gVisor Standalone ExecutionResourceProvider

> 分支：`feature/gvisor-standalone-provider`
> 基于：Agently main (`1317da67`)
> 改动范围：2 个文件，+698 行

---

## 概述

本分支新增 `GVisorExecutionResourceProvider`，直接使用 `runsc` (gVisor) 提供用户态内核沙箱隔离，**不依赖 Docker daemon**。

gVisor 是 Google 开源的用户态内核，拦截容器内系统调用并在应用层实现，提供比普通 Docker 更强的隔离。本 provider 使用 OCI runtime spec 直接调用 runsc，无需 Docker。

---

## 与 Docker 变体对比

| 特性 | Standalone (本分支) | Docker 变体 |
|------|---------------------|-------------|
| 依赖 | 仅 runsc | Docker daemon + runsc |
| provider_id | `"gvisor"` | `"docker"` (复用) |
| 配置方式 | `provider_id: "gvisor"` | `config.runtime: "runsc"` |
| 隔离强度 | 完整 OCI 容器 | Docker 容器 |
| 镜像支持 | 手动 rootfs | Docker 镜像 |
| 启动开销 | 低（无 daemon） | 较高 |

---

## 架构设计

### 符合 4.1.4.2 契约

```
provider_id = "gvisor"
supported_kinds = ("code_execution",)
```

实现了所有必需接口：
- `async_probe` — 检测 runsc 可用性
- `async_ensure` — 创建执行资源
- `async_health_check` — 健康检查
- `async_release` — 释放资源
- `async_execute_code` — 执行代码（bundle/manifest/grant 验证模式）

### 条件加载

仅在 Linux 系统加载：

```python
# __init__.py
if platform.system() == "Linux":
    from .GVisorExecutionResourceProvider import GVisorExecutionResourceProvider
```

---

## 配置方式

### 通过 `code_execution.providers` 配置

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

### 配置选项

| 选项 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `network` | bool | `False` | 是否允许网络访问 |
| `writable_paths` | list[str] | `[]` | 可写路径列表 |
| `readonly_paths` | list[str] | `[]` | 只读路径列表 |
| `runsc_binary` | str | `"runsc"` | runsc 可执行文件路径 |
| `rootfs_image` | str | `None` | rootfs 路径（目录或 tarball） |

---

## 隔离能力

| 能力 | 支持 |
|------|------|
| 进程隔离 | ✅ (PID namespace) |
| 文件系统隔离 | ✅ (mount namespace + OCI mounts) |
| 网络隔离 | ✅ (network namespace, 可配置) |
| IPC 隔离 | ✅ (IPC namespace) |
| 系统调用过滤 | ✅ (gVisor Sentry) |
| 资源限制 | ✅ (memory/cpu/pids) |

---

## OCI Bundle 生成

本 provider 自动生成 OCI runtime spec：

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

## 系统要求

- Linux 内核 4.14+ (推荐 5.0+)
- `runsc` 已安装
- KVM 可选（提升性能）

### 安装 runsc

```bash
# 下载最新版本
curl -LO https://storage.googleapis.com/gvisor/releases/release/latest/x86_64/runsc
chmod +x runsc
sudo mv runsc /usr/local/bin/
```

---

## 文件清单

| 文件 | 说明 |
|------|------|
| `GVisorExecutionResourceProvider.py` | 完整 provider 实现 (693 行) |
| `__init__.py` | Linux 条件导入 |
