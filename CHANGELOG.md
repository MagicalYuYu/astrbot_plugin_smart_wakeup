# 更新日志

## [2.1.0] - 2026-09-19

分段语音适配（TTS 逐段判定）——用户音色克隆测试需求驱动的新功能。

### 背景

框架 TTS 在 result_decorate 阶段只处理 `result.chain` 剩余内容；灵犀分段发送把前 N-1 段直接发走、只留末段 → 只有末段能触发语音（用户实测确认）。主动发言路径全程自管发送，框架 TTS 更是零触发。

### 新增

- **分段语音三模式**（`splitter.tts_mode`，默认 `last` 保持现状）：
  - `per_segment`：每个分段独立判定配音（可能多段）
  - `one_per_reply`：**每次回复整体判定一次，命中则从各段中选一段配音（有且只有一段）**——框架对末段的原生判定被接管（result 内容类型标记抑制），概率完全灵犀自管
- **概率自管**：新增 `tts_probability`（默认 0.3）——per_segment/one_per_reply 模式均使用灵犀自己的概率，不读官方 trigger_probability，为后续"配音段智能选择/心流调制"留出自管空间
- **覆盖主动发言路径**：主动发言的所有分段（含单段与末段）同样参与判定（此前主动发言永远无语音）
- **防刷屏护栏**：`tts_max_per_reply`（per_segment 单次回复配音段数上限，0=不限）、`tts_min_chars`（过短语气段不配音，默认 4）
- **尊重官方开关语义**：沿用官方 `provider_tts_settings.enable` / `dual_output`（语音+文字双发或语音替换文字）与会话级 TTS 开关；远程平台（Telegram）自动走官方 file_service 注册 HTTP URL
- 参数规模 147 → **151**（splitter 组 +4），分组数 14 不变

### 设计说明

这是"语音分布与质量"长期设计的第一步（可测试的最小增量）。one_per_reply 的选段策略 v1 为合格段随机；后续候选方向（未实现，视测试反馈决定）：LLM 自标记配音段（voice-aware writing）、心流状态调制语音概率、按情绪峰值评分选段。

注意：one_per_reply 模式下该回复不参与官方"回复内容安全检查"（content_safety.also_use_in_response，本功能默认关闭，开启者请知悉）。

## [2.0.4] - 2026-09-19

图文错配修复 + 市场文案优化补丁（用户实测反馈驱动）。

### 修复

- **首图与正文错配**：v2.0.3 的首图功能无条件取第 1 条资讯的图，但 LLM 一次拿到 3 条资讯任选/融合撰写——约 2/3 概率图文错位。修复：发送前用字符 bigram Jaccard 比对 LLM 最终输出与每条资讯的「标题+摘要」，取最匹配条目的图；最高相似度低于 0.12 时跳过图片（宁缺毋滥，错误配图比无图更伤观感），跳过原因入 DEBUG 日志

### 文案

- `metadata.yaml` desc 与 `@register` 描述重写：平台兼容性信息从句首移至句尾，去除【】术语括号，改为"让群机器人变成真正的群友——它会观察群聊、自己找话题开口，聊嗨了全程在场，被无视时知趣收声，到点睡觉"的平实表达（插件市场卡片/插件管理器展示面）

## [2.0.3] - 2026-09-18

全面审计整改 + 资讯体验增强版本。依据三路独立审计（参数语义活性 / 功能冲突矩阵 / 默认值一致性）与用户实测反馈。

### 修复（审计整改）

- **P1 单群覆盖三方对齐**：收集白名单补 `proactive_quiet_hours`（修复面板可配但被静默丢弃、海外群专属静默时段永不生效）；schema 模板补 `light_response_prob/cooldown/max_per_hour` 三字段（修复生效但面板无处可配）
- **资讯跨天重复推送**：`NewsPool._sent_per_group` 纯内存导致重载即清零（知乎热榜条目存活数天 → 重载后重推）。新增 `sent_news.json` 持久化（plugin_data 目录，TTL 14 天/每群 300 条，启动种子回池）；话题去重窗口 24h→72h、maxlen 10→20
- **默认值对齐**：`proactive_topic_categories` fallback 补齐 9 类（旧配置缺键静默丢 4 个资讯话题）；`bot_name` fallback 空串→'Bot'
- **5 个 proactive 参数补 schema min**（300/600/300/1/1，与代码 floor 对齐，面板校验免费生效）
- **路由阻断不再覆写 `extra_user_content_parts`**：只清空本插件注入，不再误删其他插件（如社区记忆插件 LivingMemory）注入的内容——为记忆生态共存铺路
- `cascade_upgrade_enabled` 显式标注"已弃用·无效参数"（审计确认零行为，仅为兼容保留）

### 新增

- **资讯推送附带首图**（`fetcher_send_first_image`，默认关）：RSS 四级提取（media:content → media:thumbnail → enclosure → 摘要内嵌 img）；带浏览器 Referer/UA 下载兜底防盗链，失败降级直链或纯文本

### 参数规模

143 → **147**（+3 单群轻量回应覆盖字段，+1 首图开关），全触点文案同步

## [2.0.2] - 2026-09-18

配置面板生效性修复版本。用户报告：Web 配置面板修改参数后不生效、仅有 AstrBot 原生面板生效，且两套面板存在"同步异常"体感。

### 根因（核查确认）

- 插件在 `__init__` 中将全部配置缓存为实例属性，仅写配置文件/内存不会刷新缓存，必须重载插件才真正生效；AstrBot 原生面板保存后由框架自动热重载（`config_service.py` save→reload），而灵犀面板依赖前端追加调 `/config/reload`，失败仅 console.warn，界面仍显示"配置已保存"——静默失败造成"保存了却不生效"
- **应用预设链路（post_preset）前后端均不触发重载**，预设应用后永不生效（v1.4.4 起一直存在）

### 修复

- `post_config` / `post_preset`（含恢复默认值分支）保存成功后由**后端统一触发热重载**（抽出 `_trigger_reload` 复用 `context._star_manager.reload()`），响应体返回 `reloaded` / `reload_error` 状态
- 前端 `saveConfig` 移除冗余的前端 reload 调用，按后端真实状态提示；`applyPreset` 同样展示重载结果——**重载失败从静默 console.warn 升级为用户可见的 warning 提示**
- `/config/reload` 端点保留用于手动触发

### 版本同步

`metadata.yaml` / `@register` / `CONFIG_VERSION` / README badge / 面板缓存号 / 文档版本标识同步至 2.0.2

## [2.0.1] - 2026-09-17

插件市场上架合规修复版本。应 AstrBot 插件市场 LLM Guard 自动安全检查意见整改，不含功能行为变更。

### 合规修复

- **日志记录器统一**：`modules/web_api.py` 移除 `import logging` + `logging.getLogger("astrbot")`，`modules/fetcher/` 5 个文件移除 `except ImportError` 标准 logging 回退分支；全插件日志统一且仅从 `astrbot.api` 导入 logger（副作用：fetcher 模块不再支持脱离 AstrBot 环境的独立测试脚本直接导入）
- **数据持久化位置迁移**：主动发言状态 `proactive_state.json` 与用户自定义预设 `presets.json` 从插件目录自身迁至 AstrBot 标准插件数据目录 `data/plugin_data/astrbot_plugin_smart_wakeup/`（经 `StarTools.get_data_dir()`，显式传插件名避免子模块栈检测失败）。旧位置文件均保留不删：`proactive_state.json` 在新位置无文件时从旧位置只读加载一次，下次保存即写入新位置，运行状态零丢失；`presets.json` 为纯用户手工维护的只读文件（插件从不写入），旧位置文件将始终作为只读回退生效，建议用户手动将其移至新位置

### 版本同步

