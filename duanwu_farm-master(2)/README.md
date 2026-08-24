# README.md
# 端午农场助手

## 功能
- 自己农场：浇水、翻地、收菜、种植、夜间换种
- 好友农场：帮忙浇水、翻地、偷菜（翻地与自己共用每日上限）
- 分通道动态调度（收菜 / 翻地 / 偷菜独立 ETA + 抖动）
- 组队秘境：常驻 WebSocket 监听，匹配公开房自动加入（可选）

## 目录结构
```
duanwu_farm/
├── .env                      # 本地配置（勿提交）
├── .env.example              # 配置示例
├── config.py                 # 从 .env 加载配置
├── main.py                   # 跑一轮农场
├── scheduler.py              # 定时循环入口
├── run_team_dungeon.py       # 单独跑秘境监听
├── requirements.txt
└── farm/
    ├── auth/                 # 签名认证
    │   ├── crypto.py
    │   └── client.py         # SignClient
    ├── own/                  # 自己农场
    │   └── farm.py           # 查询 / 收菜 / 种植 / 浇水 / 翻地 / 铲除
    ├── friend/               # 好友农场
    │   ├── list.py           # 好友列表
    │   ├── farm.py           # 好友地块查询
    │   └── actions.py        # 偷菜 / 翻地 / 帮忙浇水
    ├── schedule/             # 调度
    │   ├── round.py          # 各通道一轮流程（翻地：先自己后好友）
    │   └── scheduler.py      # 分通道循环 + 夜间 + 秘境监听线程
    └── team_dungeon/         # 组队秘境
        ├── api.py            # REST（体力 / 次数）
        ├── client.py         # Socket.IO 大厅
        ├── battle.py         # 战斗引擎桥接
        ├── secure.py         # 结算签名
        └── runner.py         # 监听编排
```

## 安装
```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## 配置
复制 `.env.example` 为 `.env` 并填写：
- `BEARER_TOKEN`
- `DAY_CROP_VIP` / `DAY_CROP_NORMAL`（默认 quinoa / carrot，按账号 VIP 自动选用）
- `NIGHT_CROP_ID`
- `AUTO_STEAL` / `AUTO_EXPLORE` / `AUTO_HARVEST` / `AUTO_PLANT` / `AUTO_CARE`
- `AUTO_TEAM_DUNGEON`（秘境类型按账号已解锁自动判断；进本前自动买/用经验水）

## 运行
```bash
# 跑一轮农场
python main.py

# 定时循环（农场 + 可选秘境监听）
python scheduler.py

# 仅秘境常驻监听
python run_team_dungeon.py
```

首次运行会在 `sign/` 下生成 `sign_key.json` 并注册签名公钥；token 失效时删除 `sign/.reg_mark` 再跑。
