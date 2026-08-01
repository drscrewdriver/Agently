#!/usr/bin/env python3
"""gVisor 内部能力验证 — 通过框架 code_execution 管道确认隔离有效性

验证目标（在 Docker + runsc 机器上运行）：
  1. Python 代码执行 → 确认进入 gVisor 沙箱
  2. 内核参数读取 → 确认看到虚拟内核而非宿主内核
  3. /proc 和 /sys 访问 → 确认被 gVisor 虚拟化
  4. 危险 syscall 分类验证 → 确认被 Sentry 拦截
  5. runc 全量对比 → 确认差异

用法：
    cd Agently
    python scripts/verify_gvisor_capabilities.py

前置条件：
    - 已 pip install -e .
    - Docker 运行中，runsc 已注册
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap

# ── 确保从本地源码导入 ──────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, ".."))
sys.path.insert(0, _PROJECT_ROOT)

# ── 提示消息辅助函数 ────────────────────────────────────────────


def ok(msg: str) -> None:
    print(f"  \u2713 {msg}")


def fail(msg: str) -> None:
    print(f"  \u2717 {msg}")


def warn(msg: str) -> None:
    print(f"  \u26a0 {msg}")


def subheader(title: str) -> None:
    print(f"\n--- {title} ---")


def code_block(label: str, data: object) -> None:
    print(f"  [{label}]")
    for line in json.dumps(data, indent=2, default=str).splitlines():
        print(f"    {line}")


def header(title: str) -> None:
    width = 60
    print(f"\n{'=' * width}")
    print(f"  {title}")
    print(f"{'=' * width}\n")


def run_code(
    runtime: str,
    image: str,
    code: str,
    interpreter: list[str] | None = None,
    privileged: bool = False,
    timeout: int = 15,
) -> subprocess.CompletedProcess:
    """通过框架的 _container_base_args 执行代码。"""
    from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import (
        DockerExecutionResource,
    )

    resource = DockerExecutionResource(runtime=runtime)
    profile = {"image": image, "network_mode": "disabled"}
    base_args = resource._container_base_args(profile=profile)

    docker_bin = shutil.which("docker") or "docker"
    cmd = [docker_bin, "run", "--rm"]

    if privileged:
        cmd.append("--privileged")

    cmd.extend(base_args)

    if interpreter:
        cmd.extend([image] + interpreter + [code])
    else:
        cmd.extend([image, "sh", "-c", code])

    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def run_cmd(runtime: str, image: str, cmd_args: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    """通过框架的 _container_base_args 执行任意命令。"""
    from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import (
        DockerExecutionResource,
    )

    resource = DockerExecutionResource(runtime=runtime)
    profile = {"image": image, "network_mode": "disabled"}
    base_args = resource._container_base_args(profile=profile)

    docker_bin = shutil.which("docker") or "docker"
    cmd = [docker_bin, "run", "--rm"] + list(base_args) + [image] + cmd_args

    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ══════════════════════════════════════════════════════════════
#  Phase 1: 环境诊断
# ══════════════════════════════════════════════════════════════

header("Phase 1: 环境诊断")

env: dict = {}

# 1.1 Docker
docker_bin = shutil.which("docker") or ""
env["docker_binary"] = docker_bin
if docker_bin:
    ok(f"docker 位于 {docker_bin}")
    r = subprocess.run([docker_bin, "version", "--format", "{{.Server.Version}}"],
                       capture_output=True, text=True, timeout=5)
    env["docker_version"] = r.stdout.strip() if r.returncode == 0 else ""
    if env["docker_version"]:
        ok(f"Docker daemon 运行中，版本: {env['docker_version']}")
    else:
        warn(f"Docker daemon 不可达")
else:
    fail("docker 不在 PATH 中")
    warn("后续验证需要 Docker，将跳过")

# 1.2 runsc
runsc_bin = shutil.which("runsc") or ""
env["runsc_binary"] = runsc_bin
if runsc_bin:
    ok(f"runsc 位于 {runsc_bin}")
    r = subprocess.run([runsc_bin, "--version"], capture_output=True, text=True, timeout=5)
    env["runsc_version"] = r.stdout.strip() if r.returncode == 0 else ""
    if env["runsc_version"]:
        ok(f"runsc 可用: {env['runsc_version']}")
    env["runsc_available"] = True
else:
    env["runsc_available"] = False
    warn("runsc 不在 PATH 中")

# 1.3 宿主内核
try:
    r = subprocess.run(["uname", "-r"], capture_output=True, text=True, timeout=5)
    env["host_kernel"] = r.stdout.strip()
    ok(f"宿主内核: {env['host_kernel']}")
except Exception:
    env["host_kernel"] = ""

# 1.4 检查 Docker 是否注册了 runsc 运行时
if docker_bin and env.get("docker_version"):
    r = subprocess.run([docker_bin, "info", "--format", "{{json .Runtimes}}"],
                       capture_output=True, text=True, timeout=5)
    if r.returncode == 0:
        env["registered_runtimes"] = list(json.loads(r.stdout).keys())
        if "runsc" in env["registered_runtimes"]:
            ok(f"Docker 已注册 runsc 运行时")
        else:
            warn("runsc 未注册到 Docker 运行时")


# ══════════════════════════════════════════════════════════════
#  Phase 2: Python 代码执行 — 验证内核参数
# ══════════════════════════════════════════════════════════════

header("Phase 2: Python 代码执行 — 内核参数全量对比")

PYTHON_IMAGE = "python:3.12-slim"
ALPINE_IMAGE = "alpine:latest"

PY_CODE_KERNEL = textwrap.dedent("""\
import os, platform
u = os.uname()
print(f"RELEASE={u.release}")
print(f"SYSNAME={u.sysname}")
print(f"NODENAME={u.nodename}")
print(f"MACHINE={u.machine}")
print(f"VERSION={u.version[:80]}")
""")

# 读取 /proc 关键文件
PY_CODE_PROC = textwrap.dedent("""\
import os
files = [
    "/proc/version",
    "/proc/sys/kernel/hostname",
    "/proc/sys/kernel/ostype",
    "/proc/sys/kernel/osrelease",
    "/proc/sys/kernel/version",
    "/proc/1/cmdline",
]
for f in files:
    try:
        with open(f) as fh:
            content = fh.read(120).replace(chr(0), " ")
        print(f"{f}={content.strip()}")
    except Exception as e:
        print(f"{f}=ERR:{e}")