- `metadata.yaml`、`main.py` `@register`、`modules/web_api.py` `CONFIG_VERSION`、README 双版本 badge、文档站 hero badge、配置面板静态资源缓存版本号同步至 2.0.1；`docs/usage_guide.md` / `docs/troubleshooting_guide.md` 持久化路径说明同步更新

## [2.0.0] - 2026-09-17

版本规范化与品牌重构版本。v1.4.4 实际合并了内部开发序列 v1.8.4 与 v1.9.0 ~ v1.9.8 共 10 个大版本的演进（主动发言引擎、Fetcher 资讯模块、五路退却、静默作息、Web 配置面板等），以补丁号发布不符合语义化版本规范。本版本不含功能代码变更，将对外版本号正名为 2.0.0，并完成全部对外文本的定位重构：从"被动唤醒插件"升级为"会主动、知进退、有作息的群友型 Bot 节律引擎"。

### 版本规范化

- 对外版本号 1.4.4 → 2.0.0，同步点：`metadata.yaml`、`main.py` `@register`、README 双版本 badge、文档站
- 勘误声明：v1.4.4 条目中 AIF 全称误写为 "Anticipation-Initiation-Forecasting"，正确全称为 "Anticipation-Initiation-Planning"（以 `main.py` 实现为准）；历史条目保留原文，不做回溯修改

### 文档与品牌重构

- `metadata.yaml`：`short_desc` / `desc` 重写（插件市场卡片第一触点），新定位 + 品类词"群友型 Bot"
- `main.py`：`@register` 描述同步新定位（AstrBot WebUI 插件管理器展示面）
- `README.md` / `README_EN.md` 全量重写：三支柱叙事（会主动 / 知进退 / 可调校）、19 条卖点、功能一览勘误与扩充（去重窗口 30s→60s、移除已弃用的级联升级表述、图片双模式"互斥"更正为主备降级）、调试指令 4→11 条、FAQ 持久化条目修正并新增主动发言相关条目、mermaid 工作原理图补主动发言回路
- `docs/usage_guide.md` / `docs/troubleshooting_guide.md`：版本标识、特性清单与调试指令同步至 v2.0.0 实际能力
- `docs/index.html` 文档站整站文案重构（hero / 特性 / 统计数字等由 v1.0 口径更新至 v2.0.0）
- `docs/manifest.json` 描述更新

## [1.4.4] - 2026-09-16

本次发布合并 v1.4.3（2026-07-15）之后的全部演进（内部开发序列 v1.8.4 与 v1.9.0 ~ v1.9.8 均未对外发布过），跨越主动发言模块首发、退让信号系统、Fetcher 外部资讯抓取模块、可配置化重构、P0 根因修复、静默时段与递进退让、Web 自定义配置面板、对抗评估修复、[SKIP] 混合体泄露修复等关键节点。下文按功能分类分组，不再按内部版本切分。

### 新增

- **Web 自定义配置面板（v1.9.0 ~ v1.9.3）**：基于 AstrBot 插件自定义 Web 页面机制新增可视化配置面板，替代原 `_conf_schema.json` 生成的冗长表单。后端新增 `modules/web_api.py`（配置读写、schema 下发、运行时状态查看，路由前缀 `/api/plug/astrbot_plugin_smart_wakeup/`），前端新增 `pages/config/`（index.html / app.js / styles.css）。历经三轮迭代：白名单语义修正与配置同步、长文本全宽（v1.9.1）、全宽项输入框弹性布局（v1.9.2）、粘性标题穿入修复（v1.9.3）
- **退却时长可配置（v1.9.7）**：主动发言退却时长硬编码改为可配置，新增 5 个配置项（main.py + `_conf_schema.json`）

- **主动发言模块（AIF 三要素模型）**：基于 Anticipation-Initiation-Forecasting 三要素模型设计的 Bot 主动发起话题能力，使 Bot 从纯被动回复转变为可定时主动发声。Bot 定时主动发起话题，与被动回复共享同一套记忆系统（`_msg_buffer` / `_conversation_history` / `_conversation_summaries`），避免主动发言与被动回复记忆割裂。仅支持 aiocqhttp / Telegram / 飞书平台。AIF 状态机初始版本仅 `PASSIVE_MONITORING` / `AGENT_DOMINANT` 两态。新增配置组 `proactive_speak`，覆盖启用开关、检查间隔、模型 ID、冷却、每日上限、触发概率、精力下限、时间窗口、最少上下文消息数、冷场判定阈值、自说自话占比阈值、话题方向类别、自定义话题、目标群列表等 14 个参数
- **退让信号系统**：在主动发言模块中引入三路退让信号，避免 Bot 在群内活跃激增、用户表达厌烦时仍强行发声：
  - 信号①：活跃激增检测，5 分钟内消息数超过阈值 `active_surge_threshold=20` 视为活跃激增，触发退让
  - 信号②：连续 `[SKIP]` 计数触发退让
  - 信号③：检测到厌烦关键词触发退让
- **Fetcher 外部资讯抓取模块**：为主动发言提供外部实时资讯，使 Bot 从"信息索求者"转变为"分享者与锐评人"。部署到服务器时凭据在插件内部管理，不依赖外部文件。新增 `modules/fetcher/` 目录（`base.py` / `cache.py` / `filter.py` / `rss_fetcher.py` / `api_fetcher.py` / `__init__.py`）。新增 `requirements.txt`：`feedparser>=6.0.0`、`aiohttp>=3.8.0`。默认 RSS 源覆盖科技资讯（The Verge / TechCrunch / Ars Technica / Hacker News）、游戏八卦（PC Gamer / Rock Paper Shotgun / Eurogamer / IGN）、沙雕新闻（The Onion / Weekly World News / Reddit r/nottheonion）、热点事件（NYT World / BBC World）四类。允许通过 `fetcher_rss_enabled` 关闭 RSS 数据源（适用于只想用 API 数据源的场景）
- **显式代理配置项**：新增 `fetcher_proxy` 配置项，解决 NSSM 服务 Session 0 无法读取用户代理的问题。优先级：`fetcher_proxy` 配置项 > `_get_windows_proxy()` 探测。实测：AstrBot 作为 NSSM 服务运行在 SYSTEM 账户时，`winreg.HKEY_CURRENT_USER` 指向 SYSTEM 而非当前用户，导致 `winreg` / `urllib.getproxies` 均返回空
- **静默时段配置**：新增 `proactive_quiet_hours` 单字段配置，支持多段（如 `"23:00-07:00,13:00-14:00"`，支持跨天），与 `proactive_time_window` 是交集关系：两者都允许时才发言。空字符串则不启用静默时段
- **开头去重机制与开头模式池**：新增 `_proactive_recent_openers`（每群 maxlen=5 的 deque）记录最近 5 次发言开头，prompt 显式排除已用过的开头。新增 6 种开头模式池（直接事实型 / 引用型 / 反问型 / 场景化型 / 对比型 / 数字震撼型），每次发言前随机选 1 种注入 prompt，避免 LLM 反复套用相同模板
- **递进退让机制**：`consecutive_no_response_count` 持久化到 `proactive_state.json`；冷却计算直接读 `consecutive_no_response_count` 查表（2 次→2h、3 次→6h、4 次→24h）；引入软退让分层（连续 2 次概率 ×0.3 保留探测，连续 3 次硬退让）；封顶 24h，与 `_proactive_skip_streak` 协同设计避免叠加
- **有效用户回复检测**：`on_group_message` 中新增有效用户回复检测逻辑，当群内有用户实质发言时（非纯表情、非纯 @、字符数 ≥3）重置该群的连续无回应计数，使递进退让机制只对真正的"无人回应"场景生效（深夜/凌晨），避免用户白天讨论后夜间发言被误判为"无人回应"

### 重构

