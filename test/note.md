

## 环境
# 重新激活虚拟环境或创建它
python -m venv .venv
# 然后安装依赖
pip install -r requirements.txt

## 双账号跑法(开两个终端窗口)
账号 A (默认端口 9222,默认用户目录):

1800 = 30分钟*60秒
```
python scheduler.py --interval 1800
```
账号 B (独立端口 9333 + 独立用户目录,登录态互不干扰):
需要给 scheduler.py / auto_harvest.py / auto_plant.py 都加 --port 参数,
并改 Edge 启动时 --user-data-dir 互不冲突。改完后命令:
```
python scheduler.py --interval 1800 --port 9333 --profile edge-farm-profile-2
```