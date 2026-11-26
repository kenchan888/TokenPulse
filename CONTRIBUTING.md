# 贡献指南

感谢关注 TokenPulse。提交代码前，请先阅读 `references/ARCHITECTURE.md` 与 `docs/DATA-METRICS.md`，尤其注意单对话与今日汇总是两套不同统计范围。

## 提交问题

请提供：
- Windows 版本与系统缩放比例；
- WorkBuddy 版本；
- 复现步骤与预期结果；
- 实际结果、相关错误日志；
- 是否影响当前单对话、今日汇总，或两者。

提交日志前，请删除会话正文、用户标识、访问令牌和其他隐私信息。不要上传完整 `projects/`、`logs/` 或 `workbuddy.db`。

## 提交代码

1. Fork 仓库并创建功能分支。
2. 修改统计口径前，先更新 `docs/DATA-METRICS.md` 和 `references/ARCHITECTURE.md`。
3. 保持当前单对话与今日汇总的范围边界清晰。
4. 运行适用的语法检查与回归测试。
5. 在 Pull Request 中说明改动、测试环境和结果。

## 建议验证

```bat
python -m py_compile scripts\hud.py scripts\guard.py scripts\tpctl.py
python scripts\test_metrics.py
python scripts\test_visibility.py
python scripts\probe.py
```

部分窗口显隐测试依赖真实 WorkBuddy 窗口和未锁屏桌面环境。请在报告中注明测试条件。