- **可配置化与 fallback 统一**：统一代码 fallback 与 schema default，避免新安装时行为偏差。自说自话检测参数可配置化（`proactive_self_talk_ratio`、`proactive_self_talk_hours`），替代原硬编码 `0.5` 阈值
- **持久化时间戳改为实例属性**：将持久化延迟保存的时间戳从类属性改为实例属性，避免类属性误导多实例场景
- **NewsPool 跨群共享池子重构**：引入 `NewsPool` 替代原 `sent_titles` 全局集合 + `NewsCache` 缓存组合。`fetch` 增加 `group_id` 参数支持 per-group 去重；`mark_sent` 增加 `group_id` 参数且不再清除缓存（池子要跨群复用）。fetch 时先检查池子，池子命中则 per-group 过滤返回未发送过的；池子未命中或过期时从 RSSFetcher 拉取 `pool_size` 条填充池子。池子 TTL 默认 6 小时（21600 秒），默认池子大小 15 条。区分"池子未命中"与"池子命中但该群已用尽"两种场景，后者不覆盖池子，保护其他群未发送的资讯。注释与代码行为一致：非空结果写入池子，空结果不写入
- **RSS 抓取机制改用 aiohttp 异步拉取**：弃用 `feedparser.parse(url)` 的内置 HTTP 请求（在 AstrBot 进程内通过 `run_in_executor` 调用会卡死 15s 超时，所有源全部失败，但独立进程中同样代码 1-2s 成功，根因是 AstrBot 进程内的环境因素干扰了 feedparser 内部的 urllib HTTP 请求），改用 `aiohttp` 异步拉取 RSS 内容（字符串）再传给 `feedparser.parse(content)` 解析。完全控制超时（total=15、connect=8、sock_read=10）、UA、不依赖线程池，避免 AstrBot 事件循环干扰。aiohttp 不可用时回退到旧的 `feedparser.parse(url)` 方式
- **资讯类话题 prompt 模板化**：第一段定位改为"原文关键片段引用"而非"摘要转述"，对齐用户原话"以第一条原新闻消息的形式发出来"。保留"必须提及来源"硬约束，但删除具体示例（"刚刷到"/"据 BBC 报道"等）。引入 Few-Shot Examples（2 个正面示例 + 1 个反面示例）替代纯指令。硬约束 1/2 降级为软约束（"自然融入"而非"必须包含"）
- **状态持久化字段扩展**：`_save_proactive_state` 新增 `response_tracker` / `last_speak` / `daily_count` / `retreat` / `skip_streak` 字段；`_load_proactive_state` 对应恢复逻辑，过滤 `speak_time` 超过 24h 的过期条目；重载后立即调用 `_check_no_response_all_groups()` 处理过期 tracker

### 修复

- **修复 P0 根因：LLM 编造新闻**：通过 NewsPool per-group 去重 + `_filter_command_lines_from_context` 过滤 context 中的命令文本行（用户发送 `/wakeup_proactive 科技资讯` 后命令文本被 `_record_message` 写入 `_msg_buffer`，因 `_record_message` 在指令前缀检查之前调用）+ 强硬化资讯类硬约束防止 LLM 扭曲真实资讯事实，三层修复 LLM 编造新闻问题
- **修复 P1-2 双重触发**：`_trigger_wake` 双重触发时保留首次标志，避免覆盖时间戳和取消已运行的定时器。概率唤醒 + 名称触发在 10 秒内对同一群两次调用 `_trigger_wake` 时，第二次不取消第一个定时器
- **修复退让死循环**：v1.7.0 修复退让状态机死循环问题，避免 Bot 陷入退让→恢复→立即触发→再次退让的循环
- **修复 RSS 抓取在 AstrBot 进程内卡死**：见"重构"段"RSS 抓取机制改用 aiohttp 异步拉取"。aiohttp 默认不读 Windows 代理，直连被劫持的 Facebook IP 必然失败，需要 `_get_windows_proxy()` 函数获取代理供 aiohttp 使用
- **修复代理探测失效**：服务器配置了 Clash 代理（`127.0.0.1:7897`），DNS 被劫持到 Facebook IP，`urllib` / `feedparser` 自动读 Windows 代理能正常工作，但 `aiohttp` 默认不读 Windows 代理。在 AstrBot 进程内直接读 `winreg` 返回 `None`（原因未明，可能权限/用户模拟），新增 `urllib.request.getproxies()` 作为优先获取方式（`urllib` 在 AstrBot 进程内能正常获取代理）
- **修复递进退让误判**：`consecutive_no_response_count` 此前未持久化，重载后丢失导致递进退让失效。改为持久化到 `proactive_state.json`，冷却计算直接读计数查表（2 次→2h、3 次→6h、4 次→24h）
- **修复静默时段跨天 bug**：现有 `proactive_time_window` 的 L6111 字符串字典序跨天失效（如 `"23:00" < "07:00"` 在字符串字典序下不成立）；同时 `proactive_time_window_start/end` 默认配置从 `"00:00"` / `"23:59"`（24h 全开，是凌晨发言的根因之一）改为 `"09:00"` / `"23:00"`
- **修复冷场救场绕过深夜静默**：在 `_check_dead_chat_rescue` 中新增静默时段检查，避免冷场救场绕过深夜静默规则；新增渐进式恢复（静默结束后 1h 内概率从 0.3 线性恢复到 1.0）
- **修复 @ 前缀过滤误判（M3）**：`not _stripped.startswith("@")` 会把 `@bot 你好啊` 误判为无效回复，导致 `consecutive_no_response_count` 错误累加。改为 `re.sub(r'^@\S+\s*', '', _stripped).strip()` 剥离 @ 前缀再判定
- **修复重载后 buffer 丢失导致 false positive（M4）**：tracker 持久化但 `_msg_buffer` 未持久化，重载后 buffer 为空导致 `_check_no_response_all_groups` 误判为"无用户回复"。修复：`_load_proactive_state` 重载时若 `checked=False` 且 buffer 为空，将 `checked` 标记为 `True` 跳过本次检查
- **修复 metadata 版本号与代码不一致**：metadata.yaml 与 `@register` 版本号同步至本次发布版本（此前 metadata 与代码版本不一致）
- **修复 [SKIP] 混合体思考泄露（v1.9.8）**：GLM-5.2 等推理模型偶发把 "[SKIP] 标签 + 决策理由 + 偶发回复" 全部写进 content 正文（reasoning_content 反而正常分离），原三道防线（think 标签过滤 / 精确匹配 / 前缀 + 无实质内容判定）全部漏过，导致 2026-09-16 22:32 思考内容泄露事故。修复：回复抑制规则 3 改为前缀命中即整体拦截（协议要求输出 [SKIP] 时不得输出任何其他内容，违反协议的混合输出不可信；概率唤醒场景沉默无害、泄露有害，安全优先）；主动发言路径与语义去重路径同步加固为前缀匹配
- **修复退却机制系列问题（v1.9.4 + v1.9.7）**：用户回复清除退却状态；consecutive_skip 冷却 1h；退却时长与 cooldown 协调（retreat = cooldown 查表值，避免两套时长互相矛盾）；`_proactive_speak` 调用前检查退却状态（防手动触发绕过退却）
- **修复对抗评估 Critical 3 项（v1.9.6）**：C1 退却清除逻辑区分类别（不同退却信号独立清除，避免用户正常发言误清其他信号的退却）；C2 `_check_annoyed_keywords` 添加白名单检查（白名单用户不触发厌烦退让）；C3 `POST /config` 配置深度合并（避免嵌套配置被整体覆盖导致丢项）
- **修复对抗评估 Major 5 项（v1.9.6）**：`_is_meaningful` emoji 检测完善；沙雕新闻类别备份源；默认国内源不使用代理（国内源直连，海外源走代理）；HTTP 429/403/404 重试策略优化；面板前端 isComplexType 关键词补全
- **修复 aiohttp.ClientSession 泄漏（v1.9.7）**：RSS 抓取复用会话（原每次请求新建 session，TCP 握手开销大），插件 terminate 时统一关闭
- **修复配置验证缺失（v1.9.7）**：Web 面板保存配置时新增 `_validate_config_against_schema` 校验，拒绝不符合 schema 的配置写入
- **修复面板前端两处缺陷（v1.9.7）**：formatLogTime Invalid Date（改为正则匹配 HH:MM:SS）；关闭页面前未保存提示（beforeunload + hasUnsavedChanges 检测）

