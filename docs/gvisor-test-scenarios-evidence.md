# gVisor Docker Runtime — 测试情景清单与证据

## 测试情景分类

### A. Docker 回归（3 个测试）

确保 gVisor 变更不破坏现有 Docker (runc) 行为。

| # | 测试名称 | 情景描述 | 断言 |
|---|---------|---------|------|
| A1 | `test_default_runtime_is_runc` | 默认 DockerExecutionResource 的 runtime 为 runc | `resource.runtime == "runc"` |
| A2 | `test_container_base_args_no_runtime_flag_when_runc` | runc 模式下不添加 `--runtime` 参数 | `"--runtime" not in args` |
| A3 | `test_async_probe_runc_mechanism_is_container` | runc 探针报告 `mechanism='container'`，不含 `container_runtime` | `isolation["mechanism"] == "container"`，`container_runtime` 不存在 |

**证据**：3/3 通过

```
tests/test_gvisor_execution_provider.py::TestDockerRegression::test_default_runtime_is_runc PASSED
tests/test_gvisor_execution_provider.py::TestDockerRegression::test_container_base_args_no_runtime_flag_when_runc PASSED
tests/test_gvisor_execution_provider.py::TestDockerRegression::test_async_probe_runc_mechanism_is_container PASSED
```

### B. gVisor Fail Closed（5 个测试）

当 `sandbox='gvisor'` 但 runsc 不可用时，必须显式报错而非静默回退。

| # | 测试名称 | 情景描述 | 断言 |
|---|---------|---------|------|
| B1 | `test_inspect_availability_runsc_binary_missing` | runsc 不在 PATH 中 | `available=False, reason='runsc_binary_missing'` |
| B2 | `test_inspect_availability_runsc_binary_fails` | runsc 存在但返回非零退出码 | `available=False, reason='runsc_unavailable'` |
| B3 | `test_inspect_availability_runsc_works` | runsc 正常可用 | `available=True, runsc_version 存在` |
| B4 | `test_docker_still_works_when_runsc_missing` | 选择 docker(runc) 时，runsc 缺失不影响 | `available=True, container_runtime='runc'` |
| B5 | `test_ensure_available_raises_when_runsc_missing` | `ensure_available()` 抛出 `ExecutionResourceError` | 异常消息含 `runsc_binary_missing` |

**证据**：5/5 通过

```
tests/test_gvisor_execution_provider.py::TestGVisorFailClosed::test_inspect_availability_runsc_binary_missing PASSED
tests/test_gvisor_execution_provider.py::TestGVisorFailClosed::test_inspect_availability_runsc_binary_fails PASSED
tests/test_gvisor_execution_provider.py::TestGVisorFailClosed::test_inspect_availability_runsc_works PASSED
tests/test_gvisor_execution_provider.py::TestGVisorFailClosed::test_docker_still_works_when_runsc_missing PASSED
tests/test_gvisor_execution_provider.py::TestGVisorFailClosed::test_ensure_available_raises_when_runsc_missing PASSED
```

### C. gVisor 隔离能力（2 个测试）

`async_probe()` 报告 gVisor 模式下更强的隔离保证。

| # | 测试名称 | 情景描述 | 断言 |
|---|---------|---------|------|
| C1 | `test_async_probe_gvisor_mechanism_is_gvisor_container` | runtime='runsc' 时 | `mechanism='gvisor_container'`, `syscalls_restricted=True`, `container_runtime='gvisor/runsc'` |
| C2 | `test_async_probe_gvisor_overrides_unsafe_args` | 即使 default_args 包含 `--privileged` | `syscalls_restricted=True`, `mechanism='gvisor_container'` |

**证据**：2/2 通过

```
tests/test_gvisor_execution_provider.py::TestGVisorIsolationCapabilities::test_async_probe_gvisor_mechanism_is_gvisor_container PASSED
tests/test_gvisor_execution_provider.py::TestGVisorIsolationCapabilities::test_async_probe_gvisor_overrides_unsafe_args PASSED
```

### D. 管道集成（4 个测试）

`sandbox='gvisor'` 通过完整的注册管道流动。

| # | 测试名称 | 情景描述 | 断言 |
|---|---------|---------|------|
| D1 | `test_normalize_code_sandbox_gvisor` | `_normalize_code_sandbox('gvisor')` | 返回 `'gvisor'` |
| D2 | `test_normalize_code_sandbox_gvisor_runsc_alias` | `_normalize_code_sandbox('gvisor/runsc')` | 规约为 `'gvisor'` |
| D3 | `test_normalize_code_sandbox_runsc_alias` | `_normalize_code_sandbox('runsc')` | 规约为 `'gvisor'` |
| D4 | `test_normalize_code_sandbox_rejects_invalid` | 未知值 | 抛出 `ValueError` |

**证据**：4/4 通过

```
tests/test_gvisor_execution_provider.py::TestGVisorPipelineIntegration::test_normalize_code_sandbox_gvisor PASSED
tests/test_gvisor_execution_provider.py::TestGVisorPipelineIntegration::test_normalize_code_sandbox_gvisor_runsc_alias PASSED
tests/test_gvisor_execution_provider.py::TestGVisorPipelineIntegration::test_normalize_code_sandbox_runsc_alias PASSED
tests/test_gvisor_execution_provider.py::TestGVisorPipelineIntegration::test_normalize_code_sandbox_rejects_invalid PASSED
```

### E. 清理 / 生命周期（3 个测试）

gVisor 容器被正确清理。

