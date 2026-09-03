#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scheduler.py
每半小时触发一次 auto_harvest.py + auto_plant.py
按顺序执行: 先收菜, 再种菜

用法:
  python scheduler.py                       # 默认每 30 分钟一次
  python scheduler.py --interval 600        # 自定义间隔 (秒)
  python scheduler.py --once                # 只跑一次后退出
  python scheduler.py --harvest-only        # 只跑收菜
  python scheduler.py --plant-only          # 只跑种菜
"""

import argparse
import io
import os
import subprocess
import sys
import time
import urllib.request

# 强制 stdout/stderr 用 UTF-8 (Windows 默认 GBK, 中文 + 特殊字符会乱码/崩)
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)
except Exception:
    pass
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DEBUG_PORT = 9222
DEFAULT_EDGE_USER_DATA_DIR = os.path.join(HERE, "edge-farm-profile")
FARM_URL = "https://www.duanwuqiufenmao.top/farm"

# 子进程下一轮启动间隔(秒) 由 auto_plant.py 通过 stdout 一行 NEXT_INTERVAL=NNN 告知
# 没拿到就用这个默认值
DEFAULT_NEXT_INTERVAL = 30 * 60  # 30 分钟

# 常见 Edge 安装位置
EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
]


def log(msg, lines):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    lines.append(line)


def run_script(name, lines, extra_args=None, env=None):
    """运行 auto_xxx.py, 实时打印输出, 返回 (success, captured_stdout)"""
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        log(f"[!] 找不到脚本: {path}", lines)
        return False, ""
    log(f"=== 开始执行 {name}  (args={extra_args or []}) ===", lines)
    # DIAG: 启动子进程前先确认 CDP 端口和 Edge 进程
    port = (env or {}).get("FARM_DEBUG_PORT", str(DEFAULT_DEBUG_PORT))
    if is_debug_port_up(int(port)):
        log(f"[DIAG] 启动前 CDP 端口 {port} 存活", lines)
    else:
        log(f"[DIAG] ⚠ 启动前 CDP 端口 {port} 不通, 子进程大概率失败", lines)
    try:
        import psutil
        edges = [p for p in psutil.process_iter(['pid','name']) if p.info['name'] and 'msedge' in p.info['name'].lower()]
        log(f"[DIAG] msedge 进程数={len(edges)}", lines)
        for p in edges[:3]:
            log(f"[DIAG]   - pid={p.info['pid']}  name={p.info['name']}", lines)
    except ImportError:
        pass
    except Exception as e:
        log(f"[DIAG] 进程枚举失败: {e}", lines)
    captured = []
    try:
        # 把当前调试端口/用户目录通过环境变量透传给子脚本
        child_env = os.environ.copy()
        if env:
            child_env.update(env)
        # 子脚本已强制 UTF-8 输出, 这里也用 UTF-8 读
        proc = subprocess.Popen(
            [sys.executable, path] + (extra_args or []),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", cwd=HERE,
            env=child_env,
        )
        log(f"[DIAG] {name} 子进程已启动 pid={proc.pid}", lines)
        for stdout_line in iter(proc.stdout.readline, ""):
            if stdout_line:
                captured.append(stdout_line)
                ts = datetime.now().strftime("%H:%M:%S")
                line = f"  [{ts}] {stdout_line.rstrip()}"
                print(line, flush=True)
                lines.append(line)
        proc.wait()
        if proc.returncode == 0:
            log(f"=== {name} 执行成功 ===", lines)
            return True, "".join(captured)
        else:
            log(f"=== {name} 执行失败 (exit={proc.returncode}) ===", lines)
            return False, "".join(captured)
    except Exception as e:
        log(f"[!] 执行 {name} 异常: {e}", lines)
        return False, "".join(captured)


def parse_next_interval(captured_stdout: str) -> int:
    """从子进程 stdout 找 'NEXT_INTERVAL=NNN', 找不到就用默认"""
    import re
    for line in captured_stdout.splitlines():
        m = re.search(r"NEXT_INTERVAL\s*=\s*(\d+)", line)
        if m:
            return int(m.group(1))
    return DEFAULT_NEXT_INTERVAL


def parse_next_harvest_actions(captured_stdout: str, default: str = "翻地,收获") -> list:
    """从子进程 stdout 找 'NEXT_HARVEST_ACTIONS=道具,翻地,收获', 解析成 list[str]"""
    import re
    for line in captured_stdout.splitlines():
        m = re.search(r"NEXT_HARVEST_ACTIONS\s*=\s*(.+?)(?:\s|$)", line)
        if m:
            actions = [a.strip() for a in m.group(1).split(",") if a.strip()]
            if actions:
                return actions
    return [a.strip() for a in default.split(",") if a.strip()]


def find_edge_exe():
    """按常见路径找 msedge.exe, 找到返回绝对路径, 否则返回 None"""
    for p in EDGE_PATHS:
        if os.path.exists(p):
            return p
    return None


def is_debug_port_up(port, timeout=1.5):
    """CDP 端口是否在监听"""
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def launch_edge(lines, port, user_data_dir):
    """以远程调试模式启动 Edge; 已起来则跳过"""
    if is_debug_port_up(port):
        log(f"Edge CDP 端口 {port} 已就绪, 跳过启动", lines)
        return True

    exe = find_edge_exe()
    if not exe:
        log(f"[!] 找不到 msedge.exe, 请手动启动 Edge 并加 --remote-debugging-port={port}", lines)
        log("    或把 msedge.exe 装到以下任一位置: " + " | ".join(EDGE_PATHS), lines)
        return False

    log(f"Edge 未启动, 准备拉起: {exe}", lines)
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        FARM_URL,
    ]
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log(f"[!] 启动 Edge 失败: {e}", lines)
        return False

    # 等端口起来, 最多 15s
    for i in range(30):
        time.sleep(0.5)
        if is_debug_port_up(port):
            log(f"Edge CDP 端口 {port} 已就绪 (用时 ~{(i+1)*0.5:.1f}s)", lines)
            # 再多等一会儿, 让 /json 里的目标 tab 注册进来
            time.sleep(1.5)
            return True
    log(f"[!] Edge 启动了但 {port} 端口 15s 内未响应", lines)
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--harvest-only", action="store_true", help="只跑收菜")
    parser.add_argument("--plant-only", action="store_true", help="只跑种菜")
    parser.add_argument("--harvest-args", default="", help="透传给 auto_harvest.py 的额外参数")
    parser.add_argument("--plant-args", default="", help="透传给 auto_plant.py 的额外参数")
    parser.add_argument("--no-launch-edge", action="store_true", help="不自动启动 Edge (默认会自动以远程调试模式拉起)")
    parser.add_argument("--port", type=int, default=DEFAULT_DEBUG_PORT, help=f"Edge CDP 端口 (默认 {DEFAULT_DEBUG_PORT}, 多账号互不冲突: 9222/9333/9444 ...)")
    parser.add_argument("--profile", default="edge-farm-profile", help="Edge 用户目录名 (相对脚本所在目录, 多账号每个要不同)")
    args = parser.parse_args()

    # 用户目录支持相对路径(相对 HERE)或绝对路径
    if os.path.isabs(args.profile):
        user_data_dir = args.profile
    else:
        user_data_dir = os.path.join(HERE, args.profile)
    os.makedirs(user_data_dir, exist_ok=True)

    # 通过环境变量把端口/用户目录透传给子脚本
    child_env = {
        "FARM_DEBUG_PORT": str(args.port),
        "FARM_USER_DATA_DIR": user_data_dir,
    }

    lines = []
    log(f"=== 启动调度器 (一直运行, Ctrl+C 退出) ===", lines)
    log(f"  模式: {'收菜+种菜' if not args.harvest_only and not args.plant_only else ('只收菜' if args.harvest_only else '只种菜')}", lines)
    log(f"  CDP 端口: {args.port}", lines)
    log(f"  用户目录: {user_data_dir}", lines)
    log(f"  启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", lines)
    log(f"  间隔策略: 由 auto_plant.py 决定 (默认 {DEFAULT_NEXT_INTERVAL}s = {DEFAULT_NEXT_INTERVAL/60:.0f} 分钟)", lines)

    # 先确保 Edge 已起 + CDP 端口可连
    if not args.no_launch_edge:
        if not launch_edge(lines, args.port, user_data_dir):
            log("[!] Edge 不可用, 后续脚本大概率会失败, 仍继续 (按 Ctrl+C 中止)", lines)

    cycle = 0
    # 本轮收菜要处理的动作; 第一轮用默认, 之后每轮用上轮 auto_plant.py 传来的
    next_harvest_actions = ["翻地", "收获"]
    try:
        while True:
            cycle += 1
            log(f"\n========== 第 {cycle} 轮 ==========", lines)
            # DIAG: 轮次开始时, 检查 CDP 端口 + 屏幕是否锁屏
            if is_debug_port_up(args.port):
                log(f"[DIAG] 第 {cycle} 轮: CDP 端口 {args.port} 存活", lines)
            else:
                log(f"[DIAG] 第 {cycle} 轮: ⚠ CDP 端口 {args.port} 不通, Edge 可能被杀/未起", lines)
            # 锁屏检测 (PowerShell): session 0 锁屏时 GetForegroundWindow 返回 0
            try:
                import ctypes
                user32 = ctypes.windll.user32
                kernel32 = ctypes.windll.kernel32
                hwnd = user32.GetForegroundWindow()
                locked = (hwnd == 0)
                log(f"[DIAG] 第 {cycle} 轮: GetForegroundWindow={hwnd}  推测锁屏={locked}", lines)
            except Exception as e:
                log(f"[DIAG] 锁屏检测失败: {e}", lines)
            # 收菜 (动作列表由上轮 auto_plant.py 决定)
            if not args.plant_only:
                # CLI --harvest-args 透传的内容 + 自动注入的 --action
                extra_from_cli = [a for a in args.harvest_args.split() if a]
                # 自动注入 --action <列表>
                auto_args = ["--action", ",".join(next_harvest_actions)]
                harvest_extra = extra_from_cli + auto_args
                run_script("auto_harvest.py", lines, harvest_extra, env=child_env)
            # 种菜 (auto_plant.py 通过 stdout NEXT_INTERVAL=NNN 决定下次循环间隔)
            this_interval = DEFAULT_NEXT_INTERVAL
            if not args.harvest_only:
                plant_extra = [a for a in args.plant_args.split() if a]
                _, captured = run_script("auto_plant.py", lines, plant_extra, env=child_env)
                this_interval = parse_next_interval(captured)
                # 解析"下次收菜动作列表"
                next_harvest_actions = parse_next_harvest_actions(captured)
                log(f"  📋 下次收菜动作: {next_harvest_actions}", lines)

            # 等下一轮
            next_time = datetime.now() + timedelta(seconds=this_interval)
            next_time_str = next_time.strftime("%Y-%m-%d %H:%M:%S")
            if this_interval >= 3600:
                dur_str = f"{this_interval/3600:.2f} 小时 ({int(this_interval/60)} 分钟)"
            else:
                dur_str = f"{this_interval/60:.1f} 分钟"
            log(f"下一轮: {next_time_str}  (间隔 {dur_str} = {this_interval} 秒)...", lines)
            time.sleep(this_interval)
    except KeyboardInterrupt:
        log(f"\n[!] 用户中断, 停止调度器 (累计 {cycle} 轮)", lines)
        log(f"  停止时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", lines)


if __name__ == "__main__":
    main()
