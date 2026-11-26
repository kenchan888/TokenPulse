---
name: tokenpulse
display_name: "TokenPulse · 对话用量仪表"
display_name_en: "TokenPulse · Conversation Usage Meter"
description: "在 WorkBuddy 每个对话窗口底部挂一条实时浮字条：左下是本对话轮数与累计积分，右下是本对话总 token 与缓存命中率；侧边栏底部另有三列今日 KPI（今日对话 / 今日消耗 / 今日Token）。只读本地会话日志，不需要任何配置，纯标准库、零第三方依赖。切到专家 / 技能 / 连接器 / 定时任务 / 资料库等入口页自动隐藏。触发词：状态条、悬浮条、浮字条、用量条、token 消耗、token 速率、缓存命中率、上下文占用、积分、额度、今日花了多少、对话轮数、token bar、cache hit rate、credits、status bar、TokenPulse。"
description_zh: "在 WorkBuddy 每个对话窗口底部挂一条悬浮浮字条：左下是本对话轮数与累计积分，右下是本对话总 token 与缓存命中率；侧边栏底部另有三列今日 KPI。切到非对话页自动隐藏，直接读本地会话日志。"
description_en: "A live usage bar pinned to the bottom of each WorkBuddy conversation window: turn count and credits on the left, total tokens and cache hit rate on the right. A three-column today panel sits above the sidebar account row. Reads local session logs only."
examples_zh:
  - "帮我把用量条打开"
  - "这个对话一共花了多少 token？"
  - "这两个窗口怎么显示的是同一个数？"
examples_en:
  - "Turn on the usage bar"
  - "How many tokens did this whole conversation use?"
  - "Why do both windows show the same numbers?"
version: 1.0.0
author: "陈彦恺"
---

# TokenPulse · 对话用量仪表

在 WorkBuddy **每个对话窗口的底部**挂一条只画字的浮条：**左下**与底部那句提示文案同一行、字号大半号（13 vs 12 DIP）、字色深一档（`#4C4C4C`）；**右下**贴右边缘对齐。侧边栏底部另有一块三列「今日」KPI。

```
● 对话23轮 · 消耗90.88分            内容由 AI 生成，请核实重要信息            Token总计53.12M · 缓存命中99.3%
```

随主窗口移动、最小化隐藏、关闭一起销毁。**只在 WorkBuddy 自己处在前台时显示**：切到浏览器或任何别的程序，条子与今日面板一起收起，绝不画到别人的界面上。

## 何时使用

- 想知道本对话累计多少 token、花了多少积分、缓存命中多少
- 想看今天所有会话合起来用了多少（侧边栏「今日」三列）
- 条子没出现、位置不对、多窗口数字一样、跑到非对话页上了
- 要调整位置、颜色深浅、不透明度

## 目录结构

```
tokenpulse/
├── SKILL.md                     # 技能定义（本文件）
├── references/                  # 参考资料，正文以 @references/xxx.md 引用
│   ├── ARCHITECTURE.md          # 口径判据、常量、线程模型、坐标系
│   ├── TROUBLESHOOTING.md       # 故障全表、验证命令、CPU 基线
│   └── SETTINGS.md              # TP_* 环境变量全量
└── scripts/                     # 可执行脚本，由 Bash 调用
    ├── hud.py                   # 主程序：浮条 + 今日面板
    ├── guard.py                 # 守护：拉起、热重载、单实例
    ├── tpctl.py                 # 自启开关 install / remove / status
    ├── start-hud.bat            # 启动并自检
    ├── stop-hud.bat             # 停止浮条与守护
    ├── install-autostart.bat    # 写入启动文件夹 vbs
    ├── uninstall-autostart.bat  # 移除自启
    ├── check_hud.py             # 查窗口坐标与可见性
    ├── check_ghost.py           # 查 DWM 幽灵窗口
    ├── test_metrics.py          # 统计口径回归
    ├── test_visibility.py       # 显隐判定回归
    ├── probe.py                 # 独立口径探针，用于对账
    └── render_panel.py          # 离屏渲染面板 PNG，验证版式
```

运行时产物（`scripts/_hud.log`、`_guard.log`、`_tmp/`）由脚本自行生成，可随时删除。

## 四项数据

| 显示 | 含义 | 口径 |
|---|---|---|
| `对话N轮` | 你提问、且回答**完整结束**的次数。按**请求**（`conversationRequestId`）计，一轮里分几步输出仍算 1 轮 | 本窗口全对话，**跨天累计** |
| `消耗X.XX分` | 本窗口所有模型调用的积分合计，含工具调用、上下文压缩、自动取标题 | 本窗口全对话 |
| `Token总计` | 本窗口所有调用的 `total_tokens` 合计，不只算最后一次回复 | 本窗口全对话 |
| `缓存命中XX.X%` | 全局 `Σ prompt_cache_hit_tokens ÷ Σ prompt_tokens`，不是逐条平均 | 本窗口全对话 |

- **换单位**：不足 10K 给数字（`8,420`）；10K–999K 一位小数 K（`128.4K`）；百万以上两位小数 M（`132.82M`）。
- **跨天不清零**：这是"这个对话累计花了多少"，不是日报；切会话才全部重算。
- **轮数与钱不同步是正常的**：被中断、被回退的请求不算轮，但钱照算。

侧边栏「今日」三列（`今日对话 / 今日消耗 / 今日Token`，标签在上、数字在下）只换时间窗与文件范围，判据与前四项同一套；今天一笔都没有时整块不显示。**面板宽度跟着侧边栏走**：拖动侧边栏时三列按新宽度重新分配（贴左 / 居中 / 贴右），下限 150 DIP。

