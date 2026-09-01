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
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DEBUG_PORT = 9222
EDGE_USER_DATA_DIR = os.path.join(HERE, "edge-farm-profile")
FARM_URL = "https://www.duanwuqiufenmao.top/farm"

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


def run_script(name, lines, extra_args=None):
    """运行 auto_xxx.py, 实时打印输出, 返回是否成功"""
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        log(f"[!] 找不到脚本: {path}", lines)
        return False
    log(f"=== 开始执行 {name} ===", lines)
    try:
        # 子脚本已强制 UTF-8 输出, 这里也用 UTF-8 读
        proc = subprocess.Popen(
            [sys.executable, path] + (extra_args or []),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", cwd=HERE,
        )
        for stdout_line in iter(proc.stdout.readline, ""):
            if stdout_line:
                ts = datetime.now().strftime("%H:%M:%S")
                line = f"  [{ts}] {stdout_line.rstrip()}"
                print(line, flush=True)
                lines.append(line)
        proc.wait()
        if proc.returncode == 0:
            log(f"=== {name} 执行成功 ===", lines)
            return True
        else:
            log(f"=== {name} 执行失败 (exit={proc.returncode}) ===", lines)
            return False
    except Exception as e:
        log(f"[!] 执行 {name} 异常: {e}", lines)
        return False


def find_edge_exe():
    """按常见路径找 msedge.exe, 找到返回绝对路径, 否则返回 None"""
    for p in EDGE_PATHS:
        if os.path.exists(p):
            return p
    return None


def is_debug_port_up(port=DEBUG_PORT, timeout=1.5):
    """CDP 端口是否在监听"""
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def launch_edge(lines):
    """以远程调试模式启动 Edge; 已起来则跳过"""
    if is_debug_port_up():
        log(f"Edge CDP 端口 {DEBUG_PORT} 已就绪, 跳过启动", lines)
        return True

    exe = find_edge_exe()
    if not exe:
        log(f"[!] 找不到 msedge.exe, 请手动启动 Edge 并加 --remote-debugging-port={DEBUG_PORT}", lines)
        log("    或把 msedge.exe 装到以下任一位置: " + " | ".join(EDGE_PATHS), lines)
        return False

    log(f"Edge 未启动, 准备拉起: {exe}", lines)
    args = [
        exe,
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={EDGE_USER_DATA_DIR}",
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
        if is_debug_port_up():
            log(f"Edge CDP 端口 {DEBUG_PORT} 已就绪 (用时 ~{(i+1)*0.5:.1f}s)", lines)
            # 再多等一会儿, 让 /json 里的目标 tab 注册进来
            time.sleep(1.5)
            return True
    log(f"[!] Edge 启动了但 {DEBUG_PORT} 端口 15s 内未响应", lines)
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=1800, help="间隔秒数 (默认 1800=30分钟)")
    parser.add_argument("--harvest-only", action="store_true", help="只跑收菜")
    parser.add_argument("--plant-only", action="store_true", help="只跑种菜")
    parser.add_argument("--harvest-args", default="", help="透传给 auto_harvest.py 的额外参数")
    parser.add_argument("--plant-args", default="", help="透传给 auto_plant.py 的额外参数")
    parser.add_argument("--no-launch-edge", action="store_true", help="不自动启动 Edge (默认会自动以远程调试模式拉起)")
    args = parser.parse_args()

    lines = []
    log(f"=== 启动调度器 (一直运行, Ctrl+C 退出) ===", lines)
    log(f"  间隔: {args.interval} 秒 ({args.interval/60:.1f} 分钟)", lines)
    log(f"  模式: {'收菜+种菜' if not args.harvest_only and not args.plant_only else ('只收菜' if args.harvest_only else '只种菜')}", lines)
    log(f"  启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", lines)

    # 先确保 Edge 已起 + CDP 端口可连
    if not args.no_launch_edge:
        if not launch_edge(lines):
            log("[!] Edge 不可用, 后续脚本大概率会失败, 仍继续 (按 Ctrl+C 中止)", lines)

    cycle = 0
    try:
        while True:
            cycle += 1
            log(f"\n========== 第 {cycle} 轮 ==========", lines)
            # 收菜
            if not args.plant_only:
                harvest_extra = [a for a in args.harvest_args.split() if a]
                run_script("auto_harvest.py", lines, harvest_extra)
                # 收完后等几秒, 让 Vue 状态稳定
                log("等待 3s 让 Vue 状态稳定...", lines)
                time.sleep(3)
            # 种菜
            if not args.harvest_only:
                plant_extra = [a for a in args.plant_args.split() if a]
                run_script("auto_plant.py", lines, plant_extra)
                log("等待 3s 让 Vue 状态稳定...", lines)
                time.sleep(3)

            # 等下一轮
            log(f"下一轮在 {args.interval} 秒后 ({args.interval/60:.1f} 分钟)...", lines)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log(f"\n[!] 用户中断, 停止调度器 (累计 {cycle} 轮)", lines)
        log(f"  停止时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", lines)


if __name__ == "__main__":
    main()