以下为原计划 2026-07-15 发布的内部批次修复（P0/P1 级，同样从未对外发布，一并并入本次）：

- **修复 `tool.handler` 共享引用导致跨群并发污染（P0-1）**：`on_using_llm_tool` 中临时替换 `tool.handler` 为空操作函数，但 `tool` 对象来自框架 `tools_map` 的全局共享引用。0.5s 恢复窗口内若 B 群事件触发 `send_message_to_user`，会拿到 A 群替换后的 handler 导致 B 群消息被静默吞掉。移除防线2（handler 替换+恢复），仅保留防线1（tool_args 清空），后续由 `on_decorating_result` 的 4 策略检测 tool_call 并清空 result.chain 保证不重复输出
- **修复 `_filter_duplicate_response` 误切合法代码块（P0-2）**：原逻辑用 `re.split(r'\n```[ \t]*\n', text)` 切分多版本回复，但此模式同时匹配无语言标识的合法代码块。LLM 输出 `"解释\n```\ncode\n```\n结论"` 会被切为 `["解释", "code", "结论"]`，取最后一段导致前面的解释和代码全部丢失。新逻辑：仅当段数≥3（至少 2 个 ``` 分隔符，即草稿模式）且最后一段不像代码时才切分
- **修复同一回复被记录两次导致标签污染记忆（P0-3）**：`on_decorating_result` 中调用 `_record_assistant_message(response_text)` 记录过滤前的 LLM 原始输出（可能含思考标签、上下文标签），`after_message_sent` 再次调用记录过滤后的文本。同一条 BOT 回复被记录两次且内容不一致。删除 `on_decorating_result` 中的记录调用，仅在 `after_message_sent` 中记录（此时已过滤且确认发送）
- **修复 `_conversation_summaries` 无限增长导致 prompt 膨胀（P0-4）**：每次摘要都追加到 `existing` 后面，无长度上限、无滚动淘汰。长期运行后 `_conversation_summaries[group_id]` 无限增长，消耗大量 token。新增 `MAX_SUMMARIES = 5` 滚动淘汰机制，最多保留最近 5 条摘要
- **修复 14 个按群状态字典无清理机制（P1-5）**：`_cleanup_expired_buffers` 仅清理 `_msg_buffer`，其余 14 个按群字典永不清理，临时群/不活跃群状态永久驻留。在清理循环中同步清理所有 16 个按群状态字典（含防抖定时器取消、LLM 标志定时器取消）
- **修复空/伪 sender ID 污染概率判定（P1-6）**：sender 为 None 时 `str(None)` 产生 `"None"`，`_get_user_prob("None")` 返回默认 1.0，导致被设为概率 0 的用户可因 sender 解析失败被 `max()` 取 1.0 绕过。在 `_get_user_prob` 源头过滤空/伪 ID 返回 0.0，覆盖全部 7 个调用点
- **修复 LLM 标志超时阈值不一致（P1-7）**：主动定时器 60s，被动检查 120s，60-120s 窗口期内标志已清除但 LLM 仍在执行可能重复触发。定义 `_LLM_FLAG_TIMEOUT = 60` 常量，三处统一引用
- **修复分段器 CancelledError 丢失中间段（P1-8）**：取消后 `remaining = segments[sent_count:]` 只取 `last_seg = remaining[-1]`（最后一段），中间未发送段全部丢失。改为合并所有未发送段到 `result.chain`
- **修复摘要任务并发无去重（P1-9）**：`_maybe_summarize_history` 无并发锁，多条 assistant 消息触发多个摘要任务并发修改 `_conversation_history`（popleft）和 `_conversation_summaries`（追加）导致竞态。新增 `_summary_in_progress: set[str]` 标记集 + finally 清理
- **修复 `on_using_llm_tool` 去重检查恒 False 死代码（P1-11）**：hook 执行顺序导致 `_sent_content_cache` 为空时检查，整个去重分支是死代码。原逻辑若生效会清空 tool_args 导致 tool_loop 不发送、用户无输出，与 v1.3.2 设计冲突。移除死代码去重分支，保留 tool_args 不清空让 tool_loop 成为唯一发送通道
- **修复 tool_call 零宽空格污染记忆（P1-12）**：设置 `result.chain = [Plain("\u200b")]` 但未设置 `smart_wakeup_suppressed` 标记，零宽空格被记录到 `_msg_buffer` 和 `_conversation_history`。tool_call 分支中设置 suppressed 标记
- **修复重复输出命中后去重缓存状态不一致（P1-13）**：`chain.clear()` 后未更新 `_last_bot_reply_text`，下次语义去重比较的是更早的回复可能误判或漏判。命中时更新 `_last_bot_reply_text` + 设置 suppressed 标记
- **修复去重缓存记录未实际发送的内容（P1-14）**：`_record_sent_content` 在确认发送前就记录，后续 tool_call/抑制/重新生成导致实际发送内容不同。改为只在实际确认发送后（通过所有拦截分支后）才记录
- **移除未实现的级联升级功能（P1-10）**：`on_llm_response` 中设置 `smart_wakeup_cascade_upgrade` 标记但 `on_decorating_result` 从未检查，功能未实现且统计虚高。移除级联升级逻辑和统计展示行，保留配置项避免破坏用户配置结构

### 优化

- **LLM 诊断功能增强**：新增 LLM 请求开始时间记录（`on_llm_request` hook）、LLM 响应到达时间记录（`on_llm_response` hook），统一清理诊断数据防止跨请求残留
- **Fetcher 日志可见性修复**：fetcher 模块原使用 Python 标准 `logging`，但 AstrBot 的 `loguru` 日志系统不捕获标准 `logging` 的输出，导致 `FetcherManager` 内部所有诊断日志（fetch 开始 / 调用 fetcher / 过滤完成等）完全不显示在服务器日志中，无法排查 fetch 返回空的根因。改为优先使用 AstrBot 的 `loguru` logger，回退到标准 `logging`（测试脚本场景）
- **替换失效 RSS 源**：移除 Kotaku（403）、Polygon（证书过期）、IGN（404）、BBC Oddly Enough（404）、Guardian Oddly Enough（404）等失效源
- **替换沙雕新闻 RSS 源**：原 BBC UK News / Guardian World 太正经（严肃新闻），不包含荒诞内容。新源（2026-07-17 验证通过，通过 Clash 代理可访问）：The Onion（美国讽刺假新闻）、Weekly World News（荒诞新闻）、Reddit r/nottheonion（真实发生的荒诞事情）
- **APIFetcher 复用 aiohttp.ClientSession**：避免每次请求新建 session（连接池无法复用，TCP 握手开销大），lazy create 在 event loop 内首次调用 `_get_session` 时才真正创建。异常日志脱敏 API Key，避免通过 URL 泄露
- **RSS 单源抓取数与外层 max_items 解耦**：取 `max_items*2` 适度冗余，避免过度抓取
- **浏览器 User-Agent**：使用 `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36` 避免被站点限流
- **默认配置调整**：`proactive_time_window_start/end` 默认值从 `"00:00"` / `"23:59"` 改为 `"09:00"` / `"23:00"`，`proactive_quiet_hours` 默认 `"23:00-07:00"`，避免凌晨发言
- **默认 RSS 源替换为国内源（v1.9.5）**：14 个源、4 个类别，解决海外源依赖代理且抓取成功率低的问题
- **schema 下发 version 字段（v1.9.7）**：`get_schema` 注入 CONFIG_VERSION，供面板前端判断配置结构版本
- **LOGO 全线更新**：新 LOGO 部署至插件根目录（AstrBot v4.5.0+ 面板图标读取本地 logo.png）、文档站（docs/）、配置面板（pages/config/）、GitHub 仓库；README 展示宽度 120 → 280
- **清理过时注释和死代码（2026-07-15 批次）**：`_suppress_reply` 中"on_decorating_result 中已提前记录了 _record_assistant_message"注释已过时（P0-3 修复后不再成立），`history.pop()` 逻辑条件永不满足，一并清理

