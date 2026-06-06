<div align="center">

English | [中文](README.md)

<img src="logo.png" width="120" alt="Lingxi Logo" />

# Lingxi

**Responds when called, chimes in when the vibe is right**

A name-based natural wakeup plugin for AstrBot — powered by an energy system, flow state machine, idle-rescue, and message debounce working in concert to give your bot a natural social rhythm. Compatible with Telegram and QQ (aiocqhttp).

[![Version](https://img.shields.io/badge/version-1.0.1-blue?style=flat-square)](https://github.com/MagicalYuYu/astrbot_plugin_smart_wakeup)
[![Platform](https://img.shields.io/badge/platform-Telegram%20%7C%20QQ(aiocqhttp)-green?style=flat-square)](https://github.com/MagicalYuYu/astrbot_plugin_smart_wakeup)
[![License](https://img.shields.io/badge/license-AGPL--3.0-orange?style=flat-square)](LICENSE)
[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A5v4.5.0-purple?style=flat-square)](https://github.com/Soulter/AstrBot)

</div>

---

## What It Does

- **Responds to its name** — Triggers on messages containing the bot's name or aliases, case-insensitive
- **Chimes in when engaged** — Probabilistic wakeup dynamically calculated from energy, flow state, and participation — not random noise
- **Takes breaks when tired** — An energy system simulates social fatigue; when depleted, the bot stops initiating and recovers over time
- **Rescues dead chats** — Detects idle conversations and steps in, with a cooldown to prevent over-rescuing
- **Waits for you to finish** — Message debounce aggregates rapid-fire messages before replying, so it never interrupts mid-thought
- **Stays out of repeat chains** — Detects repeat-copying and drastically lowers reply probability, keeping the group's repeat culture intact
- **Remembers what was said** — Layered conversation memory: recent turns kept verbatim, older turns compressed into summaries
- **Saves tokens where it can** — Context compression, smart model routing, and token usage tracking with anomaly alerts
- **Types like a real person** — Long replies are split into natural segments with realistic pacing and trailing punctuation cleanup

<details>
<summary><strong>Full Feature List</strong></summary>

| Module | Description |
|:-------|:------------|
| Name Wakeup | Triggers when a message contains the bot's name or aliases (`|`-separated), case-insensitive |
| Probabilistic Wakeup | May reply even without being named; probability dynamically computed from energy/flow/participation |
| Energy System | Simulates social fatigue — each reply costs energy, which recovers over time; depletion pauses proactive replies |
| Flow State Machine | Bystander → Attentive → Flow → Fatigued — four states dynamically adjust reply strategy and probability |
| Idle Rescue | Steps in when group chat goes quiet, with a cooldown to prevent over-rescuing |
| Message Debounce | Waits for users to finish speaking before replying, aggregating multiple messages into one input |
| Repeat Suppression | Detects repeat-copy chains and drastically lowers reply probability, preserving group repeat culture |
| Conversation Memory | Layered memory: recent turns verbatim + older turns compressed into summaries for coherent multi-turn dialogue |
| Context Compression | Compresses group chat context with a smaller model before injecting into the main model, significantly reducing token usage |
| Smart Model Routing | Routes simple messages to a small model and complex ones to a large model, with cascade escalation (experimental) |
| Message Splitting | Splits long replies into natural segments with realistic pacing and optional trailing punctuation cleanup |
| Thinking Tag Filter | Strips LLM thinking content (e.g. `<think/>` tags) from replies as a safety net against prompt leakage |
| Token Usage Tracking | Real-time token usage statistics with anomaly detection (σ-threshold + prompt ratio) |

</details>

## How It Works

```
Group message arrives
  │
  ├─ Logged to message buffer
  ├─ Group whitelist/blacklist filter
  │
  ▼ Wakeup Check
  │
  ├─ Name hit ──────────────────→ Definite reply
  ├─ Keyword hit ───────────────→ Probabilistic reply
  ├─ Probabilistic wakeup ──────→ Probabilistic reply
  │   (energy × flow × participation)
  ├─ Idle rescue (timeout+cooldown) → Proactive reply
  └─ Repeat chain ─────────────→ Lowered probability
  │
  ▼ Message Debounce (wait for user to finish, aggregate messages)
  │
  ▼ Context Construction
  │
  ├─ Incremental injection + backfill
  ├─ Small-model context compression (optional)
  ├─ Layered memory: recent verbatim + older summaries
  └─ Smart routing: simple → small model / complex → large model (optional)
  │
  ▼ LLM Generates Reply
  │
  ├─ Thinking tag filter
  ├─ Message splitting + delayed sending (optional)
  └─ Energy consumed → Flow state updated
```

## Installation

**Plugin Marketplace (recommended)** — Search for "Lingxi" in the AstrBot plugin marketplace and click Install.

**Manual** — Place the project folder into AstrBot's `data/plugins/` directory, then restart AstrBot or enable the plugin in the WebUI.

## Quick Setup

After installation, configure via **AstrBot WebUI → Plugin Management → Lingxi → Settings**:

| Setting | Description | Required |
|:--------|:------------|:--------:|
| `bot_name` | Bot name/aliases, `|`-separated, e.g. `Bot|Assistant` | Yes |
| `probability_wakeup` | Enable probabilistic wakeup — the bot may reply even without being named | No |
| `splitter.enabled` | Enable message splitting — long replies are sent in natural segments | No |

All other settings have sensible defaults and work out of the box. For detailed configuration, see the [Usage Guide](docs/usage_guide.md) (Chinese).

## Debug Commands

Admin-only commands, sent in group chat:

| Command | Description |
|:--------|:------------|
| `/wakeup_status` | Plugin status, statistics, buffer overview |
| `/wakeup_energy` | Current group energy state |
| `/wakeup_flow` | Current group flow state |
| `/wakeup_token` | Token usage statistics |

## FAQ

<details>
<summary>Which platforms are supported?</summary>

Telegram and QQ (via the aiocqhttp adapter). Both platforms have identical functionality; group IDs differ in format — Telegram group IDs are typically negative numbers, QQ group IDs are positive.

</details>

<details>
<summary>Is data persisted?</summary>

No. All runtime data (message buffers, energy/flow states, conversation history) is stored in memory and cleared on AstrBot restart or plugin reload. This is by design — the buffer only provides recent context and doesn't need persistence.

</details>

<details>
<summary>Can different groups have different settings?</summary>

Yes. Use "Advanced Settings → Per-Group Overrides" to set independent parameters for specific groups. Unoverridden fields fall back to global defaults.

</details>

<details>
<summary>How do I disable probabilistic wakeup and keep name-trigger only?</summary>

Set `probability_wakeup = false`. The bot will only reply when its name or keywords are mentioned — no proactive participation.

</details>

<details>
<summary>Token usage is too high — what can I do?</summary>

1. Enable `context_compression_enabled` — compresses group chat context with a smaller model
2. Enable `bypass_core_context` — avoids duplicate injection
3. Reduce `context_messages_count` — fewer messages in context
4. Enable `incremental_context_enabled` — avoids re-injecting content
5. If possible, configure a dedicated small model for compression (`compression_model`)

</details>

<details>
<summary>The bot replies too often / too rarely — how to adjust?</summary>

**Too active**: Lower `flow_flow_prob` (flow-state reply probability) first, then increase `energy_decay_rate` (energy cost per reply) so the bot tires faster.

**Too quiet**: Raise `flow_bystander_prob` (bystander-state reply probability) first, then lower `energy_decay_rate` so the bot doesn't tire as easily.

Adjust one parameter at a time and observe for 1–2 days. For more tuning tips, see the [Troubleshooting & Debug Guide](docs/troubleshooting_guide.md) (Chinese).

</details>

<details>
<summary>Why isn't the bot replying?</summary>

Troubleshooting steps:
1. `/wakeup_status` — check if the plugin is running
2. `/wakeup_groups` — verify the current group is allowed
3. Check if the message contains the configured bot name or keywords
4. Check if energy is depleted (`/wakeup_energy`)
5. Check AstrBot logs for errors
6. Confirm an LLM provider is configured

</details>

<details>
<summary>Does this conflict with AstrBot's native @-mention mechanism?</summary>

No. This plugin handles "natural wakeup" (name/keyword/probability), while AstrBot's native @-trigger uses a separate channel. Both can coexist.

</details>

<details>
<summary>How do I limit the bot to specific groups?</summary>

Enable the whitelist under "Group Filtering" and add target group IDs to `enabled_groups`. Groups not on the whitelist won't trigger any wakeup.

</details>

<details>
<summary>Punctuation is being stripped from split messages — how to stop that?</summary>

The splitting module strips trailing neutral punctuation (periods, semicolons, colons, etc.) by default to make replies feel more natural in chat. To disable:
1. Turn off `strip_trailing_punct_enabled`
2. Or modify `strip_trailing_punct_chars` to remove characters you want to keep

</details>

## Prerequisites

- AstrBot >= v4.5.0
- A configured Telegram or QQ (aiocqhttp) platform adapter
- A configured LLM provider
- A configured persona (recommended)

## Documentation

| Document | Description |
|:---------|:------------|
| [Usage Guide](docs/usage_guide.md) | Full configuration, how it works, scenario behavior matrix (Chinese) |
| [Troubleshooting & Debug Guide](docs/troubleshooting_guide.md) | Fault diagnosis, AI-assisted debugging, parameter tuning, issue reporting (Chinese) |

## Acknowledgments

This project was conceived and led by [MagicalYuYu](https://github.com/MagicalYuYu), developed in collaboration with AI-assisted coding tools — all core design decisions and code reviews were handled by the author, with AI handling code generation and iterative implementation. Thanks to the open-source community for the tools and inspiration.

## License

[AGPL-3.0](LICENSE)
