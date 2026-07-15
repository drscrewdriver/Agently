---
title: Sandbox Plugin
description: Agently 沙箱插件系统 — 容器级隔离执行环境。
keywords: Agently, sandbox, Docker, gVisor, container, isolation, security
---

# Sandbox Plugin

> 语言：[English](../../en/sandbox/README.md) · **中文**

Sandbox Plugin 是 Agently 的容器级沙箱执行环境，为 Agent 动作提供安全隔离的运行时。

## 概述

沙箱插件系统提供两种隔离级别：

| 隔离级别 | 后端 | 隔离机制 | 适用场景 |
|----------|------|----------|----------|
| `CONTAINER` | `DockerSandboxBackend` | Linux namespace + cgroup | 一般沙箱任务 |
| `ENHANCED_CONTAINER` | `GVisorSandboxBackend` | 用户态内核 syscall 拦截 | 高安全要求场景 |

## 模块结构

```text
agently/builtins/sandbox/
├── protocol.py          # 核心协议类型定义
├── docker_backend.py    # Docker 运行时后端
├── gvisor_backend.py    # gVisor 运行时后端
├── network_policy.py    # 网络策略引擎
└── events.py            # 事件发射器
```

## 核心协议

### IsolationLevel

```python
from agently.builtins.sandbox.protocol import IsolationLevel

IsolationLevel.CONTAINER           # 标准容器隔离
IsolationLevel.ENHANCED_CONTAINER  # gVisor 增强隔离
```

### SandboxConfig

```python
from agently.builtins.sandbox.protocol import SandboxConfig

config = SandboxConfig(
    # 容器配置
    image="python:3.12-slim",
    isolation_level=IsolationLevel.CONTAINER,
    
    # 资源限制
    memory_limit="512m",
    cpu_limit=1.0,
    timeout=30,
    
    # 网络策略
    network_enabled=False,
    network_allowlist=[],
    block_cloud_metadata=True,
    
    # 文件系统
    read_only_fs=True,
    writable_tmp_size="100m",
    
    # 权限
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

## 后端使用

### DockerSandboxBackend

```python
import asyncio
from agently.builtins.sandbox.docker_backend import DockerSandboxBackend
from agently.builtins.sandbox.protocol import SandboxConfig

async def main():
    backend = DockerSandboxBackend()
    config = SandboxConfig(timeout=30)
    
    # 创建会话
    session = await backend.create_session(config)
    print(f"Session: {session.session_id}")
    
    # 执行命令
    result = await session.execute("echo hello")
    print(result.stdout)
    
    # 执行 Python 代码
    result = await session.execute_python("print(2 + 2)")
    
    # 关闭
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
    
    # 创建 gVisor 会话（使用 runsc 运行时）
    session = await backend.create_session(config)
    print(f"Isolation: {session.isolation_level}")  # ENHANCED_CONTAINER
    
    # 执行命令（在 gVisor 沙箱中）
    result = await session.execute("echo hello from gVisor")
    
    # 关闭
    await session.close()
    await backend.destroy_session(session.session_id)

asyncio.run(main())
```

## 网络策略

```python
from agently.builtins.sandbox.network_policy import NetworkPolicyEngine
from agently.builtins.sandbox.protocol import SandboxConfig

engine = NetworkPolicyEngine()

# 网络禁用配置
config = SandboxConfig(network_enabled=False)
policy = engine.evaluate(config)
print(policy.allowed)  # False

# 网络白名单配置
config = SandboxConfig(
    network_enabled=True,
    network_allowlist=["pypi.org", "github.com"],
)
policy = engine.evaluate(config)
print(policy.allowed_domains)  # ["pypi.org", "github.com"]
```

## SandboxActionExecutor

统一沙箱执行器，符合 Agently Action Executor 协议：

```python
from agently.builtins.plugins.ActionExecutor.SandboxActionExecutor import SandboxActionExecutor

executor = SandboxActionExecutor(
    backend="docker",           # "docker" 或 "gvisor"
    default_image="python:3.12-slim",
    network_enabled=False,
)

# 协议属性
print(f"kind: {executor.kind}")          # "sandbox"
print(f"sandboxed: {executor.sandboxed}")  # True

# 执行动作
result = await executor.execute(
    spec={"action_id": "sandbox"},
    action_call={"action_input": {"cmd": "echo hello"}},
    policy={"timeout_seconds": 60},
    settings=None,
)
```

## 事件系统

```python
from agently.builtins.sandbox.events import (
    emit_session_created,
    emit_execution_completed,
    emit_policy_violation,
)

# 监听事件
# 事件通过 Agently 事件总线发射，可在 Agent 层订阅
```

## 环境要求

### Docker 模式
- Docker >= 20.x
- 无需额外配置

### gVisor 模式
- Docker >= 20.x
- gVisor (runsc) 已安装并注册到 Docker

安装 gVisor：

```bash
# 下载 runsc
wget -O runsc https://storage.googleapis.com/gvisor/releases/release/latest/x86_64/runsc
sudo install -m 0755 runsc /usr/local/bin/runsc

# 配置 Docker daemon
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

# 重启 Docker
sudo systemctl restart docker

# 验证
runsc --version
docker run --runtime=runsc --rm hello-world
```

## 判断运行时

```bash
# 查看容器运行时
docker inspect <container_id> --format '{{.HostConfig.Runtime}}'
# runc = Docker 模式, runsc = gVisor 模式

# 查看内核
docker exec <container_id> uname -r
# 宿主机内核 = Docker, 4.19.0-gvisor = gVisor
```

## 相关文档

- [Actions](../actions/README.md) — Action 执行架构
- [Execution Resource](../actions/execution-environment.md) — 托管执行资源层
- [Architecture](../architecture/README.md) — 系统架构概览