## 什么时候显示

两条硬规则，任一不满足就整块收起：

1. **WorkBuddy 必须是当前前台窗口**。切到浏览器、记事本、任何别的程序都不显示——这些字是贴在 WorkBuddy 界面上的，不能压到别人的画面里。
2. **必须停在对话页**。新建任务 / 助理 / 专家·技能·连接器 / 定时任务 / 资料库 / 发现 这六个入口页一律不显示。

第 2 条只能看像素判定：窗口标题不随路由变化，UI 树也拿不到，所以靠**侧边栏任何时刻恰好一行高亮**——高亮在「最近」列表上方即入口页、下方即对话页，以 `340 DIP` 为界。

实测入口行位置随 WorkBuddy 版本可能微调，若判错改 `SB_NAV_TOP_DIP` / `SB_NAV_SPLIT_DIP` 对齐；详见 @references/ARCHITECTURE.md。

## 启动与停止

脚本都在本文件同级的 `scripts/` 目录里，Windows 专用。所有命令先切到脚本目录：

```bat
cd /d "%USERPROFILE%\.workbuddy\skills\tokenpulse\scripts"

start-hud.bat                       :: 启动 + 自检（首次或进程被杀后双击）
stop-hud.bat                        :: 停掉浮条与守护
python tpctl.py install|remove|status
python check_hud.py                 :: 窗口坐标与可见性
python check_ghost.py               :: 幽灵窗口计数
python test_metrics.py              :: 统计口径回归
python test_visibility.py           :: 显隐判定回归
python render_panel.py _tmp\panel.png
```

`start-hud.bat` 会自己找 Python（优先 WorkBuddy 自带的那份），命令行里跑 `.py` 则用 PATH 上的 `python`。首次安装或进程被强杀后双击 `start-hud.bat`；`tpctl.py` 是那两个自启 bat 的本体。

**改完代码不用重启**：`hud.py` 自带热重载，源文件 mtime/大小稳定 0.7 秒后自行退出，守护进程 1 秒内拉起新实例，2~3 秒生效。只有首次安装或进程被强杀后才需要双击 `start-hud.bat`。

## 环境变量

在 `start-hud.bat` 顶部放开对应的 `set` 行（改完重新双击）：

```bat
set "TP_LEFT=300"        兜底起点，仅当探测失败时使用（DIP）
set "TP_RIGHT=6"         右段距窗口右边（DIP）
set "TP_BOTTOM=7.3"      兜底距离，仅当量不到提示文案时使用（DIP）
set "TP_INK=0.70"        文字不透明度 0~1，越大越深
set "TP_GAMMA=0.90"      抗锯齿 gamma，<1 加粗
set "TP_OPACITY=255"     整条不透明度 60~255
set "TP_ONLY_CHAT=1"     0 = 关掉"只在对话页显示"
set "TP_SESSION="        钉死某个会话（uuid 或 jsonl 路径），留空=自动跟随
set "TP_NORELOAD=1"      关掉热重载
```

完整清单与推荐值见 @references/SETTINGS.md。

## 常见故障 top5

| 现象 | 处理 |
|---|---|
| 双击后没反应 | 看窗口里的自检输出和 `scripts\_guard.log`；再跑 `check_hud.py` |
| 桌面上有块黑色方块，点不掉 | DWM 幽灵窗口（`Ghost` 类，标题 `TPulseToday`）。`Stop-Process` 掉 pythonw，守护进程会用新代码拉起，幽灵随之消失 |
| 浮条一直不出现 | 先看 `scripts\_hud.log`，再跑 `python scripts\check_hud.py` 看窗口坐标与可见性 |
| 出现两条条子 / 面板画到浏览器上 | 浏览器打开 `open.workbuddy.cn` 时标题含 “WorkBuddy”、类名同 Chromium，会被当成主窗口。`is_main_win()` 现在比对进程名 `workbuddy.exe`；仍出现就 `Stop-Process` 重起 |
| 侧边栏拖窄后第三列跑出框 / 挤成一团 | 面板宽应等于侧边栏宽 − 24 DIP。`python scripts\check_hud.py` 看 `TPulseToday` 宽度对不对；宽度没跟着变说明状态条的 `left_dip` 没更新 |

其余 20+ 条按现象索引：@references/TROUBLESHOOTING.md。

## 兼容性

Windows 专用（Win32 + GDI + sqlite3，纯标准库）。已适配高 DPI：按 `GetScaleFactorForDevice` 缩放，150% 下 13 DIP 字号落到约 19.5 px 物理像素。不写注册表、不留服务，删掉目录即干净。

## 深入阅读

- **要改统计口径 / 显隐判定 / 面板版式** → @references/ARCHITECTURE.md（判据、常量、线程模型、坐标系）
- **要跑回归、对账或排查疑难** → @references/TROUBLESHOOTING.md（故障全表、验证命令、CPU 基线、锁屏限制）
- **要调外观与位置** → @references/SETTINGS.md（`TP_*` 全量、各常量的物理含义）

## 署名与免责

- 作者：陈彦恺 · 版本 1.0.0
- 平台：Windows（Win32 + GDI + sqlite3，纯 Python 标准库，无第三方依赖）
- 界面渲染、显隐判定、会话跟随、今日汇总、线程模型均为本项目自有实现；使用与转载请保留本署名。
- 只读本地会话日志，不联网、不上传任何数据；不写注册表、不留后台服务，删掉目录即干净。
