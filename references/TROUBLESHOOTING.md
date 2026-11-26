# 排障手册

> 先看 `SKILL.md` 的 top5，这里是不常见的部分。
> 日志：`scripts\_hud.log`（主程序，异常自动落盘）、`scripts\_guard.log`（守护进程）。

---

## 1. 故障表（按现象索引）

### 显示与消失

| 现象 | 原因与处理 |
|---|---|
| 换了页面还剩着 | 侧边栏折叠或窗口太矮时返回"判断不了"、保持原样。入口行位置随版本可能微调（实测 96/132/168/204/240/276 起、各 34 DIP 高），改 `SB_NAV_TOP_DIP`、`SB_NAV_SPLIT_DIP` 对齐 |
| 别的窗口盖住侧边栏右半，切页没反应 | 扫描区已收窄到侧边栏左侧 8~70 DIP，这种遮挡基本不触发；仍复现就看 `_trace.log` 里是不是 `covered at …`（返回"判断不了"→保持现状） |
| 入口页来回切时闪一下 | 只认"连续两帧都是入口页"（0.15 秒）才收，单帧假象不收。若闪，`_trace.log` 里 `v` 会在两帧间跳变，核对是否过渡帧 |
| 鼠标扫到「发现」弹出小菜单，数字立刻没了 | 现在不看点击记忆，只看"高亮在「最近」上面还是下面"，这类悬停不会误判；若仍复现，抓 `_trace.log` 的 `bands=` |
| 点了入口、鼠标停在那一行，条子不收 | 入口行点击会记 `nav_click_t`，0.5 秒内直接按入口页处理；检查 `lbtn()` 是否还在找上升沿 |
| 重启软件后停在「新建任务」，条子却挂着数字 | 第一帧就该看像素位置，不依赖操作历史。若出现，说明扫描区被遮住或上沿压到了 96 |
| 打开设置窗口，设置面板底部也冒出一行 | `is_main_win()` 必须要求最小化按钮（主窗口 `0x14C70000` / 设置窗口 `0x14C50000`），排查用 `test_visibility.py` 的 K 组 |
| 点开头像/设置面板，条就消失 | 靠「灰阶差 ≥7 + 行高 18~52 DIP」两道门挡掉几百 DIP 高的弹层；复现就用 `test_visibility.py` 的 G 组核门槛 |
| 两个窗口数字一样 | 另一个窗口还没被写入过，去看看/发一句话就纠正；或设 `TP_SESSION` 钉死 |
| 左边只剩「对话N轮」/ 右边只剩「Token总计」 | 窗口太窄触发降级。拉宽窗口，或调小 `TP_RIGHT` |

### 位置与外观

| 现象 | 原因与处理 |
|---|---|
| 条子压到侧边栏上 / 太贴左侧 | 探测失败沿用了兜底值。把 `TP_LEFT` 设成"侧边栏右缘 + 想要的空隙" |
| 别的窗口压上来时左段左右乱跑 | 确认 `side_left()` 还在用 `above_rects()` / `free_spans()` 按 z 序排除遮挡，且保留 `EDGE_SHARP` 硬边关；`above_rects()` 的 skip 名单别漏掉全屏不可见的系统窗口 |
| 点头像/设置，左段右移十几 DIP，关掉又弹回 | 弹层面板比侧边栏宽。`side_left()` 只在「拖边界」或「窗口尺寸变了」时才改读数 |
| 全屏时右边太贴屏幕边 | 确认 `rect()` 取的是 DWM 可见外框而不是 `GetWindowRect`；间距统一用 `TP_RIGHT` |
| 字比旁边的提示文案还小 | 跑的是按物理像素渲染的旧进程，重新双击 `start-hud.bat` |
| 文字几乎看不见 | `TP_INK` 太小，恢复 0.70 |
| 字边有红蓝毛边 | 字体质量被改回 ClearType（应为 `ANTIALIASED = 4`），或降采样被改成 `COLORONCOLOR` |
| 拖动时甩尾 | `follow_loop` 被改回低频了；跑 `test_visibility.py` 之外的跟手测试对比数值 |

### 今日面板

