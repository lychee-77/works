# 兼容旧路径：farm.scheduler → farm.schedule.scheduler
from farm.schedule.scheduler import *  # noqa: F401,F403
from farm.schedule.scheduler import run_forever
