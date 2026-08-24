# scheduler.py — 定时循环入口（农场分通道 + 可选秘境/远征监听）
from farm.schedule import run_forever


if __name__ == "__main__":
    try:
        run_forever()
    except KeyboardInterrupt:
        print("\n已停止定时任务")
