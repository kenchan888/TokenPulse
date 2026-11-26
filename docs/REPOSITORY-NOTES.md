# 仓库整理说明

本仓库以用户提供的 `tokenpulse-1.0.0.zip` 为源，将原始 `tokenpulse/` 目录内容放到仓库根目录，并补充 GitHub 项目文档与两张界面截图。

## 本次新增

- `README.md`：项目首页，明确区分当前单对话与今日汇总两套数据。
- `docs/DATA-METRICS.md`：统计字段、公式、时间窗口、子代理、去重与账本说明。
- `CHANGELOG.md`：1.0.0 初始版本记录。
- `CONTRIBUTING.md`：贡献与测试说明。
- `SECURITY.md`：安全问题报告说明。
- `SUPPORT.md`：用户支持与问题反馈。
- `LICENSE-STATUS.md`：许可证待作者确认。
- `assets/screenshots/`：用户提供的两张界面截图。

## 保留的原始文件

原始 `SKILL.md`、`references/` 和 `scripts/` 均保留。除仓库文档外，本次整理不主动重构原始 Python 实现。

## 发布状态

1. 许可证：已确认采用 MIT License（见根目录 `LICENSE`），作者授权公开发布。
2. 隐私：已扫描全部提交文件，无个人路径、邮箱、令牌、真实会话 ID；截图经作者确认可公开。
3. 架构图：`assets/architecture.png`，展示本地日志 → 解析 → 统计 → HUD 的完整流程。

## 建议后续验证

1. 在干净 Windows 环境中执行安装与回归测试。
2. 核对 WorkBuddy 当前版本的日志路径、字段和窗口行为。
3. 如计划发布二进制包，另行说明打包方式并提供 SHA256 校验值。
