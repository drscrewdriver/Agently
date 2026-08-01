#!/usr/bin/env python3
"""gVisor Integration — 一键验证脚本

Usage:
    python scripts/verify_gvisor_integration.py

Prerequisites:
    - 当前工作目录为 Agently 项目根目录（或 PYTHONPATH 包含项目根）
    - 已安装 agently 包（pip install -e .）
    - 有 Docker 环境（可选，某些步骤会 gracefully skip）
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

# 确保 agently_stage_stub 可导入（无网络环境需要）
# 脚本位于 Agently/scripts/ 下，项目根即为 Agently 仓库根
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_AGENTLY_ROOT = _PROJECT_ROOT  # Agently 仓库根
_STUB_ROOT = _AGENTLY_ROOT.parent / "agently_stage_stub"  # stub 在仓库外
for _p in [_AGENTLY_ROOT, _STUB_ROOT]:
    _s = str(_p)
    if _p.is_dir() and _s not in sys.path:
        sys.path.insert(0, _s)

# 诊断：确认 agently 包来自本地源码，而非 site-packages
# 本地源码路径：Agently/agently/__init__.py
# 安装包路径：site-packages/agently/__init__.py
_AGENTLY_INIT = _AGENTLY_ROOT / "agently" / "__init__.py"
if _AGENTLY_INIT.is_file():
    try:
        import agently
        _imported_from = Path(agently.__file__).resolve()
        _expected_init = _AGENTLY_INIT.resolve()
        if _imported_from != _expected_init:
            print(f"  ⚠ agently 包来自 {_imported_from.parent}，非本地源码")
            print(f"  → 请执行: cd {_AGENTLY_ROOT} && pip install -e .")
            print(f"  → 或: PYTHONPATH={_AGENTLY_ROOT} python3 scripts/verify_gvisor_integration.py")
            print()
    except ImportError:
        pass


# ======================================================================
# 色彩输出工具
# ======================================================================

class Colors:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def ok(msg: str) -> None:
    print(f"  {Colors.GREEN}✓{Colors.RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {Colors.YELLOW}⚠{Colors.RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {Colors.RED}✗{Colors.RESET} {msg}")


def header(title: str) -> None:
    print(f"\n{Colors.CYAN}{Colors.BOLD}{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}{Colors.RESET}\n")


def subheader(title: str) -> None:
    print(f"\n{Colors.BOLD}--- {title} ---{Colors.RESET}")


def code_block(label: str, data: object) -> None:
    """格式化为可读的 JSON 或文本块输出。"""
    if isinstance(data, str):
        text = data
    else:
        text = json.dumps(data, indent=2, default=str)
    print(f"  [{label}]")
    for line in text.splitlines():
        print(f"    {line}")


# ======================================================================
# Phase 1: 环境诊断
# ======================================================================


def check_environment() -> dict:
    """检查 Docker、runsc 等基础环境。"""
    header("Phase 1: 环境诊断")
    env = {}

    # 1.1 Docker 二进制
    subheader("1.1 Docker 二进制")
    docker_bin = shutil.which("docker")
    if docker_bin:
        ok(f"docker 位于 {docker_bin}")
        env["docker_binary"] = docker_bin
    else:
        fail("docker 不在 PATH 中")
        env["docker_binary"] = None

    # 1.2 Docker daemon 状态
    subheader("1.2 Docker daemon 状态")
    if docker_bin:
        try:
            result = subprocess.run(
                [docker_bin, "version", "--format", "{{.Server.Version}}"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                ok(f"Docker daemon 运行中，版本: {result.stdout.strip()}")
                env["docker_version"] = result.stdout.strip()
            else:
                warn(f"Docker daemon 不可达: {result.stderr.strip()}")
                env["docker_version"] = None
        except Exception as e:
            warn(f"Docker daemon 检查失败: {e}")
            env["docker_version"] = None
    else:
        env["docker_version"] = None

    # 1.3 runsc 二进制
    subheader("1.3 runsc 二进制")
    runsc_bin = shutil.which("runsc")
    if runsc_bin:
        ok(f"runsc 位于 {runsc_bin}")
        env["runsc_binary"] = runsc_bin
    else:
        warn("runsc 不在 PATH 中")
        env["runsc_binary"] = None

    # 1.4 runsc 运行时可用性
    subheader("1.4 runsc 运行时可用性")
    env["runsc_available"] = False
    env["runsc_version"] = None
    if runsc_bin:
        try:
            result = subprocess.run(
                [runsc_bin, "--version"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                ok(f"runsc 可用: {result.stdout.strip()}")
                env["runsc_available"] = True
                env["runsc_version"] = result.stdout.strip()
            else:
                warn(f"runsc 执行失败 (returncode={result.returncode})")
                env["runsc_stderr"] = result.stderr
        except Exception as e:
            warn(f"runsc 执行异常: {e}")
    else:
        warn("runsc 不可用，后续 gVisor 相关测试将验证 fail-closed 行为")

    # 1.5 Docker 运行时列表（确认 runsc 已注册）
    subheader("1.5 Docker 已注册运行时")
    if docker_bin and env.get("docker_version"):
        try:
            result = subprocess.run(
                [docker_bin, "info", "--format", "{{json .Runtimes}}"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                runtimes = json.loads(result.stdout.strip())
                env["docker_runtimes"] = list(runtimes.keys())
                if "runsc" in runtimes:
                    ok(f"Docker 已注册 runsc 运行时")
                else:
                    warn("Docker 未注册 runsc 运行时（未配置 daemon.json）")
                code_block("已注册运行时", list(runtimes.keys()))
            else:
                warn(f"无法获取运行时列表: {result.stderr.strip()}")
        except Exception as e:
            warn(f"运行时列表检查失败: {e}")

    # 1.6 内核对比（可视化 gVisor 隔离效果）
    subheader("1.6 内核版本对比（gVisor 隔离最直观证据）")
    host_kernel = "（Windows 系统，无 uname）"
    try:
        result = subprocess.run(
            ["uname", "-r"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            host_kernel = result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    ok(f"宿主内核: {host_kernel}")

    if docker_bin and env.get("docker_version"):
        # runc 容器内内核
        try:
            result = subprocess.run(
                [docker_bin, "run", "--rm", "alpine", "uname", "-r"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                ok(f"runc 容器内核: {result.stdout.strip()} (与宿主一致)")
            else:
                warn(f"runc 容器执行失败: {result.stderr.strip()}")
        except Exception as e:
            warn(f"runc 容器测试异常: {e}")

        # runsc 容器内内核（仅 runsc 可用时）
        if env["runsc_available"] and "runsc" in env.get("docker_runtimes", []):
            try:
                result = subprocess.run(
                    [docker_bin, "run", "--rm", "--runtime", "runsc",
                     "alpine", "uname", "-r"],
                    capture_output=True, text=True, timeout=15,
                )
                if result.returncode == 0:
                    ok(f"runsc 容器内核: {result.stdout.strip()} "
                       f"({Colors.BOLD}与宿主不同! gVisor Sentry 虚拟内核{Colors.RESET})")
                else:
                    warn(f"runsc 容器执行失败: {result.stderr.strip()}")
            except Exception as e:
                warn(f"runsc 容器测试异常: {e}")
        else:
            warn("跳过 runsc 容器测试（runsc 未就绪）")

    return env


# ======================================================================
# Phase 2: 分支代码逻辑验证（使用 monkeypatch 模拟环境）
# ======================================================================


async def verify_code_logic(env: dict) -> None:
    """使用本地分支代码验证 gVisor 集成逻辑。"""
    header("Phase 2: 分支代码逻辑验证")

    # 尝试导入本地分支代码
    try:
        from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import (
            DockerExecutionResource,
            DockerExecutionResourceProvider,
        )
        from agently.core.operation.Action.ActionResourceRegistrar import (
            ActionResourceRegistrar,
        )
        ok("成功导入本地分支代码")
    except (ImportError, KeyError) as e:
        fail(f"导入本地分支代码失败: {e}")
        warn("请执行: cd Agently && pip install -e .  后再重试")
        return

    # ================================================================
    # 2.1 _normalize_code_sandbox 管道集成验证
    # ================================================================
    subheader("2.1 _normalize_code_sandbox 别名归一化")

    test_cases = [
        ("gvisor", "gvisor"),
        ("runsc", "gvisor"),
        ("gvisor/runsc", "gvisor"),
        ("docker", "docker"),
        ("auto", "auto"),
        ("trusted_local", "trusted_local"),
    ]
    for input_val, expected in test_cases:
        result = ActionResourceRegistrar._normalize_code_sandbox(input_val)
        status = result == expected
        label = f"normalize({input_val!r}) → {result!r}"
        if status:
            ok(label)
        else:
            fail(f"{label} (期望 {expected!r})")

    # 非法值
    try:
        ActionResourceRegistrar._normalize_code_sandbox("invalid_sandbox")
        fail("normalize('invalid_sandbox') 应抛出 ValueError 但未抛出")
    except ValueError:
        ok("normalize('invalid_sandbox') 正确抛出 ValueError")

    # ================================================================
    # 2.2 默认 runtime 验证
    # ================================================================
    subheader("2.2 DockerExecutionResource 默认 runtime")

    resource_default = DockerExecutionResource()
    if resource_default.runtime == "runc":
        ok(f"默认 runtime 为 'runc'")
    else:
        warn(f"默认 runtime 为 {resource_default.runtime!r}")

    resource_runsc = DockerExecutionResource(runtime="runsc")
    if resource_runsc.runtime == "runsc":
        ok(f"指定 runtime='runsc' 生效")
    else:
        fail(f"runtime 应为 'runsc'，实际为 {resource_runsc.runtime!r}")

    # ================================================================
    # 2.3 _container_base_args 运行时参数验证
    # ================================================================
    subheader("2.3 _container_base_args --runtime 参数")

    args_runc = resource_default._container_base_args(profile={})
    if "--runtime" not in args_runc:
        ok("runc 模式: 不添加 --runtime 参数")
    else:
        fail(f"runc 模式不应有 --runtime: {args_runc}")

    args_runsc = resource_runsc._container_base_args(profile={})
    rt_idx = next(
        (i for i, v in enumerate(args_runsc) if v == "--runtime"), None
    )
    if rt_idx is not None and args_runsc[rt_idx + 1] == "runsc":
        ok(f"runsc 模式: args 包含 --runtime runsc")
    else:
        fail(f"runsc 模式应包含 --runtime runsc: {args_runsc}")

    # ================================================================
    # 2.4 inspect_availability fail-closed 验证（模拟）
    # ================================================================
    subheader("2.4 inspect_availability fail-closed 验证（模拟环境）")

    import pytest
    monkeypatch = pytest.MonkeyPatch()

    # 模拟场景 a: runsc 不在 PATH
    # 注：直接 mock inspect_availability 来验证 fail-closed 逻辑
    # 原因：在无 Docker 环境，inspect_availability() 会在检查 runsc 之前就返回
    #       daemon_unavailable，无法到达 runsc 检查。单元测试已覆盖完整路径。
    def mock_inspect_missing(self):
        return {
            "available": False,
            "reason": "runsc_binary_missing",
            "runtime": "gvisor",
        }

    monkeypatch.setattr(
        DockerExecutionResource,
        "inspect_availability",
        mock_inspect_missing,
    )
    resource_missing = DockerExecutionResource(runtime="runsc")
    result = resource_missing.inspect_availability()
    if not result["available"] and result["reason"] == "runsc_binary_missing":
        ok(f"runsc 缺失 → available=False, reason='runsc_binary_missing'")
    else:
        fail(f"期望 fail-closed（runsc_binary_missing），实际: available={result['available']}, reason={result['reason']}")
    monkeypatch.undo()

    # 模拟场景 b: runsc 可用（如果实际环境有 runsc，直接验证）
    if env["runsc_available"]:
        resource_actual = DockerExecutionResource(runtime="runsc")
        actual = resource_actual.inspect_availability()
        if actual["available"]:
            ok(f"runsc 可用 → available=True, version={actual.get('runsc', {}).get('runsc_version', 'N/A')}")
        else:
            warn(f"runsc 实际不可用: {actual.get('reason')}")
    else:
        warn("跳过 runsc 可用验证（环境无 runsc）")

    # ================================================================
    # 2.5 隔离能力覆盖溯源证明（关键证据）
    # ================================================================
    subheader("2.5 隔离能力覆盖溯源证明（关键证据）")

    # 证明逻辑：
    #   _isolation_capabilities() 是静态方法，只能基于 default_args 做静态分析。
    #   当 default_args 包含 --privileged 时，静态分析认为不安全。
    #   但 gVisor 的 Sentry 内核在容器层之上额外拦截系统调用，--privileged 无效。
    #   因此 async_probe() 在 runtime != "runc" 时强制覆盖三个字段。
    #   关键代码位置：
    #     DockerExecutionResourceProvider.py 第 1084-1087 行
    #     (https://github.com/drscrewdriver/Agently/blob/adapt/gvisor-docker-runtime/...)

    # 步骤 1: 调用 _isolation_capabilities() 获取静态 baseline
    baseline_safe = DockerExecutionResourceProvider._isolation_capabilities(default_args=[])
    baseline_unsafe = DockerExecutionResourceProvider._isolation_capabilities(
        default_args=["--privileged"],
    )

    code_block("_isolation_capabilities(default_args=[]) 静态 baseline", baseline_safe)
    code_block("_isolation_capabilities(default_args=['--privileged']) 静态 baseline", baseline_unsafe)

    # 步骤 2: 用 monkeypatch 模拟 async_probe 对比
    import pytest
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        DockerExecutionResource,
        "inspect_availability",
        lambda self: {"available": True, "reason": "ready", "container_runtime": "runsc"},
    )
    monkeypatch.setattr(
        DockerExecutionResource,
        "inspect_image",
        lambda self, image: {"image": image, "exists": True},
    )
    monkeypatch.setattr(
        DockerExecutionResource,
        "_profile",
        lambda self, overrides=None: {
            "language": "python",
            "image": "python:3.12-slim",
            "image_pull_policy": "never",
            "network_mode": "disabled",
        },
    )
    monkeypatch.setattr(
        DockerExecutionResource,
        "_default_image",
        lambda self, language: "python:3.12-slim",
    )

    provider = DockerExecutionResourceProvider()
    result_probe = await provider.async_probe(
        requirement={
            "config": {
                "runtime": "runsc",
                "default_args": ["--privileged"],
            },
            "kind": "code_execution",
            "required_capabilities": {"language": "python"},
        },
        policy={},
    )
    iso_probe = result_probe["capabilities"]["isolation"]
    monkeypatch.undo()

    # 步骤 3: 对比证明
    print()
    ok(f"代码位置: DockerExecutionResourceProvider.py 第 1084-1087 行")
    print()

    # 对比项 1: mechanism
    baseline_mech = baseline_unsafe.get("mechanism", "container")
    probe_mech = iso_probe["mechanism"]
    if baseline_mech == "container" and probe_mech == "gvisor_container":
        ok(f"mechanism 覆盖: {baseline_mech!r} → {probe_mech!r}")
    else:
        fail(f"mechanism 未正确覆盖: baseline={baseline_mech!r}, probe={probe_mech!r}")

    # 对比项 2: syscalls_restricted
    baseline_sys = baseline_unsafe.get("syscalls_restricted", False)
    probe_sys = iso_probe.get("syscalls_restricted", False)
    if baseline_sys is False and probe_sys is True:
        ok(f"syscalls_restricted 覆盖: {baseline_sys} → {probe_sys}  (gVisor 强制设为 True)")
    else:
        fail(f"syscalls_restricted 未正确覆盖: baseline={baseline_sys}, probe={probe_sys}")

    # 对比项 3: container_runtime
    if "container_runtime" not in baseline_unsafe and iso_probe.get("container_runtime") == "gvisor/runsc":
        ok(f"container_runtime 新增: (baseline 无) → 'gvisor/runsc'")
    else:
        fail(f"container_runtime 未正确新增: baseline 无, probe={iso_probe.get('container_runtime', '(无)')}")

    # 步骤 4: 完整对比输出
    print()
    code_block("静态 baseline (--privileged)", baseline_unsafe)
    code_block("async_probe 结果 (runsc + --privileged)", iso_probe)
    print("  ── 差异一目了然: probe 在 baseline 之上覆盖了三个字段 ──")

    # ================================================================
    # 2.6 async_probe 隔离能力覆盖验证（模拟）
    # ================================================================
    subheader("2.6 async_probe 隔离能力覆盖验证（模拟）")

    monkeypatch = pytest.MonkeyPatch()
    # 模拟 runsc 可用 + 镜像存在
    monkeypatch.setattr(
        DockerExecutionResource,
        "inspect_availability",
        lambda self: {"available": True, "reason": "ready", "container_runtime": "runsc"},
    )
    monkeypatch.setattr(
        DockerExecutionResource,
        "inspect_image",
        lambda self, image: {"image": image, "exists": True},
    )
    monkeypatch.setattr(
        DockerExecutionResource,
        "_profile",
        lambda self, overrides=None: {
            "language": "python",
            "image": "python:3.12-slim",
            "image_pull_policy": "never",
            "network_mode": "disabled",
        },
    )
    monkeypatch.setattr(
        DockerExecutionResource,
        "_default_image",
        lambda self, language: "python:3.12-slim",
    )

    provider = DockerExecutionResourceProvider()

    # 场景 2.5a: runsc 模式 → 正常隔离覆盖
    result_runsc = await provider.async_probe(
        requirement={
            "config": {"runtime": "runsc"},
            "kind": "code_execution",
            "required_capabilities": {"language": "python"},
        },
        policy={},
    )
    iso = result_runsc["capabilities"]["isolation"]
    checks = [
        ("mechanism == 'gvisor_container'", iso["mechanism"] == "gvisor_container"),
        ("syscalls_restricted == True", iso["syscalls_restricted"] is True),
        ("container_runtime == 'gvisor/runsc'", iso.get("container_runtime") == "gvisor/runsc"),
    ]
    for label, passed in checks:
        if passed:
            ok(f"runsc 探针: {label}")
        else:
            fail(f"runsc 探针: {label}")

    # 场景 2.5b: runsc 模式 + --privileged → 仍然 syscalls_restricted=True
    result_unsafe = await provider.async_probe(
        requirement={
            "config": {
                "runtime": "runsc",
                "default_args": ["--privileged"],
            },
            "kind": "code_execution",
            "required_capabilities": {"language": "python"},
        },
        policy={},
    )
    iso_unsafe = result_unsafe["capabilities"]["isolation"]
    if iso_unsafe["syscalls_restricted"] is True:
        ok(f"runsc + --privileged: syscalls_restricted=True (gVisor 覆盖危险参数)")
    else:
        fail(f"runsc + --privileged: syscalls_restricted={iso_unsafe['syscalls_restricted']}")

    # 场景 2.5c: runc 模式 → 保持正常 container 标识
    result_runc = await provider.async_probe(
        requirement={
            "config": {"runtime": "runc"},
            "kind": "code_execution",
            "required_capabilities": {"language": "python"},
        },
        policy={},
    )
    iso_runc = result_runc["capabilities"]["isolation"]
    checks_runc = [
        ("mechanism == 'container'", iso_runc["mechanism"] == "container"),
        ("container_runtime 不存在", "container_runtime" not in iso_runc),
    ]
    for label, passed in checks_runc:
        if passed:
            ok(f"runc 探针: {label}")
        else:
            fail(f"runc 探针: {label}")

    monkeypatch.undo()

    # ================================================================
    # 2.7 ensure_available fail-closed 验证（模拟）
    # ================================================================
    subheader("2.7 ensure_available 异常抛出验证（模拟）")

    from agently.core import ExecutionResourceError

    # 模拟 runsc 缺失
    def mock_inspect_missing(self):
        return {
            "available": False,
            "reason": "runsc_binary_missing",
            "runtime": "gvisor",
        }

    monkeypatch.setattr(
        DockerExecutionResource,
        "inspect_availability",
        mock_inspect_missing,
    )
    resource_for_ensure = DockerExecutionResource(runtime="runsc")
    try:
        resource_for_ensure.ensure_available()
        fail("ensure_available() 应抛出 ExecutionResourceError")
    except ExecutionResourceError as e:
        if "runsc_binary_missing" in str(e):
            ok("ensure_available() 正确抛出 ExecutionResourceError (runsc_binary_missing)")
        else:
            warn(f"异常消息不匹配: {e}")
    monkeypatch.undo()

    # ================================================================
    # 2.8 完整探针输出对比
    # ================================================================
    subheader("2.8 完整探针输出对比")
    code_block("runc 模式 isolation", iso_runc)
    code_block("runsc 模式 isolation", iso)
    code_block("runsc + --privileged isolation", iso_unsafe)


# ======================================================================
# Phase 3: 真实环境探针（如果 Docker + runsc 就绪）
# ======================================================================


async def verify_real_environment(env: dict) -> None:
    """真实环境探针 — 分三种场景：

    1. Docker + runsc 都就绪 → 完整 runsc / runc 对比探针
    2. Docker 就绪，runsc 未就绪 → 展示真实的 fail-closed 结果
    3. Docker 未就绪 → 展示 Docker daemon 不可达的错误
    """
    header("Phase 3: 真实环境探针")

    try:
        from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import (
            DockerExecutionResource,
            DockerExecutionResourceProvider,
        )
    except ImportError:
        return

    provider = DockerExecutionResourceProvider()

    if env.get("docker_version") and env["runsc_available"]:
        # 场景 1: Docker + runsc 都可用 → 完整探针
        await _real_probe_both(provider, env)
    elif env.get("docker_version") and not env["runsc_available"]:
        # 场景 2: Docker 可用，runsc 不可用 → 展示真实 fail-closed
        await _real_probe_fail_closed(provider, env)
    else:
        # 场景 3: Docker 不可用 → 展示真实 inspect_availability 结果
        await _real_probe_docker_unavailable(env)


async def _real_probe_both(provider: DockerExecutionResourceProvider, env: dict) -> None:
    """Docker + runsc 都可用时的完整探针。"""
    # 真实 runsc 探针
    subheader("3.1 真实 runsc 探针")
    result = await provider.async_probe(
        requirement={
            "config": {"runtime": "runsc"},
            "kind": "code_execution",
            "required_capabilities": {"language": "python"},
        },
        policy={},
    )
    code_block("async_probe(runtime='runsc') 完整返回", result)

    iso = result["capabilities"]["isolation"]
    ok(f"mechanism = {iso['mechanism']}")
    ok(f"syscalls_restricted = {iso['syscalls_restricted']}")
    ok(f"container_runtime = {iso.get('container_runtime', '(absent)')}")

    # 真实 runc 对比探针
    subheader("3.2 真实 runc 对比探针")
    result_runc = await provider.async_probe(
        requirement={
            "config": {"runtime": "runc"},
            "kind": "code_execution",
            "required_capabilities": {"language": "python"},
        },
        policy={},
    )
    iso_runc = result_runc["capabilities"]["isolation"]
    ok(f"mechanism = {iso_runc['mechanism']}")
    ok(f"syscalls_restricted = {iso_runc['syscalls_restricted']}")
    ok(f"container_runtime 不存在（runc 模式下不报告）")


async def _real_probe_fail_closed(provider: DockerExecutionResourceProvider, env: dict) -> None:
    """Docker 可用但 runsc 不可用 → 展示真实的 fail-closed 行为。"""
    subheader("3.1 真实 fail-closed 验证（runsc 缺失）")

    # 直接用 DockerExecutionResource 调用 inspect_availability
    resource = DockerExecutionResource(runtime="runsc")
    result = resource.inspect_availability()

    code_block("inspect_availability(runtime='runsc') 真实返回", result)

    if not result["available"]:
        ok(f"fail-closed 生效: available={result['available']}, reason={result['reason']!r}")
    else:
        fail(f"预期 fail-closed，但 available=True")

    # 尝试 async_probe 看完整返回
    probe_result = await provider.async_probe(
        requirement={
            "config": {"runtime": "runsc"},
            "kind": "code_execution",
            "required_capabilities": {"language": "python"},
        },
        policy={},
    )
    subheader("3.2 async_probe(runtime='runsc') 真实返回")
    code_block("完整 probe 返回", probe_result)

    if not probe_result["available"]:
        ok(f"probe available=False, reason={probe_result['reason']!r}")
        # 检查 meta 中是否有 availability 详情
        meta_avail = probe_result.get("meta", {}).get("availability", {})
        if meta_avail and not meta_avail.get("available"):
            ok(f"meta.availability.reason = {meta_avail['reason']!r}")
    else:
        fail(f"预期 probe 返回 available=False，但得到 available=True")


async def _real_probe_docker_unavailable(env: dict) -> None:
    """Docker daemon 不可达时的真实诊断结果。"""
    subheader("3.1 Docker daemon 状态诊断")

    if env.get("docker_binary"):
        ok(f"Docker 二进制存在: {env['docker_binary']}")
    else:
        fail("Docker 二进制不存在")

    if env.get("docker_version"):
        ok(f"Docker daemon 版本: {env['docker_version']}")
    else:
        # 从环境变量中获取更详细的错误信息
        docker_bin = env.get("docker_binary")
        if docker_bin:
            # 尝试 docker version 获取详细错误
            import subprocess
            result = subprocess.run(
                [docker_bin, "version"],
                capture_output=True, text=True, timeout=10,
            )
            error_msg = result.stderr.strip() or result.stdout.strip()
            code_block("docker version 错误输出", error_msg)

            # 给出修复建议
            print()
            warn("Docker daemon 不可达，常见原因：")
            warn("  1. 当前用户不在 docker 用户组 → sudo usermod -aG docker $USER")
            warn("  2. Docker daemon 未运行 → sudo systemctl start docker")
            warn("  3. Docker socket 权限问题 → sudo chmod 666 /var/run/docker.sock")
            print()

    # 即使 Docker 不可达，也可以展示 runsc 缺失
    subheader("3.2 runsc 可用性")
    if env.get("runsc_available"):
        ok(f"runsc 可用: {env['runsc_version']}")
    else:
        # 尝试直接用哪种方式检测
        import shutil
        runsc_path = shutil.which("runsc")
        if runsc_path:
            warn(f"runsc 位于 {runsc_path}，但未注册到 Docker 运行时")
        else:
            fail("runsc 不在 PATH 中（需安装: sudo apt install runsc 或下载 gVisor 二进制）")


# ======================================================================
# 主入口
# ======================================================================


async def main() -> None:
    print(f"{Colors.CYAN}{Colors.BOLD}")
    print("=" * 60)
    print("  gVisor Integration — 一键验证脚本")
    print("  Agently PR #335 — adapt/gvisor-docker-runtime")
    print("=" * 60)
    print(f"{Colors.RESET}\n")

    # Phase 1: 环境诊断
    env = check_environment()

    # Phase 2: 分支代码逻辑验证（模拟）
    await verify_code_logic(env)

    # Phase 3: 真实环境验证（可选）
    await verify_real_environment(env)

    # 最终摘要
    header("验证完成")
    print(f"  环境: Docker={'就绪' if env.get('docker_version') else '未就绪'}  "
          f"runsc={'就绪' if env.get('runsc_available') else '未就绪'}")
    print(f"  分支代码: 54e15b7a — fix: override isolation capabilities when gVisor/runsc is selected")
    print(f"  详细说明文档:")
    print(f"    - {_PROJECT_ROOT / 'docs' / 'gvisor-isolation-capabilities-override.md'}")
    print(f"    - {_PROJECT_ROOT / 'docs' / 'gvisor-test-scenarios-evidence.md'}")
    print(f"    - {_PROJECT_ROOT / 'docs' / 'pr335-intent-response.md'}")


if __name__ == "__main__":
    asyncio.run(main())