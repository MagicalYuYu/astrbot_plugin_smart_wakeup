<div align="center">

# 灵犀

**心有灵犀，不唤自来**

唤名即应，投缘便聊；群冷场了就来，聊久了也会累—— 由精力系统、心流状态、冷场救场与消息防抖默契配合而成的节律，让BOT拥有了自然的社交呼吸。

[![Version](https://img.shields.io/badge/version-1.0.0-blue?style=flat-square)](https://github.com/MagicalYuYu/astrbot_plugin_smart_wakeup)
[![Platform](https://img.shields.io/badge/platform-Telegram%20%7C%20QQ(aiocqhttp)-green?style=flat-square)](https://github.com/MagicalYuYu/astrbot_plugin_smart_wakeup)
[![License](https://img.shields.io/badge/license-MIT-orange?style=flat-square)](https://github.com/MagicalYuYu/astrbot_plugin_smart_wakeup)
[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A5v4.5.0-purple?style=flat-square)](https://github.com/Soulter/AstrBot)

</div>

---

## 功能一览

| 模块 | 说明 |
|:-----|:-----|
| **名称唤醒** | 消息包含 Bot 名称即触发回复，支持多别名（`|` 分隔）、大小写不敏感 |
| **概率唤醒** | 未提及名称时按概率主动回复，概率由精力/心流/参与度动态计算 |
| **精力系统** | 模拟社交疲劳——每次回复消耗精力，随时间恢复；精力耗尽则暂停主动发言 |
| **心流状态机** | 旁观 → 关注 → 心流 → 疲劳，四状态动态调整回复策略与概率 |
| **冷场救场** | 群聊冷场后主动参与对话，有冷却期防止频繁救场 |
| **消息防抖** | 等待用户停止发言后再回复，聚合多条消息，避免打断补充发言 |
| **复读抑制** | 检测复读链并大幅降低回复概率，避免对复读刷屏高频回复 |
| **对话记忆** | 分层记忆：近期保留原文 + 远期压缩摘要，支持多轮连贯对话 |
| **上下文压缩** | 用小模型压缩群聊上下文再注入主模型，大幅减少 token 消耗 |
| **智能模型路由** | 简单消息用小模型、复杂消息用大模型，支持级联升级（实验性） |
| **消息分段** | 长回复智能分段发送，模拟真人输入节奏，支持末尾标点剔除 |
| **思考标签过滤** | 兜底过滤 LLM 回复中的 `<think>` 等思考内容，防止提示词泄露 |
| **Token 消耗追踪** | 实时统计 token 用量，异常检测告警（σ 阈值 + Prompt 占比） |

## 工作原理

```
群聊消息到达
  │
  ├─ 记录到消息缓冲区
  ├─ 群组白名单/黑名单过滤
  │
  ▼ 唤醒判定
  │
  ├─ 命中名称 ──────────────→ 确定性回复
  ├─ 命中关键词 ────────────→ 概率性回复
  ├─ 概率唤醒（精力×心流×参与度）→ 概率性回复
  ├─ 冷场救场（超时+冷却）───→ 主动回复
  └─ 复读链 ────────────────→ 降低概率
  │
  ▼ 消息防抖（等待用户说完，聚合多条消息）
  │
  ▼ 上下文构建
  │
  ├─ 增量注入新消息 + 补充旧消息
  ├─ 小模型压缩上下文（可选）
  ├─ 分层记忆：近期原文 + 远期摘要
  └─ 智能路由：简单→小模型 / 复杂→大模型（可选）
  │
  ▼ LLM 生成回复
  │
  ├─ 思考标签过滤
  ├─ 消息分段 + 延迟发送（可选）
  └─ 消耗精力 → 更新心流状态
```

## 安装

**方式一：插件市场（推荐）**

在 AstrBot 插件市场搜索「灵犀」，点击安装。

**方式二：手动安装**

将项目文件夹放入 AstrBot 的 `data/plugins/` 目录，重启 AstrBot 或在 WebUI 中启用插件。

## 快速配置

安装后在 AstrBot WebUI → 插件管理 → 灵犀 → 配置 中修改：

| 配置项 | 说明 | 必填 |
|:-------|:-----|:----:|
| `bot_name` | Bot 名称/别名，多个用 `|` 分隔，如 `Bot|小助手` | 是 |
| `probability_wakeup` | 概率唤醒开关，开启后未提及名称也有概率回复 | 否 |
| `splitter.enabled` | 消息分段开关，开启后长回复分段发送 | 否 |

其余配置项均有合理默认值，开箱即用。详细配置说明请参阅 [使用指南](docs/usage_guide.md)。

## 调试指令

所有指令仅管理员可用，在群聊中发送即可：

| 指令 | 说明 |
|:-----|:-----|
| `/wakeup_status` | 插件运行状态、统计、缓冲区概览 |
| `/wakeup_energy` | 当前群精力状态 |
| `/wakeup_flow` | 当前群心流状态 |
| `/wakeup_token` | Token 消耗统计 |

## 前置条件

- AstrBot >= v4.5.0
- 已配置 Telegram 或 QQ（aiocqhttp）平台适配器
- 已配置 LLM 服务商
- 已配置人格设定（推荐）

## 详细文档

| 文档 | 说明 |
|:-----|:-----|
| [使用指南](docs/usage_guide.md) | 完整的配置说明、工作原理、场景行为矩阵和常见问题 |
| [故障处理与调试指南](docs/troubleshooting_guide.md) | 故障排查流程、AI 辅助诊断、参数调优、作者报备规范 |

## 致谢与声明

本项目由 [MagicalYuYu](https://github.com/MagicalYuYu) 构思并主导开发，在 AI 辅助编程工具的协作下完成——核心设计决策与代码审查均由作者把控，AI 负责代码生成与迭代实现。感谢开源社区提供的工具与灵感。

## License

[MIT](LICENSE)
