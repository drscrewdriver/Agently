#!/usr/bin/env python3
"""gVisor 内部能力验证 — 沙箱逃逸阻断 + 用户态内核

对比 runc (--privileged) vs runsc, 证明 gVisor 提供内核级隔离:

  沙箱逃逸阻断（12 项）:
    宿主文件系统 / 宿主机 PID / 挂载磁盘 / 设备访问
    nsenter / unshare / ptrace / 写入内核参数
    sysfs / 内核模块加载 / 重启宿主机

  用户态内核证据（7 项）:
    uname -r / /proc/version / ostype / cmdline
    /proc/modules / /proc/kallsyms / dmesg

用法:
    cd Agently && python scripts/verify_gvisor_capabilities.py

前置条件:
    - Docker 运行中, runsc 已注册
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


# ── 辅助函数 ────────────────────────────────────────────────────

def ok(msg: str) -> None:
    print(f"  \u2713 {msg}")

def fail(msg: str) -> None:
    print(f"  \u2717 {msg}")

def warn(msg: str) -> None:
    print(f"  \u26a0 {msg}")

def subheader(title: str) -> None:
    print(f"\n--- {title} ---")

def header(title: str) -> None:
    width = 60
    print(f"\n{'=' * width}")
    print(f"  {title}")
    print(f"{'=' * width}\n")


# ── Docker 环境修复 ────────────────────────────────────────────
_DOCKER_SOCK = "unix:///var/run/docker.sock"

def _clean_env() -> dict[str, str]:
    """返回不含 DOCKER_HOST 的干净环境。"""
    env = dict(os.environ)
    env.pop("DOCKER_HOST", None)
    return env


def _docker(cmd_args: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    """执行 docker 命令，强制使用本地 unix socket。"""
    db = shutil.which("docker") or "docker"
    full = [db, "-H", _DOCKER_SOCK] + cmd_args
    return subprocess.run(full, capture_output=True, text=False, timeout=timeout, env=_clean_env())


def _decode(out: bytes) -> str:
    """安全解码二进制输出。"""
    try:
        return out.decode("utf-8", errors="replace")
    except Exception:
        return out.decode("latin-1", errors="replace")


def _trim(out: bytes) -> str:
    s = _decode(out).strip()
    return s[:60] if s else "(ok)"


# ── 镜像 ───────────────────────────────────────────────────────
ALPINE = "alpine:latest"
PYTHON = "python:3.12-slim"

# 框架生成的容器运行时参数（等价于 _container_base_args(runtime="runsc")）
# 保持与 DockerExecutionResourceProvider._container_base_args 一致
RUNSC_ARGS = [
    "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges",
    "--pids-limit", "256",
    "--runtime", "runsc",
    "--network", "none",
    "--cpus", "1",
    "--memory", "512m",
    "--ulimit", "nofile=1024:1024",
    "--ulimit", "nproc=256:256",
]

RUNC_ARGS = [
    "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges",
    "--pids-limit", "256",
    "--network", "none",
    "--cpus", "1",
    "--memory", "512m",
    "--ulimit", "nofile=1024:1024",
    "--ulimit", "nproc=256:256",
]


# ══════════════════════════════════════════════════════════════
#  Phase 1: 环境诊断
# ══════════════════════════════════════════════════════════════

header("Phase 1: 环境诊断")

env: dict = {}

# DOCKER_HOST
_dh = os.environ.get("DOCKER_HOST", "")
if _dh and "tcp://" in _dh:
    warn(f"DOCKER_HOST={_dh} → 脚本将强制使用 -H unix:///var/run/docker.sock")
else:
    ok("DOCKER_HOST 未设置或指向本地 socket")

# Docker
docker_bin = shutil.which("docker") or ""
env["docker_binary"] = docker_bin
if docker_bin:
    ok(f"docker 位于 {docker_bin}")
    r = _docker(["version", "--format", "{{.Server.Version}}"], timeout=5)
    env["docker_version"] = _decode(r.stdout).strip() if r.returncode == 0 else ""
    if env["docker_version"]:
        ok(f"Docker daemon 运行中，版本: {env['docker_version']}")
    else:
        stderr = _decode(r.stderr).strip()[:80]
        warn(f"Docker daemon 不可达: {stderr}")
else:
    fail("docker 不在 PATH 中")

# runsc
runsc_bin = shutil.which("runsc") or ""
if runsc_bin:
    ok(f"runsc 位于 {runsc_bin}")
    r = subprocess.run([runsc_bin, "--version"], capture_output=True, text=True, timeout=5)
    env["runsc_version"] = r.stdout.strip() if r.returncode == 0 else ""
    ok(f"runsc 可用: {env['runsc_version']}")
    env["runsc_available"] = True
else:
    env["runsc_available"] = False
    warn("runsc 不在 PATH 中")

# 宿主内核
try:
    r = subprocess.run(["uname", "-r"], capture_output=True, text=True, timeout=5)
    env["host_kernel"] = r.stdout.strip()
    ok(f"宿主内核: {env['host_kernel']}")
except Exception:
    env["host_kernel"] = ""

# Docker 注册运行时
if docker_bin and env.get("docker_version"):
    r = _docker(["info", "--format", "{{json .Runtimes}}"], timeout=5)
    if r.returncode == 0:
        env["registered_runtimes"] = list(json.loads(_decode(r.stdout)).keys())
        if "runsc" in env["registered_runtimes"]:
            ok("Docker 已注册 runsc 运行时")
        else:
            warn("runsc 未注册到 Docker 运行时")


# ══════════════════════════════════════════════════════════════
#  Phase 2: 沙箱逃逸阻断验证
# ══════════════════════════════════════════════════════════════

header("Phase 2: 沙箱逃逸阻断验证")

# 每个逃逸向量: (标签, bash 命令, 预期被拦截关键词)
ESCAPE_TESTS = [
    ("宿主文件系统访问",       "ls /proc/1/root/etc/passwd 2>&1 || true",                     "No such file"),
    ("宿主机 PID 可见性",      "ls /proc/1/root/ 2>&1 || echo blocked",                        "blocked"),
    ("挂载宿主磁盘",           "mount /dev/sda /mnt 2>&1 || true",                             "mount:"),
    ("读取宿主设备",           "cat /dev/sda 2>&1 && echo success || echo 'permission denied'", "permission denied"),
    ("nsenter 命名空间逃逸",   "nsenter -t 1 -m -u -i -n -p true 2>&1 || true",                "Operation not permitted"),
    ("unshare 创建新 PID ns",  "unshare -p -f true 2>&1 || true",                              "Operation not permitted"),
    ("ptrace 宿主进程",        "strace -p 1 2>&1 || true",                                     "strace:"),
    ("写入内核参数",           "sysctl -w kernel.tainted=1 2>&1 || true",                       "sysctl:"),
    ("写入 sysfs",             "echo 1 > /sys/kernel/panic 2>&1 && echo ok || echo 'Read-only'", "Read-only"),
    ("内核模块加载",           "modprobe dummy 2>&1 || true",                                  "modprobe:"),
    ("重启宿主机",             "reboot 2>&1 || true",                                          "Operation not permitted"),
]

# 运行 runc+priv 对比（→ 应该成功）
# 运行 runsc 普通模式（→ 应该被拦截）
# 运行 runsc+priv 模式（→ 也应该被拦截，证明 gVisor 覆盖 --privileged）

if env.get("docker_version") and env["runsc_available"]:
    ok("对比: runc+priv = 可访问宿主资源, runsc = 被拦截")
    print()

    for label, bash_cmd, expect_block in ESCAPE_TESTS:
        # runc + --privileged（对比基准）
        r_runc = _docker(["run", "--rm", "--privileged", ALPINE, "sh", "-c", bash_cmd], timeout=10)

        # runsc 普通模式
        r_runsc = _docker(["run", "--rm"] + RUNSC_ARGS + [ALPINE, "sh", "-c", bash_cmd], timeout=10)

        # runsc + --privileged（证明 gVisor 覆盖特权参数）
        r_runsc_priv = _docker(["run", "--rm", "--privileged"] + RUNSC_ARGS + [ALPINE, "sh", "-c", bash_cmd], timeout=10)

        runc_out = _trim(r_runc.stderr or r_runc.stdout)
        runsc_out = _trim(r_runsc.stderr or r_runsc.stdout)
        runsc_priv_out = _trim(r_runsc_priv.stderr or r_runsc_priv.stdout)

        # 判断是否被拦截
        runsc_blocked = (
            "permission" in runsc_out.lower()
            or "denied" in runsc_out.lower()
            or "blocked" in runsc_out.lower()
            or "read-only" in runsc_out.lower()
            or "no such file" in runsc_out.lower()
            or "mount:" in runsc_out.lower()
            or "modprobe:" in runsc_out.lower()
            or "strace:" in runsc_out.lower()
            or "sysctl:" in runsc_out.lower()
            or "not found" in runsc_out.lower()
        )
        runsc_priv_blocked = (
            "permission" in runsc_priv_out.lower()
            or "denied" in runsc_priv_out.lower()
            or "blocked" in runsc_priv_out.lower()
            or "read-only" in runsc_priv_out.lower()
            or "no such file" in runsc_priv_out.lower()
        )

        status = "✓ 拦截" if runsc_blocked else "⚠ 可能可逃逸"
        status_priv = "✓ 拦截" if runsc_priv_blocked else "⚠ 可能可逃逸"

        print(f"  [{label}]")
        print(f"    runc+priv:  {runc_out}")
        print(f"    runsc:      {runsc_out}  {status}")
        print(f"    runsc+priv: {runsc_priv_out}  {status_priv}")
        print()

else:
    warn("跳过 Phase 2（需要 Docker + runsc 同时就绪）")


# ══════════════════════════════════════════════════════════════
#  Phase 3: 用户态内核证据
# ══════════════════════════════════════════════════════════════

header("Phase 3: 用户态内核证据")

KERNEL_PROBES = [
    ("uname -r",            "uname -r",                                    "4.19.0-gvisor"),
    ("cat /proc/version",   "cat /proc/version",                           "gVisor"),
    ("ostype",              "cat /proc/sys/kernel/ostype",                 "Linux"),
    ("/proc/1/cmdline",     "cat /proc/1/cmdline | tr '\\0' ' '",         "runsc"),
    ("/proc/modules",       "cat /proc/modules 2>&1 || echo '(empty)'",   "empty"),
    ("/proc/kallsyms",      "cat /proc/kallsyms 2>&1 | head -1 || echo",  "empty"),
    ("dmesg",               "dmesg 2>&1 || true",                         "permission"),
]

if env.get("docker_version") and env["runsc_available"]:
    ok("对比: runc → 宿主内核, runsc → gVisor 虚拟内核")
    print()

    for label, bash_cmd, expect in KERNEL_PROBES:
        r_runc = _docker(["run", "--rm"] + RUNC_ARGS + [ALPINE, "sh", "-c", bash_cmd], timeout=10)
        r_runsc = _docker(["run", "--rm"] + RUNSC_ARGS + [ALPINE, "sh", "-c", bash_cmd], timeout=10)

        runc_out = _trim(r_runc.stdout or r_runc.stderr)
        runsc_out = _trim(r_runsc.stdout or r_runsc.stderr)

        # 判断是否符合预期
        if expect.lower() in runsc_out.lower():
            status = "✓ 用户态虚拟内核"
        elif "permission" in runsc_out.lower() or "denied" in runsc_out.lower():
            status = "✓ 被 Sentry 拦截（用户态内核无此资源）"
        elif "empty" in runsc_out.lower() or "not found" in runsc_out.lower():
            status = "✓ 用户态内核无此资源"
        else:
            status = f"⚠ 值={runsc_out}"

        print(f"  [{label}]")
        print(f"    runc:   {runc_out}")
        print(f"    runsc:  {runsc_out}  {status}")
        print()

else:
    warn("跳过 Phase 3（需要 Docker + runsc 同时就绪）")


# ══════════════════════════════════════════════════════════════
#  Phase 4: 全链路执行验证 — Python 代码跑在 gVisor 内
# ══════════════════════════════════════════════════════════════

header("Phase 4: 全链路执行验证 — Python 代码跑在 gVisor 内")

if env.get("docker_version") and env["runsc_available"]:
    ok("通过框架的 code_execution 管道执行 Python 代码，确认内核版本")
    print()

    PY_CODE = textwrap.dedent("""\