| 现象 | 原因与处理 |
|---|---|
| 面板不显示（今天确实有数据） | 依次查 ① `_TREADY` 是否置位（`today_poll_loop` 跑没跑）② WorkBuddy 是否在前台（不在前台就不显示，这是设计）③ 主窗口是否被识别（`wb_windows()` 拿到窗口才 `place()`）④ `python render_panel.py` 看绘制管线本身 |
| 第三列贴右边缘或缺一截 | ① 先看面板宽是否等于「侧边栏宽 − 24 DIP」（`check_hud.py` 报 `TPulseToday` 尺寸）② 再查 `MEAS_DC`：没建起来时量字宽全返回 0 → 右对齐失效。正常进程里由 `main()` 建；自己写脚本拉 `TodayPanel` 必须先补 `B.MEAS_DC, _, _ = B.new_dib(1, 1)` |
| 侧边栏拖窄后三列跑出框 / 挤在一起 | 面板宽必须跟着侧边栏实时变（`today_panel_w()` 读状态条的 `left_dip`）。写死 `TODAY_W_DIP` 会拖窄后溢出、拖宽后三列挤在左边 |
| 展开「最近所有对话」滚动时数据一闪一闪 | `today_obscured` 不能只扫灰线上方一条缝。正常应展开后 0.3 秒内消失、滚动全程不出现、收起后 0.25 秒内回来 |
| 切到浏览器/记事本，两处数据都消失 | 这是设计：只在 WorkBuddy 前台时显示。所有抓屏判定（侧边栏高亮、灰线、遮挡）在非前台时直接跳过，因为抓到的是别人的界面像素 |
| 切回 WorkBuddy 后面板一直不回来 | 面板是附属窗口，位置没变就不重设 z 序 → 一直沉在下面。`place()` 每 0.25 秒重提一次 z 序（`_z_t`），且 `park()` 后靠 `shown=False` 触发重新 ShowWindow |
| 出现两条条子，面板画到浏览器界面上 | Edge 打开 `open.workbuddy.cn` 时标题含 “WorkBuddy”、类名同为 `Chrome_WidgetWin_1`、也有最小化按钮。`is_main_win()` 必须比对进程名 `workbuddy.exe`（`proc_exe_name()`） |
| 全屏下数据整块消失 | 底色必须按众数现算，不能把"够白"一律当账号菜单 |
| 全屏切中等屏，先卡在左下再归位 / 迟迟才出现 | 面板跟 rect 每轮重算，0.05~0.1 秒到位；若延迟，先查 `TODAY_OBSC_FREEZE_S` 是不是被改小，导致动画里的列表行误触发收起 |
| 切全屏/最大化时整块表情卡一下 | 先量时序再动手。`today_obscured` 单帧只要 0.018 ms，卡顿来自逻辑上人为等待或误判收起，不是绘制慢 |
| 今日消耗比 `probe.py` 少一笔（几十分） | 子代理日志没被扫到。层级是 `<项目>/<会话ID>/subagents/agent-*.jsonl`，下钻只认 `subagents`/`agents` 两个名字；修完再跑 `probe.py` 对账，两者应逐位一致（相差 0.1 分内属 8 秒落盘的时间差） |
| 删了对话，今日数字不降 | 这是设计如此：钱真花掉了，账本按会话只增不减。要清掉这部分，删 `_tmp/spend-<日期>.json` 后重启 hud |

### 进程与自检

| 现象 | 原因与处理 |
|---|---|
| 说重启了但过一会儿自己消失 | 命令里 `Start-Process` 拉起的进程挂在本次命令的作业对象上，命令结束整棵树被回收（`CREATE_BREAKAWAY_FROM_JOB` 被拒、`PermissionError 5`）。只能用 `explorer.exe start-hud.bat`，或 `tpctl.py install` 装自启后重新登录 |
| `check_hud.py` 报找不到窗口 | 先确认进程在（互斥体 `Local\TPulse`），再看 `_hud.log`；`Local\TPulseGuard` 是守护进程 |
| 改了样式没反应 | 等 3 秒（热重载 1~3 秒），再看 `_hud.log` 有没有 `reload:` 那行 |
| 数值全是 0 | 日志 `timestamp` 被当秒用了，需 `/1000` |

---

## 2. 验证命令