## [1.4.3] - 2026-07-09

### 修复

- **修复 `_llm_running_groups` 标志卡死导致群聊无法触发新唤醒（P0）**：当 LLM 请求被 `CancelledError` 中断管道时，`on_decorating_result` 和 `after_message_sent` 均不被调用，导致 LLM 执行中标志永久残留，该群长时间无法触发任何新的唤醒。新增 asyncio 主动超时定时器机制：
  - 新增 `_start_llm_flag_timer` / `_cancel_llm_flag_timer` / `_auto_clear_llm_flag` 三个方法
  - `_trigger_wake` 设置标志时同步启动 60 秒主动超时定时器（与 httpx timeout=60 对齐）
  - 正常路径（`on_decorating_result` / `after_message_sent`）主动取消定时器
  - 被动路径（概率唤醒 / 冷场救场）120 秒超时检查时同步取消定时器
  - `terminate` 完整清理所有定时器，防止插件卸载时悬挂任务
  - 三层防护防误清：sleep 可中断 + 二次存在性检查 + `elapsed >= timeout` 严格判断
- **修复 `UnboundLocalError: 'Plain'` 导致 LLM 结果替换失败（P0）**：`on_decorating_result` 中使用小模型回复替换主模型输出时，函数内部有局部 `from astrbot.core.agent.message import Plain` 导入，使 Python 将 `Plain` 视为整个函数的局部变量，在导入语句之前访问即触发 `UnboundLocalError`。移除局部 import，统一使用文件顶部 L17 全局导入的 `Plain`

## [1.4.2] - 2026-06-22

### 修复

- **修复思考标签泄漏（ToolCall 场景）**：LLM 在 `send_message_to_user` 的 tool_args 中混入 `<arg_key>` 思考标签时，原有过滤仅覆盖 `on_decorating_result` 路径，ToolCall 场景下标签直接泄漏到用户消息。新增 `_filter_tool_args_text` 方法，在 `on_using_llm_tool` 中对 tool_args 文本统一过滤思考标签、上下文标签和重复回复
- **修复重复输出根因**：对话记忆中 BOT 的回复被原样注入 LLM，LLM 看到自己说过的原文措辞后倾向于延续相同表达，导致重复输出（如连续两次说"<BOT_NAME>都看不下去了"）。新增 `_summarize_bot_reply_for_memory` 方法，对 BOT 回复做要点化处理，只保留话题要点而非原文措辞，从根源上切断 LLM 复制自身表达的倾向
- **修复上下文发言人识别错误**：小模型压缩群聊上下文时使用"自己"等代词，导致 GLM-4 将代词指代错误归因（如"吃到撑拿自己垫背"被理解为"<BOT_NAME>吃到撑拿BOT垫背"）。优化压缩 prompt，禁止使用代词指代他人行为，必须用具体昵称明确行为主体；转述他人对 BOT 的行为时必须标注 BOT 为承受方
- **增加调试标记引导**：在 `<conversation_guidance>` 中增加对【调试定位】等方括号标记的引导，LLM 识别到调试标记后会意识到此前对话可能存在发言者识别错误，在后续回复中仔细核对发言者身份
- **LLM 执行超时标志清除优化**：将超时时间从 300 秒（5分钟）缩短为 120 秒（2分钟），正常 LLM 调用应在 60 秒内完成

## [1.4.1] - 2026-06-20

### 修复

- **修复消息首词截断**：`_filter_duplicate_response` 使用 `" response"` 作为 GLM 多段草稿分隔符时，会误匹配英文常见词汇（如 `"few responses"`），导致 `split(" response")` 将 `"responses"` 拆成 `"response"` + `"s"`，最终输出以截断的 `"s, ..."` 开头。改用正则 `r" response(?!\w)"` 负向前瞻，仅当 `" response"` 后不紧跟单词字符时才判定为草稿分隔符
- **修复 Think 内容泄露**：GLM-4 使用 ```` ``` ```` 直接跟在文字后面（如 `这么有创意```[SKIP]```[SKIP]`）作为多段草稿分隔符，但原有正则 `\n```[ \t]*\n` 要求 ```` ``` ```` 前后有换行，无法匹配此新模式，导致 ```` ``` ```` 标记残留在用户端输出中。新增 ```` ``` ```` 内联模式检测（仅当 ```` ``` ```` 后紧跟 `[SKIP]` 或另一个 ```` ``` ```` 时才判定为草稿分隔符），并在 `_filter_thinking_tags` 末尾增加 ```` ``` ```` 残留兜底清理
- **修复 `_filter_thinking_tags` 的 `" response"` 同样误匹配**：与 `_filter_duplicate_response` 相同的根因，`_filter_thinking_tags` 中的 `re.sub(r'^[\s\S]*? response\s*', '', text)` 也会误匹配 `"few responses"` 等英文词汇，导致正常内容被误删。同步改用 `r" response(?!\w)"` 正则
- **修复概率唤醒语义重复回复**：两次独立的概率唤醒触发两次 LLM 调用时，LLM 基于相似上下文可能生成语义重复的回复（表述不同但实质内容相近）。采用三层机制从根源解决：
  - **源头预防**（主）：在 `on_llm_request` 中注入 `<anti_repeat_guidance>` 提示，告知 LLM 最近一次回复内容，引导其主动避免重复，从源头减少重复生成
  - **重新生成**（兜底）：输出阶段检测到与 BOT 最近一次回复的字符 bigram Jaccard 相似度≥0.55时，注入防重复提示重新调用 LLM 生成不同内容，而非直接堵住输出
  - **最终抑制**（保底）：重新生成仍重复或 LLM 主动输出 `[SKIP]` 时才抑制回复
  - 仅对非直接呼叫场景（概率唤醒、冷场救场）生效，直接呼叫（名称触发/关键词触发/回复BOT）不受影响

## [1.4.0] - 2026-06-19

### 新功能

- **轻量回应（安全话语库）**：当回复被回复抑制机制拦截时，不再强制完全沉默，而是以一定概率输出一句简短附和语（如"确实""有道理"）替代沉默，表达在场感。这是「群聊 BOT 真人化体验改进」第一批实施，直接解决"沉默频率过高降低存在感"的核心痛点
  - **双轨实现**：
    - **LLM 自行生成路径**（主）：在概率唤醒和冷场救场提示中增加 `<light_response_guidance>` 引导，LLM 可在判断无合适话题时自行生成低信息量、高通用性的自然附和
    - **预置话语库路径**（辅）：当 LLM 输出被回复抑制拦截时，以一定概率从预置话语库中随机抽取一条替代完全沉默
  - **三重频率控制**：触发概率（默认 0.3）+ 冷却时间（默认 300 秒）+ 每小时上限（默认 3 次），防止安全话语刷屏产生模板化重复观感
  - **对话记忆协调**：安全话语触发时撤销被抑制的 LLM 原始输出，由 `after_message_sent` 自动记录安全话语到缓冲区和对话记忆，确保多轮对话记忆连贯
  - **精力系统协调**：安全话语是 BOT 的真实发言，不触发精力恢复（与完全沉默的抑制路径不同）
  - **直接呼叫豁免**：名称触发、关键词触发、回复 BOT 等直接呼叫场景被抑制时不输出安全话语（直接呼叫被抑制说明内容严重不当）
  - **新增配置块 `light_response`**（5 个参数）：启用开关、触发概率、冷却时间、每小时上限、自定义话语库
  - **单群覆盖支持**：`light_response_prob`、`light_response_cooldown`、`light_response_max_per_hour` 纳入 `group_overrides` 可覆盖参数列表

