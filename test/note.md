## 环境

```
# 创建虚拟环境
python -m venv .venv

# 激活虚拟环境
.venv\Scripts\activate

# 然后安装依赖
pip install -r requirements.txt
```

## 双账号跑法(开两个终端窗口)
间隔由 auto_plant.py 决定: 种胡萝卜 = 30 分钟, 种菠萝 = 405 分钟 (6h45m)

账号 A (默认端口 9222,默认用户目录):
```
python scheduler.py
```
账号 B (独立端口 9333 + 独立用户目录,登录态互不干扰):
```
python scheduler.py --port 9333 --profile edge-farm-profile-2
```