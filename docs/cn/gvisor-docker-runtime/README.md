# gVisor Docker Runtime 扩展

> 分支：`feature/gvisor-docker-runtime`
> 基于：Agently 4.1.4.1 (`bbcd6335`)
> 改动范围：2 个文件，+54 行 / -4 行

---

## 概述

本分支在 **不新增任何外部依赖** 的前提下，为上游 `DockerExecutionResourceProvider` 注入 gVisor (`runsc`) 运行时支持。

gVisor 是 Google 开源的用户态内核，拦截容器内系统调用并在应用层实现，提供比普通 Docker（runc + seccomp/AppArmor）更强的隔离。

---

## 与上游的差异

### 改动文件清单

| 文件 | 改动 | 说明 |
|------|------|------|
| `DockerExecutionResourceProvider.py` | +48 行 | 新增 `runtime` 参数、`SUPPORTED_RUNTIMES` 常量、`inspect_gvisor_availability()` 方法 |
| `ActionResourceRegistrar.py` | +10 / -4 行 | `_normalize_code_sandbox` 新增 `"gvisor"` 值；`_docker_runtime_requirement` 新增 `docker_runtime` 参数 |

### 侵入性分析

**零破坏性改动** — 所有变更都是向后兼容的增量添加：

1. **默认行为不变**：`runtime` 参数默认 `"runc"`，不传则与上游完全一致
2. **不修改基类**：`ExecutionResource` / `ExecutionResourceProvider` 基类未改动
3. **不改 `create_handle` 签名**：通过 `config` 字典传递 `runtime`，不增加位置参数
4. **不改 `sandbox` 枚举的默认解析**：`"gvisor"` 只是新增的可选值

---

## 配置方式

### 1. 通过 `sandbox` 参数选择运行时

```python
agent = Agent("sandbox_agent")

# 标准 Docker 沙箱（默认，与上游一致）
agent \
    .register_docker_python_action(
        action_id="safe_run",
        sandbox="docker",
    )

# gVisor 沙箱（新增值）
agent \
    .register_docker_python_action(
        action_id="gvisor_run",
        sandbox="gvisor",  # ← 新增值
    )
```

### 2. 通过 `docker_runtime` 参数精确控制

```python
agent \
    .register_docker_python_action(
        action_id="custom_runtime",
        docker_runtime="runsc",  # 直接指定底层 runtime
    )
```

### 3. 通过 `config.yaml` 全局配置

```yaml
execution_resources:
  providers:
    docker:
      config:
        runtime: runsc          # "runc"（默认）或 "runsc"
        docker_binary: docker
        timeout: 60
```

---

## 运行时检测

### 自动检测 gVisor 可用性

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

### 检测逻辑

`inspect_gvisor_availability()` 依次检查：

1. `runsc` 二进制是否存在于 `$PATH`
2. `runsc --version` 是否可执行
3. Docker daemon 是否已注册 `runsc` 运行时（`docker info --format '{{json .Runtimes}}'`）

三项全部通过才返回 `"available": True`。

---

## 内部实现细节

### runtime 参数传递链

```
config["runtime"] → DockerExecutionResourceProvider.create_handle()
    → DockerExecutionResource(runtime="runsc")
        → _container_base_args() 注入 --runtime runsc
            → docker run --rm --runtime runsc ...
```

### 防重复注入

```python
if self.runtime != "runc" and "--runtime" not in " ".join(self.default_args):
    args.extend(["--runtime", self.runtime])
```

如果用户已通过 `default_args` 手动传入 `--runtime`，不会重复注入。

### 无效 runtime 回退

```python
self.runtime = runtime if runtime in self.SUPPORTED_RUNTIMES else "runc"
```

传入不支持的 runtime 会静默回退到 `"runc"`，不会崩溃。

---

## 前置条件

### 安装 gVisor

```bash
# 下载 runsc
ARCH=x86_64
sudo curl -L -o /usr/local/bin/runsc \
    "https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}/runsc"
sudo chmod +x /usr/local/bin/runsc

# 验证
runsc --version
```

### 注册到 Docker daemon

编辑 `/etc/docker/daemon.json`：

```json
{
    "runtimes": {
        "runsc": {
            "path": "/usr/local/bin/runsc"
        }
    }
}
```

重启 Docker：

```bash
sudo systemctl restart docker
```

### 验证注册成功

```bash
docker info --format '{{json .Runtimes}}'
# 应包含 "runsc"
```

---

## 安全层级对比

| 层级 | 标准 Docker (runc) | Docker + gVisor (runsc) |
|------|-------------------|------------------------|
| 系统调用 | 直接到宿主内核 | runsc 用户态拦截 |
| `/proc` `/sys` | seccomp 部分过滤 | 完整虚拟化 |
| 内核漏洞利用面 | 较大 | 极小 |
| 性能开销 | ~0% | I/O 密集约 5-15% |
| 启动延迟 | 镜像拉取后 <1s | 相同 |
| 需要额外安装 | 否 | 需要 runsc 二进制 |

---

## 与 `sandbox-extension-framework` 的关系

本分支是 **纯代码改动**，不包含设计草案。

如果需要在设计层面讨论多平台沙箱扩展（包括 gVisor、Seatbelt、bwrap、landlock 等），请参考 `feature/sandbox-extension-framework` 分支的设计文档。

---

## 合并建议

1. **可独立合并**：本分支不依赖其他 feature 分支
2. **向后兼容**：默认 `runtime="runc"` 保持上游行为
3. **建议先合并本分支**，再合并 `sandbox-extension-framework` 的设计文档