## [1.3.7] - 2026-06-17

### Bug 修复

- **LLM 工具调用重复输出根治**：修复当 LLM 返回 `finish_reason='tool_calls'` 且同时包含 `content` 和 `send_message_to_user` 工具调用时，框架先通过 `on_decorating_result` 发送 content（经分段器），再通过 tool_loop 的 `send_message_to_user` 发送相同内容，导致用户看到重复消息的顽固问题。此前 v1.3.2 声称根治但实际未解决，本次采用全新三层拦截机制：
  - **防线1**：在 `on_using_llm_tool` 中检测 `send_message_to_user`，提取其消息文本与已发送内容缓存（`_sent_content_cache`）比对，若重复则清空 `tool_args` 替换为零宽空格
  - **防线2**：临时替换 `tool.handler` 为空操作函数（返回 None），阻止工具实际执行发送；替换后通过 `asyncio.create_task` 延迟 0.5s 恢复原始 handler，避免影响后续正常工具调用
  - **防线3**：现有 dedup 机制继续拦截第二轮 `on_decorating_result` 调用中的重复内容
  - 新增 `_extract_tool_message_text` 辅助方法，从 `send_message_to_user` 的 `tool_args` 中提取纯文本内容用于重复比对

## [1.3.6] - 2026-06-14

### Bug 修复

- **"昵称:内容"引用模式导致 LLM 发言人识别错乱**：修复当用户消息以"昵称:"开头（如 `某群友:服务器不插网线...`）时，上下文格式 `[用户A]: 某群友:xxx` 中的双重冒号结构使 LLM 误判被引用者为实际发言者，导致回复对象错误的问题。在 `_format_context` 和 `_format_conversation_memory` 中增加歧义消除：检测到消息内容以"昵称:"模式开头时，用引号包裹内容（`「某群友:xxx」`），使 LLM 明确区分发送者标注与被引用/转述的内容

## [1.3.5] - 2026-06-14

### Bug 修复

- **GLM 孤立 `</think>` 标签导致内容重复输出**：修复 GLM-4 模型在 `content` 字段中输出"实际回复`</think>`实际回复"模式时，`_filter_thinking_tags` 无法识别孤立 `</think>` 标签（无对应开标签 `<think>`），导致 `</think>` 标签和重复内容直接泄露给用户的问题。新增模式1.5：检测孤立 `</think>` 标签时，移除标签及其之前的草稿内容，仅保留标签后的最终版本；若标签后无内容则仅移除标签本身

## [1.3.4] - 2026-06-13

### Bug 修复

- **对话记忆发送者归属修复**：修复对话记忆（`_format_conversation_memory`）中所有用户消息统一标记为"用户"导致 LLM 错误归因消息发送人的严重问题。例如用户 A 发送某条消息，LLM 却将其归因于用户 B。根因是 `_record_user_message` 不记录发送者名称，`_format_conversation_memory` 和 `_maybe_summarize_history` 仅用通用"用户"标签，LLM 无法区分不同发言者
  - `_record_user_message` 新增 `sender_name` 参数，元组从 3 元素扩展为 `(role, text, timestamp, sender_name)`
  - `_record_assistant_message` 同步扩展为 4 元素元组
  - `_format_conversation_memory` 和 `_maybe_summarize_history` 改用具体发送者名称替代通用"用户"标签
  - 所有读取历史记录的位置使用 `record[3] if len(record) > 3 else ""` 模式，向后兼容旧的 3 元素元组

## [1.3.3] - 2026-06-13

### Bug 修复

- **GLM 多段草稿输出过滤**：修复 GLM-4-7-251222 模型在回复中将推理过程的多段草稿混入 `content` 字段导致异常输出的问题。异常输出包含用户消息回显、上下文元数据（"[发送时间:...]"）、多版本回复草稿和 ` response` 分隔符。根因是现有 `_filter_thinking_tags` 仅处理单个 ` response` 分隔符，无法应对多段草稿场景
  - `_filter_thinking_tags` 模式2改为两步检测：先非贪婪移除第一个 ` response` 段，若残留仍含 ` response` 则贪婪移除全部，仅保留最终版
  - 新增 LLM 元数据回显过滤：移除 `[发送时间:...]`、`[平台:...]` 等模式
  - `_filter_duplicate_response` 新增 ` response` 多段草稿模式处理，作为 `_filter_thinking_tags` 之前的防线

## [1.3.2] - 2026-06-13

### Bug 修复

- **tool_call 重复输出根治**：修复 LLM 使用 `send_message_to_user` 工具调用（`finish_reason='tool_calls'`）时消息被发送两次的严重问题。根因是 AstrBot 的 tool_loop 机制会独立发送完整消息，而 `on_decorating_result` 中的分段模块也会通过正常流程发送，两个独立发送通道导致重复。采用多策略检测 tool_call 并在检测到时清空 `result.chain`（用零宽空格替代，防止 intelligent_retry 插件重试），让 tool_loop 成为唯一发送通道
  - 策略1：`on_llm_response` 中检测 `finish_reason='tool_calls'` 并设置 event flag
  - 策略2：`on_decorating_result` 中检查 `result` 对象的 `tool_calls`/`finish_reason` 属性
  - 策略3：检查 `event` 对象的 LLM 响应相关属性
  - 策略4：深度遍历 `event` 所有属性查找 `finish_reason`/`tool_calls` 信息
  - 新增 `on_using_llm_tool` 钩子检测 `send_message_to_user` 工具调用并设置 event flag
  - `_splitter_process` 增加 tool_call 保护，检测到时跳过分段
  - 首次运行时添加属性探测日志，确认 AstrBot 框架中 tool_call 信息的实际存储位置

### 优化

- **去重窗口扩大**：将去重时间窗口从 30 秒扩大到 60 秒，覆盖 tool_loop 执行延迟导致的重复

## [1.3.1] - 2026-06-10

### Bug 修复

- **回复抑制失效导致 [SKIP] 原样发出**：修复 `_suppress_reply` 使用 `result.chain.clear()` 清空输出后，`intelligent_retry` 插件将空回复判定为 LLM 失败并触发重试，重试结果绕过 `on_decorating_result` 直接发送给用户的问题。改用零宽空格替换输出内容，使 retry 插件判定为"非空回复"跳过重试，同时设置 `smart_wakeup_suppressed` 标记让 `after_message_sent` 跳过记录
- **复读检测日志重复展示**：修复 `_evaluate_debounced_messages` 中同一条消息的复读检测被名称匹配、关键词匹配、复读抑制三个循环分别调用，产生多条相同日志的问题。改为预扫描缓存复读检测结果，后续循环复用缓存
- **概率唤醒并发触发防护**：在 `_check_probability_wakeup` 开头增加 `_llm_running_groups` 检查，LLM 执行中时跳过概率唤醒判定，防止并发触发重复输出；超时 5 分钟的标志自动清除并警告

## [1.3.0] - 2026-06-10

### 新功能

- **回复抑制机制**：当 BOT 判断不应该回复时，实现真正的沉默。提供两种可选方案：
  - **方案A（关键词匹配拦截）**：主 LLM 输出抑制关键词（默认 `[SKIP]`）时拦截，零成本零延迟。需配合人格提示词中的回复抑制规则使用
  - **方案B（独立小 LLM 合规判别）**：主 LLM 正常回复后，由独立小模型判别是否适宜发出，仅概率唤醒/冷场救场场景触发，名称匹配/回复BOT跳过
  - **双模式（both）**：两种方案串联运行，方案A为第一道防线，方案B为第二道防线
  - 拦截后自动撤销对话记忆、恢复精力值，确保副作用完整处理
  - 新增配置分组 `reply_suppression`：enabled / mode / keyword / judge_model / judge_prompt，默认关闭

