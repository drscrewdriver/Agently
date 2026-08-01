# gVisor/runsc Isolation Capabilities Override

## 变更概述

在 `DockerExecutionResourceProvider.async_probe()` 中，当检测到运行时为 `runsc`（gVisor）时，覆盖隔离能力报告，使下游消费者能够感知到更强的隔离保证。

### 变更位置

- **文件**: [`DockerExecutionResourceProvider.py`](file:///e:/test/rewrite-agently/Agently/agently/builtins/plugins/ExecutionResourceProvider/DockerExecutionResourceProvider.py)
- **行号**: 1076-1087（`async_probe` 方法内）
- **相关测试**: [`test_gvisor_execution_provider.py`](file:///e:/test/rewrite-agently/Agently/tests/test_gvisor_execution_provider.py) (`TestGVisorIsolationCapabilities`)

### 变更代码

```python
# async_probe 方法中，在构建 isolation 字典后：
isolation: dict[str, Any] = {
    **self._isolation_capabilities(default_args),
    "network_mode": str(runtime_profile.get("network_mode", "disabled")),
}
# 当 gVisor/runsc 被选中时，Sentry 用户空间内核强制执行
# 严格的系统调用过滤，无论 default_args 如何请求。
# 覆盖静态隔离报告，使下游消费者无需检查不透明的 runtime 字段。
if runtime != "runc":
    isolation["syscalls_restricted"] = True
    isolation["mechanism"] = "gvisor_container"
    isolation["container_runtime"] = "gvisor/runsc"
```

## 为什么需要这个变更

### 问题背景

`_isolation_capabilities()` 是一个静态方法，它基于 `default_args` 参数静态分析容器是否具备隔离能力。该方法的工作原理是检查 `--privileged`、`--pid=host`、`--cap-add` 等危险参数是否存在，据此推断隔离级别。

**静态分析的局限性**：`_isolation_capabilities()` 无法感知运行时的选择。当用户选择 `runtime="runsc"` 时，gVisor 的 Sentry 用户空间内核会在容器层面之上强制执行系统调用过滤——即使 `default_args` 中传入了 `--privileged`，gVisor 仍然会拦截并过滤所有系统调用。静态分析无法捕捉到这个信息。

### 解决方式

在 `async_probe()` 中，`isolation` 字典构建完成后，追加一个运行时检查：

- 如果 `runtime == "runc"` → 保持现状，`mechanism` 为 `"container"`，`syscalls_restricted` 基于 `default_args` 静态分析
- 如果 `runtime != "runc"`（即 `runsc`）→ 覆盖三个字段：
  - `syscalls_restricted = True`（gVisor 保证系统调用过滤）
  - `mechanism = "gvisor_container"`（标识隔离机制为 gVisor 容器）
  - `container_runtime = "gvisor/runsc"`（明确标识运行时）

### 效果

下游消费者（如 `ExecutionResourceManager`、`ActionResourceRegistrar`）可以通过检查 `isolation["mechanism"]` 和 `isolation["syscalls_restricted"]` 来了解当前容器的真实隔离级别，而不需要理解 `runtime` 字段的含义。

### 测试覆盖

`TestGVisorIsolationCapabilities` 类包含两个测试：

1. **`test_async_probe_gvisor_mechanism_is_gvisor_container`** — 验证 `runtime='runsc'` 时，返回 `mechanism='gvisor_container'`、`syscalls_restricted=True`、`container_runtime='gvisor/runsc'`
2. **`test_async_probe_gvisor_overrides_unsafe_args`** — 验证即使 `default_args` 包含 `--privileged`，gVisor 模式下仍然强制 `syscalls_restricted=True`

## 测试结果

```
tests/test_gvisor_execution_provider.py::TestGVisorIsolationCapabilities::test_async_probe_gvisor_mechanism_is_gvisor_container PASSED
tests/test_gvisor_execution_provider.py::TestGVisorIsolationCapabilities::test_async_probe_gvisor_overrides_unsafe_args PASSED
```

全量 20 个测试均通过，涵盖 Docker 回归、gVisor 故障关闭、隔离能力、管道集成、清理/生命周期、健康/探针/一致性六大类别。

## 附带变更：`conftest.py` 添加 `agently_stage_stub` 路径

为支持测试环境，[`conftest.py`](file:///e:/test/rewrite-agently/Agently/tests/conftest.py) 添加了 `agently_stage_stub` 模块的路径注入，使得无网络环境也能运行测试。

```python
_STUB_ROOT = PROJECT_ROOT.parent / "agently_stage_stub"
stub_root_str = str(_STUB_ROOT)
if _STUB_ROOT.is_dir() and stub_root_str not in sys.path:
    sys.path.insert(0, stub_root_str)
```