```bat
cd /d "%USERPROFILE%\.workbuddy\skills\tokenpulse\scripts"

python -m py_compile hud.py guard.py tpctl.py
python test_metrics.py        :: 纯逻辑：单位换档 / 空态 / 三列锚点 / 对齐基准，应 70 passed
python test_visibility.py     :: 显隐回归；G/I/J/K/L 纯逻辑组必须全绿
python check_ghost.py         :: 幽灵窗口自查，必须 0 个 Ghost
python check_hud.py           :: 进程 / 窗口坐标 / 可见性
python probe.py               :: 今日口径独立对账
python render_panel.py        :: 离线渲染面板到 _tmp\panel.png，报墨量与墨迹框
```

**没有 Python？** 用 WorkBuddy 自带的那份：
`"%USERPROFILE%\.workbuddy\binaries\python\versions\<版本>\python.exe"`（`start-hud.bat` 里会自动找）。

### 对账

`probe.py` 按日扫全部日志，输出轮 / 分 / Token / 命中四项。**面板数值必须与它一致才可交付。** 二者口径同源，差异只可能来自增量读的落盘延迟（0.15 秒内追平）。

### 环境限制

- **锁屏下别指望截图**：`LockScreenBackstopFrame` 全屏遮挡，`BitBlt` 拿到的是壁纸，扫描判定一律"判断不了"。验渲染用 `render_panel.py`，验逻辑用 `test_metrics.py`。
- **`test_visibility.py` 的 A 组（切入口页→收起）在锁屏时必失败**，`_trace.log` 里是一串 `covered at …`。这是设计如此，不是回归；跑之前确认屏幕没锁。G/I/J/K/L 与 B~F 组不受影响。
- **CPU 基线**：浮条 + 面板 + 跟随线程合计约 1~2%，超过 4% 要查。
- **手动重启（模拟开机）**：杀掉命令行含 `hud.py` 的 `pythonw.exe`，守护进程 1 秒内拉新实例。

---

## 3. 踩坑清单

- 别用"点击记忆 + 鼠标是否压着"判路由：重启后没有点击记忆，必然把入口页当对话页。
- 扫描带上沿不要正好压在「新建任务」行上沿（96 DIP），贴边高亮会被"被切过"规则丢掉；定 84。
- 扫描不要横跨整条侧边栏：右半被任何窗口盖住就"判断不了"，停在旧状态。
- 弹层白 255 与侧边栏 247 只差 8：门槛太松（6）会把它当选中，门槛 7 + 行高 18~52 双保险。
- 独立顶层窗口筛选必须看 `WS_MINIMIZEBOX`，否则设置面板底部也挂一条。
- 假窗口的 `WM_PAINT` 里分块 `FillRect` 会被截到半幅，偶发"悬停被当成选中"；必须双缓冲（`CreateCompatibleDC` + 一次 `BitBlt`）。
- `GetAsyncKeyState(1) & 1` 在注入点击时不置位，必须自己找上升沿。
- `log_err` 有 30 秒去重，会吞掉测试日志；排障走 `_traw()` 直写 `_trace.log`。
- `HWND_TOPMOST` 会让面板变独立浮窗盖住别的程序；用 owner + `SWP_NOZORDER`。
- `Start-Process` 拉起的 GUI 进程会随命令树回收；用 `explorer.exe start-hud.bat`。
- 写 Python 脚本改文件时别把 `\n` 写成字面 `/n`（heredoc 里的 `\\n` 要看清）。
- 建了窗口不抽消息 → DWM 盖黑色 `Ghost` 窗口，点不掉、重启 WorkBuddy 也没用。
- 面板与浮条共用窗口类名 → 按类名枚举会抓错窗口，测试满屏 FAIL、截图截到面板。
- `MEAS_DC` 在 `main()` 里建：离线渲染没补它会 `SelectObject(None)` 失败、量字宽返回 0。
- 热重载连续触发 = 连续全量重扫大会话，和主进程抢 I/O 会把主进程拖死。别连续保存 `hud.py`。
- `GetTextExtentPoint32W` 量高做垂直排版：数字行要"贴底"就别用写死的偏移，拿字高 `sz.cy/SS` 去算 `bottom - vh`，字号一改也不会飘。