## [1.2.5] - 2026-06-09

### Bug 修复

- **LLM 执行中标志泄漏**：修复 `_llm_running_groups` 仅在 `after_message_sent` 中清除，当 LLM 请求失败或结果为空时标志永远不清除，导致冷场救场永久阻塞的问题。改为 `dict[str, float]` 存储时间戳，在 `on_decorating_result` 中双重清除，并增加 5 分钟超时自动清除安全机制
- **QQ 平台图片描述属性探测**：扩大 Image 组件描述属性探测范围（`desc`/`description`/`summary`/`text`/`content`/`caption`），排除 QQ 自带的低价值摘要（如 `[动画表情]`），增加调试日志打印 Image 组件全部属性便于排查
- **`_is_low_info_message` 中 `isinstance(comp, Plain)` 潜在风险**：改为 `comp_type == "Plain"` 字符串比较，与 Image 检测方式一致，避免局部 import 导致的 `UnboundLocalError`

## [1.2.4] - 2026-06-09

### Bug 修复

- **Plain 组件类型检查 UnboundLocalError**：修复 `_record_message` 中使用 `isinstance(comp, Plain)` 导致 `UnboundLocalError`，原因是方法内部其他位置有局部 `from ... import Plain`，Python 将 `Plain` 视为局部变量。改为与 Image 一致的 `comp_type == "Plain"` 字符串比较模式，避免依赖导入

## [1.2.3] - 2026-06-09

### Bug 修复

- **BOT 自身消息过滤**：修复 QQ 平台（NapCat）将 BOT 自身发送的消息作为群消息分发，导致 BOT 消息进入唤醒判定流程、反复触发回复的问题。在 `on_group_message` 入口增加 `sender_id in _bot_user_ids` 检查，BOT 消息直接跳过判定
- **QQ 平台图片描述丢失**：修复 QQ 平台图片描述存储在 Image 组件的 `desc`/`description` 属性中而非 `message_str`，导致 `_record_message` 无法提取图片描述的问题。增加从 Image 组件属性获取描述的 fallback 机制，优先从 `message_str` 提取 `[Image: 描述]` 格式，其次从组件属性获取
- **Sticker 占位符永远 pending**：修复 Telegram 的 Sticker 消息（Image + Plain("Sticker: 🫤")）图片被记录为 `image_pending=True`，占位符永远不会被填充的问题。检测 Sticker 类型后标记 `image_pending=False`，无需视觉模型识别

## [1.2.2] - 2026-06-08

### Bug 修复

- **冷场救场重复输出**：修复极端配置下冷场救场并发触发导致 BOT 连续输出相同内容的问题。根因是 `rescue_idle_threshold` 和 `rescue_cooldown` 被设为极低值（如0），导致用户连续发言时仍触发冷场救场，且短时间内多次触发无冷却保护
- **配置值安全下限**：`_get_group_param` 中为 `rescue_idle_threshold`（最低60秒）和 `rescue_cooldown`（最低60秒）增加安全下限，即使配置为0也会自动修正，防止极端配置导致冷场救场误触发
- **LLM 执行中保护**：在 `_trigger_wake` 中统一设置 LLM 执行中标志，`_check_dead_chat_rescue` 入口检查该标志，LLM 执行期间跳过冷场救场，`after_message_sent` 中清除标志，防止并发触发重复输出

## [1.2.1] - 2026-06-08

### 优化

- **压缩提示词增强**：上下文压缩时明确要求保留 BOT 的回复内容要点，特别是 BOT 已回应过的话题和观点，避免压缩后 LLM 不知道自己已经说过什么而重复回应
- **BOT 消息免过滤**：`_format_context` 中 BOT 消息不再受"过短消息过滤"影响，确保 BOT 回复始终出现在上下文中

## [1.2.0] - 2026-06-08

### 重构

- **图片上下文机制重构**：核心改变——在 `_record_message` 中直接从 `message_str` 提取框架生成的 `[Image: 描述]` 格式图片描述，图片到达时描述即完整，不再存在"占位符待填充"的中间状态
- **移除系统图片识别模式**：不再需要从 `req.contexts` 提取图片描述并匹配占位符，该逻辑已由 `_record_message` 直接提取替代
- **移除图片描述等待时间配置**：`image_context_wait_max` 配置项已移除，防抖不再因图片而延长（图片描述在记录时已完整）
- **移除 Fallback 机制**：不再需要占位符匹配失败时的降级注入
- **自定义模型降级化**：自定义模型图片识别改为降级方案，仅在框架未提供图片描述时（`image_pending=True`）触发，默认关闭

### 配置变更

- 移除 `image_context_system`（系统图片识别）配置项
- 移除 `image_context_wait_max`（图片描述等待时间）配置项
- `image_context_custom_model` 改为降级方案，描述和提示更新

### 已知限制

- 框架延迟分发：图片消息仍需等待描述生成完毕才分发给插件（约 20 秒），期间插件不知道有图片存在。这是 AstrBot 框架级行为，插件无法绕过

## [1.1.1] - 2026-06-08

### Bug 修复

- **图片唯一编码机制**：为每张图片分配唯一 `image_id`（格式 `img_{8位hex}`），解决多图场景下图片描述与占位符错配的问题。图片描述通过 `image_id` 精确关联到对应占位符，不再依赖位置或发送者猜测
- **系统模式 FIFO 匹配**：从缓冲区头部开始搜索最早的 `image_pending` 占位符，避免多图时描述错配到后发的图片
- **系统模式 Fallback**：当缓冲区中无 `image_pending` 占位符时（如框架延迟分发导致占位符未记录），图片描述仍能作为独立条目注入上下文，不会丢失

### 优化

- **移除自动补充回复机制**：v1.1.0 中尝试在图片延迟到达时自动触发补充回复，但该机制侵入性强、场景误判风险高，已移除。改为确保用户自然追问时图片上下文一定可用（Fallback 保障）
- **带文字的图片消息**：同时包含文字和图片的消息现在也会记录 `image_id`，支持自定义模型识别
- **自定义模型精确匹配**：`_describe_image_custom` 通过 `image_id` 精确匹配占位符，替代原先的发送者+文本匹配

## [1.1.0] - 2026-06-08

### 新增

- **图片上下文关联**（默认关闭）：让 Bot 理解图片内容，避免图片+文本消息的上下文断裂
  - 系统图片识别：从框架上下文提取图片描述，零额外 API 成本
  - 自定义模型图片识别：使用自选多模态模型识别图片，描述更贴合群聊场景
  - 两种模式互斥，只能开启一个或全部关闭
  - 可配置等待时间，平衡响应速度与描述获取
- **在场用户感知**（始终启用）：上下文中标注近期活跃用户列表，约束 Bot 只对在场用户说话，避免幻觉提及不在场用户
- **上下文对话关系标注**（始终启用）：
  - BOT 发言标注为 `[BOT]`，帮助 LLM 识别自己的发言
  - 回复关系标注：`→ 回复[BOT]`，明确对话指向
  - 隐式回应标注：紧跟 BOT 消息 5 秒内的用户消息标注 `(回应BOT)`
  - 省略主语提示：引导 LLM 正确理解省略主语的句子
- **图片消息占位符**：纯图片消息在上下文中记录 `[图片]` 占位符，识别后更新为 `[图片: 描述]`

### 改进

- 消息缓冲区扩展为四元组 `(sender, text, timestamp, meta)`，支持存储回复关系、BOT 标识、图片描述等元信息
- 配置面板新增「图片上下文」分组，布局优化

## 1.0.6 (2026-06-08)

### Bug 修复