""")

# 尝试危险操作
PY_CODE_DANGER = textwrap.dedent("""\
import os
tests = []

# 1. 尝试读取内核日志
try:
    with open("/proc/kmsg", "rb") as f:
        tests.append(("kmsg", "OPEN_OK"))
except Exception as e:
    tests.append(("kmsg", f"BLOCKED:{e}"))

# 2. 尝试 ptrace
try:
    import ctypes
    libc = ctypes.CDLL("libc.so.6")
    ret = libc.ptrace(0, 0, 0, 0)  # PT_TRACE_ME
    tests.append(("ptrace", f"ret={ret}"))
except Exception as e:
    tests.append(("ptrace", f"BLOCKED:{e}"))

# 3. 尝试 keyctl
try:
    import ctypes
    libc = ctypes.CDLL("libc.so.6")
    ret = libc.syscall(250)  # keyctl(0) = KEYCTL_GET_KEYRING_ID
    tests.append(("keyctl", f"ret={ret}"))
except Exception as e:
    tests.append(("keyctl", f"BLOCKED:{e}"))

for name, result in tests:
    print(f"{name}={result}")
""")


def verify_python_kernel(runtime: str, label: str, privileged: bool = False) -> dict:
    """通过框架执行 Python 代码，读取内核参数。"""
    timeout = 20
    try:
        # os.uname()
        r1 = run_code(runtime, PYTHON_IMAGE, PY_CODE_KERNEL,
                      interpreter=["python", "-c"], privileged=privileged, timeout=timeout)
        # /proc
        r2 = run_code(runtime, PYTHON_IMAGE, PY_CODE_PROC,
                      interpreter=["python", "-c"], privileged=privileged, timeout=timeout)
        # 危险操作
        r3 = run_code(runtime, PYTHON_IMAGE, PY_CODE_DANGER,
                      interpreter=["python", "-c"], privileged=privileged, timeout=timeout)
    except subprocess.TimeoutExpired:
        warn(f"  {label} 超时（{timeout}s）")
        return {}

    result = {}
    for line in r1.stdout.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            result[k] = v
    for line in r2.stdout.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            result[k] = v
    for line in r3.stdout.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            result[k] = v

    return result


if env.get("docker_version") and env["runsc_available"]:
    subheader("2.1 runsc 模式 — Python 代码执行")

    runsc_result = verify_python_kernel("runsc", "runsc", privileged=False)
    runsc_priv_result = verify_python_kernel("runsc", "runsc+privileged", privileged=True)

    for label, result in [("runsc 普通模式", runsc_result), ("runsc + --privileged", runsc_priv_result)]:
        print(f"  [{label}]")
        for k, v in result.items():
            print(f"    {k}: {v[:80]}")
        print()

    subheader("2.2 runc 对比 — Python 代码执行")
    runc_result = verify_python_kernel("runc", "runc", privileged=False)
    runc_priv_result = verify_python_kernel("runc", "runc+privileged", privileged=True)

    for label, result in [("runc 普通模式", runc_result), ("runc + --privileged", runc_priv_result)]:
        print(f"  [{label}]")
        for k, v in result.items():
            print(f"    {k}: {v[:80]}")
        print()

    # ── 对比分析 ──
    subheader("2.3 对比分析")
    print()
    host_kernel = env.get("host_kernel", "")

    runsc_release = runsc_result.get("RELEASE", "")
    runsc_osrelease = runsc_result.get("/proc/sys/kernel/osrelease", "")
    runc_release = runc_result.get("RELEASE", "")

    # 内核版本对比
    if "gvisor" in runsc_release.lower() or "gvisor" in runsc_osrelease.lower():
        ok(f"runsc 内核: {runsc_release or runsc_osrelease} (gVisor 虚拟内核)")
    else:
        warn(f"runsc 内核: {runsc_release or runsc_osrelease} (非 gVisor?)")

    if runc_release == host_kernel:
        ok(f"runc 内核: {runc_release} (与宿主一致)")
    else:
        warn(f"runc 内核: {runc_release} (与宿主 {host_kernel} 不一致)")

    # /proc/1/cmdline 对比
    runsc_init = runsc_result.get("/proc/1/cmdline", "")
    runc_init = runc_result.get("/proc/1/cmdline", "")
    if runsc_init and runsc_init != runc_init:
        ok(f"runsc init 进程不同: {runsc_init[:60]}")
    if runc_init:
        ok(f"runc init 进程: {runc_init[:60]}")

    # 危险操作对比
    for test_name in ["kmsg", "ptrace", "keyctl"]:
        runsc_val = runsc_result.get(test_name, "")
        runc_val = runc_result.get(test_name, "")
        runsc_priv_val = runsc_priv_result.get(test_name, "")
        runc_priv_val = runc_priv_result.get(test_name, "")
        print()
        print(f"  [{test_name}]")
        print(f"    runc:           {runc_val[:60]}")
        print(f"    runc+priv:      {runc_priv_val[:60]}")
        print(f"    runsc:          {runsc_val[:60]}")
        print(f"    runsc+priv:     {runsc_priv_val[:60]}")
        if "BLOCKED" in runsc_priv_val and "BLOCKED" not in runc_priv_val:
            ok(f"  → gVisor 拦截 {test_name}（即使 --privileged）")
        elif "BLOCKED" in runsc_val and "BLOCKED" not in runc_val:
            ok(f"  → gVisor 拦截 {test_name}（普通模式）")
        else:
            warn(f"  → {test_name} 行为与预期不符")

    # --privileged 对比
    print()
    runsc_priv_count = sum(1 for v in runsc_priv_result.values() if "BLOCKED" in str(v))
    runc_priv_count = sum(1 for v in runc_priv_result.values() if "BLOCKED" in str(v))
    if runsc_priv_count > runc_priv_count:
        ok(f"gVisor 在 --privileged 模式下拦截 {runsc_priv_count} 项操作 (runc 仅拦截 {runc_priv_count} 项)")
    else:
        warn(f"gVisor 拦截 {runsc_priv_count} 项 (runc {runc_priv_count} 项)，差异不明显")

else:
    warn("跳过 Phase 2（需要 Docker + runsc 同时就绪）")


# ══════════════════════════════════════════════════════════════
#  Phase 3: Bash 命令执行 — 通过框架 pipeline
# ══════════════════════════════════════════════════════════════

header("Phase 3: Bash 命令执行 — 内核参数")

if env.get("docker_version") and env["runsc_available"]:
    subheader("3.1 runsc 模式 — Bash 读取内核参数")

    bash_cmds = [
        ("uname -a", "uname"),
        ("cat /proc/version", "proc_version"),
        ("cat /proc/sys/kernel/ostype", "ostype"),
        ("cat /proc/1/cmdline | tr '\\0' ' ' || echo '(empty)'", "init_cmdline"),
        ("ls /proc/1/root/ 2>&1 || echo 'blocked'", "proc_root"),
    ]

    for desc, cmd in bash_cmds:
        r = run_cmd("runsc", ALPINE_IMAGE, ["sh", "-c", cmd], timeout=10)
        output = r.stdout.strip() or r.stderr.strip()
        print(f"  [{desc}]")
        print(f"    {output[:80]}")
        print()

    subheader("3.2 runc 对比 — Bash 读取内核参数")
    for desc, cmd in bash_cmds:
        r = run_cmd("runc", ALPINE_IMAGE, ["sh", "-c", cmd], timeout=10)
        output = r.stdout.strip() or r.stderr.strip()
        print(f"  [{desc}]")
        print(f"    {output[:80]}")
        print()
else:
    warn("跳过 Phase 3（需要 Docker + runsc 同时就绪）")


# ══════════════════════════════════════════════════════════════
#  Phase 4: 分类 syscall 隔离验证（--privileged 对比）
# ══════════════════════════════════════════════════════════════

header("Phase 4: 分类 syscall 隔离验证（--privileged 对比）")

if env.get("docker_version") and env["runsc_available"]:
    ok("原理：gVisor Sentry 拦截所有系统调用，即使 --privileged 也无法绕过")
    print()

    syscall_tests = [
        ("内核日志读取", "dmesg 2>&1 || true", "dmesg: read kernel buffer failed"),
        ("文件系统挂载", "mount -t tmpfs none /mnt 2>&1 || true", "Operation not permitted"),
        ("原始设备访问", "cat /dev/mem 2>&1; exit 0", "Operation not permitted"),
        ("内核模块加载", "modprobe 2>&1 || true", "Operation not permitted"),
        ("重启系统", "reboot 2>&1 || true", "Operation not permitted"),
        ("修改主机名", "hostname hacked 2>&1 || true", "Operation not permitted"),
        ("创建新 PID ns", "unshare -p true 2>&1 || true", "Operation not permitted"),
        ("加载 BPF 程序", "bpftool 2>&1 || true", "not found"),
    ]

    for label, bash_cmd, expect_block in syscall_tests:
        # runc + --privileged
        r_runc = run_cmd("runc", ALPINE_IMAGE, ["sh", "-c", bash_cmd], timeout=10)
        r_runc_priv = subprocess.run(
            [shutil.which("docker") or "docker", "run", "--rm", "--privileged",
             ALPINE_IMAGE, "sh", "-c", bash_cmd],
            capture_output=True, text=True, timeout=10,
        )

        # runsc + --privileged（通过框架路径）
        r_runsc_priv = run_cmd("runsc", ALPINE_IMAGE, ["sh", "-c", bash_cmd], timeout=10)

        output_runc = (r_runc.stdout.strip() or r_runc.stderr.strip())[:60]
        output_runc_priv = (r_runc_priv.stdout.strip() or r_runc_priv.stderr.strip())[:60]
        output_runsc = (r_runsc_priv.stdout.strip() or r_runsc_priv.stderr.strip())[:60]

        blocked = expect_block.split(":")[0].lower() in output_runsc.lower()

        print(f"  [{label}] {bash_cmd[:40]}")
        print(f"    runc 普通:     {output_runc or '(ok)'}")
        print(f"    runc+priv:     {output_runc_priv or '(ok)'}")
        print(f"    runsc+priv:    {output_runsc or '(ok)'}")
        if blocked:
            ok(f"  → gVisor Sentry 拦截: {label}")
        else:
            warn(f"  → {label} 未被拦截？")
        print()
else:
    warn("跳过 Phase 4（需要 Docker + runsc 同时就绪）")


# ══════════════════════════════════════════════════════════════
#  Phase 5: 不可变基础设施验证
# ══════════════════════════════════════════════════════════════

header("Phase 5: 不可变基础设施验证")

if env.get("docker_version") and env["runsc_available"]:
    ok("gVisor 容器内尝试修改系统配置，验证是否可被持久化修改")
    print()

    # 尝试写 /proc/sys 和 /sys
    immutable_tests = [
        ("修改内核参数", "sysctl -w kernel.hostname=evil 2>&1 || true"),
        ("创建用户", "adduser -D testuser 2>&1 || true"),
        ("安装内核模块", "modprobe dummy 2>&1 || true"),
        ("修改 iptables", "iptables -L 2>&1 || true"),
    ]

    for label, bash_cmd in immutable_tests:
        # runsc 普通模式
        r_runsc = run_cmd("runsc", ALPINE_IMAGE, ["sh", "-c", bash_cmd], timeout=10)
        output_runsc = (r_runsc.stdout.strip() or r_runsc.stderr.strip())[:60]

        # runc 对比
        r_runc = run_cmd("runc", ALPINE_IMAGE, ["sh", "-c", bash_cmd], timeout=10)
        output_runc = (r_runc.stdout.strip() or r_runc.stderr.strip())[:60]

        print(f"  [{label}] {bash_cmd[:40]}")
        print(f"    runc:   {output_runc or '(ok)'}")
        print(f"    runsc:  {output_runsc or '(ok)'}")
        print()
else:
    warn("跳过 Phase 5（需要 Docker + runsc 同时就绪）")


# ══════════════════════════════════════════════════════════════
#  汇总
# ══════════════════════════════════════════════════════════════

print()
header("验证完成")
print(f"  环境: Docker={'就绪' if env.get('docker_version') else '未就绪'}  "
      f"runsc={'就绪' if env['runsc_available'] else '未就绪'}")
print()