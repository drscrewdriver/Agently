# PR #335 — gVisor Docker Runtime Integration

## 用意说明（供上游评审参考）

### 直接目标

在 Docker 执行资源提供器中集成 gVisor/runsc 运行时支持，为 AI Agent 代码执行提供更强的系统调用隔离能力。

### 关键设计决策

1. **Docker 运行时集成（而非独立 runsc）**
   - 复用现有 Docker 提供器的完整生命周期管理（grant、image、cleanup、health check）
   - 仅通过 `--runtime runsc` 参数切换到底层 OCI 运行时
   - 容器运行时贡献者可通过 `create_resource()` 工厂方法扩展

2. **Fail Closed 行为**
   - 当用户选择 `sandbox='gvisor'` 但 runsc 不可用时，显式报错而非静默回退到 runc
   - 在 `inspect_availability()` 中检测 runsc 二进制可用性

3. **隔离能力透明化**
   - 当 `runtime != "runc"` 时，`async_probe()` 覆盖隔离报告：
     - `mechanism: "container"` → `"gvisor_container"`
     - `syscalls_restricted` 基于 default_args 静态推断 → 强制 `true`
     - 新增 `container_runtime: "gvisor/runsc"`
   - 下游消费者无需理解不透明的 `runtime` 字段即可感知隔离级别

### 变更范围

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `DockerExecutionResourceProvider.py` | 修改 | 新增 `_inspect_runsc_availability()`、`async_probe()` 隔离覆盖、`runtime` 参数传递 |
| `ActionResourceRegistrar.py` | 修改 | `_normalize_code_sandbox()` 支持 `gvisor`/`runsc`/`gvisor/runsc` |
| `test_gvisor_execution_provider.py` | 新增 | 20 个测试，6 大类别，全部通过 |
| `conftest.py` | 修改 | 添加 `agently_stage_stub` 路径，支持无网络测试环境 |

### 测试覆盖

| 类别 | 测试数 | 说明 |
|------|-------|------|
| A. Docker 回归 | 3 | 确保 gVisor 变更不破坏现有 runc 行为 |
| B. gVisor Fail Closed | 5 | runsc 不可用时显式报错 |
| C. 隔离能力 | 2 | async_probe() 报告更强的隔离保证 |
| D. 管道集成 | 4 | sandbox='gvisor' 流经完整注册管道 |
| E. 清理/生命周期 | 3 | gVisor 容器正确清理 |
| F. 健康/探针一致性 | 3 | 四个报告通道一致反映 gVisor 状态 |
| **总计** | **20** | **全部通过** |

### 变更记录

```
54e15b7a fix: override isolation capabilities when gVisor/runsc is selected
```

### 与现有 PR 的关系

- Supersedes: #334（standalone runsc，已关闭）
- Related: #333（Docker 安全加固基线）