- **复读内容包含名称/关键词时不再触发唤醒**：群友复读包含 BOT 名称的消息（如"你当Bot傻吗"），之前会因名称匹配而触发强制唤醒。修复后，所有名称/关键词匹配路径均先检查该消息是否为复读，复读消息跳过名称/关键词匹配，但不影响常规的名称/关键词唤醒。覆盖路径：
  - 防抖判定中的名称匹配和关键词匹配
  - 立即判定中的名称匹配和关键词匹配
  - 防抖入口处的名称匹配判断（决定是否跳过防抖）
  - `_calc_debounce_wait` 中的名称匹配（决定防抖等待时间）

## 1.0.5 (2026-06-07)

### Bug 修复

- **Telegram "加一"复读检测修复**：Telegram 的"加一"功能通过 `forward_origin` 转发消息（无 Reply 组件），现有复读屏蔽规则未覆盖该场景，导致转发复读 BOT 消息时误触发概率唤醒。修复内容：
  - 重构 `_is_forward_from_bot`：多属性名探测原始消息对象、属性遍历兜底、拆分为三个子方法
  - 修复 BOT ID 比对：`self_id` 可能是用户名而非数字 ID，现同时比较数字 ID 和用户名；同时检查 `first_name` 和 `username`
  - 新增兜底策略 `_check_forward_repeat_by_buffer`：当无法访问 Telegram 原始消息对象时，通过"无 Reply + 文本与缓冲区 BOT 消息匹配"检测转发复读
  - 在 `on_group_message` 入口处添加转发复读过滤（步骤 2.6），转发复读直接跳过唤醒判定
  - 从 `_is_reply_to_bot` 中移除转发检测：转发复读本质是复读而非"回复BOT"，不应走回复触发路径
  - 增强 BOT ID 记录：`after_message_sent` 中同时从 `context` 获取 BOT 数字 ID
  - 增加调试日志：`_is_repeat_message` 和 `_is_forward_from_bot` 中添加详细调试输出

## 1.0.4 (2026-06-07)

### Bug 修复

- **防抖名称/关键词匹配修复**：防抖聚合多条消息后，名称和关键词匹配只检查最后一条消息，导致前面消息中的名称被忽略，走概率判定被淘汰。现改为遍历所有暂存消息检查
- **防抖回复BOT检测修复**：防抖聚合多条消息后，回复BOT检测同样只检查最后一条消息。现改为遍历所有暂存消息
- **防抖等待时间计算修复**：`_calc_debounce_wait` 只检查当前消息是否命中名称，第二条消息覆盖后等待时间从短变长（如3秒→21秒）。现改为遍历所有暂存消息
- **概率唤醒用户概率修复**：概率唤醒使用最后一条消息的发送者做用户概率检查，若该用户概率为0则直接跳过。现改为取所有发送者中的最大概率
- **防抖聚合 message_str 修复**：多条消息聚合时，`_trigger_wake` 修改的 `message_str` 只有最后一条消息内容，LLM 看不到完整上下文。现改为将聚合文本设为 `message_str`
- **复读检测失效修复**：分段器修改 `result.chain` 只保留最后一段，`after_message_sent` 只记录部分回复到缓冲区，导致复读检测匹配不到完整文本。现改为分段前保存完整回复文本
- **Sticker emoji 噪音过滤**：上下文注入中 `"Sticker: 🤣"` 类内容与贴纸实际内容无关，会误导 LLM 模仿 emoji。现替换为 `"[贴纸]"` 标记，从源头消除噪音

## 1.0.3 (2026-06-07)

### Bug 修复

- **指令前缀检查修复**：`event.message_str` 已被框架去掉 `/` 前缀（如 `/查询卡池` → `查询卡池`），导致以 `/` 开头的系统指令通过前缀检查、误触发概率唤醒。现改为从消息链 Plain 组件获取原始文本，正确识别 `/` 前缀
- **分段范围修复**：`on_decorating_result` 中的分段处理对所有经过管道的输出均生效，导致其他插件（如查询卡池）的输出也被拆成多段发送。现增加 `event.is_at_or_wake_command` 条件判断，仅对本插件主动触发的 LLM 回复做分段

## 1.0.2 (2026-06-07)

### Bug 修复

- **致命缩进错误修复**：v1.0.1 添加输出去重方法时缩进错误，导致分段模块代码被嵌套进 `_record_sent_content` 方法体内，引发 `NameError: name 'advanced' is not defined`。此错误使分段器、概率插值优化、分段高级参数全部未生效，现已修复
- **概率插值修复生效**：旁观状态的参与度插值从线性改为平方曲线（`engagement²`），此前因缩进错误未生效，现已正常工作
- **Telegram Sticker 处理**：Sticker（贴纸表情包）不再触发正常对话流程。新增消息链级别 Sticker 检测，将纯 Sticker 组合（Image + Plain("Sticker: xxx")）归入低信息量消息，与图片、emoji 同类处理
- **媒体标签过滤补充**：`media_message_patterns` 默认值新增 `Sticker:` 前缀匹配

## 1.0.1 (2026-06-06)

### Bug 修复

- **输出去重**：新增内容指纹去重机制，当 LLM 使用 `send_message_to_user` 工具或产生重复响应时，自动拦截重复输出，避免同一条消息被发送多次
- **概率插值优化**：旁观状态的参与度插值从线性改为平方曲线，避免高参与度时基础概率被过度拉高，使概率增长更加保守自然

## 1.0.0 (2026-06-06)

### 首发版本

**核心功能**

- 名称自然唤醒：消息包含 Bot 名称即触发回复，支持多别名（| 分隔）、大小写不敏感
- 概率唤醒：未提及名称时按概率主动回复，动态概率由精力、心流、参与度综合计算
- 精力系统：模拟社交疲劳，每次回复消耗精力，随时间自动恢复
- 心流状态机：旁观→关注→心流→疲劳，四状态动态调整回复策略
- 冷场救场：群聊冷场后主动参与对话，可配置冷场阈值和冷却期
- 消息防抖：等待用户停止发言后再回复，聚合多条消息为一次输入
- 复读抑制：检测复读链时大幅降低回复概率，避免打断群友复读氛围

**上下文与记忆**

- 独立消息缓冲区：记录群内所有消息供 LLM 理解完整对话氛围
- 增量上下文注入：仅注入上次回复后的新消息，避免重复
- 上下文摘要压缩：用小模型压缩群聊上下文，大幅减少 token 消耗
- 分层对话记忆：近期原文 + 远期摘要，支持多轮连贯对话
- 绕过核心上下文：唤醒消息使用插件自管理上下文，精确控制 token

**模型与路由**

- 智能模型路由：简单消息用小模型，复杂消息用大模型
- 级联升级：小模型回复质量不足时自动升级到大模型
- Token 消耗追踪：实时统计 token 用量，按模型/群组/时间/唤醒类型分类
- Token 异常检测：消耗超出正常范围时告警

**输出处理**

- 消息分段：长回复智能分段发送，模拟真人输入节奏
- 末尾标点剔除：分段后自动剔除句末语气中性标点，更符合自然聊天习惯
- 思考标签过滤：兜底过滤 LLM 回复中的思考内容，防止提示词泄露
- 重复回复过滤：检测并过滤 LLM 输出中的多版本回复

**平台兼容**

- 兼容 Telegram 和 QQ（aiocqhttp）
- 指令前缀跳过：以 / 开头的系统指令不触发唤醒，回复 Bot 消息时除外
- 群组过滤：白名单/黑名单灵活控制

**配置与调试**

- 丰富的配置面板：基础设置、精力、心流、冷场、防抖、分段、过滤、高级设置
- 单群参数覆盖：为特定群组自定义参数
- 用户概率覆盖：为特定用户自定义回复概率乘数
- 调试指令：状态查看、缓冲区可视化、精力/心流/Token 统计
- 调试模式：详细日志输出，便于排查问题