| # | 测试名称 | 情景描述 | 断言 |
|---|---------|---------|------|
| E1 | `test_async_close_cleans_up_active_containers` | `async_close()` 移除所有活跃容器 | `removed == ["gvisor-c1", "gvisor-c2"]`, `_active_containers == set()` |
| E2 | `test_remove_container_timeout_raises` | 容器删除超时 | 抛出 `RuntimeError` 含 `container_cleanup_timeout` |
| E3 | `test_run_container_with_closed_resource` | 已关闭的资源拒绝新容器 | 返回 `ok=False`, error 含 `closed` |

**证据**：3/3 通过

```
tests/test_gvisor_execution_provider.py::TestGVisorCleanup::test_async_close_cleans_up_active_containers PASSED
tests/test_gvisor_execution_provider.py::TestGVisorCleanup::test_remove_container_timeout_raises PASSED
tests/test_gvisor_execution_provider.py::TestGVisorCleanup::test_run_container_with_closed_resource PASSED
```

### F. 健康 / 探针 / 确保一致性（3 个测试）

所有四个报告通道一致反映 gVisor 状态。

| # | 测试名称 | 情景描述 | 断言 |
|---|---------|---------|------|
| F1 | `test_health_check_unhealthy_when_runsc_unavailable` | runsc 不可用时 health check | 返回 `'unhealthy'` |
| F2 | `test_health_check_ready_when_gvisor_available` | gVisor 正常时 health check | 返回 `'ready'` |
| F3 | `test_inspect_availability_returns_container_runtime` | availability 包含 `container_runtime` | `result["container_runtime"] == "runsc"` |

**证据**：3/3 通过

```
tests/test_gvisor_execution_provider.py::TestGVisorConsistency::test_health_check_unhealthy_when_runsc_unavailable PASSED
tests/test_gvisor_execution_provider.py::TestGVisorConsistency::test_health_check_ready_when_gvisor_available PASSED
tests/test_gvisor_execution_provider.py::TestGVisorConsistency::test_inspect_availability_returns_container_runtime PASSED
```

## 汇总

| 类别 | 测试数 | 通过 | 失败 |
|------|-------|------|------|
| A. Docker 回归 | 3 | 3 | 0 |
| B. gVisor Fail Closed | 5 | 5 | 0 |
| C. gVisor 隔离能力 | 2 | 2 | 0 |
| D. 管道集成 | 4 | 4 | 0 |
| E. 清理/生命周期 | 3 | 3 | 0 |
| F. 健康/探针一致性 | 3 | 3 | 0 |
| **总计** | **20** | **20** | **0** |

## 测试运行日志

```
============================= test session starts =============================
platform win32 -- Python 3.13.10, pytest-9.0.3, pluggy-1.6.0
rootdir: E:\test\rewrite-agently\Agently
configfile: pytest.ini
collected 20 items

tests/test_gvisor_execution_provider.py::TestDockerRegression::test_default_runtime_is_runc PASSED
tests/test_gvisor_execution_provider.py::TestDockerRegression::test_container_base_args_no_runtime_flag_when_runc PASSED
tests/test_gvisor_execution_provider.py::TestDockerRegression::test_async_probe_runc_mechanism_is_container PASSED
tests/test_gvisor_execution_provider.py::TestGVisorFailClosed::test_inspect_availability_runsc_binary_missing PASSED
tests/test_gvisor_execution_provider.py::TestGVisorFailClosed::test_inspect_availability_runsc_binary_fails PASSED
tests/test_gvisor_execution_provider.py::TestGVisorFailClosed::test_inspect_availability_runsc_works PASSED
tests/test_gvisor_execution_provider.py::TestGVisorFailClosed::test_docker_still_works_when_runsc_missing PASSED
tests/test_gvisor_execution_provider.py::TestGVisorFailClosed::test_ensure_available_raises_when_runsc_missing PASSED
tests/test_gvisor_execution_provider.py::TestGVisorIsolationCapabilities::test_async_probe_gvisor_mechanism_is_gvisor_container PASSED
tests/test_gvisor_execution_provider.py::TestGVisorIsolationCapabilities::test_async_probe_gvisor_overrides_unsafe_args PASSED
tests/test_gvisor_execution_provider.py::TestGVisorPipelineIntegration::test_normalize_code_sandbox_gvisor PASSED
tests/test_gvisor_execution_provider.py::TestGVisorPipelineIntegration::test_normalize_code_sandbox_gvisor_runsc_alias PASSED
tests/test_gvisor_execution_provider.py::TestGVisorPipelineIntegration::test_normalize_code_sandbox_runsc_alias PASSED
tests/test_gvisor_execution_provider.py::TestGVisorPipelineIntegration::test_normalize_code_sandbox_rejects_invalid PASSED
tests/test_gvisor_execution_provider.py::TestGVisorCleanup::test_async_close_cleans_up_active_containers PASSED
tests/test_gvisor_execution_provider.py::TestGVisorCleanup::test_remove_container_timeout_raises PASSED
tests/test_gvisor_execution_provider.py::TestGVisorCleanup::test_run_container_with_closed_resource PASSED
tests/test_gvisor_execution_provider.py::TestGVisorConsistency::test_health_check_unhealthy_when_runsc_unavailable PASSED
tests/test_gvisor_execution_provider.py::TestGVisorConsistency::test_health_check_ready_when_gvisor_available PASSED
tests/test_gvisor_execution_provider.py::TestGVisorConsistency::test_inspect_availability_returns_container_runtime PASSED

============================= 20 passed in 10.35s =============================
```