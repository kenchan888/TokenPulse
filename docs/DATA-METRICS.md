# 数据口径与计算说明

本文专门解释界面中的两套统计数据。两者使用相同的底层用量字段和轮数判据，但统计范围与时间窗口不同。

## 1. 先区分两套数据

| 数据集 | 显示位置 | 文件范围 | 时间窗口 |
|---|---|---|---|
| 当前单对话 | WorkBuddy 窗口底部悬浮条 | 当前界面对应的单个会话 JSONL | 该会话完整历史，跨天累计 |
| 今日汇总 | 左侧栏底部三列面板 | 今天有用量的所有会话及其子代理日志 | 本地当天 00:00 至当前 |

**不要把底部悬浮条与左侧面板当成同一组数字。** 当前会话可能包含多天历史；今日面板则包含今天所有会话。切换对话只改变底部悬浮条的会话范围，不改变今日汇总的全局范围。

## 2. 当前单对话：底部悬浮条

读取当前会话文件：

```text
~/.workbuddy/projects/<项目目录>/<会话UUID>.jsonl
```

显示四项：

### 对话轮数

按 `conversationRequestId` 聚合。一个请求可能包含多次模型调用或工具步骤，只有请求满足完整结束条件才计为一轮。必须同时满足：

1. 请求最后一条记录是 assistant 消息；
2. 存在 `providerData.usage`；
3. `providerData.error` 为空；
4. 没有被 `resend-fork-notice.editedUserItemId` 作废；
5. 不是纯子代理请求。

用户中断、回退重发、上下文压缩摘要、自动取标题、后台子代理等请求可以产生真实用量，但不一定计入完整对话轮数。

### 积分消耗

```text
当前会话积分 = Σ providerData.rawUsage.credit
```

对当前会话文件中各模型调用记录的 `rawUsage.credit` 求和，显示两位小数。该值不只统计最终回答，还包括请求过程中产生用量的模型调用。

### Token 总计

```text
当前会话 Token = Σ providerData.rawUsage.total_tokens
```

`total_tokens` 包含输入与输出 Token。统计每次模型调用记录，而不是只取最后一条 assistant 消息。

### 缓存命中率

```text
缓存命中率 = Σ prompt_cache_hit_tokens ÷ Σ prompt_tokens × 100%
```

先累计所有适用记录的命中 Token 与 Prompt Token，再计算整体比例。不能先算每条记录的百分比再做简单平均，否则小请求与大请求会获得相同权重。

## 3. 今日汇总：左侧三列面板

今日汇总扫描所有项目会话日志，以及会话目录内的子代理日志：

```text
~/.workbuddy/projects/
  <项目目录>/
    <会话UUID>.jsonl
    <会话UUID>/
      subagents/agent-*.jsonl
      agents/...
```

不把 `tool-results/*.ndjson` 当作模型用量账目。

统计时间按本地自然日划分：当天 00:00 至当前时刻。归日依据是记录自己的 `timestamp`，而不是文件修改时间或会话创建时间。日志中的 `timestamp` 为毫秒时间戳。

### 今日对话

沿用完整对话轮数的五项判据，跨会话汇总；纯子代理请求不增加轮数。轮数按请求完成时刻归属日期。

### 今日消耗

```text
今日积分 = Σ 当日所有主会话与子代理记录的 rawUsage.credit
```

中断、回退、上下文压缩、自动取标题等产生的真实用量照常累计，即使对应请求没有形成一轮完整对话。

### 今日 Token

```text
今日 Token = Σ 当日所有主会话与子代理记录的 rawUsage.total_tokens
```

### 子代理和去重

子代理用量可能只存在于：

```text
<会话UUID>/subagents/agent-*.jsonl
```

所以只扫描主会话 JSONL 会漏掉子代理产生的消耗。与此同时，`credit-usage`（`source=team_member`）与子代理 `rawUsage` 可能是同一笔费用的两种记录形式，不能重复相加；以带 `rawUsage` 的一侧为准。

### 跨午夜请求

- 积分与 Token：按每条 `rawUsage` 记录的 `timestamp` 分配到对应日期。
- 完整对话轮数：按请求完成时刻归日。

因此，一个跨越午夜的请求，其调用用量可能分布在两个自然日，而完整轮数只归入完成的那一天。

## 4. 今日账本

今日汇总维护按会话分项的临时账本：

```text
scripts/_tmp/spend-<日期>.json
```

每轮扫描后，将会话的 `[轮数, 积分, Token, prompt, hit]` 与内存值逐项取最大值，再写入账本；写入约每 8 秒节流一次。这样，即使某个会话随后从 WorkBuddy 中删除，当天已经发生的用量也不会从今日总数中回退。

账本仅保留最近 7 天。删除账本会丢失已删除会话对应的那部分历史累计数据。账本属于运行时数据，不应提交到 Git。

## 5. 数据源

| 路径 | 读取内容 | 用途 |
|---|---|---|
| `projects/<项目>/<会话UUID>.jsonl` | `rawUsage.credit`、`total_tokens`、`prompt_tokens`、`prompt_cache_hit_tokens`、`conversationRequestId`、usage/error/末条类型 | 会话统计 |
| `projects/<项目>/<会话UUID>/subagents/agent-*.jsonl` | 子代理 `rawUsage` | 今日汇总 |
| `logs/daemon.log` | 最后一条 `history-probe` 中的 `conversationId` | 当前会话识别 |
| `logs/renderer.log` | heartbeat 中的 `currentConversationId` | 当前会话识别 |
| `workbuddy.db` | `sessions` 表的活动时间字段 | 当前会话识别辅助 |

`rawUsage` 可能出现在一次请求的多次模型调用记录中。只读 `message.usage` 会漏掉大量 Token 用量。

## 6. 读取与刷新

- 文件不超过 64 MB：整份读取。
- 文件超过 64 MB：退化为读取尾部 24 MB。
- 增量读取只处理最后一个完整换行；半行留待下一次。
- 文件变小或被重写：从头重新读取。
- 空闲轮询约 0.25 秒；最近 3 分钟有调用时约 0.15 秒。
- 今日账本约每 8 秒节流写入。

## 7. 示例解释

假设底部显示：

```text
对话45轮 · 消耗105.79分 · Token总计52.85M · 缓存命中98.5%
```

这四项描述的是**当前打开的单个会话从创建至今的累计用量**。

同时左侧显示：

```text
今日对话4轮 · 今日消耗2.71分 · 今日Token1.06M
```

这三项描述的是**今天所有会话及子代理在本地日界内的汇总**。

这两组数值统计范围不同，不能用一组去校验另一组。要核对今日汇总，请运行 `scripts/probe.py`，并在相同时间点比较结果。
