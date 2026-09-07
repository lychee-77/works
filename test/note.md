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

## 其他功能
独立端口9555 不影响种菜
### 爬塔
climbing_town.py: 先去背包使用还魂丹，还魂丹使用个数REVIVE_TIMES = 0可以设置，使用后选择层数TARGET_FLOOR = 48开始爬塔，
循环对战5次REPEAT_TIMES = 5+使用小瓶经验水，大瓶经验水自行修改参数

执行以下命令开启脚本：
```
python climbing_town.py
```

### 使用灵核碎片
use_ling_ker.py: 点击背包的灵核，使用所有灵核碎片，

执行以下命令开启脚本：
```
python use_ling_ker.py
```