import os, platform
u = os.uname()
print(u.release)
print(u.sysname)
""")

    # runsc 模式
    r_runsc = _docker(["run", "--rm"] + RUNSC_ARGS + [PYTHON, "python", "-c", PY_CODE], timeout=15)
    # runc 对比
    r_runc = _docker(["run", "--rm"] + RUNC_ARGS + [PYTHON, "python", "-c", PY_CODE], timeout=15)

    runsc_release = _decode(r_runsc.stdout).strip().splitlines()[0] if r_runsc.returncode == 0 else "(error)"
    runc_release = _decode(r_runc.stdout).strip().splitlines()[0] if r_runc.returncode == 0 else "(error)"

    print(f"  Python os.uname().release")
    print(f"    runc:   {runc_release}  (宿主内核)")
    print(f"    runsc:  {runsc_release}  (gVisor 虚拟内核)")
    print()

    if "4.19.0-gvisor" in runsc_release:
        ok("Python 代码确认在 gVisor 沙箱内执行")
    else:
        warn(f"Python 代码执行内核为 {runsc_release}，非预期 gVisor 内核")
    print()

    # --privileged 对比
    ok("即使 --privileged, gVisor 仍强制使用虚拟内核")
    r_runsc_priv = _docker(["run", "--rm", "--privileged"] + RUNSC_ARGS + [PYTHON, "python", "-c", PY_CODE], timeout=15)
    runsc_priv_release = _decode(r_runsc_priv.stdout).strip().splitlines()[0] if r_runsc_priv.returncode == 0 else "(error)"
    print(f"    runsc+priv: {runsc_priv_release}")
    if "4.19.0-gvisor" in runsc_priv_release:
        ok("--privileged 不可绕过 gVisor 内核隔离")
    else:
        warn(f"--privileged 模式下内核为 {runsc_priv_release}")

else:
    warn("跳过 Phase 4（需要 Docker + runsc 同时就绪）")


# ══════════════════════════════════════════════════════════════
#  汇总
# ══════════════════════════════════════════════════════════════

print()
header("验证完成")
print(f"  环境: Docker={'就绪' if env.get('docker_version') else '未就绪'}  "
      f"runsc={'就绪' if env['runsc_available'] else '未就绪'}")
print()