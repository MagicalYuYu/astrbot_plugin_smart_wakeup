import asyncio
import hashlib
import json
import math
import os
import random
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.message_components import Plain, BaseMessageComponent, Reply, Record

# 回复抑制：内置默认合规判别提示词
_DEFAULT_JUDGE_PROMPT = """请判断BOT的回复是否适宜在群聊中发出。

判定为【不适宜】的情况：
1. 回复是对冷淡回应的复读或追着回复（如对方说"不知道""哦"，BOT复读同样的话）
2. 回复暴露了无法理解上下文（如反复问"在说啥""？"）
3. 回复对群友态度无礼或傲慢（如"懒得搜""懒得理"）
4. 回复是对与BOT无关的话题强行凑话，且凑话内容无信息量
5. 回复与上下文明显脱节，显得突兀

判定为【适宜】的情况：
1. 回复有实质信息量，与当前话题相关
2. 回复是自然的群聊参与（简短附和、吐槽、接话），不突兀
3. 回复体现了BOT的个性，但不过分

请输出JSON格式：
{"verdict": "pass" 或 "block", "reason": "简短原因"}"""


class FlowState(Enum):
    BYSTANDER = "旁观"
    ATTENTIVE = "关注"
    FLOW = "心流"
    FATIGUED = "疲劳"


@dataclass
class ChatEnergy:
    energy: float = 1.0
    last_reply_time: float = 0.0
    total_replies: int = 0


@dataclass
class ChatFlowState:
    state: FlowState = FlowState.BYSTANDER
    state_enter_time: float = 0.0
    message_count_in_window: int = 0
    window_start_time: float = 0.0
    relevance_score: float = 0.0
    engagement: float = 0.0          # 参与度 0.0~1.0，按时间衰减
    engagement_last_update: float = 0.0  # 参与度上次更新时间
    conversation_turns: int = 0       # 当前参与期间的对话轮数（用于疲劳计算）


@dataclass
class ChatRescueState:
    last_rescue_time: float = 0.0
    total_rescues: int = 0


@dataclass
class DebounceState:
    """防抖状态"""
    timer_task: object = None  # asyncio.Task | None
    pending_messages: list = None  # list[tuple]
    last_msg_time: float = 0.0
    last_msg_sender: str = ""
    silence_gap: float = 0.0  # 首条消息到达时的静默间隔（秒）


@register(
    "astrbot_plugin_lingxi",
    "AstrBot Plugin Developer",
    "灵犀——赋予 Bot 自然的社交节律，兼容 Telegram 和 QQ",
    "1.4.4",
)
class LingxiPlugin(Star):
    """灵犀插件

    被叫到名字就应，话题投缘就聊，群冷场了就来，聊久了也会累。
    精力系统、心流状态机、冷场救场、消息防抖协同运作，
    赋予 Bot 自然的社交呼吸。支持 Telegram 和 QQ (aiocqhttp) 平台。

    核心机制：
    - 维护独立消息缓冲区，记录群内所有消息（包括未 @ 机器人的）
    - 命中名称时设置 is_at_or_wake_command = True，让核心管道处理
    - 通过 on_llm_request 钩子注入群聊上下文，使 LLM 了解完整对话氛围
    - 提供调试指令：状态查看、缓冲区可视化、手动清理
    - 自动定期清理过期缓冲区数据
    - 精力系统：控制机器人回复频率，避免过度参与
    - 心流状态机：根据群聊活跃度动态调整概率唤醒概率
    - 冷场救场：群聊冷场时主动参与
    - 消息分段：将长回复智能分段发送，模拟真人输入节奏
    """

    # 缓冲区数据最大保留时长（秒），默认 24 小时
    BUFFER_MAX_AGE_SECONDS = 86400
    # 自动清理间隔（秒），默认 1 小时
    CLEANUP_INTERVAL_SECONDS = 3600
    # 心流活跃度滑动窗口（秒）
    FLOW_ACTIVITY_WINDOW = 300  # 5分钟滑动窗口
    # 心流状态最少停留时间（秒）
    MIN_STATE_DURATION = 15     # 最少停留15秒

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 机器人名称配置，支持 | 分隔的多个别名
        basic = self.config.get("basic", {})
        bot_name_str = basic.get("bot_name", "")
        self.bot_names = [
            name.strip() for name in bot_name_str.split("|") if name.strip()
        ]
        self.enable_private_chat = basic.get("enable_private_chat", False)

        # 消息缓冲区配置
        self.context_messages_count = min(
            basic.get("context_messages_count", 10), 200
        )

        # 上下文优化配置
        self.context_truncation_enabled = basic.get("context_truncation_enabled", True)
        self.context_truncation_max_len = basic.get("context_truncation_max_len", 60)
        self.context_truncation_keep_len = basic.get("context_truncation_keep_len", 30)
        self.context_min_length = basic.get("context_min_length", 3)  # 过滤过短消息
        self.incremental_context_enabled = basic.get("incremental_context_enabled", True)
        self.incremental_context_min_new = basic.get("incremental_context_min_new", 5)  # 新增消息少于此数时补充旧消息
        self.context_compression_enabled = basic.get("context_compression_enabled", True)
        self.compression_model = basic.get("compression_model", "")  # 留空则使用当前LLM提供者

        # 智能模型路由配置
        self.model_routing_enabled = basic.get("model_routing_enabled", False)
        self.routing_small_model = basic.get("routing_small_model", "")  # 留空则不路由
        self.cascade_upgrade_enabled = basic.get("cascade_upgrade_enabled", False)  # 级联升级默认关闭

        # 异常检测配置
        self.anomaly_detection_enabled = basic.get("anomaly_detection_enabled", True)
        self.anomaly_sigma_threshold = basic.get("anomaly_sigma_threshold", 2.0)  # σ阈值
        self.anomaly_prompt_ratio_threshold = basic.get("anomaly_prompt_ratio_threshold", 0.95)  # prompt占比阈值

        # 上下文管理策略
        self.bypass_core_context = basic.get("bypass_core_context", True)  # 绕过AstrBot核心上下文，使用插件自管理的上下文

        # 分层对话记忆配置
        self.conversation_memory_enabled = basic.get("conversation_memory_enabled", True)
        self.recent_rounds_keep = basic.get("recent_rounds_keep", 10)  # 保留最近N轮原文
        self.summary_rounds_max = basic.get("summary_rounds_max", 30)  # 摘要覆盖的最大轮数
        self.summary_model = basic.get("summary_model", "")  # 摘要模型，留空则使用compression_model

        # 群白名单/黑名单配置
        group_filter = self.config.get("group_filter", {})
        self.whitelist_enabled = group_filter.get("whitelist_enabled", False)
        self.enabled_groups = [
            str(g) for g in group_filter.get("enabled_groups", [])
        ]
        self.blocked_groups = [
            str(g) for g in group_filter.get("blocked_groups", [])
        ]

        # 概率唤醒配置
        self.probability_wakeup = basic.get("probability_wakeup", True)

        # 指令前缀跳过配置
        self.command_prefix_enabled = basic.get("command_prefix_enabled", True)
        self.command_prefix = basic.get("command_prefix", "/")

        # 低信息量消息过滤配置
        self.ignore_media_messages = basic.get("ignore_media_messages", True)
        media_patterns_str = basic.get("media_message_patterns", "[图片]|[动画表情]|[表情]|[视频]|[语音]|Sticker:")
        self.media_message_patterns = [p.strip() for p in media_patterns_str.split("|") if p.strip()] if media_patterns_str else []

        # 复读抑制配置
        self.repeat_suppress_enabled = basic.get("repeat_suppress_enabled", True)
        self.repeat_suppress_factor = basic.get("repeat_suppress_factor", 0.1)
        self.repeat_min_length = basic.get("repeat_min_length", 4)

        # LLM聊天唤醒前缀配置
        self.wake_command_prefix = basic.get("wake_command_prefix", "")

        # 调试模式
        self.debug_mode = basic.get("debug_mode", False)

        # 图片上下文关联配置
        image_context_config = self.config.get("image_context", {})
        self.image_context_custom_model = image_context_config.get("image_context_custom_model", False)
        self.image_context_custom_model_id = image_context_config.get("image_context_custom_model_id", "")

        # 图片上下文是否启用（自定义模型模式）
        self.image_context_enabled = self.image_context_custom_model

        # 精力系统
        energy_config = self.config.get("energy", {})
        self.energy_decay_rate = energy_config.get("energy_decay_rate", 0.15)
        self.energy_recovery_rate = energy_config.get("energy_recovery_rate", 0.02)

        # 心流状态机
        flow_config = self.config.get("flow", {})
        self.flow_bystander_prob = flow_config.get("flow_bystander_prob", 0.08)
        self.flow_attentive_prob = flow_config.get("flow_attentive_prob", 0.20)
        self.flow_flow_prob = flow_config.get("flow_flow_prob", 0.40)
        self.engagement_decay_per_minute = flow_config.get("engagement_decay_per_minute", 0.08)
        self.engagement_refresh_on_reply = flow_config.get("engagement_refresh_on_reply", 0.3)
        self.fatigue_coefficient = flow_config.get("fatigue_coefficient", 0.3)
        self.fatigue_max_multiplier = flow_config.get("fatigue_max_multiplier", 3.0)

        # 冷场救场
        rescue_config = self.config.get("rescue", {})
        self.rescue_enabled = rescue_config.get("rescue_enabled", True)
        self.rescue_idle_threshold = rescue_config.get("rescue_idle_threshold", 300)
        self.rescue_cooldown = rescue_config.get("rescue_cooldown", 1800)

        # 防抖配置
        debounce_config = self.config.get("debounce", {})
        self.debounce_enabled = debounce_config.get("debounce_enabled", True)
        # 强制防抖：统一所有触发类型的防抖行为（替代旧配置 debounce_skip_name_trigger）
        # 兼容旧配置：debounce_skip_name_trigger=true 等同于 force_debounce=false
        if "force_debounce" in debounce_config:
            self.force_debounce = debounce_config.get("force_debounce", True)
        elif "debounce_skip_name_trigger" in debounce_config:
            # 旧配置迁移：skip_name_trigger=true → force_debounce=false
            self.force_debounce = not debounce_config.get("debounce_skip_name_trigger", True)
        else:
            self.force_debounce = True
        self.debounce_wait_name = debounce_config.get("debounce_wait_name", 5)
        self.debounce_wait_prob = debounce_config.get("debounce_wait_prob", 10)
        self.debounce_wait_rescue = debounce_config.get("debounce_wait_rescue", 3)

        # 思考标签过滤配置
        filter_config = self.config.get("filter_settings", {})
        self.filter_thinking_tags = filter_config.get("filter_thinking_tags", True)

        # 回复抑制配置
        suppression_config = self.config.get("reply_suppression", {})
        self.reply_suppression_enabled = suppression_config.get("reply_suppression_enabled", False)
        self.reply_suppression_mode = suppression_config.get("reply_suppression_mode", "keyword")
        self.reply_suppression_keyword = suppression_config.get("reply_suppression_keyword", "[SKIP]")
        self.reply_suppression_judge_model = suppression_config.get("reply_suppression_judge_model", "")
        self.reply_suppression_judge_prompt = suppression_config.get("reply_suppression_judge_prompt", "")

        # 轻量回应配置
        light_config = self.config.get("light_response", {})
        self.light_response_enabled = light_config.get("light_response_enabled", True)
        self.light_response_prob = light_config.get("light_response_prob", 0.3)
        self.light_response_cooldown = light_config.get("light_response_cooldown", 300)
        self.light_response_max_per_hour = light_config.get("light_response_max_per_hour", 3)
        pool_str = light_config.get("light_response_pool", "确实|是这样的|嗯|有道理|哈哈|学到了|原来如此|有点意思")
        self._light_response_pool = [s.strip() for s in pool_str.split("|") if s.strip()] if pool_str else ["确实", "嗯", "哈哈"]
        # 状态变量（按群维度）
        self._light_response_last: dict[str, float] = {}       # {group_id: last_timestamp}
        self._light_response_count: dict[str, int] = {}        # {group_id: count_in_current_hour}
        self._light_response_hour_reset: dict[str, float] = {} # {group_id: hour_start_timestamp}

        # 关键词触发配置
        keywords_str = basic.get("keywords", "")
        self.keywords = [k.strip() for k in keywords_str.split("|") if k.strip()] if keywords_str else []
        self.keyword_reply_prob = basic.get("keyword_reply_prob", 0.5)

        # 单群参数覆盖配置（template_list 格式）
        self.group_overrides: dict[str, dict] = {}
        advanced = self.config.get("advanced", {})
        group_overrides_list = advanced.get("group_overrides", [])
        if isinstance(group_overrides_list, list):
            for item in group_overrides_list:
                if not isinstance(item, dict):
                    continue
                gid = str(item.get("group_id", "")).strip()
                if not gid:
                    continue
                # 只收集非空且非None的覆盖参数
                overrides = {}
                param_keys = [
                    "energy_decay_rate", "energy_recovery_rate",
                    "flow_bystander_prob", "flow_attentive_prob", "flow_flow_prob",
                    "engagement_decay_per_minute", "engagement_refresh_on_reply",
                    "fatigue_coefficient", "fatigue_max_multiplier",
                    "rescue_idle_threshold", "rescue_cooldown",
                    "debounce_wait_name", "debounce_wait_prob", "debounce_wait_rescue",
                    "keyword_reply_prob",
                    # 轻量回应参数（新增）
                    "light_response_prob", "light_response_cooldown", "light_response_max_per_hour",
                    # 主动发言单群覆盖参数（v1.5.0 新增）
                    "proactive_enabled", "proactive_cooldown", "proactive_daily_limit",
                    "proactive_probability", "proactive_min_energy",
                ]
                for key in param_keys:
                    val = item.get(key)
                    if val is not None:
                        overrides[key] = val
                if overrides:
                    self.group_overrides[gid] = overrides
            if self.group_overrides:
                logger.info(f"已加载 {len(self.group_overrides)} 个群组覆盖配置: {list(self.group_overrides.keys())}")

        # 用户概率覆盖配置（template_list 格式）
        self.user_prob_overrides: dict[str, float] = {}
        user_prob_list = advanced.get("user_prob_overrides", [])
        if isinstance(user_prob_list, list):
            for item in user_prob_list:
                if not isinstance(item, dict):
                    continue
                uid = str(item.get("user_id", "")).strip()
                prob = item.get("reply_prob")
                if uid and prob is not None and isinstance(prob, (int, float)):
                    self.user_prob_overrides[uid] = max(0.0, min(1.0, float(prob)))
            if self.user_prob_overrides:
                logger.info(f"已加载 {len(self.user_prob_overrides)} 个用户概率覆盖: {list(self.user_prob_overrides.keys())}")

        # 防抖状态
        self._debounce_states: dict[str, DebounceState] = {}

        # 已知的 BOT 用户 ID 集合（用于回复检测）
        # 当 BOT 发送消息时自动记录其 user_id，供 _is_reply_to_bot 比对
        self._bot_user_ids: set[str] = set()

        # 独立消息缓冲区：{group_id: deque of (sender, text, timestamp, meta)}
        # 记录群内所有消息，包括未 @ 机器人的，供 LLM 理解完整对话氛围
        self._msg_buffer: dict[str, deque] = {}

        # 增量上下文状态：记录每个群上次注入上下文时的最新消息时间戳
        self._last_context_ts: dict[str, int] = {}

        # 分层对话记忆状态
        # _conversation_history: {group_id: deque of (role, text, timestamp, sender_name)}
        # role: "user" or "assistant"
        self._conversation_history: dict[str, deque] = {}
        # _conversation_summaries: {group_id: summary_text}
        self._conversation_summaries: dict[str, str] = {}
        # _summary_checkpoint: {group_id: number of rounds already summarized}
        self._summary_checkpoint: dict[str, int] = {}
        # P1-9 修复：摘要任务并发去重标记，防止多条 assistant 消息触发多个摘要任务并发修改
        # _conversation_history（popleft）和 _conversation_summaries（追加）导致竞态
        self._summary_in_progress: set[str] = set()

        # 输出去重缓存：防止 LLM 工具调用或重复响应导致同一内容被多次发送
        # _sent_content_cache: {group_id: deque of (fingerprint, timestamp)}
        self._sent_content_cache: dict[str, deque] = {}
        self._DEDUP_WINDOW = 60  # 去重时间窗口（秒），扩大以覆盖 tool_loop 执行延迟

        # 语义去重：记录每群 BOT 最近一次回复的文本和时间
        # 用于检测短时间内两次概率唤醒生成语义相近的重复回复
        self._last_bot_reply_text: dict[str, str] = {}   # {group_id: last_reply_text}
        self._last_bot_reply_time: dict[str, float] = {}  # {group_id: last_reply_timestamp}
        self._SIMILARITY_WINDOW = 120  # 语义去重时间窗口（秒）

        # 三大系统状态
        self._energy_states: dict[str, ChatEnergy] = {}
        self._flow_states: dict[str, ChatFlowState] = {}
        self._rescue_states: dict[str, ChatRescueState] = {}

        # LLM 执行中标志：防止冷场救场并发触发重复输出
        # 存储 {group_id: 触发时间戳}，超时自动清除防止标志泄漏
        self._llm_running_groups: dict[str, float] = {}
        # LLM 标志的主动超时定时器，防止 CancelledError 中断管道后标志卡死
        self._llm_flag_timers: dict[str, "asyncio.Task"] = {}
        # 诊断（v1.7.3）：LLM 请求开始时间（on_llm_request hook 记录）
        self._llm_request_started_at: dict[str, float] = {}
        # 诊断（v1.7.3）：LLM 响应到达时间（on_llm_response hook 记录）
        self._llm_response_received_at: dict[str, float] = {}
        # P1-7 修复：统一 LLM 标志超时阈值，避免主动定时器(60s)与被动检查(120s)不一致
        # 导致 60-120s 窗口期内标志已清除但 LLM 仍在执行、新唤醒重复触发
        self._LLM_FLAG_TIMEOUT = 60  # 与 httpx timeout=60 对齐

        # 统计计数器
        self._stats = {
            "total_messages_recorded": 0,
            "total_wakeups": 0,
            "name_trigger_wakeups": 0,
            "probability_wakeups": 0,
            "rescue_wakeups": 0,
            "probability_checks": 0,
            "probability_passed": 0,
            "total_cleanups": 0,
            "last_cleanup_time": 0,
            "plugin_start_time": int(time.time()),
            "debounce_fired": 0,        # 防抖触发次数
            "debounce_cancelled": 0,    # 防抖取消次数
            "thinking_filtered": 0,     # 思考标签过滤次数
            # Token 消耗追踪
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_tokens": 0,
            "llm_call_count": 0,
            "token_by_model": {},  # {model_name: {"prompt": 0, "completion": 0, "total": 0, "count": 0}}
            "token_by_wakeup_type": {
                "name_trigger": {"prompt": 0, "completion": 0, "total": 0, "count": 0},
                "keyword_trigger": {"prompt": 0, "completion": 0, "total": 0, "count": 0},
                "probability_wakeup": {"prompt": 0, "completion": 0, "total": 0, "count": 0},
                "dead_chat_rescue": {"prompt": 0, "completion": 0, "total": 0, "count": 0},
            },
            "token_by_group": {},  # {group_id: {"prompt": 0, "completion": 0, "total": 0, "count": 0}}
            "hourly_tokens": {},   # {"2026-06-05T14": {"prompt": 0, "completion": 0, "total": 0, "count": 0}}
            "peak_prompt_tokens": 0,
            "peak_prompt_tokens_detail": "",  # "prompt=XXX 群=XXX 唤醒=XXX 时间=XXX"
            # 压缩统计
            "compression_stats": {
                "total_original_chars": 0,
                "total_compressed_chars": 0,
                "compression_count": 0,
            },
            # 路由统计
            "routing_stats": {
                "glm47_count": 0,
                "small_model_count": 0,
                "cascade_upgrade_count": 0,
            },
            # 分段统计
            "splitter_stats": {
                "total_splits": 0,
                "total_segments_sent": 0,
            },
            # 回复抑制统计
            "suppression_stats": {
                "keyword_suppressed": 0,
                "judge_suppressed": 0,
                "judge_passed": 0,
                "judge_errors": 0,
            },
        }

        # ─── 分段模块 ───
        splitter_config = self.config.get("splitter", {})
        self.splitter_enabled = splitter_config.get("enabled", False)

        # 始终初始化分段属性（即使未启用，避免 AttributeError）
        self.split_regex = splitter_config.get("split_regex", r"[。？！?!.\n…]+")
        self.enable_smart_split = splitter_config.get("enable_smart_split", True)
        self.balanced_split_mode = splitter_config.get("balanced_split_mode", False)
        self.trim_segment_edge_blank_lines = splitter_config.get("trim_segment_edge_blank_lines", True)

        # 末尾标点剔除
        self.strip_trailing_punct_enabled = splitter_config.get("strip_trailing_punct_enabled", True)
        self.strip_trailing_punct_chars = splitter_config.get("strip_trailing_punct_chars", "。；;：:、")

        # 分段高级参数：优先从 advanced.splitter_advanced 读取，回退到 splitter（兼容旧配置）
        advanced = self.config.get("advanced", {})
        splitter_adv = advanced.get("splitter_advanced", {})
        self.max_segments = splitter_adv.get("max_segments", splitter_config.get("max_segments", 7))
        self.min_segment_length = splitter_adv.get("min_segment_length", splitter_config.get("min_segment_length", 10))
        self.delay_strategy = splitter_adv.get("delay_strategy", splitter_config.get("delay_strategy", "linear"))
        self.linear_base = splitter_adv.get("linear_base", splitter_config.get("linear_base", 0.5))
        self.linear_factor = splitter_adv.get("linear_factor", splitter_config.get("linear_factor", 0.1))
        self.log_base = splitter_adv.get("log_base", splitter_config.get("log_base", 0.5))
        self.log_factor = splitter_adv.get("log_factor", splitter_config.get("log_factor", 0.8))
        self.random_min = splitter_adv.get("random_min", splitter_config.get("random_min", 1.0))
        self.random_max = splitter_adv.get("random_max", splitter_config.get("random_max", 3.0))
        self.fixed_delay = splitter_adv.get("fixed_delay", splitter_config.get("fixed_delay", 1.5))

        # 成对字符映射（智能分段时避免在内部切断）
        self._pair_map = {
            '"': '"', "《": "》", "（": "）", "(": ")",
            "[": "]", "{": "}", "'": "'", "【": "】",
        }
        self._quote_chars = {'"', "'", "`"}
        self._secondary_pattern = re.compile(r"[，,、；;]+")

        logger.info(
            f"灵犀插件已加载 | 名称: {self.bot_names} | "
            f"关键词: {self.keywords or '无'} | "
            f"上下文消息数: {self.context_messages_count} | "
            f"上下文截断: {'启用' if self.context_truncation_enabled else '关闭'} | "
            f"增量注入: {'启用' if self.incremental_context_enabled else '关闭'} | "
            f"摘要压缩: {'启用' if self.context_compression_enabled else '关闭'} | "
            f"模型路由: {'启用' if self.model_routing_enabled else '关闭'}(小模型={self.routing_small_model or '未指定'}) | "
            f"异常检测: {'启用' if self.anomaly_detection_enabled else '关闭'}(σ={self.anomaly_sigma_threshold}) | "
            f"绕过核心上下文: {'启用' if self.bypass_core_context else '关闭'} | "
            f"对话记忆: {'启用' if self.conversation_memory_enabled else '关闭'}(近{self.recent_rounds_keep}轮原文+{self.summary_rounds_max}轮摘要) | "
            f"白名单: {'启用' if self.whitelist_enabled else '关闭'} | "
            f"白名单群: {self.enabled_groups} | 黑名单群: {self.blocked_groups} | "
            f"概率唤醒: {'启用' if self.probability_wakeup else '关闭'} | "
            f"冷场救场: {'启用' if self.rescue_enabled else '关闭'} | "
            f"防抖: {'启用' if self.debounce_enabled else '关闭'} | "
            f"思考过滤: {'启用' if self.filter_thinking_tags else '关闭'} | "
            f"回复抑制: {'启用' if self.reply_suppression_enabled else '关闭'}(模式={self.reply_suppression_mode}) | "
            f"指令前缀跳过: {'启用' if self.command_prefix_enabled else '关闭'}(前缀='{self.command_prefix}') | "
            f"唤醒前缀: '{self.wake_command_prefix}' | "
            f"调试模式: {'启用' if self.debug_mode else '关闭'} | "
            f"群组覆盖: {len(self.group_overrides)} 个群 | "
            f"用户概率覆盖: {len(self.user_prob_overrides)} 个用户 | "
            f"分段: {'启用' if self.splitter_enabled else '关闭'}"
        )

        # ─── 主动发言模块（v1.5.0 新增） ───
        # 基于三要素模型（Anticipation-Initiation-Planning）设计
        # Bot 定时主动发起话题，与被动回复共享同一套记忆系统
        proactive_cfg = self.config.get("proactive_speak", {})
        self._proactive_enabled = proactive_cfg.get("proactive_enabled", False)
        # v1.7.0 修复：统一代码 fallback 与 schema default，避免新安装时行为偏差
        self._proactive_check_interval = max(300, proactive_cfg.get("proactive_check_interval", 600))
        self._proactive_cooldown = max(600, proactive_cfg.get("proactive_cooldown", 1800))
        self._proactive_daily_limit = max(1, proactive_cfg.get("proactive_daily_limit", 20))
        self._proactive_probability = proactive_cfg.get("proactive_probability", 0.6)
        self._proactive_time_window_start = proactive_cfg.get("proactive_time_window_start", "09:00")
        self._proactive_time_window_end = proactive_cfg.get("proactive_time_window_end", "23:00")
        # v1.8.4 新增：静默时段配置（支持多段，如 "23:00-07:00,13:00-14:00"）
        # 与 time_window 是交集关系：两者都允许时才发言
        # 空字符串则不启用静默时段
        self._proactive_quiet_hours = proactive_cfg.get("proactive_quiet_hours", "23:00-07:00")
        self._proactive_min_context_messages = max(1, proactive_cfg.get("proactive_min_context_messages", 1))
        # 自说自话检测参数（v1.7.0 可配置化，替代原硬编码 0.5 阈值 + 退让死循环）
        self._proactive_self_talk_ratio = proactive_cfg.get("proactive_self_talk_ratio", 0.8)
        self._proactive_self_talk_hours = proactive_cfg.get("proactive_self_talk_hours", 2.0)
        self._proactive_min_energy = proactive_cfg.get("proactive_min_energy", 0.15)
        self._proactive_model = proactive_cfg.get("proactive_model", "")
        # 冷场检测参数
        self._proactive_idle_threshold = max(300, proactive_cfg.get("proactive_idle_threshold", 600))
        # UMO 有效期（24 小时）
        self.UMO_VALIDITY_PERIOD = 86400
        # 话题类别
        categories_str = proactive_cfg.get("proactive_topic_categories", "分享想法|提问讨论|回忆过去|关注某人|活跃气氛")
        self._proactive_topic_categories = [c.strip() for c in categories_str.split("|") if c.strip()]
        # 自定义话题
        custom_topics_str = proactive_cfg.get("proactive_custom_topics", "")
        self._proactive_custom_topics = [t.strip() for t in custom_topics_str.split("|") if t.strip()] if custom_topics_str else []

        # ─── Fetcher 模块（v1.8.0 新增）：外部资讯抓取 ───
        # 为主动发言提供外部实时资讯，使 Bot 从"信息索求者"转变为"分享者与锐评人"
        # 部署到服务器时凭据在插件内部管理，不依赖 laptop 上的外部文件
        fetcher_cfg = self.config.get("fetcher", {})
        self._fetcher = None
        if fetcher_cfg.get("fetcher_enabled", False):
            try:
                from .modules.fetcher import FetcherManager
                self._fetcher = FetcherManager(fetcher_cfg)
                logger.info(f"[Fetcher] 模块已启用，{len(self._fetcher.fetchers)} 个数据源")
            except Exception as e:
                logger.warning(f"[Fetcher] 模块初始化失败，将使用原有逻辑: {e}")
                self._fetcher = None

        # 主动发言专用状态（按群维度）
        # 缓存每群的 unified_msg_origin + 缓存时间（v2.0 改进：增加有效期检查）
        # 数据结构：{group_id: (umo_str, cached_time)}
        self._group_umo: dict[str, tuple[str, float]] = {}
        # 每群上次主动发言时间戳（冷却控制）
        self._proactive_last_speak: dict[str, float] = {}
        # 每群每日主动发言计数 {group_id: {date_str: count}}
        self._proactive_daily_count: dict[str, dict[str, int]] = {}
        # 主动发言调度器任务引用（生命周期管理）
        self._proactive_task: "asyncio.Task | None" = None
        # 退让状态记录 {group_id: {"until": timestamp, "reason": str}}
        self._proactive_retreat: dict[str, dict] = {}
        # 主动发言 AIF 状态 {group_id: ProactiveState}
        # v1.5.0 仅使用 PASSIVE_MONITORING / AGENT_DOMINANT 两态
        self._proactive_states: dict[str, str] = {}
        # 连续 [SKIP] 计数（触发退让）
        self._proactive_skip_streak: dict[str, int] = {}

        # ─── Phase 2/3 新增数据结构（v1.6.0） ───
        # 退让信号①：活跃激增检测——5分钟消息数阈值（经验值，后续可调优）
        self._proactive_active_surge_threshold = 20  # 5分钟内超过20条消息视为活跃激增
        self._proactive_active_surge_window = 300    # 5分钟窗口

        # 退让信号②：无人回应追踪——记录每群主动发言时间戳，用于30分钟后检查是否有用户回复
        # 数据结构：{group_id: {"speak_time": float, "checked": bool, "cooldown_multiplier": float}}
        self._proactive_response_tracker: dict[str, dict] = {}
        self._proactive_no_response_window = 1800   # 30分钟追踪窗口
        self._proactive_no_response_penalty = 2.0   # 冷却×2

        # 退让信号③：厌烦关键词检测
        self._proactive_annoyed_keywords = [
            "别说了", "闭嘴", "吵死了", "太吵了", "能不能安静", "你好吵",
            "闭嘴吧", "别逼逼", "烦死了", "能不能别说话了", "安静点",
            "你能不能闭嘴", "别废话", "少说两句", "话真多", "啰嗦"
        ]

        # v1.9.7 新增 M1：退却时长可配置化（替代硬编码）
        # 读取顺序：proactive_speak 配置 → schema 默认值
        self._retreat_active_surge_secs = max(60, proactive_cfg.get("proactive_retreat_active_surge_secs", 3600))
        self._retreat_consecutive_skip_secs = max(60, proactive_cfg.get("proactive_retreat_consecutive_skip_secs", 3600))
        self._retreat_no_response_base_secs = max(300, proactive_cfg.get("proactive_retreat_no_response_base_secs", 3600))
        self._retreat_no_response_max_secs = max(3600, proactive_cfg.get("proactive_retreat_no_response_max_secs", 86400))
        self._retreat_annoyed_secs = max(3600, proactive_cfg.get("proactive_retreat_annoyed_secs", 86400))

        # 话题去重：记录每群近期发起的话题摘要（避免短期内重复）
        # 数据结构：{group_id: deque([(timestamp, topic_summary), ...])}， maxlen=10
        self._proactive_topic_history: dict[str, deque] = {}

        # v1.8.4 新增：开头去重机制（避免资讯类发言每次都以相同模式开头）
        # 数据结构：{group_id: deque([opener_str, ...])}， maxlen=5
        # 用于 prompt 显式排除最近 5 次开头，避免 LLM 重复套用模板
        self._proactive_recent_openers: dict[str, deque] = {}

        # v1.7.1：持久化延迟保存的上次保存时间戳（实例属性，避免类属性误导）
        self._last_save_proactive_state_ts: float = 0.0

        # Phase 3 评估指标：主动发言效果追踪
        # 数据结构：{group_id: [(timestamp, cps, adopted: bool), ...]}，仅保留最近 20 条
        self._proactive_outcomes: dict[str, list] = {}

        # 主动发言统计（全局）
        self._proactive_stats: dict[str, int] = {
            "total_attempts": 0,       # 总尝试次数
            "total_success": 0,        # 成功发送次数
            "total_skip": 0,           # LLM 判断不宜发言次数
            "total_duplicate": 0,      # 去重跳过次数
            "total_send_fail": 0,      # 发送失败次数
            "total_retreats": 0,       # 退让触发次数
            "retreat_self_talk": 0,    # 退让信号④触发次数
            "retreat_skip": 0,         # 退让信号⑤触发次数
            "retreat_active_surge": 0, # 退让信号①触发次数
            "retreat_no_response": 0, # 退让信号②触发次数
            "retreat_annoyed": 0,      # 退让信号③触发次数
        }

        # 敏感词过滤（5指标评分后处理——接受度验证）
        self._proactive_sensitive_words = [
            "政治", "政府", "国家领导", "六四", "台独", "藏独",
            "色情", "裸体", "性行为", " porn ",
            "赌博", "毒品", "违禁品",
            "自杀", "自残", "自杀方法",
        ]

        if self._proactive_enabled:
            logger.info(f"主动发言: 启用 | 检查间隔: {self._proactive_check_interval}s | 冷却: {self._proactive_cooldown}s | 每日上限: {self._proactive_daily_limit} | 概率: {self._proactive_probability}")
        else:
            logger.info("主动发言: 关闭（调度器仍启动以支持单群覆盖）")

        # ─── Web API 注册（v1.9.0 配置面板后端）───
        # 注册 REST API 供 pages/config/ 前端调用
        # 路由前缀：/api/plug/astrbot_plugin_smart_wakeup/config/...
        try:
            from .modules.web_api import register_web_apis
            register_web_apis(self, context)
            logger.info("[Web API] v1.9.0 配置面板后端已注册")
        except Exception as e:
            logger.warning(f"[Web API] 注册失败，配置面板将不可用: {e}")

        # ─── 输出去重 ───

    def _content_fingerprint(self, text: str) -> str:
        """生成内容指纹用于去重，归一化后取 MD5"""
        normalized = re.sub(r'\s+', ' ', text.strip().lower())[:500]
        return hashlib.md5(normalized.encode()).hexdigest()

    def _is_duplicate_content(self, group_id: str, text: str) -> bool:
        """检查该群是否在去重窗口内已发送过相同内容"""
        fingerprint = self._content_fingerprint(text)
        now = time.time()

        if group_id not in self._sent_content_cache:
            self._sent_content_cache[group_id] = deque(maxlen=50)
            return False

        cache = self._sent_content_cache[group_id]

        # 清理过期条目
        while cache and now - cache[0][1] > self._DEDUP_WINDOW:
            cache.popleft()

        # 检查是否重复
        for fp, _ in cache:
            if fp == fingerprint:
                return True

        return False

    def _record_sent_content(self, group_id: str, text: str):
        """记录已发送内容到去重缓存"""
        fingerprint = self._content_fingerprint(text)
        now = time.time()

        if group_id not in self._sent_content_cache:
            self._sent_content_cache[group_id] = deque(maxlen=50)

        self._sent_content_cache[group_id].append((fingerprint, now))

    @staticmethod
    def _calc_text_similarity(text_a: str, text_b: str) -> float:
        """计算两段文本的字符 bigram Jaccard 相似度

        使用字符级 bigram 集合的 Jaccard 系数衡量文本相似度。
        对中文文本效果良好，因为每个汉字都携带语义信息。
        返回 0.0~1.0 之间的浮点数，1.0 表示完全相同。
        """
        # 归一化：去除空白和标点，转小写
        normalize = lambda t: re.sub(r'[\s\u3000\uff01\uff08\uff09\uff0c\uff1f\uff1b\uff1a\u201c\u201d\u2018\u2019\u3002\uff0e!?;:,\.\(\)"\'\-]', '', t.lower())
        a = normalize(text_a)
        b = normalize(text_b)

        if not a or not b:
            return 0.0

        # 完全相同
        if a == b:
            return 1.0

        # 构建字符 bigram 集合
        bigrams_a = {a[i:i+2] for i in range(len(a) - 1)}
        bigrams_b = {b[i:i+2] for i in range(len(b) - 1)}

        if not bigrams_a or not bigrams_b:
            return 0.0

        # Jaccard 相似度 = 交集 / 并集
        intersection = len(bigrams_a & bigrams_b)
        union = len(bigrams_a | bigrams_b)

        return intersection / union if union > 0 else 0.0

    async def _regenerate_with_anti_repeat(
        self, event: AstrMessageEvent, group_id: str, current_text: str, last_text: str
    ) -> str | None:
        """检测到语义重复时，注入防重复提示重新调用 LLM 生成不同内容

        返回重新生成的文本（与上次回复相似度低于阈值），失败或仍相似则返回 None。
        仅尝试一次重新生成，避免无限循环。
        """
        try:
            # 获取当前使用的 provider
            provider = None
            # 优先使用路由模型（如果有的话）
            routing_model = self._determine_routing_model(event)
            if routing_model:
                provider = self.context.get_provider_by_id(routing_model)
            # 回退到默认 provider
            if not provider:
                provider = self.context.get_using_provider()

            if not provider:
                logger.warning("[SemanticDedup] 无可用 provider，跳过重新生成")
                return None

            # 构建防重复提示
            display_last = last_text[:150] + ("..." if len(last_text) > 150 else "")
            anti_repeat_prompt = (
                "你刚才说了：\n「{display_last}」\n"
                "你这次的回复和刚才说的内容太相似了，请换一个完全不同的角度或表达方式，"
                "或者补充新的信息和观点。如果确实没有新的内容可说，请输出 [SKIP]。\n\n"
                "你刚才的回复：\n「{current_text}」"
            ).format(display_last=display_last, current_text=current_text[:200])

            # 调用 LLM 重新生成
            regen_resp = await provider.text_chat(
                prompt=anti_repeat_prompt,
                session_id=f"regen_{group_id}_{int(time.time())}",
                system_prompt=(
                    "你是一个群聊中的普通成员。你刚才的回复被判定为与上一条重复，"
                    "请重新生成一个不同的回复。保持自然，不要提及你被要求重新生成。"
                ),
            )

            # 提取重新生成的文本
            regen_text = ""
            if hasattr(regen_resp, 'completion_text'):
                regen_text = regen_resp.completion_text or ""
            elif hasattr(regen_resp, 'result'):
                regen_text = str(regen_resp.result) if regen_resp.result else ""

            if not regen_text:
                logger.warning("[SemanticDedup] 重新生成返回为空")
                return None

            # 过滤思考标签和重复回复
            if self.filter_thinking_tags:
                regen_text = self._filter_thinking_tags(regen_text)
            regen_text = self._filter_duplicate_response(regen_text)
            regen_text = regen_text.strip()

            # 检查是否输出 [SKIP]（LLM 认为确实没有新内容可说）
            # v1.9.8 加固：前缀匹配（防推理模型 "[SKIP]+理由+回复" 混合体泄露）
            if regen_text.startswith("[SKIP]") or regen_text.upper().startswith("[SKIP]"):
                logger.info("[SemanticDedup] LLM 主动选择跳过（输出 [SKIP]）")
                return None

            if not regen_text:
                return None

            # 验证重新生成的内容与上次回复的相似度
            new_similarity = self._calc_text_similarity(regen_text, last_text)
            if new_similarity >= 0.55:
                logger.info(
                    f"[SemanticDedup] 重新生成仍相似 | 新相似度={new_similarity:.2f} "
                    f"内容='{regen_text[:60]}'"
                )
                return None

            # 估算 token 消耗
            est_tokens = self._estimate_tokens(anti_repeat_prompt + regen_text)
            self._record_token_usage(
                event=event,
                prompt_tokens=est_tokens // 2,
                completion_tokens=est_tokens // 2,
                model_name=getattr(provider, 'model_name', 'unknown'),
                is_estimated=True,
            )

            return regen_text

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[SemanticDedup] 重新生成异常: {e}")
            return None

    def _is_reply_to_bot(self, event: AstrMessageEvent) -> bool:
        """检测消息是否是回复BOT的消息

        检测方式（按优先级）：
        1. 检查消息链中的 Reply 组件，判断回复目标的 sender_id 是否为BOT
        2. 检查消息链中 Plain 组件是否包含 [引用消息(BOT名称:...)] 格式
        3. 检查 message_str 中是否包含 [引用消息(BOT名称:...)] 格式

        注意：转发自BOT的消息（Telegram 加一复读）不属于"回复BOT"，
        已在 on_group_message 入口处通过 _is_forward_from_bot 过滤。

        适配 Telegram 和 QQ 两种场景：
        - Telegram: Reply 组件含 sender_id，核心日志格式为 [引用消息(BOT名: 内容)]
        - QQ: Reply 组件含 sender_id，message_str 可能包含引用格式
        """
        try:
            from astrbot.api.message_components import Reply, Plain
            message_obj = event.message_obj

            if not message_obj or not message_obj.message:
                return self._detect_quote_reply_to_bot(event)

            # 收集消息链信息
            has_reply = False
            reply_sender_id = None
            reply_sender_name = None
            plain_texts = []

            for comp in message_obj.message:
                if isinstance(comp, Reply):
                    has_reply = True
                    reply_sender_id = getattr(comp, "sender_id", None)
                    reply_sender_name = getattr(comp, "sender", None)
                elif isinstance(comp, Plain):
                    comp_text = getattr(comp, "text", "")
                    if comp_text:
                        plain_texts.append(comp_text)

            self._debug(
                f"回复检测 | 消息链: Reply={has_reply}(sender_id={reply_sender_id}, sender={reply_sender_name}), "
                f"Plain组件={len(plain_texts)}个"
            )

            # 方式1：Reply 组件的 sender_id 与 BOT ID 比较
            if has_reply and reply_sender_id:
                # 1a: 与 message_obj.self_id 比较
                self_id = getattr(message_obj, "self_id", None)
                if self_id:
                    self._debug(f"回复检测 | self_id={self_id}, reply_sender_id={reply_sender_id}")
                    if str(reply_sender_id) == str(self_id):
                        self._debug(f"回复检测 | Reply.sender_id == self_id，确认为回复BOT")
                        return True

                # 1b: 尝试从 context 获取 bot_id
                for attr_name in ("bot_id", "self_id", "bot_user_id"):
                    try:
                        attr_val = getattr(self.context, attr_name, None)
                        if attr_val and str(reply_sender_id) == str(attr_val):
                            self._debug(f"回复检测 | Reply.sender_id匹配context.{attr_name}={attr_val}")
                            return True
                    except Exception:
                        pass  # context 属性访问可能失败，属正常情况

                # 1c: 与已记录的 BOT 用户 ID 比较
                if str(reply_sender_id) in self._bot_user_ids:
                    self._debug(f"回复检测 | Reply.sender_id在已知BOT用户ID列表中")
                    return True

            # 方式2：检查 Plain 组件中的引用格式
            # AstrBot 核心可能将 [引用消息(BOT名称:...)] 放在 Plain 组件中
            for text in plain_texts:
                for name in self.bot_names:
                    if re.search(r'\[引用消息\(' + re.escape(name) + r'[:/\] ]', text):
                        self._debug(f"回复检测 | Plain组件匹配引用BOT名称'{name}'")
                        # 反向记录：如果此消息有 Reply 组件，其 sender_id 就是 BOT 的数字 ID
                        if has_reply and reply_sender_id and str(reply_sender_id) not in self._bot_user_ids:
                            self._bot_user_ids.add(str(reply_sender_id))
                            self._debug(f"BOT用户ID记录(反向) | 从引用消息推断BOT ID={reply_sender_id}")
                        return True

            # 方式3：检查 message_str 中的引用格式
            if self._detect_quote_reply_to_bot(event):
                self._debug(f"回复检测 | message_str匹配引用BOT名称")
                # 同样尝试反向记录
                if has_reply and reply_sender_id and str(reply_sender_id) not in self._bot_user_ids:
                    self._bot_user_ids.add(str(reply_sender_id))
                    self._debug(f"BOT用户ID记录(反向) | 从引用消息推断BOT ID={reply_sender_id}")
                return True

            # 方式4：检查 Telegram 转发来源（加一复读等场景）
            # 注意：转发自BOT的消息本质是复读，不是"回复BOT"。
            # 在 on_group_message 入口处已通过 _is_forward_from_bot 过滤，
            # 此处不再将转发复读视为"回复BOT"，避免复读触发唤醒。
            # 保留此注释以便理解设计意图。

            self._debug(f"回复检测 | 未检测到回复BOT消息")
            return False

        except Exception as e:
            self._debug(f"回复检测 | 出错: {e}")
        return False

    def _is_forward_from_bot(self, event: AstrMessageEvent) -> bool:
        """检测消息是否是转发自BOT的消息（Telegram 加一复读等场景）

        Telegram 的"加一"复读功能通过 forward_origin 转发消息：
        - forward_origin=MessageOriginUser(sender_user=User(id=xxx, is_bot=True, ...))
        - 消息链中没有 Reply 组件
        - 框架显示发送者为转发者（Unknown/xxx），而非原始发送者

        检测策略（按优先级）：
        1. 从 message_obj 上寻找原始 Telegram Message 对象（属性名可能为 raw_message/message/_raw 等）
        2. 检查 forward_origin 的 sender_user.id 是否为已知 BOT ID
        3. 检查 forward_origin 的 sender_user.is_bot 且名称/内容匹配
        4. 备用：检查 api_kwargs 中的 forward_from
        5. 兜底：检查消息文本是否与缓冲区中 BOT 消息匹配（无 Reply 的纯文本复读）
        """
        try:
            message_obj = event.message_obj
            if not message_obj:
                return False

            # 策略1-4：尝试从 message_obj 上寻找原始 Telegram Message 对象
            # AstrBot 框架可能将原始消息存储在不同属性名下
            raw_msg = None
            for attr in ("raw_message", "message", "_raw_message", "raw_msg", "telegram_message"):
                candidate = getattr(message_obj, attr, None)
                if candidate is not None:
                    # 排除消息链（list 类型）和字符串类型
                    if not isinstance(candidate, (list, str)):
                        raw_msg = candidate
                        self._debug(f"转发检测 | 从 message_obj.{attr} 获取到原始消息对象: {type(raw_msg).__name__}")
                        break

            if raw_msg:
                # 尝试获取 forward_origin（python-telegram-bot v20+ 属性）
                forward_origin = getattr(raw_msg, "forward_origin", None)
                if forward_origin:
                    result = self._check_forward_origin(message_obj, forward_origin, event)
                    if result:
                        return True

                # 备用：检查 api_kwargs（python-telegram-bot v20+ 的 Message 对象有此字段）
                api_kwargs = getattr(raw_msg, "api_kwargs", None)
                if api_kwargs and isinstance(api_kwargs, dict):
                    result = self._check_api_kwargs_forward(message_obj, api_kwargs, event)
                    if result:
                        return True

            else:
                self._debug(f"转发检测 | message_obj 上未找到原始消息对象，尝试遍历属性")
                # 遍历 message_obj 的所有属性，寻找包含 forward_origin 的对象
                for attr_name in dir(message_obj):
                    if attr_name.startswith("_"):
                        continue
                    try:
                        attr_val = getattr(message_obj, attr_name, None)
                        if attr_val is None or isinstance(attr_val, (str, int, float, bool, list, dict)):
                            continue
                        forward_origin = getattr(attr_val, "forward_origin", None)
                        if forward_origin:
                            self._debug(f"转发检测 | 从 message_obj.{attr_name} 找到 forward_origin")
                            result = self._check_forward_origin(message_obj, forward_origin, event)
                            if result:
                                return True
                        api_kwargs = getattr(attr_val, "api_kwargs", None)
                        if api_kwargs and isinstance(api_kwargs, dict):
                            result = self._check_api_kwargs_forward(message_obj, api_kwargs, event)
                            if result:
                                return True
                    except Exception:
                        continue

            # 策略5（兜底）：无 Reply 组件 + 消息文本与缓冲区中 BOT 消息匹配
            # Telegram 加一复读的特征：无 Reply 组件，纯文本，内容是 BOT 消息的子串
            return self._check_forward_repeat_by_buffer(event)

        except Exception as e:
            self._debug(f"转发检测 | 出错: {e}")
        return False

    def _check_forward_origin(self, message_obj, forward_origin, event: AstrMessageEvent) -> bool:
        """检查 forward_origin 是否指向 BOT"""
        sender_user = getattr(forward_origin, "sender_user", None)
        if not sender_user:
            self._debug(f"转发检测 | forward_origin 无 sender_user")
            return False

        sender_id = str(getattr(sender_user, "id", ""))
        is_bot = getattr(sender_user, "is_bot", False)
        sender_name = getattr(sender_user, "first_name", "")
        sender_username = getattr(sender_user, "username", "")
        self._debug(f"转发检测 | forward_origin: id={sender_id} is_bot={is_bot} name='{sender_name}' username='{sender_username}'")

        # 检查是否为已知 BOT ID（数字 ID 或用户名）
        if sender_id and sender_id in self._bot_user_ids:
            self._debug(f"转发检测 | sender_user.id={sender_id} 在已知BOT ID列表中")
            return True

        # 检查 self_id（可能是数字 ID 或用户名）
        self_id = getattr(message_obj, "self_id", None)
        if self_id:
            self_id_str = str(self_id)
            if sender_id and (sender_id == self_id_str or sender_username == self_id_str):
                self._debug(f"转发检测 | sender匹配self_id(self_id={self_id_str})")
                # 记录数字 ID
                if sender_id and sender_id not in self._bot_user_ids:
                    self._bot_user_ids.add(sender_id)
                    self._debug(f"BOT用户ID记录(转发self_id) | 新增user_id={sender_id}")
                return True

        # 检查 is_bot 且名称/用户名匹配 BOT 名称
        if is_bot:
            for name in self.bot_names:
                # 检查 first_name 或 username 中包含 BOT 名称
                if name in sender_name or name.lower() in sender_username.lower():
                    self._debug(f"转发检测 | is_bot=True且名称匹配'{name}'(name='{sender_name}', username='{sender_username}')")
                    if sender_id and sender_id not in self._bot_user_ids:
                        self._bot_user_ids.add(sender_id)
                        self._debug(f"BOT用户ID记录(转发名称) | 新增user_id={sender_id}")
                    return True

        # is_bot=True 但名称不匹配：检查转发内容是否与缓冲区中 BOT 消息匹配
        if is_bot and sender_id:
            group_id = message_obj.group_id
            if group_id:
                buffer = self._get_buffer(group_id)
                msg_text = (event.message_str or "").strip()
                if msg_text and buffer:
                    for _sender, buf_text, _ts, _meta in reversed(buffer):
                        if _sender in self.bot_names and msg_text in buf_text:
                            self._debug(f"转发检测 | is_bot=True且转发内容匹配缓冲区BOT消息")
                            if sender_id not in self._bot_user_ids:
                                self._bot_user_ids.add(sender_id)
                                self._debug(f"BOT用户ID记录(转发匹配) | 新增user_id={sender_id}")
                            return True

        return False

    def _check_api_kwargs_forward(self, message_obj, api_kwargs: dict, event: AstrMessageEvent) -> bool:
        """检查 api_kwargs 中的 forward_from 信息"""
        forward_from = api_kwargs.get("forward_from")
        if not forward_from or not isinstance(forward_from, dict):
            return False

        fwd_id = str(forward_from.get("id", ""))
        fwd_is_bot = forward_from.get("is_bot", False)
        fwd_name = forward_from.get("first_name", "")
        fwd_username = forward_from.get("username", "")
        self._debug(f"转发检测(api_kwargs) | id={fwd_id} is_bot={fwd_is_bot} name='{fwd_name}' username='{fwd_username}'")

        if fwd_id and fwd_id in self._bot_user_ids:
            self._debug(f"转发检测(api_kwargs) | forward_from.id={fwd_id} 在已知BOT ID列表中")
            return True

        if fwd_is_bot and fwd_id:
            self_id = getattr(message_obj, "self_id", None)
            if self_id:
                self_id_str = str(self_id)
                if fwd_id == self_id_str or fwd_username == self_id_str:
                    self._debug(f"转发检测(api_kwargs) | forward_from匹配self_id")
                    if fwd_id not in self._bot_user_ids:
                        self._bot_user_ids.add(fwd_id)
                        self._debug(f"BOT用户ID记录(api_kwargs) | 新增user_id={fwd_id}")
                    return True

            # 检查名称匹配
            for name in self.bot_names:
                if name in fwd_name or name.lower() in fwd_username.lower():
                    self._debug(f"转发检测(api_kwargs) | is_bot=True且名称匹配'{name}'")
                    if fwd_id not in self._bot_user_ids:
                        self._bot_user_ids.add(fwd_id)
                        self._debug(f"BOT用户ID记录(api_kwargs名称) | 新增user_id={fwd_id}")
                    return True

        return False

    def _check_forward_repeat_by_buffer(self, event: AstrMessageEvent) -> bool:
        """兜底检测：无 Reply 组件 + 消息文本与缓冲区中 BOT 消息匹配

        Telegram 加一复读的特征：
        - 消息链无 Reply 组件
        - 纯文本消息
        - 内容与 BOT 最近发送的消息相同或为其子串

        此方法作为 _is_forward_from_bot 的兜底策略，
        当无法访问 Telegram 原始消息对象时使用。
        """
        try:
            message_obj = event.message_obj
            if not message_obj or not message_obj.message:
                return False

            # 检查消息链是否无 Reply 组件
            from astrbot.api.message_components import Reply
            has_reply = any(isinstance(comp, Reply) for comp in message_obj.message)
            if has_reply:
                return False  # 有 Reply 组件的不是转发复读

            # 检查消息是否为纯文本（无图片/贴纸等）
            msg_text = (event.message_str or "").strip()
            if not msg_text or len(msg_text) < 4:
                return False  # 过短文本不检测

            group_id = message_obj.group_id
            if not group_id:
                return False

            buffer = self._get_buffer(group_id)
            if not buffer:
                return False

            # 检查消息文本是否与缓冲区中 BOT 最近的消息匹配
            current_normalized = self._normalize_for_repeat_check(msg_text)
            if not current_normalized or len(current_normalized) < 4:
                return False

            for _sender, buf_text, _ts, _meta in reversed(buffer):
                if _sender not in self.bot_names:
                    continue
                buf_normalized = self._normalize_for_repeat_check(buf_text)
                if not buf_normalized:
                    continue
                # 全文复读或部分复读（当前消息是 BOT 消息的子串）
                if current_normalized == buf_normalized:
                    self._debug(f"转发复读检测(兜底) | 全文匹配BOT消息 sender={_sender}")
                    return True
                if len(current_normalized) >= 4 and current_normalized in buf_normalized:
                    ratio = len(current_normalized) / len(buf_normalized)
                    if ratio >= 0.2:  # 占比20%以上视为复读
                        self._debug(f"转发复读检测(兜底) | 部分匹配BOT消息 sender={_sender} 占比={ratio:.0%}")
                        return True

            return False
        except Exception as e:
            self._debug(f"转发复读检测(兜底) | 出错: {e}")
            return False

    def _detect_quote_reply_to_bot(self, event: AstrMessageEvent) -> bool:
        """从 message_str 中检测 [引用消息(BOT名称: ...)] 格式"""
        message_str = event.message_str or ""
        # AstrBot 核心将回复解析为 [引用消息(发送者名: 内容)]
        # 检查是否有引用消息且引用的发送者是BOT名称之一
        for name in self.bot_names:
            # 匹配 [引用消息(名称: 或 [引用消息(名称/ 或 [引用消息(名称]
            if re.search(r'\[引用消息\(' + re.escape(name) + r'[:/\] ]', message_str):
                return True
        return False

    def _debug(self, msg: str):
        """调试日志：仅在调试模式开启时以 INFO 级别输出，确保不被日志级别过滤"""
        if self.debug_mode:
            logger.info(f"[调试] {msg}")

    def _is_low_info_message(self, message_str: str, message_chain=None) -> bool:
        """判断消息是否为低信息量消息（纯媒体/纯emoji/颜文字），应跳过唤醒判定

        判断流程：
        1. message_str 为空或仅含空白字符 → 低信息量
        2. 检测消息链是否为纯 Sticker（Image + Plain("Sticker: xxx")）→ 低信息量
        3. 移除所有媒体标签（如 [图片]、Sticker: 等）
        4. 移除所有 emoji 字符
        5. 检查剩余文本是否包含有效内容（中文字符或连续字母数字词）
           - 若无有效内容 → 低信息量（如颜文字 (¬_¬)、纯标点等）
        """
        if not message_str or not message_str.strip():
            return True

        # 检测消息链是否为纯 Sticker 组合（Image + Plain("Sticker: xxx")）
        if message_chain is not None:
            has_image = False
            sticker_emoji = False
            has_other_content = False
            for comp in message_chain:
                comp_type = type(comp).__name__
                if comp_type == "Image":
                    has_image = True
                elif comp_type == "Plain":
                    comp_text = getattr(comp, "text", "")
                    if comp_text.startswith("Sticker:"):
                        sticker_emoji = True
                    elif comp_text.strip():
                        has_other_content = True
                else:
                    # 有非 Image/Plain 组件（如 Reply 等），不是纯 Sticker
                    has_other_content = True
            if has_image and sticker_emoji and not has_other_content:
                return True

        stripped = message_str.strip()

        # 第一步：移除所有匹配的媒体标签
        text = stripped
        if self.media_message_patterns:
            for pattern in self.media_message_patterns:
                text = text.replace(pattern, "")

        # 第二步：移除所有 emoji 字符
        def _is_emoji_char(ch: str) -> bool:
            cp = ord(ch)
            return (
                0x1F600 <= cp <= 0x1F64F   # emoticons
                or 0x1F300 <= cp <= 0x1F5FF  # symbols & pictographs
                or 0x1F680 <= cp <= 0x1F6FF  # transport & map
                or 0x1F1E0 <= cp <= 0x1F1FF  # flags
                or 0x2702 <= cp <= 0x27B0    # dingbats
                or 0x24C2 <= cp <= 0x24FF    # enclosed alphanumerics
                or 0x1F100 <= cp <= 0x1F1FF  # enclosed alphanumeric supplement
                or 0x1F900 <= cp <= 0x1F9FF  # supplemental symbols and pictographs
                or 0x1FA00 <= cp <= 0x1FA6F  # chess symbols
                or 0x1FA70 <= cp <= 0x1FAFF  # symbols and pictographs extended-A
                or 0x2600 <= cp <= 0x26FF    # misc symbols
                or 0x2700 <= cp <= 0x27BF    # dingbats
                or 0x2300 <= cp <= 0x23FF    # misc technical
                or 0x2B50 <= cp <= 0x2B55    # stars/circles
                or 0x2900 <= cp <= 0x297F    # supplemental arrows
                or 0x3000 <= cp <= 0x303F    # CJK symbols (含 wavy dash 等)
                or 0x3200 <= cp <= 0x32FF    # enclosed CJK letters
                or cp == 0x200D              # zero width joiner
                or cp == 0xFE0F              # variation selector
            )

        text = "".join(ch for ch in text if not _is_emoji_char(ch))

        # 第三步：移除空白和零宽字符
        text = text.replace("\u200d", "").replace("\ufe0f", "").strip()
        if not text:
            return True

        # 第四步：检查剩余文本是否包含有效内容
        # 有效内容 = 中文字符（CJK统一汉字）或连续2个及以上的字母/数字
        # 纯标点、颜文字如 (¬_¬) ¯\_(ツ)_/¯ 等不含有效内容
        has_cjk = any(0x4E00 <= ord(ch) <= 0x9FFF or 0x3400 <= ord(ch) <= 0x4DBF for ch in text)
        if has_cjk:
            return False

        has_word = bool(re.search(r'[a-zA-Z0-9]{2,}', text))
        if has_word:
            return False

        # 剩余文本仅含标点、符号、单个字母/数字 → 低信息量
        return True

    def _is_repeat_message(self, group_id: str, sender_name: str, message_str: str) -> tuple:
        """检测当前消息是否为复读（与缓冲区中近期消息相同/相似/子串）

        检测模式：
        1. 全文复读：归一化后文本完全相同
        2. 部分复读：当前消息整体是某条历史消息的子串（如从BOT长回复中拆出一句复读）
        3. BOT发言复读：BOT的发言已在 after_message_sent 中记录到缓冲区，自然纳入检测

        返回: (is_repeat: bool, match_info: str)
        """
        buffer = self._msg_buffer.get(group_id)
        if not buffer or len(buffer) < 1:
            self._debug(f"复读检测 | 缓冲区为空或无消息 群={group_id}")
            return False, ""

        current_text = self._normalize_for_repeat_check(message_str)

        if not current_text or len(current_text) < 2:
            return False, ""

        # 从最新消息往前检查，范围与真实记忆轮数一致（每轮2条：用户+BOT）
        messages = list(reversed(buffer))
        check_limit = self.recent_rounds_keep * 2

        checked_count = 0
        for i, (sender, text, timestamp, _meta) in enumerate(messages):
            if i >= check_limit:
                break

            # 跳过同一发送者的消息（使用昵称比较，与缓冲区存储格式一致）
            if sender == sender_name:
                continue

            hist_text = self._normalize_for_repeat_check(text)
            if not hist_text or len(hist_text) < 2:
                continue

            checked_count += 1

            # 模式1：全文复读（归一化后文本完全相同）
            if current_text == hist_text:
                return True, f"全文复读 | 发送者={sender} 当前='{current_text[:30]}' 历史='{hist_text[:30]}'"

            # 模式2：部分复读（当前消息是历史消息的子串）
            # 仅检测 current in hist 方向：当前消息整体出现在历史消息中
            # 不检测 hist in current：那说明当前消息添加了新内容，不是复读
            # 额外要求：当前消息长度占历史消息的30%以上，避免短词偶然命中长消息
            min_len = self.repeat_min_length
            if len(current_text) >= min_len and len(current_text) <= len(hist_text):
                if current_text in hist_text and len(current_text) / len(hist_text) >= 0.3:
                    return True, f"部分复读 | 发送者={sender} 当前='{current_text[:30]}' 历史片段='{hist_text[:50]}' 占比={len(current_text)/len(hist_text):.0%}"

        self._debug(f"复读检测 | 未匹配 sender={sender_name} current='{current_text[:40]}' 检查了{checked_count}条历史消息(共{len(messages)}条)")
        return False, ""

    @staticmethod
    def _normalize_for_repeat_check(text: str) -> str:
        """归一化文本用于复读比较：去除首尾空白、标点、全半角差异"""
        if not text:
            return ""
        t = text.strip()
        # 全角转半角
        t = t.replace("？", "?").replace("！", "!").replace("。", ".").replace("，", ",").replace("：", ":").replace("；", ";")
        # 去除末尾标点
        t = t.rstrip("?!.,;:!?。，！？；：~～")
        return t

    def _trigger_wake(self, event: AstrMessageEvent):
        """触发唤醒：设置唤醒标志，并在配置了LLM聊天唤醒前缀时补上前缀

        当AstrBot系统设置中配置了「LLM聊天额外唤醒前缀」时，
        核心管道会检查消息是否以该前缀开头。此处将前缀补到 message_str 前面，
        确保核心管道能正确处理唤醒请求。

        注意：用户在插件配置中只填写额外部分（如 chat），系统会自动补上斜杠（/chat）。
        """
        # 设置 LLM 执行中标志，防止冷场救场并发触发重复输出
        group_id = event.message_obj.group_id
        if group_id:
            # P1-2 修复（v1.7.2）：双重触发时保留首次标志，避免覆盖时间戳和取消已运行的定时器
            # 根因：概率唤醒+名称触发在10秒内对同一群两次调用 _trigger_wake 时，
            # 第二次会覆盖时间戳并取消第一个定时器（T1），启动新定时器（T2）。
            # 如果第一个 LLM 管道完成后 on_decorating_result 清除了标志并取消了 T2，
            # 而第二个 LLM 管道也被中断（如 Telegram 网络错误），则无定时器兜底，标志永久卡死。
            # 修复：标志已存在时不覆盖、不重启定时器，让首次的定时器作为兜底。
            if group_id not in self._llm_running_groups:
                self._llm_running_groups[group_id] = time.time()
                # 启动主动超时定时器（60秒后自动清除），防止 CancelledError
                # 中断管道后 on_decorating_result/after_message_sent 均不被调用导致标志卡死
                self._start_llm_flag_timer(group_id)
            else:
                elapsed = time.time() - self._llm_running_groups[group_id]
                self._debug(f"触发唤醒 | 群={group_id} LLM执行中({elapsed:.0f}秒)，保留首次定时器不覆盖")

        event.is_at_or_wake_command = True
        if self.wake_command_prefix:
            original = event.message_str or ""
            # 直接使用用户填写的前缀，核心管道会自行处理斜杠
            event.message_str = self.wake_command_prefix + " " + original
            self._debug(f"触发唤醒 | 前缀='{self.wake_command_prefix}' 修改前='{original[:40]}' 修改后='{event.message_str[:40]}'")
        else:
            self._debug(f"触发唤醒 | 无前缀 message_str='{(event.message_str or '')[:40]}'")

        # 记录用户消息到对话历史
        if self.conversation_memory_enabled:
            group_id = event.message_obj.group_id
            if group_id:
                self._record_user_message(group_id, event.message_str or "", event.get_sender_name() or "")

    def _start_llm_flag_timer(self, group_id: str):
        """启动 LLM 执行中标志的主动超时定时器

        防止 CancelledError 中断管道后 on_decorating_result 和 after_message_sent
        均不被调用，导致标志卡死、该群长时间无法触发任何新唤醒。
        定时器在 60 秒后自动清除标志（与 httpx timeout=60 对齐）。
        """
        # P1-2 修复（v1.7.2）：定时器已存在时不取消重启，保留首次的超时窗口
        # 根因：_trigger_wake 双重触发时，第二次调用会取消第一个定时器（T1）并启动新定时器（T2）。
        # 如果第一个 LLM 的 on_decorating_result 清除了标志并取消了 T2，
        # 而第二个 LLM 管道也被中断，则无定时器兜底，标志永久卡死。
        # 修复：定时器已存在时直接返回，让首次的定时器作为兜底。
        if group_id in self._llm_flag_timers:
            return
        # 启动新定时器
        self._llm_flag_timers[group_id] = asyncio.create_task(
            self._auto_clear_llm_flag(group_id, self._LLM_FLAG_TIMEOUT)
        )

    def _cancel_llm_flag_timer(self, group_id: str):
        """取消指定群的 LLM 标志定时器（正常清除路径调用）"""
        task = self._llm_flag_timers.pop(group_id, None)
        if task and not task.done():
            task.cancel()
        # 诊断（v1.7.3）：统一清理诊断数据，防止跨请求残留
        self._llm_request_started_at.pop(group_id, None)
        self._llm_response_received_at.pop(group_id, None)

    async def _auto_clear_llm_flag(self, group_id: str, timeout: int):
        """主动超时清除 LLM 执行中标志"""
        try:
            await asyncio.sleep(timeout)
            if group_id in self._llm_running_groups:
                elapsed = time.time() - self._llm_running_groups[group_id]
                if elapsed >= timeout:
                    # 诊断（v1.7.3）：检查 on_llm_request/on_llm_response 是否到达
                    request_started = self._llm_request_started_at.get(group_id)
                    response_received = self._llm_response_received_at.get(group_id)
                    if request_started is None:
                        diag_info = "未记录on_llm_request（可能为插件自身LLM调用如ContextCompress）"
                    elif response_received is None:
                        llm_elapsed = time.time() - request_started
                        diag_info = (
                            f"on_llm_request已触发({llm_elapsed:.1f}秒前)但on_llm_response未到达，"
                            f"实际聊天LLM调用可能被中断或超时"
                        )
                    else:
                        llm_duration = response_received - request_started
                        diag_info = f"on_llm_response已到达（LLM耗时{llm_duration:.1f}秒），标志清除可能为正常路径遗漏"

                    logger.warning(
                        f"LLM执行中标志主动超时清除 | 群={group_id} "
                        f"已等待{elapsed:.0f}秒 | 诊断: {diag_info}"
                    )
                    del self._llm_running_groups[group_id]
            # 清理诊断数据和定时器引用
            self._llm_request_started_at.pop(group_id, None)
            self._llm_response_received_at.pop(group_id, None)
            self._llm_flag_timers.pop(group_id, None)
        except asyncio.CancelledError:
            # 定时器被取消（正常路径已主动清除标志），无需处理
            pass

    def _get_user_prob(self, sender_id: str) -> float:
        """获取用户概率乘数

        返回值范围 0.0~1.0：
        - 1.0 = 默认，不影响原始概率
        - 0.0 = 永不回复该用户
        - 中间值 = 作为最终概率的乘数
        """
        # P1-6 修复：空/伪 sender ID 返回 0.0，防止 sender 解析失败时默认 1.0 绕过概率 0 限制
        # 场景：getattr(sender, "user_id", "") 在 user_id 为 None 时产生 "None"/""，
        # 这些伪 ID 在 user_prob_overrides 中无对应键，原逻辑返回默认 1.0，
        # 导致被设为概率 0 的用户可因 sender 解析失败被 max() 取 1.0 绕过
        sid = str(sender_id).strip() if sender_id is not None else ""
        if not sid or sid.lower() in ("none", "null"):
            return 0.0
        return self.user_prob_overrides.get(sid, 1.0)

    def _match_keyword(self, text: str) -> str | None:
        """检测消息是否包含关注关键词

        返回匹配到的第一个关键词，未匹配返回 None。
        大小写不敏感。
        """
        if not self.keywords or not text:
            return None
        text_lower = text.lower()
        for kw in self.keywords:
            if kw.lower() in text_lower:
                return kw
        return None

    # ─── 消息缓冲区 ───────────────────────────────────────

    def _get_buffer(self, group_id: str) -> deque:
        """获取指定群的消息缓冲区，自动创建"""
        if group_id not in self._msg_buffer:
            maxlen = max(self.context_messages_count * 2, 40)
            self._msg_buffer[group_id] = deque(maxlen=maxlen)
        return self._msg_buffer[group_id]

    def _record_message(self, event: AstrMessageEvent, meta=None):
        """将群聊消息记录到缓冲区

        记录所有群消息（包括未 @ 机器人的），
        这样唤醒时 LLM 可以看到完整的群聊上下文。
        """
        group_id = event.message_obj.group_id
        if not group_id:
            return

        text = event.message_str

        sender = event.get_sender_name()
        buffer = self._get_buffer(group_id)

        # 检测图片消息：message_str 为空但消息链包含 Image 组件时，记录图片信息
        has_image = False
        image_url = None
        image_comp_desc = ""  # 从 Image 组件属性获取的描述
        is_sticker = False    # 是否为 Sticker 表情包
        if event.message_obj and event.message_obj.message:
            for comp in event.message_obj.message:
                comp_type = type(comp).__name__
                if comp_type == "Image":
                    has_image = True
                    # 尝试获取图片 URL
                    image_url = getattr(comp, "url", None) or getattr(comp, "image_url", None) or getattr(comp, "file", None)
                    # 尝试从 Image 组件属性获取描述（QQ平台框架将描述存储在组件属性中）
                    # 探测多个可能的属性名：desc, description, summary, text, content, caption
                    for attr in ("desc", "description", "summary", "text", "content", "caption"):
                        val = getattr(comp, attr, None)
                        if val and isinstance(val, str) and val.strip():
                            # 排除 QQ 自带的低价值摘要（如 [动画表情]）
                            if attr == "summary" and re.match(r'^\[.+\]$', val.strip()):
                                continue
                            image_comp_desc = val.strip()
                            self._debug(f"图片描述来源 | 属性={attr} 值='{image_comp_desc[:80]}'")
                            break
                    # 调试：打印 Image 组件所有属性，便于排查描述获取问题
                    if self.debug_mode and not image_comp_desc:
                        all_attrs = {k: v for k, v in vars(comp).items() if not k.startswith('_')}
                        self._debug(f"Image组件属性(无描述) | {all_attrs}")
                elif comp_type == "Plain":
                    comp_text = getattr(comp, "text", "")
                    if comp_text.startswith("Sticker:"):
                        is_sticker = True

        if has_image and (not text or not text.strip()):
            # 纯图片消息：分配唯一编码，尝试提取框架生成的图片描述
            image_id = f"img_{uuid.uuid4().hex[:8]}"
            meta = meta or {}
            meta["image_id"] = image_id
            if image_url:
                meta["image_url"] = image_url

            # 优先从 message_str 提取 [Image: 描述] 格式，其次从 Image 组件属性获取
            image_desc = ""
            image_match = re.search(r'\[Image:\s*(.+?)\]', text) if text else None
            if image_match:
                image_desc = image_match.group(1).strip()
            elif image_comp_desc:
                image_desc = image_comp_desc.strip()

            if image_desc:
                # 框架已提供描述：直接记录完整图片信息
                meta["image_pending"] = False
                meta["image_description"] = image_desc
                buffer.append((sender, f"[图片: {image_desc}]", int(time.time()), meta))
                self._debug(f"图片记录(含描述) | 群={group_id} 发送者={sender} image_id={image_id} 描述='{image_desc[:50]}'")
            else:
                # 框架未提供描述：记录占位符，等待自定义模型识别
                meta["image_pending"] = True
                buffer.append((sender, "[图片]", int(time.time()), meta))
                self._debug(f"图片记录(待识别) | 群={group_id} 发送者={sender} image_id={image_id}")

                # 自定义模型模式：异步调用多模态模型识别图片
                if self.image_context_custom_model and image_url:
                    asyncio.ensure_future(self._describe_image_custom(group_id, sender, image_url, image_id, buffer))

            self._stats["total_messages_recorded"] += 1
            return

        if not text or not text.strip():
            return

        # 带文字的图片消息：在文本末尾标注图片编码
        if has_image:
            if meta is None:
                meta = {}
            image_id = f"img_{uuid.uuid4().hex[:8]}"
            meta["image_id"] = image_id
            # 优先从 message_str 提取框架生成的图片描述，其次从 Image 组件属性获取
            image_desc = ""
            image_match = re.search(r'\[Image:\s*(.+?)\]', text) if text else None
            if image_match:
                image_desc = image_match.group(1).strip()
                meta["image_pending"] = False
                meta["image_description"] = image_desc
            elif image_comp_desc:
                image_desc = image_comp_desc.strip()
                meta["image_pending"] = False
                meta["image_description"] = image_desc
            elif is_sticker:
                # Sticker 表情包：无需视觉模型识别，标记为已识别
                meta["image_pending"] = False
            else:
                meta["image_pending"] = True
                if image_url:
                    meta["image_url"] = image_url
                # 自定义模型模式：异步调用多模态模型识别图片
                if self.image_context_custom_model and image_url:
                    asyncio.ensure_future(self._describe_image_custom(group_id, sender, image_url, image_id, buffer))
            self._debug(f"图片记录(带文字) | 群={group_id} 发送者={sender} image_id={image_id} pending={meta.get('image_pending', False)} sticker={is_sticker}")

        # 检测回复关系：如果消息链包含 Reply 组件，提取回复目标的发送者
        if meta is None:
            meta = {}
        reply_to = None
        if event.message_obj and event.message_obj.message:
            from astrbot.api.message_components import Reply, Plain
            for comp in event.message_obj.message:
                if isinstance(comp, Reply):
                    # 尝试从引用消息格式中提取发送者名称
                    # AstrBot 核心将回复解析为 [引用消息(发送者名: 内容)]
                    reply_text = getattr(comp, "text", "") or ""
                    # 匹配 [引用消息(名称: 或 [引用消息(名称/ 或 [引用消息(名称]
                    for name in self.bot_names:
                        if re.search(r'\[引用消息\(' + re.escape(name) + r'[:/\] ]', reply_text):
                            reply_to = name
                            break
                    # 如果引用文本中没有名称，检查 sender_id 是否匹配 BOT
                    if not reply_to:
                        sender_id = getattr(comp, "sender_id", None)
                        if sender_id and str(sender_id) in self._bot_user_ids:
                            reply_to = "BOT"
                    break
        if reply_to:
            meta["reply_to"] = reply_to

        buffer.append((sender, text.strip(), int(time.time()), meta))
        self._stats["total_messages_recorded"] += 1

    async def _describe_image_custom(self, group_id: str, sender: str, image_url: str, image_id: str, buffer):
        """使用自定义多模态模型识别图片内容"""
        try:
            provider = self.context.get_provider_by_id(self.image_context_custom_model_id)
            if not provider:
                logger.warning(f"[ImageContext] 未找到自定义图片识别模型 {self.image_context_custom_model_id}")
                return

            prompt = (
                "请用简洁的中文描述这张图片的内容，重点关注：\n"
                "1. 图片的主体内容和主题\n"
                "2. 如果是表情包/梗图，描述其表达的情绪或含义\n"
                "3. 如果是截图，描述关键信息\n"
                "请控制在50字以内。\n\n"
                f"图片地址：{image_url}"
            )

            resp = await provider.text_chat(
                prompt=prompt,
                session_id=f"img_desc_{group_id}_{int(time.time())}",
            )

            description = ""
            if hasattr(resp, 'completion_text'):
                description = resp.completion_text
            elif hasattr(resp, 'result'):
                description = str(resp.result)
            else:
                description = str(resp)

            if description:
                # 通过 image_id 精确匹配缓冲区中的图片占位符
                for i in range(len(buffer) - 1, -1, -1):
                    _s, _t, _ts, _m = buffer[i]
                    if _m and _m.get("image_id") == image_id and _m.get("image_pending"):
                        new_meta = dict(_m) if _m else {}
                        new_meta["image_pending"] = False
                        new_meta["image_description"] = description.strip()
                        buffer[i] = (_s, f"[图片: {description.strip()}]", _ts, new_meta)
                        self._debug(f"图片识别(自定义) | 群={group_id} image_id={image_id} 描述='{description.strip()[:50]}'")
                        break

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[ImageContext] 自定义图片识别失败: {e}")

    @staticmethod
    def _filter_sticker_noise(text: str) -> str:
        """过滤上下文中的 Sticker emoji 噪音

        Telegram Sticker 的 emoji 与实际贴纸内容大多无关（如贴纸是猫但 emoji 是🤣），
        注入上下文只会误导 LLM。处理策略：
        - "Sticker: 🤣" → "[贴纸]"
        - "[图片] Sticker: 🤣" → "[贴纸]"
        - 保留识图模块生成的描述（如果有）
        """
        if not text:
            return text
        # 匹配 "Sticker: <emoji>" 模式（含可选的前缀如 [图片]）
        text = re.sub(r'\[图片\]\s*Sticker:\s*\S+', '[贴纸]', text)
        text = re.sub(r'Sticker:\s*\S+', '[贴纸]', text)
        return text

    def _format_context(self, group_id: str, incremental: bool = False) -> tuple[str, int, int]:
        """将消息缓冲区格式化为 LLM 可读的上下文文本

        Args:
            group_id: 群ID
            incremental: 是否使用增量模式（只注入新增消息）

        Returns:
            (formatted_text, new_msg_count, old_msg_count)
            - formatted_text: 格式化后的上下文文本
            - new_msg_count: 新增消息数（增量注入时）
            - old_msg_count: 补充的旧消息数
        """
        buffer = self._msg_buffer.get(group_id)
        if not buffer:
            return "", 0, 0

        all_messages = list(buffer)
        new_msg_count = 0
        old_msg_count = 0

        if incremental and self.incremental_context_enabled and group_id in self._last_context_ts:
            # 增量模式：只取上次注入后的新消息
            last_ts = self._last_context_ts[group_id]
            new_messages = []
            old_messages = []

            for msg in all_messages:
                _, _, ts, _meta = msg
                if ts > last_ts:
                    new_messages.append(msg)
                else:
                    old_messages.append(msg)

            new_msg_count = len(new_messages)

            if new_msg_count >= self.incremental_context_min_new:
                # 新增消息足够，只注入新增消息
                messages = new_messages[-self.context_messages_count:]
                new_msg_count = len(messages)
                old_msg_count = 0
            else:
                # 新增消息不足，补充最近的旧消息
                supplement_count = self.context_messages_count - new_msg_count
                supplement = old_messages[-supplement_count:] if supplement_count > 0 else []
                messages = new_messages + supplement
                new_msg_count = len(new_messages)
                old_msg_count = len(supplement)
        else:
            # 全量模式：取最近 context_messages_count 条
            messages = all_messages[-self.context_messages_count:]
            old_msg_count = len(messages)
            new_msg_count = 0

        if not messages:
            return "", 0, 0

        # 统计近期活跃用户（去重，排除 BOT）
        active_users = set()
        active_check_limit = self.recent_rounds_keep * 2
        for msg in all_messages[-active_check_limit:]:
            _s, _t, _ts, _m = msg
            if _m and _m.get("is_bot_message"):
                continue
            if _s and _s not in self.bot_names:
                active_users.add(_s)

        lines = []
        # 在场用户感知：在上下文开头标注近期活跃用户
        if active_users:
            lines.append(f"[近期活跃]: {', '.join(sorted(active_users))}")

        prev_is_bot = False
        prev_bot_time = 0

        for sender, text, _ts, _meta in messages:
            # 过滤过短消息（BOT 消息不过滤，保留 BOT 回复内容要点）
            is_bot_msg = _meta and _meta.get("is_bot_message")
            if not is_bot_msg and self.context_truncation_enabled and len(text.strip()) < self.context_min_length:
                continue

            # Sticker emoji 噪音过滤：Sticker 的 emoji 与实际内容无关，
            # 注入上下文只会误导 LLM，替换为 [贴纸] 标记
            text = self._filter_sticker_noise(text)

            # 截断过长消息
            if self.context_truncation_enabled and len(text) > self.context_truncation_max_len:
                text = text[:self.context_truncation_keep_len] + "..."

            # 对话关系标注
            display_name = sender
            relation_tag = ""

            if _meta and _meta.get("is_bot_message"):
                display_name = "BOT"
                prev_is_bot = True
                prev_bot_time = _ts
            else:
                # 回复关系标注
                if _meta and _meta.get("reply_to"):
                    relation_tag = f" → 回复[{_meta['reply_to']}]"
                elif prev_is_bot and (_ts - prev_bot_time) <= 5:
                    # 紧跟 BOT 消息 5 秒内的用户消息，标注为回应 BOT
                    relation_tag = " (回应BOT)"
                prev_is_bot = False
                prev_bot_time = 0

            # 消息内容歧义消除：当内容以"昵称:"模式开头时，
            # 格式如 [MagicalYu]: gamer:xxx 中的双重冒号会让 LLM 误判 gamer 为实际发言者。
            # 用引号包裹内容，使 LLM 明确区分发送者标注与被引用/转述的内容。
            if re.match(r'^[\w\u4e00-\u9fff]+[:：]', text):
                text = f"「{text}」"

            lines.append(f"[{display_name}]{relation_tag}: {text}")

        return "\n".join(lines), new_msg_count, old_msg_count

    def _filter_command_lines_from_context(self, context_text: str) -> str:
        """过滤 context 中的命令文本行（v1.8.1 新增，修复 P0 根因 2）

        问题：用户发送 /wakeup_proactive 科技资讯 后，命令文本被 _record_message
        写入 _msg_buffer（因为 _record_message 在指令前缀检查之前调用）。
        _format_context 读取后注入 <group_chat_context>，即使 category 降级为
        "活跃气氛"，LLM 仍从 context 看到命令文本并编造"刚刷了一下科技资讯..."

        修复策略：移除 context_text 中包含命令特征的行。
        命令特征：
        - wakeup_proactive
        - 主动发言触发
        - /wakeup_proactive（带斜杠前缀）

        Args:
            context_text: _format_context 返回的格式化上下文文本
        Returns:
            过滤后的 context_text，移除了命令行
        """
        if not context_text:
            return context_text

        # v1.8.1 命令特征关键词列表
        # 注意：这些关键词在正常群聊中几乎不会出现，过滤不会误伤正常消息
        command_keywords = ["wakeup_proactive", "主动发言触发"]

        lines = context_text.split("\n")
        filtered_lines = []
        removed_count = 0

        for line in lines:
            # 检查是否包含命令特征
            is_command_line = False
            for keyword in command_keywords:
                if keyword in line:
                    is_command_line = True
                    break

            if is_command_line:
                removed_count += 1
                logger.debug(f"[ContextFilter] 移除命令行: {line[:80]}")
            else:
                filtered_lines.append(line)

        if removed_count > 0:
            logger.info(
                f"[ContextFilter] 过滤命令文本: 移除 {removed_count} 行 "
                f"(原 {len(lines)} 行 → {len(filtered_lines)} 行)"
            )

        return "\n".join(filtered_lines)

    async def _compress_context(self, context_text: str, group_id: str) -> str:
        """使用小模型对群聊上下文进行摘要压缩

        利用火山平台小模型额度充裕的优势，将群聊上下文压缩后再注入主模型，
        大幅减少主模型的 prompt_tokens 消耗。
        """
        try:
            compress_prompt = (
                "你是一个信息压缩助手。将以下群聊消息压缩为简洁摘要，要求：\n"
                "1. 保留所有关键信息和话题\n"
                "2. 保留发言者昵称\n"
                "3. 去除寒暄、重复和无关内容\n"
                "4. 摘要长度不超过原文的30%\n"
                "5. 【重要】必须保留 BOT 的回复内容要点，特别是 BOT 已回应过的话题和观点，"
                "以便后续对话中 BOT 知道自己已经说过什么，避免重复回应\n"
                "6. 【重要】禁止使用'自己'等代词指代他人行为，必须用具体昵称明确行为主体，"
                "例如'<昵称>吃到撑'而非'吃到撑拿自己垫背'，避免代词指代歧义导致发言者识别错误\n"
                "7. 【重要】转述他人对 BOT 的行为时，必须标注 BOT 为承受方，"
                "例如'<用户>祝BOT父亲节快乐'而非'祝自己父亲节快乐'\n\n"
                f"群聊消息：\n{context_text}"
            )

            # 尝试使用指定压缩模型或当前LLM提供者
            if self.compression_model:
                # 使用指定的压缩模型（提供商ID格式：Volcengine/doubao-seed-2-0-lite-260215）
                provider = self.context.get_provider_by_id(self.compression_model)
                if provider:
                    resp = await provider.text_chat(
                        prompt=compress_prompt,
                        session_id=f"compress_{group_id}",
                    )
                    compressed = resp.completion_text if hasattr(resp, 'completion_text') else str(resp)
                else:
                    logger.warning(
                        f"[ContextCompress] 未找到提供商 {self.compression_model}，回退到当前提供者"
                    )
                    compressed = await self._compress_with_current_provider(compress_prompt)
            else:
                compressed = await self._compress_with_current_provider(compress_prompt)

            if compressed and len(compressed) < len(context_text):
                # 更新压缩统计
                self._stats["compression_stats"]["total_original_chars"] += len(context_text)
                self._stats["compression_stats"]["total_compressed_chars"] += len(compressed)
                self._stats["compression_stats"]["compression_count"] += 1
                logger.info(
                    f"[ContextCompress] 群={group_id} 原始={len(context_text)}字符 → "
                    f"压缩={len(compressed)}字符 ({len(compressed)/len(context_text)*100:.0f}%)"
                )
                return compressed
            else:
                # 压缩失败或压缩后更长，返回原文
                self._debug(f"[ContextCompress] 群={group_id} 压缩未生效，使用原文")
                return context_text

        except asyncio.CancelledError:
            # 诊断（v1.7.3）：记录完整 traceback，定位 CancelledError 真实来源
            import traceback as _tb
            logger.warning(
                f"[ContextCompress] 群={group_id} 压缩被取消（CancelledError）\n"
                f"Traceback:\n{_tb.format_exc()}"
            )
            return context_text
        except Exception as e:
            logger.warning(f"[ContextCompress] 群={group_id} 压缩异常: {e}")
            return context_text

    async def _compress_with_current_provider(self, prompt: str) -> str:
        """使用当前LLM提供者进行压缩"""
        try:
            provider = self.context.get_using_provider()
            if provider:
                resp = await provider.text_chat(
                    prompt=prompt,
                    session_id=f"compress_{int(time.time())}",
                )
                if hasattr(resp, 'completion_text'):
                    return resp.completion_text
                elif hasattr(resp, 'result'):
                    return str(resp.result)
                else:
                    return str(resp)
        except asyncio.CancelledError:
            raise  # 向上传播，由 _compress_context 处理
        except Exception as e:
            logger.warning(f"[ContextCompress] 当前提供者压缩失败: {e}")
        return ""

    def _determine_routing_model(self, event: AstrMessageEvent) -> str:
        """根据消息特征决定路由到哪个模型

        路由规则（纯规则判定，无需额外 LLM 调用）：
        - 概率唤醒 + 消息 < 20 字符 → 小模型（简单寒暄）
        - 冷场救场 → 小模型（主动发起话题，无需强推理）
        - 名称触发 + 消息 < 15 字符 → 小模型（简单回应）
        - 名称触发 + 包含问号 + 消息 > 20 字符 → 大模型（复杂问题）
        - 关键词触发 → 大模型（关键词通常指向重要内容）
        - 消息 > 50 字符 → 大模型（长文本需要强理解力）
        - 默认 → 大模型

        Returns:
            模型名称，空字符串表示使用默认大模型
        """
        if not self.model_routing_enabled or not self.routing_small_model:
            return ""  # 不路由，使用默认模型

        wakeup_type = event.get_extra("wakeup_type") or "name_trigger"
        message_str = event.message_str or ""
        message_len = len(message_str)

        # 冷场救场 → 小模型
        if wakeup_type == "dead_chat_rescue":
            self._debug(f"[ModelRoute] 冷场救场 → 小模型")
            return self.routing_small_model

        # 概率唤醒 + 短消息 → 小模型
        if wakeup_type == "probability_wakeup" and message_len < 20:
            self._debug(f"[ModelRoute] 概率唤醒+短消息({message_len}字符) → 小模型")
            return self.routing_small_model

        # 名称触发 + 短消息（无问号）→ 小模型
        if wakeup_type == "name_trigger" and message_len < 15 and "?" not in message_str and "？" not in message_str:
            self._debug(f"[ModelRoute] 名称触发+短消息({message_len}字符) → 小模型")
            return self.routing_small_model

        # 关键词触发 → 大模型
        if wakeup_type == "keyword_trigger":
            self._debug(f"[ModelRoute] 关键词触发 → 大模型")
            return ""

        # 长消息 → 大模型
        if message_len > 50:
            self._debug(f"[ModelRoute] 长消息({message_len}字符) → 大模型")
            return ""

        # 包含问号的中等消息 → 大模型
        if "?" in message_str or "？" in message_str:
            self._debug(f"[ModelRoute] 含问号 → 大模型")
            return ""

        # 默认 → 大模型
        self._debug(f"[ModelRoute] 默认 → 大模型")
        return ""

    def _check_token_anomaly(self):
        """检查 Token 消耗异常

        滑动窗口统计：记录每小时 token 消耗，计算均值和标准差。
        当当前小时消耗超过均值 + Nσ 时，输出告警日志。
        同时检查 prompt/completion 比率。
        """
        if not self.anomaly_detection_enabled:
            return

        hourly_data = self._stats["hourly_tokens"]
        if len(hourly_data) < 3:
            return  # 数据不足

        import statistics
        totals = [d["total"] for d in hourly_data.values()]
        if len(totals) < 3:
            return

        mean = statistics.mean(totals)
        std = statistics.stdev(totals)

        if std <= 0:
            return

        # 当前小时检查
        current_hour = datetime.now().strftime("%Y-%m-%dT%H")
        current_data = hourly_data.get(current_hour)
        if not current_data:
            return

        current_total = current_data["total"]
        z_score = (current_total - mean) / std

        if z_score > self.anomaly_sigma_threshold * 1.5:
            # 超过 3σ → ERROR
            logger.error(
                f"[TokenAnomaly] Token消耗严重异常! "
                f"当前小时={self._fmt_tokens(current_total)} "
                f"均值={self._fmt_tokens(int(mean))} "
                f"Z-score={z_score:.2f} "
                f"偏差={(current_total - mean) / mean * 100:+.1f}%"
            )
        elif z_score > self.anomaly_sigma_threshold:
            # 超过 2σ → WARNING
            logger.warning(
                f"[TokenAnomaly] Token消耗异常 "
                f"当前小时={self._fmt_tokens(current_total)} "
                f"均值={self._fmt_tokens(int(mean))} "
                f"Z-score={z_score:.2f} "
                f"偏差={(current_total - mean) / mean * 100:+.1f}%"
            )

        # prompt/completion 比率检查
        total_prompt = self._stats["total_prompt_tokens"]
        total_all = self._stats["total_tokens"]
        if total_all > 10000 and self._stats["llm_call_count"] > 5:
            prompt_ratio = total_prompt / total_all
            if prompt_ratio > self.anomaly_prompt_ratio_threshold:
                logger.warning(
                    f"[TokenAnomaly] prompt占比过高: {prompt_ratio:.1%} "
                    f"(阈值={self.anomaly_prompt_ratio_threshold:.1%})，"
                    f"建议优化上下文注入"
                )

    def _cleanup_expired_buffers(self):
        """清理过期的缓冲区数据

        清理策略：
        1. 删除超过 24 小时没有任何新消息的群缓冲区（整个群）
        2. 对活跃群缓冲区，移除超过 24 小时的单条消息
        """
        now = int(time.time())
        cutoff = now - self.BUFFER_MAX_AGE_SECONDS
        expired_groups = []

        for group_id, buffer in list(self._msg_buffer.items()):
            if not buffer:
                expired_groups.append(group_id)
                continue

            # 检查最新一条消息的时间，如果整个缓冲区都过期了，删除整个群
            newest_time = buffer[-1][2] if buffer else 0
            if newest_time < cutoff:
                expired_groups.append(group_id)
                continue

            # 移除单条过期消息（从左侧即最旧的开始）
            while buffer and buffer[0][2] < cutoff:
                buffer.popleft()

        # 删除完全过期的群缓冲区
        for group_id in expired_groups:
            del self._msg_buffer[group_id]
            # P1-5 修复：同步清理其他按群状态字典，防止临时群/不活跃群状态永久驻留
            # 取消防抖定时器任务（避免悬挂 task）
            state = self._debounce_states.pop(group_id, None)
            if state and state.timer_task is not None and not state.timer_task.done():
                state.timer_task.cancel()
            self._last_context_ts.pop(group_id, None)
            self._conversation_history.pop(group_id, None)
            self._conversation_summaries.pop(group_id, None)
            self._summary_checkpoint.pop(group_id, None)
            self._sent_content_cache.pop(group_id, None)
            self._last_bot_reply_text.pop(group_id, None)
            self._last_bot_reply_time.pop(group_id, None)
            self._energy_states.pop(group_id, None)
            self._flow_states.pop(group_id, None)
            self._rescue_states.pop(group_id, None)
            self._light_response_last.pop(group_id, None)
            self._light_response_count.pop(group_id, None)
            self._light_response_hour_reset.pop(group_id, None)
            # LLM 执行中标志：先取消定时器再删除标志
            self._cancel_llm_flag_timer(group_id)
            self._llm_running_groups.pop(group_id, None)
            # ─── v1.5.0 主动发言：同步清理主动发言相关状态，防止悬挂数据 ───
            self._group_umo.pop(group_id, None)
            self._proactive_last_speak.pop(group_id, None)
            self._proactive_daily_count.pop(group_id, None)
            self._proactive_retreat.pop(group_id, None)
            self._proactive_states.pop(group_id, None)
            self._proactive_skip_streak.pop(group_id, None)
            # ─── v1.6.0 Phase 2/3 新增数据结构清理 ───
            self._proactive_response_tracker.pop(group_id, None)
            self._proactive_topic_history.pop(group_id, None)
            self._proactive_outcomes.pop(group_id, None)
            logger.info(f"缓冲区清理: 已删除群 {group_id} 的过期缓冲区及所有状态")

        # ─── v1.5.0 主动发言：跨日数据与过期退让状态清理（针对持续活跃群） ───
        # 1. 清理过期的每日计数（跨日数据）：只保留今天的计数，旧日期全部清除
        #    设计文档 §6.3.3 要求：避免长期运行时 _proactive_daily_count 累积旧日期键造成内存泄漏
        today = datetime.now().strftime("%Y-%m-%d")
        expired_daily_groups = []
        for gid in list(self._proactive_daily_count.keys()):
            self._proactive_daily_count[gid] = {
                d: c for d, c in self._proactive_daily_count[gid].items() if d == today
            }
            if not self._proactive_daily_count[gid]:
                expired_daily_groups.append(gid)
        for gid in expired_daily_groups:
            del self._proactive_daily_count[gid]

        # 2. 清理过期的退让状态：退让期已结束的群自动清除
        #    设计文档 §6.3.3 要求：_is_in_retreat 隐式清理的显式补充，防止长期不被调度的群残留退让状态
        expired_retreat_groups = []
        for gid in list(self._proactive_retreat.keys()):
            if time.time() >= self._proactive_retreat[gid]["until"]:
                expired_retreat_groups.append(gid)
        for gid in expired_retreat_groups:
            del self._proactive_retreat[gid]

        if expired_daily_groups or expired_retreat_groups:
            logger.debug(
                f"主动发言状态清理: 跨日计数清理 {len(expired_daily_groups)} 群, "
                f"过期退让清理 {len(expired_retreat_groups)} 群"
            )

        self._stats["total_cleanups"] += 1
        self._stats["last_cleanup_time"] = now

        if expired_groups:
            logger.info(
                f"缓冲区清理完成: 删除了 {len(expired_groups)} 个过期群缓冲区，"
                f"当前活跃群数: {len(self._msg_buffer)}"
            )

    def _maybe_cleanup(self):
        """检查是否需要执行定期清理，如果距上次清理超过间隔则执行"""
        now = int(time.time())
        last = self._stats["last_cleanup_time"]
        if last == 0 or (now - last) >= self.CLEANUP_INTERVAL_SECONDS:
            self._cleanup_expired_buffers()

    # ─── 群过滤 ────────────────────────────────────────────

    def _get_group_param(self, group_id: str, param_name: str, default_value):
        """获取群组特定参数，优先使用群组覆盖值，否则使用全局默认值

        支持覆盖的参数：
        - energy_decay_rate: 精力消耗速率
        - energy_recovery_rate: 精力恢复速率
        - flow_bystander_prob: 旁观状态回复概率
        - flow_attentive_prob: 关注状态回复概率
        - flow_flow_prob: 心流状态回复概率
        - engagement_decay_per_minute: 参与度衰减速率
        - engagement_refresh_on_reply: 概率唤醒回复时参与度刷新量
        - fatigue_coefficient: 疲劳系数（每轮对话增加的防抖倍率）
        - fatigue_max_multiplier: 疲劳最大倍率上限
        - rescue_idle_threshold: 冷场判定时间
        - rescue_cooldown: 冷场救场冷却时间
        - debounce_wait_name: 名称触发等待时间
        - debounce_wait_prob: 概率唤醒等待时间
        - debounce_wait_rescue: 冷场救场等待时间
        - keyword_reply_prob: 关键词回复概率
        - proactive_enabled: 主动发言开关（v1.5.0 新增）
        - proactive_cooldown: 主动发言冷却（v1.5.0 新增）
        - proactive_daily_limit: 主动发言每日上限（v1.5.0 新增）
        - proactive_probability: 主动发言触发概率（v1.5.0 新增）
        - proactive_min_energy: 主动发言最少精力值（v1.5.0 新增）
        """
        overrides = self.group_overrides.get(str(group_id), {})
        if param_name in overrides:
            value = overrides[param_name]
        else:
            value = default_value

        # 安全下限：防止极端配置导致冷场救场误触发或主动发言刷屏
        _MIN_VALUES = {
            "rescue_idle_threshold": 60,   # 冷场判定最低60秒，避免用户连续发言时误触发
            "rescue_cooldown": 60,         # 冷却期最低60秒，避免短时间内重复救场
            # 主动发言安全下限（v1.5.0 新增）
            "proactive_cooldown": 600,          # 冷却最低 10 分钟，避免刷屏
            "proactive_check_interval": 300,    # 检查间隔最低 5 分钟，避免过于频繁（设计文档 §6.5.1 声明，虽然此参数不通过 group_overrides 覆盖，仍声明以保证文档一致性）
            "proactive_daily_limit": 1,         # 每日上限最低 1 次，至少允许一次
            "proactive_min_energy": 0.0,        # 精力下限最低 0，不强制要求精力
        }
        if param_name in _MIN_VALUES:
            min_val = _MIN_VALUES[param_name]
            if isinstance(value, (int, float)) and value < min_val:
                logger.warning(f"参数安全下限: {param_name}={value} 低于最低值 {min_val}，已自动修正")
                value = min_val

        return value

    def _is_group_allowed(self, group_id: str) -> bool:
        """检查群是否允许触发唤醒

        黑名单优先级最高，其次白名单，最后默认允许。
        """
        if not group_id:
            return False
        if group_id in self.blocked_groups:
            return False
        if self.whitelist_enabled and group_id not in self.enabled_groups:
            return False
        return True

    # ─── 精力系统 ──────────────────────────────────────────

    def _get_energy(self, group_id: str) -> ChatEnergy:
        if group_id not in self._energy_states:
            self._energy_states[group_id] = ChatEnergy()
        return self._energy_states[group_id]

    def _consume_energy(self, group_id: str):
        state = self._get_energy(group_id)
        # 先恢复再消耗，确保计算准确
        self._recover_energy(group_id)
        decay_rate = self._get_group_param(group_id, "energy_decay_rate", self.energy_decay_rate)
        state.energy = max(0.1, state.energy - decay_rate)
        state.last_reply_time = time.time()
        state.total_replies += 1
        logger.info(f"精力消耗: 群 {group_id} 精力降至 {state.energy:.2f}")

    def _recover_energy(self, group_id: str):
        state = self._get_energy(group_id)
        if state.last_reply_time == 0:
            return
        now = time.time()
        elapsed = now - state.last_reply_time
        recovery_rate = self._get_group_param(group_id, "energy_recovery_rate", self.energy_recovery_rate)
        recovery = recovery_rate * (elapsed / 60.0)
        if recovery > 0:
            state.energy = min(1.0, state.energy + recovery)

    # ─── 心流状态机 ────────────────────────────────────────

    def _get_flow(self, group_id: str) -> ChatFlowState:
        if group_id not in self._flow_states:
            now = time.time()
            self._flow_states[group_id] = ChatFlowState(
                state_enter_time=now,
                window_start_time=now,
            )
        return self._flow_states[group_id]

    def _update_flow_state(self, group_id: str, event: AstrMessageEvent):
        flow = self._get_flow(group_id)
        energy = self._get_energy(group_id)

        # 先恢复精力
        self._recover_energy(group_id)

        # 1. 更新活跃度（滑动窗口）
        now = time.time()
        if now - flow.window_start_time > self.FLOW_ACTIVITY_WINDOW:
            # 窗口过期，重置
            flow.message_count_in_window = 1
            flow.window_start_time = now
        else:
            flow.message_count_in_window += 1

        # 参与度时间衰减
        if flow.engagement > 0:
            if flow.engagement_last_update > 0:
                elapsed_minutes = (now - flow.engagement_last_update) / 60.0
                if elapsed_minutes > 0:
                    decay_rate = self._get_group_param(group_id, "engagement_decay_per_minute", self.engagement_decay_per_minute)
                    flow.engagement = max(0.0, flow.engagement - elapsed_minutes * decay_rate)
                    if flow.engagement <= 0:
                        flow.engagement = 0.0
                        flow.conversation_turns = 0
                        self._debug(f"参与度归零 | 群={group_id} 对话轮数重置")
                    else:
                        self._debug(f"参与度衰减 | 群={group_id} 参与度={flow.engagement:.2f} 衰减={elapsed_minutes:.1f}分钟×{decay_rate}")
            flow.engagement_last_update = now

        activity = flow.message_count_in_window

        # 2. 更新话题相关度
        flow.relevance_score = self._calc_relevance(event)

        # 3. 精力强制降级
        if energy.energy < 0.3 and flow.state in (FlowState.FLOW, FlowState.ATTENTIVE):
            self._transition_flow(group_id, FlowState.FATIGUED, "精力不足")
            return

        # 4. 状态转换（最少停留时间）
        time_in_state = now - flow.state_enter_time
        if time_in_state < self.MIN_STATE_DURATION:
            return

        if flow.state == FlowState.BYSTANDER:
            if activity >= 3 and flow.relevance_score >= 0.2:
                self._transition_flow(group_id, FlowState.ATTENTIVE, "群聊活跃+话题相关")
            elif activity >= 8:
                self._transition_flow(group_id, FlowState.ATTENTIVE, "群聊非常活跃")

        elif flow.state == FlowState.ATTENTIVE:
            if activity >= 10 and flow.relevance_score >= 0.5 and energy.energy >= 0.5:
                self._transition_flow(group_id, FlowState.FLOW, "高活跃+高相关+精力充足")
            elif activity < 3:
                self._transition_flow(group_id, FlowState.BYSTANDER, "群聊冷清")

        elif flow.state == FlowState.FLOW:
            if energy.energy < 0.4:
                self._transition_flow(group_id, FlowState.FATIGUED, "精力下降")
            elif activity < 5:
                self._transition_flow(group_id, FlowState.ATTENTIVE, "活跃度下降")

        elif flow.state == FlowState.FATIGUED:
            if energy.energy >= 0.6 and activity < 5:
                self._transition_flow(group_id, FlowState.BYSTANDER, "精力恢复+群聊平静")

    def _transition_flow(self, group_id: str, new_state: FlowState, reason: str):
        flow = self._get_flow(group_id)
        old_state = flow.state
        flow.state = new_state
        flow.state_enter_time = time.time()
        logger.info(f"心流转换: 群 {group_id} {old_state.value}→{new_state.value}（{reason}）")

    def _calc_relevance(self, event: AstrMessageEvent) -> float:
        text = (event.message_str or "").lower()
        for name in self.bot_names:
            if name.lower() in text:
                return 1.0
        return 0.0

    # ─── 概率唤醒 ──────────────────────────────────────────

    async def _check_probability_wakeup(self, event: AstrMessageEvent):
        group_id = event.message_obj.group_id
        flow = self._get_flow(group_id)
        energy = self._get_energy(group_id)
        sender_id = str(getattr(event.message_obj.sender, "user_id", ""))

        # v1.8.5 新增：退让状态检查（避免概率唤醒绕过递进退让机制）
        # 与 _should_proactive_speak 第 1 步退让状态检查保持一致
        # 原因：bot 连续多次发言无人回应时已进入退让状态（2h/6h/24h），
        #       此时即使有用户消息到达，也不应通过概率唤醒自动回复，
        #       否则会形成"发言→无人回应→下一条用户消息→概率唤醒→又发言"死循环
        if self._is_in_retreat(group_id):
            self._debug(f"概率唤醒 | 群={group_id} 退让状态中（连续无人回应），跳过")
            return

        # LLM 执行中检查：防止概率唤醒并发触发重复输出
        if group_id in self._llm_running_groups:
            elapsed = time.time() - self._llm_running_groups[group_id]
            if elapsed > self._LLM_FLAG_TIMEOUT:
                logger.warning(f"概率唤醒 | LLM执行中标志超时清除 群={group_id} 已等待{elapsed:.0f}秒")
                del self._llm_running_groups[group_id]
                self._cancel_llm_flag_timer(group_id)
            else:
                self._debug(f"概率唤醒 | 群={group_id} LLM执行中({elapsed:.0f}秒)，跳过")
                return

        # 用户概率检查：防抖聚合多条消息时，取所有发送者中的最大概率
        # 避免最后一条消息的发送者概率为0时，阻止了其他用户的正常触发
        aggregated_senders = event.get_extra("aggregated_sender_ids")
        if aggregated_senders:
            user_prob = max(self._get_user_prob(sid) for sid in aggregated_senders)
        else:
            user_prob = self._get_user_prob(sender_id)
        if user_prob <= 0:
            self._debug(f"概率唤醒 | 所有发送者概率均为0，跳过")
            return

        # 疲劳状态不触发概率唤醒
        if flow.state == FlowState.FATIGUED:
            self._debug(f"概率唤醒 | 群={group_id} 疲劳状态，跳过概率唤醒")
            return

        self._stats["probability_checks"] += 1

        # 计算动态概率，乘以用户概率乘数
        prob = self._calc_dynamic_probability(group_id) * user_prob

        # 复读抑制：检测聚合文本是否为复读
        if self.repeat_suppress_enabled:
            # 优先使用防抖阶段的复读检测结果（已检查所有暂存消息）
            has_repeat = event.get_extra("debounce_has_repeat")
            repeat_info = event.get_extra("debounce_repeat_info") or ""
            if has_repeat is None:
                # 非防抖路径（如无防抖或直接判定），使用当前消息检测
                message_str = event.message_str or ""
                sender_name = event.get_sender_name() or ""
                has_repeat, repeat_info = self._is_repeat_message(group_id, sender_name, message_str)
            if has_repeat:
                original_prob = prob
                prob *= self.repeat_suppress_factor
                self._debug(f"复读抑制 | 原始概率={original_prob:.4f} 抑制系数={self.repeat_suppress_factor} 抑制后={prob:.4f} 匹配详情={repeat_info}")

        roll = random.random()

        if roll < prob:
            logger.info(
                f"概率唤醒: 群 {group_id} 概率 {prob:.4f}(用户乘数×{user_prob:.2f}) 掷骰 {roll:.4f} → 命中！"
            )
            self._trigger_wake(event)
            self._debug(f"概率唤醒命中 | 已调用 _trigger_wake is_at_or_wake_command={event.is_at_or_wake_command} message_str='{(event.message_str or '')[:40]}'")
            event.set_extra("smart_wakeup_triggered", True)
            event.set_extra("wakeup_type", "probability_wakeup")
            self._consume_energy(group_id)
            self._stats["total_wakeups"] += 1
            self._stats["probability_wakeups"] += 1
            self._stats["probability_passed"] += 1
            # 概率唤醒参与度刷新
            flow = self._get_flow(group_id)
            flow.engagement = min(1.0, flow.engagement + self._get_group_param(group_id, "engagement_refresh_on_reply", self.engagement_refresh_on_reply))
            flow.conversation_turns += 1
            flow.engagement_last_update = time.time()
        else:
            logger.debug(
                f"概率唤醒: 群 {group_id} 概率 {prob:.4f} 掷骰 {roll:.4f} → 未命中"
            )

    def _calc_dynamic_probability(self, group_id: str) -> float:
        flow = self._get_flow(group_id)
        energy = self._get_energy(group_id)

        # 先恢复精力
        self._recover_energy(group_id)

        # base_prob 由心流状态决定，支持群组覆盖
        # 参与度插值：从旁观概率平滑过渡到关注概率
        # 使用平方曲线使插值更保守，避免高参与度时旁观概率被拉得过高
        if flow.state == FlowState.BYSTANDER and flow.engagement > 0:
            bystander_prob = self._get_group_param(group_id, "flow_bystander_prob", self.flow_bystander_prob)
            attentive_prob = self._get_group_param(group_id, "flow_attentive_prob", self.flow_attentive_prob)
            engagement_factor = flow.engagement ** 2  # 平方曲线：参与度越高，边际增益越小
            base_prob = bystander_prob + (attentive_prob - bystander_prob) * engagement_factor
        elif flow.state == FlowState.BYSTANDER:
            base_prob = self._get_group_param(group_id, "flow_bystander_prob", self.flow_bystander_prob)
        elif flow.state == FlowState.ATTENTIVE:
            base_prob = self._get_group_param(group_id, "flow_attentive_prob", self.flow_attentive_prob)
        elif flow.state == FlowState.FLOW:
            base_prob = self._get_group_param(group_id, "flow_flow_prob", self.flow_flow_prob)
        else:
            return 0.0  # FATIGUED

        energy_factor = energy.energy
        timing_factor = self._calc_timing_factor(group_id)

        prob = base_prob * energy_factor * timing_factor
        engagement_info = f" 参与度={flow.engagement:.2f}" if flow.engagement > 0 else ""
        logger.debug(
            f"概率计算: 群 {group_id} | "
            f"状态={flow.state.value} base={base_prob:.3f} "
            f"精力={energy_factor:.2f} "
            f"时间因子={timing_factor:.2f}{engagement_info} → 最终={prob:.4f}"
        )
        return min(prob, 1.0)  # 上限为1

    def _calc_timing_factor(self, group_id: str) -> float:
        """计算时间因子

        时间因子反映"距上次回复的时间间隔"对回复意愿的影响：
        - 长时间未回复 → 因子高（更想说话）
        - 刚回复过 → 因子低（不需要急着再说）

        但在活跃对话场景中（关注/心流状态，或参与度期间），
        "刚回复过"不应成为降低概率的理由——连续对话中bot应保持参与。
        """
        energy = self._get_energy(group_id)
        if energy.last_reply_time == 0:
            return 1.5

        # 活跃对话场景：关注/心流状态或参与度期间，不因刚回复而惩罚
        flow = self._get_flow(group_id)
        if flow.state in (FlowState.ATTENTIVE, FlowState.FLOW) or flow.engagement > 0:
            # 活跃对话中，时间因子不低于1.0
            elapsed = time.time() - energy.last_reply_time
            if elapsed < 300:
                return 1.0
            elif elapsed < 1800:
                return 1.0 + (elapsed - 300) / 3000  # 5~35分钟从1.0缓升至1.5
            else:
                return 1.5

        # 旁观状态：正常衰减逻辑
        elapsed = time.time() - energy.last_reply_time
        if elapsed < 300:
            return 0.5
        elif elapsed < 1800:
            return 0.5 + (elapsed - 300) / 1500
        else:
            return 2.0

    # ─── 冷场救场 ──────────────────────────────────────────

    async def _check_dead_chat_rescue(self, event: AstrMessageEvent, silence_gap: float = 0.0):
        group_id = event.message_obj.group_id

        now = time.time()
        flow = self._get_flow(group_id)
        energy = self._get_energy(group_id)

        # 先恢复精力
        self._recover_energy(group_id)

        # LLM 执行中检查：防止冷场救场并发触发重复输出
        # 超时安全清除：如果标志存在超过超时阈值，说明 after_message_sent 未被调用（LLM失败/结果为空），
        # 自动清除标志防止冷场救场永久阻塞
        if group_id in self._llm_running_groups:
            elapsed = time.time() - self._llm_running_groups[group_id]
            if elapsed > self._LLM_FLAG_TIMEOUT:
                logger.warning(f"LLM执行中标志超时清除 | 群={group_id} 已等待{elapsed:.0f}秒，自动清除")
                del self._llm_running_groups[group_id]
                self._cancel_llm_flag_timer(group_id)
            else:
                self._debug(f"冷场救场 | 群={group_id} LLM执行中({elapsed:.0f}秒)，跳过")
                return

        # v1.8.4 新增：静默时段检查（避免深夜冷场救场绕过静默规则）
        # 与 _should_proactive_speak 的静默时段检查保持一致
        current_time = datetime.now().strftime("%H:%M")
        if self._is_in_quiet_hours(group_id, current_time):
            self._debug(f"冷场救场 | 群={group_id} 静默时段内({current_time})，跳过")
            return

        # v1.8.5 新增：退让状态检查（避免冷场救场绕过递进退让机制）
        # 与 _should_proactive_speak 第 1 步退让状态检查保持一致
        # 原因：bot 连续多次发言无人回应时已进入退让状态（2h/6h/24h），
        #       此时即使群里冷场也不应救场，否则会形成"发言→冷场→救场→发言"死循环
        if self._is_in_retreat(group_id):
            self._debug(f"冷场救场 | 群={group_id} 退让状态中（连续无人回应），跳过")
            return

        # 疲劳状态不执行冷场救场
        if flow.state == FlowState.FATIGUED:
            self._debug(f"冷场救场 | 群={group_id} 疲劳状态，跳过")
            return

        # 精力不足不执行
        if energy.energy < 0.2:
            self._debug(f"冷场救场 | 群={group_id} 精力不足({energy.energy:.2f}<0.2)，跳过")
            return

        # 计算静默间隔：优先使用防抖状态记录的间隔（准确），
        # 否则从缓冲区计算（适用于非防抖场景）
        idle_gap = silence_gap
        if idle_gap <= 0:
            buffer = self._msg_buffer.get(group_id)
            if not buffer or len(buffer) < 2:
                self._debug(f"冷场救场 | 群={group_id} 缓冲区不足2条，跳过")
                return
            idle_gap = buffer[-1][2] - buffer[-2][2]

        # 冷场判定：静默间隔超过阈值
        idle_threshold = self._get_group_param(group_id, "rescue_idle_threshold", self.rescue_idle_threshold)
        if idle_gap < idle_threshold:
            self._debug(f"冷场救场 | 群={group_id} 静默间隔={idle_gap:.0f}秒 < 阈值={idle_threshold}秒，未达冷场")
            return

        # 冷却期检查
        rescue_state = self._rescue_states.get(group_id)
        if not rescue_state:
            rescue_state = ChatRescueState()
            self._rescue_states[group_id] = rescue_state

        cooldown = self._get_group_param(group_id, "rescue_cooldown", self.rescue_cooldown)
        if (now - rescue_state.last_rescue_time) < cooldown:
            self._debug(f"冷场救场 | 群={group_id} 冷却中(距上次{now - rescue_state.last_rescue_time:.0f}秒 < {cooldown}秒)，跳过")
            return

        # 执行冷场救场
        rescue_state.last_rescue_time = now
        rescue_state.total_rescues += 1
        logger.info(
            f"冷场救场: 群 {group_id} 冷场 {self._format_duration(int(idle_gap))}，主动参与"
        )
        self._trigger_wake(event)
        event.set_extra("smart_wakeup_triggered", True)
        event.set_extra("wakeup_type", "dead_chat_rescue")
        self._consume_energy(group_id)
        self._stats["total_wakeups"] += 1
        self._stats["rescue_wakeups"] += 1
        # 冷场救场参与度激活
        flow = self._get_flow(group_id)
        flow.engagement = min(1.0, max(0.5, flow.engagement + self._get_group_param(group_id, "engagement_refresh_on_reply", self.engagement_refresh_on_reply)))
        flow.conversation_turns += 1
        flow.engagement_last_update = time.time()

    # ─── 消息防抖 ──────────────────────────────────────────

    async def _debounce_message(self, event: AstrMessageEvent):
        """防抖处理：暂存消息，在管道内等待计时器到期后判定

        关键设计：防抖等待在 on_group_message 的管道内执行（而非后台任务），
        确保 _trigger_wake 修改 event.is_at_or_wake_command 时管道仍在处理该事件，
        核心管道能正确识别唤醒请求并调用 LLM。
        """
        group_id = event.message_obj.group_id
        sender = event.get_sender_name()
        text = event.message_str or ""
        now = time.time()

        # 获取或创建防抖状态
        if group_id not in self._debounce_states:
            self._debounce_states[group_id] = DebounceState(pending_messages=[])
        state = self._debounce_states[group_id]

        # 首条消息到达时，计算与上一条消息的静默间隔
        if not state.pending_messages:
            buffer = self._msg_buffer.get(group_id)
            if buffer and len(buffer) >= 2:
                # buffer[-1] 是刚记录的当前消息，buffer[-2] 是上一条消息
                state.silence_gap = buffer[-1][2] - buffer[-2][2]
            else:
                state.silence_gap = 0.0

        # 将消息暂存
        state.pending_messages.append((sender, text.strip(), now, event))
        state.last_msg_time = now
        state.last_msg_sender = sender

        # 取消已有的防抖等待任务（前一条消息的 on_group_message 正在等待）
        if state.timer_task is not None and not state.timer_task.done():
            state.timer_task.cancel()
            self._stats["debounce_cancelled"] += 1

        # 确定等待时间（传入已有暂存消息，以便检查名称匹配）
        wait_time = self._calc_debounce_wait(group_id, event, state.pending_messages)

        # 记录当前任务，以便下一条消息到来时能取消本任务的等待
        state.timer_task = asyncio.current_task()

        logger.debug(f"防抖: 群 {group_id} 暂存消息，等待 {wait_time}秒")
        self._debug(f"防抖暂存 | 群={group_id} 等待={wait_time}秒 已暂存={len(state.pending_messages)}条")

        # 在管道内等待防抖计时器
        try:
            await asyncio.sleep(wait_time)
        except asyncio.CancelledError:
            # 被新消息重置，本消息的管道处理让位给新消息
            return

        # 计时器到期，执行判定
        self._stats["debounce_fired"] += 1

        # 取出所有暂存消息
        messages = state.pending_messages.copy()
        state.pending_messages.clear()

        # 使用最后一条消息的 event 进行判定
        last_event = messages[-1][3]

        # 聚合消息文本
        aggregated_text = self._aggregate_messages(messages)

        # 将聚合文本存入 event extra，供 on_llm_request 使用
        last_event.set_extra("aggregated_text", aggregated_text)
        last_event.set_extra("aggregated_count", len(messages))

        # 收集所有发送者ID，供概率唤醒的用户概率检查使用
        sender_ids = list(set(
            str(getattr(evt.message_obj.sender, "user_id", ""))
            for _sender, _text, _ts, evt in messages
        ))
        last_event.set_extra("aggregated_sender_ids", sender_ids)

        # 多条消息聚合时，将 message_str 替换为聚合文本
        # 这样 _trigger_wake 添加前缀后，LLM 收到的输入是完整的聚合内容
        # 而非仅最后一条消息
        if len(messages) > 1:
            last_event.message_str = aggregated_text

        logger.info(f"防抖触发: 群 {group_id} 聚合 {len(messages)} 条消息")
        self._debug(f"防抖触发 | 群={group_id} 聚合{len(messages)}条 静默间隔={state.silence_gap:.1f}秒 聚合文本='{aggregated_text[:60]}'")

        # 执行判定，传入首条消息到达时的静默间隔
        try:
            await self._evaluate_debounced_messages(group_id, last_event, messages, state.silence_gap)
        except Exception as e:
            logger.error(f"防抖判定异常: 群 {group_id} 错误: {e}", exc_info=True)

    def _calc_debounce_wait(self, group_id: str, event: AstrMessageEvent, pending_messages: list = None) -> float:
        """计算自适应等待时间"""
        # 检查是否命中名称：遍历所有暂存消息（名称可能出现在任意一条中）
        # 但跳过复读消息：复读内容包含名称时不应缩短防抖等待时间
        name_matched = False
        if pending_messages:
            for _sender, msg_text, _ts, _evt in pending_messages:
                # 跳过复读消息
                if self.repeat_suppress_enabled and self._is_repeat_message(group_id, _sender, msg_text)[0]:
                    continue
                msg_lower = msg_text.lower()
                if any(name.lower() in msg_lower for name in self.bot_names):
                    name_matched = True
                    break
        if not name_matched:
            # 也检查当前消息（同样跳过复读）
            message_str = event.message_str or ""
            message_lower = message_str.lower()
            sender_name = event.get_sender_name() or ""
            is_repeat = self.repeat_suppress_enabled and self._is_repeat_message(group_id, sender_name, message_str)[0]
            name_matched = not is_repeat and any(name.lower() in message_lower for name in self.bot_names)

        if name_matched:
            base_wait = float(self._get_group_param(group_id, "debounce_wait_name", self.debounce_wait_name))
        # 检查是否可能是冷场救场
        elif self._msg_buffer.get(group_id) and len(self._msg_buffer[group_id]) >= 2:
            prev_time = self._msg_buffer[group_id][-2][2]
            idle_threshold = self._get_group_param(group_id, "rescue_idle_threshold", self.rescue_idle_threshold)
            if (time.time() - prev_time) >= idle_threshold:
                base_wait = float(self._get_group_param(group_id, "debounce_wait_rescue", self.debounce_wait_rescue))
            else:
                base_wait = float(self._get_group_param(group_id, "debounce_wait_prob", self.debounce_wait_prob))
        else:
            base_wait = float(self._get_group_param(group_id, "debounce_wait_prob", self.debounce_wait_prob))

        # 疲劳系数：对话轮数越多，防抖等待越长（模拟聊久了回复变慢）
        flow = self._get_flow(group_id)
        if flow.conversation_turns > 0:
            fatigue_multiplier = min(self._get_group_param(group_id, "fatigue_max_multiplier", self.fatigue_max_multiplier), 1.0 + (flow.conversation_turns * self._get_group_param(group_id, "fatigue_coefficient", self.fatigue_coefficient)))
            base_wait *= fatigue_multiplier

        return base_wait

    def _aggregate_messages(self, messages: list) -> str:
        """将多条消息聚合为一条逻辑话语"""
        if len(messages) == 1:
            return messages[0][1]

        # 同一用户连续消息直接拼接，不同用户消息用换行分隔
        parts = []
        current_sender = None
        current_parts = []

        for sender, text, _ts, _event in messages:
            if sender != current_sender:
                if current_parts:
                    parts.append(" ".join(current_parts))
                current_sender = sender
                current_parts = [text]
            else:
                current_parts.append(text)

        if current_parts:
            parts.append(" ".join(current_parts))

        return "\n".join(parts)

    async def _evaluate_debounced_messages(self, group_id: str, event: AstrMessageEvent, messages: list, silence_gap: float = 0.0):
        """防抖到期后执行唤醒判定"""
        message_str = event.message_str or ""
        sender_id = str(getattr(event.message_obj.sender, "user_id", ""))
        self._debug(f"防抖判定开始 | 群={group_id} 消息='{message_str[:40]}' 概率唤醒={'启用' if self.probability_wakeup else '关闭'} 冷场救场={'启用' if self.rescue_enabled else '关闭'}")

        # 预扫描：统一检测所有暂存消息的复读状态，避免后续名称/关键词/复读抑制循环重复调用
        repeat_cache: dict[int, tuple[bool, str]] = {}
        if self.repeat_suppress_enabled and messages:
            for i, (msg_sender, msg_text, _ts, _evt) in enumerate(messages):
                is_rep, match_info = self._is_repeat_message(group_id, msg_sender, msg_text)
                repeat_cache[i] = (is_rep, match_info)
            rep_count = sum(1 for v in repeat_cache.values() if v[0])
            self._debug(f"复读预检(防抖) | 共检{len(repeat_cache)}条 检出复读{rep_count}条")

        # 0. 检查回复BOT（遍历所有暂存消息，回复BOT可能出现在任意一条中）
        reply_to_bot_event = None
        reply_sender_id = sender_id
        for msg_sender, msg_text, _ts, _evt in messages:
            if self._is_reply_to_bot(_evt):
                reply_to_bot_event = _evt
                reply_sender_id = str(getattr(_evt.message_obj.sender, "user_id", ""))
                break

        if reply_to_bot_event:
            # 用户概率检查（使用回复BOT那条消息的发送者）
            user_prob = self._get_user_prob(reply_sender_id)
            self._debug(f"回复BOT(防抖) | 命中发送者={reply_sender_id} 用户概率={user_prob:.2f}")
            if user_prob <= 0:
                return
            if user_prob < 1.0 and random.random() > user_prob:
                return

            logger.info(
                f"灵犀(防抖): 检测到回复BOT，"
                f"聚合 {len(messages)} 条消息"
            )
            self._trigger_wake(event)
            flow = self._get_flow(group_id)
            if flow.engagement <= 0:
                flow.conversation_turns = 1
            else:
                flow.conversation_turns += 1
            flow.engagement = 1.0
            flow.engagement_last_update = time.time()
            if flow.state == FlowState.BYSTANDER:
                self._transition_flow(group_id, FlowState.ATTENTIVE, "回复BOT触发升级")
            event.set_extra("smart_wakeup_triggered", True)
            event.set_extra("wakeup_type", "name_trigger")
            self._stats["total_wakeups"] += 1
            self._stats["name_trigger_wakeups"] += 1
            return

        # 1. 检查名称匹配（遍历所有暂存消息，名称可能出现在任意一条中）
        # 但跳过复读消息：复读内容包含名称时不应触发名称唤醒
        matched_name = None
        matched_sender_id = sender_id
        for i, (msg_sender, msg_text, _ts, _evt) in enumerate(messages):
            # 跳过复读消息：如果该消息是复读（与缓冲区中其他用户的消息相同/相似），
            # 则不应因复读内容包含名称而触发唤醒
            if self.repeat_suppress_enabled and repeat_cache.get(i, (False, ""))[0]:
                self._debug(f"名称匹配跳过复读 | 发送者={msg_sender} 内容='{msg_text[:30]}' 为复读消息")
                continue
            msg_lower = msg_text.lower()
            for name in self.bot_names:
                if name.lower() in msg_lower:
                    matched_name = name
                    matched_sender_id = str(getattr(_evt.message_obj.sender, "user_id", ""))
                    break
            if matched_name:
                break

        if matched_name:
            # 用户概率检查（使用命中名称那条消息的发送者）
            user_prob = self._get_user_prob(matched_sender_id)
            self._debug(f"名称匹配(防抖) | 命中='{matched_name}' 命中发送者={matched_sender_id} 用户概率={user_prob:.2f}")
            if user_prob <= 0:
                return
            if user_prob < 1.0 and random.random() > user_prob:
                return

            logger.info(
                f"灵犀(防抖): 命中名称 '{matched_name}'，"
                f"聚合 {len(messages)} 条消息"
            )
            self._trigger_wake(event)
            # 参与度激活：名称触发=满参与度
            flow = self._get_flow(group_id)
            if flow.engagement <= 0:
                flow.conversation_turns = 1  # 新的参与期间
            else:
                flow.conversation_turns += 1  # 继续对话
            flow.engagement = 1.0
            flow.engagement_last_update = time.time()
            if flow.state == FlowState.BYSTANDER:
                self._transition_flow(group_id, FlowState.ATTENTIVE, "名称触发升级")
            event.set_extra("smart_wakeup_triggered", True)
            event.set_extra("wakeup_type", "name_trigger")
            event.set_extra("matched_name", matched_name)
            # 名称触发不扣除精力：被动唤醒，精力系统仅约束主动行为
            self._stats["total_wakeups"] += 1
            self._stats["name_trigger_wakeups"] += 1
            return

        # 2. 检查关键词匹配（遍历所有暂存消息，关键词可能出现在任意一条中）
        # 但跳过复读消息：复读内容包含关键词时不应触发关键词唤醒
        matched_keyword = None
        keyword_sender_id = sender_id
        for i, (msg_sender, msg_text, _ts, _evt) in enumerate(messages):
            # 跳过复读消息：复读内容包含关键词时不应触发关键词唤醒
            if self.repeat_suppress_enabled and repeat_cache.get(i, (False, ""))[0]:
                self._debug(f"关键词匹配跳过复读 | 发送者={msg_sender} 内容='{msg_text[:30]}' 为复读消息")
                continue
            kw = self._match_keyword(msg_text)
            if kw:
                matched_keyword = kw
                keyword_sender_id = str(getattr(_evt.message_obj.sender, "user_id", ""))
                break
        if matched_keyword:
            user_prob = self._get_user_prob(keyword_sender_id)
            self._debug(f"关键词匹配(防抖) | 命中='{matched_keyword}' 命中发送者={keyword_sender_id} 用户概率={user_prob:.2f}")
            if user_prob <= 0:
                self._debug(f"关键词跳过 | 用户概率为0")
                return

            keyword_prob = self._get_group_param(group_id, "keyword_reply_prob", self.keyword_reply_prob)
            final_prob = keyword_prob * user_prob
            roll = random.random()
            self._debug(f"关键词判定 | 关键词概率={keyword_prob:.2f} × 用户概率={user_prob:.2f} = {final_prob:.2f} 掷骰={roll:.4f} → {'命中' if roll < final_prob else '未命中'}")

            if roll < final_prob:
                logger.info(
                    f"关键词自然唤醒(防抖): 命中关键词 '{matched_keyword}'，"
                    f"概率: {final_prob:.2f}，聚合 {len(messages)} 条消息"
                )
                self._trigger_wake(event)
                # 参与度激活：关键词触发=满参与度
                flow = self._get_flow(group_id)
                if flow.engagement <= 0:
                    flow.conversation_turns = 1  # 新的参与期间
                else:
                    flow.conversation_turns += 1  # 继续对话
                flow.engagement = 1.0
                flow.engagement_last_update = time.time()
                if flow.state == FlowState.BYSTANDER:
                    self._transition_flow(group_id, FlowState.ATTENTIVE, "关键词触发升级")
                event.set_extra("smart_wakeup_triggered", True)
                event.set_extra("wakeup_type", "keyword_trigger")
                event.set_extra("matched_keyword", matched_keyword)
                # 关键词触发不扣除精力：被动唤醒，精力系统仅约束主动行为
                self._stats["total_wakeups"] += 1
                self._stats["keyword_trigger_wakeups"] = self._stats.get("keyword_trigger_wakeups", 0) + 1
                return

        # 2.5 复读抑制：使用预扫描缓存结果
        # 防抖聚合了多条消息，复读可能出现在任意一条中，不能只检查最后一条
        has_repeat = False
        repeat_info = ""
        if self.repeat_suppress_enabled and messages:
            for i, (sender, text, _ts, _evt) in enumerate(messages):
                is_repeat, match_info = repeat_cache.get(i, (False, ""))
                if is_repeat:
                    has_repeat = True
                    repeat_info = match_info
                    break
            if has_repeat:
                self._debug(f"复读抑制(防抖) | 暂存消息中检测到复读: {repeat_info}")
            else:
                self._debug(f"复读抑制(防抖) | 暂存消息中未检测到复读")
        # 将复读检测结果存入 event extra，供 _check_probability_wakeup 使用
        event.set_extra("debounce_has_repeat", has_repeat)
        event.set_extra("debounce_repeat_info", repeat_info)

        # 3. 概率唤醒
        if self.probability_wakeup:
            await self._check_probability_wakeup(event)
        else:
            self._debug(f"概率唤醒 | 已关闭，跳过")

        # 4. 冷场救场
        if self.rescue_enabled:
            await self._check_dead_chat_rescue(event, silence_gap)
        else:
            self._debug(f"冷场救场 | 已关闭，跳过")

    # ─── 消息监听 ──────────────────────────────────────────

    @filter.platform_adapter_type(filter.PlatformAdapterType.TELEGRAM | filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听群聊消息（Telegram / QQ）

        流程：
        1. 记录消息到缓冲区（所有消息，不论是否命中名称）
        2. 指令前缀跳过（含防抖清理，以 / 开头的消息直接跳过并取消已有防抖）
        2.5. 低信息量消息过滤（纯图片/表情包/emoji跳过判定）
        3. 检查是否需要定期清理
        4. 检查群白名单/黑名单
        5. 更新心流状态
        6. 防抖处理 / 立即判定（受 force_debounce 控制）
        """
        group_id = event.message_obj.group_id
        # ─── v1.5.0 主动发言：缓存 UMO（unified_msg_origin）用于主动发送消息 ───
        # 每次 on_group_message 触发时刷新缓存，主动发言调度器通过 _is_umo_valid 校验有效期（默认 24h）
        # v1.7.1：延迟持久化，重载后可恢复 UMO（避免沉默群无法被调度）
        if group_id:
            self._group_umo[group_id] = (event.unified_msg_origin, time.time())
            self._maybe_save_proactive_state()
        message_str = event.message_str or ""
        sender_name = event.get_sender_name()
        sender_id = str(getattr(event.message_obj.sender, "user_id", ""))

        # ─── v1.6.0 退让信号③：厌烦关键词检测（Phase 3 提前实现，复杂度低） ───
        # 在消息记录之前检测，确保即使指令前缀/媒体过滤也能捕获厌烦信号
        if group_id and message_str:
            self._check_annoyed_keywords(group_id, message_str)

        # 1. 记录消息到缓冲区
        self._record_message(event)
        self._debug(f"收到群消息 | 群={group_id} 发送者={sender_name}({sender_id}) 内容='{message_str[:50]}'")

        # v1.8.4 新增：有效用户回复检测（重置 consecutive_no_response_count）
        # 当群内有用户实质发言时（非纯表情、非纯@、字符数≥3），重置该群的连续无回应计数
        # 这样递进退让机制只对真正的"无人回应"场景生效（深夜/凌晨场景）
        # 避免用户白天讨论后夜间发言被误判为"无人回应"
        if group_id and message_str:
            # 剥除 @ 提及（如 @Bot）后的纯文本
            _stripped = message_str.strip()
            # v1.8.4 修复 M3：先剥离 @ 前缀再判定，"@bot 你好啊"应为有效回复
            _stripped_no_at = re.sub(r'^@\S+\s*', '', _stripped).strip()
            # 简单判定：非空、长度≥3、不是纯 emoji/表情符号
            # （这里用简单判定，复杂判定留给 LLM 后续处理）
            # v1.9.6 修正：使用 Unicode emoji 区间检测，替代 16 字符白名单
            # 原问题：emoji 白名单仅 16 个字符，大量常见 emoji 未覆盖，
            # 纯 emoji 消息被误判为有效回复，触发退却清除和计数重置
            _is_pure_emoji = bool(re.match(
                r'^[\U0001F300-\U0001FAFF\U00002600-\U000027BF'
                r'\U0001F1E6-\U0001F1FF\U00002B00-\U00002BFF\uFE0F]+$',
                _stripped_no_at
            ))
            _is_meaningful = (
                len(_stripped_no_at) >= 3
                and not _is_pure_emoji  # 不是纯 emoji
            )
            if _is_meaningful:
                tracker = self._proactive_response_tracker.get(group_id)
                if tracker and tracker.get("consecutive_no_response_count", 0) > 0:
                    prev_count = tracker["consecutive_no_response_count"]
                    tracker["consecutive_no_response_count"] = 0
                    # 同时重置 cooldown_multiplier（用户回来了，惩罚解除）
                    tracker["cooldown_multiplier"] = 1.0
                    logger.debug(
                        f"[主动发言] 群={group_id} 检测到用户有效回复，"
                        f"重置 consecutive_no_response_count（{prev_count}→0）"
                    )
                # v1.9.4 修复（v1.9.6 修正）：用户有效回复时清除退却状态
                # 原问题：consecutive_skip/consecutive_no_response 退却长达 6h/24h，
                # 期间概率唤醒被阻塞，用户感知"bot 不参与正常交流"。
                # v1.9.6 修正：仅清除 consecutive_skip 和 consecutive_no_response 退却，
                # 不清除 annoyed 退却（用户明确要求闭嘴的 24h 沉默承诺不应被其他人打破）。
                if group_id in self._proactive_retreat:
                    old_reason = self._proactive_retreat[group_id].get("reason", "")
                    if old_reason in ("consecutive_skip", "consecutive_no_response"):
                        del self._proactive_retreat[group_id]
                        logger.info(
                            f"[主动发言] 群={group_id} 用户有效回复，清除退却状态"
                            f"（原退却原因: {old_reason}）"
                        )
                    elif old_reason == "annoyed":
                        logger.debug(
                            f"[主动发言] 群={group_id} 用户有效回复，但 annoyed 退却不清除"
                            f"（用户明确要求沉默，需自然过期）"
                        )

        # 诊断：输出消息链结构，便于排查回复检测问题
        if self.debug_mode:
            try:
                from astrbot.api.message_components import Reply, Plain
                chain_info = []
                if event.message_obj and event.message_obj.message:
                    for i, comp in enumerate(event.message_obj.message):
                        if isinstance(comp, Reply):
                            sid = getattr(comp, "sender_id", None)
                            chain_info.append(f"[{i}]Reply(id={getattr(comp, 'id', '?')}, sender_id={sid})")
                        elif isinstance(comp, Plain):
                            t = getattr(comp, "text", "")
                            chain_info.append(f"[{i}]Plain('{t[:60]}')")
                        else:
                            chain_info.append(f"[{i}]{type(comp).__name__}")
                self._debug(f"消息链结构 | {len(chain_info)}个组件: {' | '.join(chain_info)}")
            except Exception:
                pass  # 消息链诊断非关键，失败不影响主流程

        # 2. 指令前缀跳过：以 / 等前缀开头的消息是系统指令，不走唤醒逻辑
        # 但回复BOT消息除外——用户回复BOT时，即使内容以 / 开头，也应正常处理
        #
        # 重要：event.message_str 可能已被框架去掉 / 前缀（如 /查询卡池 → 查询卡池），
        # 因此需要同时检查消息链中 Plain 组件的原始文本。
        if self.command_prefix_enabled and self.command_prefix:
            # 从消息链中获取原始文本（保留 / 前缀）
            raw_msg_from_chain = ""
            if event.message_obj and event.message_obj.message:
                for comp in event.message_obj.message:
                    if hasattr(comp, "text") and comp.text:
                        raw_msg_from_chain += comp.text
            raw_msg = raw_msg_from_chain.strip() or (event.message_str or "").strip()
            if raw_msg.startswith(self.command_prefix):
                # message_str 以指令前缀开头，但需排除回复BOT消息的情况
                is_reply_to_bot = self._is_reply_to_bot(event)
                if is_reply_to_bot:
                    self._debug(f"前缀检查跳过 | message_str以'{self.command_prefix}'开头，但为回复BOT消息，不应用指令前缀过滤")
                else:
                    # 取消该群已有的防抖计时器，防止到期后触发概率唤醒
                    if group_id in self._debounce_states:
                        ds = self._debounce_states[group_id]
                        if ds.timer_task is not None and not ds.timer_task.done():
                            ds.timer_task.cancel()
                            self._stats["debounce_cancelled"] += 1
                        ds.pending_messages.clear()
                        self._debug(f"指令前缀跳过 | 已取消群 {group_id} 的防抖计时器并清除暂存消息")
                    self._debug(f"指令前缀跳过 | 消息以 '{self.command_prefix}' 开头，跳过所有判定")
                    return
            else:
                self._debug(f"前缀检查通过 | 消息不以 '{self.command_prefix}' 开头，继续判定")

        # 2.5 低信息量消息过滤：纯图片/表情包/emoji/Sticker不进入判定
        if self.ignore_media_messages and self._is_low_info_message(
            message_str,
            message_chain=event.message_obj.message if event.message_obj else None
        ):
            self._debug(f"低信息量过滤 | 消息为纯媒体/emoji，跳过判定 内容='{message_str[:30]}'")
            return

        # 2.6 转发复读过滤：Telegram 加一等转发BOT消息的场景
        # 这类消息本质是复读BOT发言，不应触发唤醒
        if self._is_forward_from_bot(event):
            self._debug(f"转发复读过滤 | 消息为转发自BOT的复读，跳过判定 内容='{message_str[:30]}'")
            return

        # 2.7 BOT自身消息过滤：BOT通过其他插件发送的消息不应触发唤醒判定
        # QQ平台（NapCat）会将BOT自身消息作为群消息分发，需在此拦截
        if sender_id in self._bot_user_ids:
            self._debug(f"BOT消息过滤 | 发送者={sender_name}({sender_id}) 为BOT自身，跳过判定")
            return

        # 3. 检查是否需要定期清理
        self._maybe_cleanup()

        # 4. 检查群白名单/黑名单
        if not self._is_group_allowed(group_id):
            self._debug(f"群组过滤 | 群 {group_id} 不在允许列表中，跳过")
            return
        self._debug(f"群组过滤 | 群 {group_id} 允许唤醒")

        # 5. 更新心流状态（每条消息都更新）
        self._update_flow_state(group_id, event)
        flow = self._get_flow(group_id)
        energy = self._get_energy(group_id)
        self._recover_energy(group_id)
        self._debug(f"心流状态 | 群={group_id} 状态={flow.state.value} 活跃度={flow.message_count_in_window} 相关度={flow.relevance_score:.2f} 精力={energy.energy:.2f} 参与度={flow.engagement:.2f}")

        # 6. 防抖处理
        if self.debounce_enabled:
            # 检查是否命中名称（跳过复读消息）
            message_lower = message_str.lower()
            sender_name = event.get_sender_name() or ""
            is_repeat = self.repeat_suppress_enabled and self._is_repeat_message(group_id, sender_name, message_str)[0]
            name_matched = not is_repeat and any(name.lower() in message_lower for name in self.bot_names)

            # 检查是否是回复BOT消息
            is_reply_to_bot = self._is_reply_to_bot(event)

            if not self.force_debounce and (name_matched or is_reply_to_bot):
                # 强制防抖关闭时：名称匹配或回复BOT → 跳过防抖，立即判定（旧行为）
                self._debug(f"跳过防抖 | 名称匹配={name_matched} 回复BOT={is_reply_to_bot} 强制防抖=关闭，立即判定")
                buffer = self._msg_buffer.get(group_id)
                sg = 0.0
                if buffer and len(buffer) >= 2:
                    sg = buffer[-1][2] - buffer[-2][2]
                await self._immediate_evaluate(event, sg)
            else:
                # 强制防抖开启：所有触发类型统一走防抖
                # 或强制防抖关闭但非名称/非回复BOT也走防抖
                self._debug(f"进入防抖 | 名称匹配={name_matched} 回复BOT={is_reply_to_bot} 强制防抖={'启用' if self.force_debounce else '关闭'}")
                await self._debounce_message(event)
        else:
            # 无防抖，直接判定
            buffer = self._msg_buffer.get(group_id)
            sg = 0.0
            if buffer and len(buffer) >= 2:
                sg = buffer[-1][2] - buffer[-2][2]
            await self._immediate_evaluate(event, sg)

    async def _immediate_evaluate(self, event: AstrMessageEvent, silence_gap: float = 0.0):
        """立即执行唤醒判定（无防抖）"""
        group_id = event.message_obj.group_id
        message_str = event.message_str or ""
        message_lower = message_str.lower()
        sender_id = str(getattr(event.message_obj.sender, "user_id", ""))
        self._debug(f"立即判定开始 | 群={group_id} 消息='{message_str[:40]}'")

        # 1. 检查名称匹配
        matched_name = None
        for name in self.bot_names:
            if name.lower() in message_lower:
                matched_name = name
                break

        if matched_name:
            # 跳过复读消息：复读内容包含名称时不应触发名称唤醒
            sender_name = event.get_sender_name() or ""
            if self.repeat_suppress_enabled and self._is_repeat_message(group_id, sender_name, message_str)[0]:
                self._debug(f"名称匹配跳过复读(立即) | 发送者={sender_name} 内容='{message_str[:30]}' 为复读消息")
            else:
                # 用户概率检查：0.0 = 永不回复
                user_prob = self._get_user_prob(sender_id)
                self._debug(f"名称匹配(立即) | 命中='{matched_name}' 用户概率={user_prob:.2f}")
                if user_prob <= 0:
                    logger.info(f"名称唤醒被用户概率覆盖阻止: 用户 {sender_id} 概率为 0")
                    return
                # 非满概率时进行随机判定
                if user_prob < 1.0 and random.random() > user_prob:
                    logger.info(f"名称唤醒被用户概率覆盖阻止: 用户 {sender_id} 概率 {user_prob:.2f}")
                    return

                logger.info(
                    f"灵犀: 命中名称 '{matched_name}'，"
                    f"群: {group_id}，消息内容: {message_str[:50]}"
                )
                self._trigger_wake(event)
                # 参与度激活：名称触发=满参与度
                flow = self._get_flow(group_id)
                if flow.engagement <= 0:
                    flow.conversation_turns = 1  # 新的参与期间
                else:
                    flow.conversation_turns += 1  # 继续对话
                flow.engagement = 1.0
                flow.engagement_last_update = time.time()
                if flow.state == FlowState.BYSTANDER:
                    self._transition_flow(group_id, FlowState.ATTENTIVE, "名称触发升级")
                self._debug(f"触发唤醒 | 类型=名称 前缀='{self.wake_command_prefix}' message_str='{event.message_str[:40]}'")
                event.set_extra("smart_wakeup_triggered", True)
                event.set_extra("wakeup_type", "name_trigger")
                event.set_extra("matched_name", matched_name)
                # 名称触发不扣除精力：被动唤醒，精力系统仅约束主动行为
                self._stats["total_wakeups"] += 1
                self._stats["name_trigger_wakeups"] += 1
                return

        # 2. 检查关键词匹配
        matched_keyword = self._match_keyword(message_str)
        if matched_keyword:
            # 跳过复读消息：复读内容包含关键词时不应触发关键词唤醒
            sender_name = event.get_sender_name() or ""
            if self.repeat_suppress_enabled and self._is_repeat_message(group_id, sender_name, message_str)[0]:
                self._debug(f"关键词匹配跳过复读(立即) | 发送者={sender_name} 内容='{message_str[:30]}' 为复读消息")
            else:
                # 用户概率检查
                user_prob = self._get_user_prob(sender_id)
                self._debug(f"关键词匹配(立即) | 命中='{matched_keyword}' 用户概率={user_prob:.2f}")
                if user_prob <= 0:
                    self._debug(f"关键词跳过 | 用户概率为0")
                    return

                # 关键词回复概率 = keyword_reply_prob × 用户概率乘数
                keyword_prob = self._get_group_param(group_id, "keyword_reply_prob", self.keyword_reply_prob)
                final_prob = keyword_prob * user_prob
                roll = random.random()
                self._debug(f"关键词判定 | 关键词概率={keyword_prob:.2f} × 用户概率={user_prob:.2f} = {final_prob:.2f} 掷骰={roll:.4f} → {'命中' if roll < final_prob else '未命中'}")

                if roll < final_prob:
                    logger.info(
                        f"关键词自然唤醒: 命中关键词 '{matched_keyword}'，"
                        f"群: {group_id}，概率: {final_prob:.2f}，消息内容: {message_str[:50]}"
                    )
                    self._trigger_wake(event)
                    # 参与度激活：关键词触发=满参与度
                    flow = self._get_flow(group_id)
                    if flow.engagement <= 0:
                        flow.conversation_turns = 1  # 新的参与期间
                    else:
                        flow.conversation_turns += 1  # 继续对话
                    flow.engagement = 1.0
                    flow.engagement_last_update = time.time()
                    if flow.state == FlowState.BYSTANDER:
                        self._transition_flow(group_id, FlowState.ATTENTIVE, "关键词触发升级")
                    event.set_extra("smart_wakeup_triggered", True)
                    event.set_extra("wakeup_type", "keyword_trigger")
                    event.set_extra("matched_keyword", matched_keyword)
                    # 关键词触发不扣除精力：被动唤醒，精力系统仅约束主动行为
                    self._stats["total_wakeups"] += 1
                    self._stats["keyword_trigger_wakeups"] = self._stats.get("keyword_trigger_wakeups", 0) + 1
                    return

        # 3. 概率唤醒
        if self.probability_wakeup:
            await self._check_probability_wakeup(event)
        else:
            self._debug(f"概率唤醒 | 已关闭，跳过")

        # 4. 冷场救场
        if self.rescue_enabled:
            await self._check_dead_chat_rescue(event, silence_gap)
        else:
            self._debug(f"冷场救场 | 已关闭，跳过")

    @filter.platform_adapter_type(filter.PlatformAdapterType.TELEGRAM | filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        """监听私聊消息（可选启用，Telegram / QQ）"""
        if not self.enable_private_chat:
            return

        message_str = event.message_str or ""
        message_lower = message_str.lower()
        matched_name = None
        for name in self.bot_names:
            if name.lower() in message_lower:
                matched_name = name
                break

        if matched_name:
            logger.info(
                f"灵犀(私聊): 命中名称 '{matched_name}'，"
                f"消息内容: {message_str[:50]}"
            )
            self._trigger_wake(event)
            event.set_extra("smart_wakeup_triggered", True)
            event.set_extra("wakeup_type", "name_trigger")
            event.set_extra("matched_name", matched_name)
            self._stats["total_wakeups"] += 1
            self._stats["name_trigger_wakeups"] += 1

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        """记录机器人自己发送的消息到缓冲区

        这样上下文中不仅包含用户消息，也包含机器人的回复，
        LLM 可以看到完整的对话流程。适用于 Telegram 和 QQ。
        同时记录 BOT 的 user_id，供 _is_reply_to_bot 比对。
        """
        group_id = event.message_obj.group_id
        if not group_id:
            return

        # 清除 LLM 执行中标志（双重保障：on_decorating_result 也会清除）
        self._llm_running_groups.pop(group_id, None)
        # 取消主动超时定时器
        self._cancel_llm_flag_timer(group_id)

        # 回复抑制时跳过记录（零宽空格消息不应写入缓冲区和对话历史）
        if event.get_extra("smart_wakeup_suppressed"):
            self._debug(f"after_message_sent | 群={group_id} 回复已抑制，跳过记录")
            return

        # 记录 BOT 的 user_id（用于回复检测）
        # 注意：after_message_sent 中 event.message_obj.sender 是原始消息发送者（用户），
        # 不是 BOT 自己。需要从其他途径获取 BOT 的 ID。
        # self_id 可能是用户名或数字 ID，两者都需要记录
        self_id = getattr(event.message_obj, "self_id", None)
        if self_id:
            bot_uid = str(self_id)
            if bot_uid and bot_uid not in self._bot_user_ids:
                self._bot_user_ids.add(bot_uid)
                self._debug(f"BOT用户ID记录(self_id) | 新增user_id={bot_uid}，当前已知BOT ID: {self._bot_user_ids}")
        # 尝试从 context 获取 BOT 的数字 ID
        for attr_name in ("bot_id", "bot_user_id"):
            try:
                attr_val = getattr(self.context, attr_name, None)
                if attr_val:
                    bot_uid2 = str(attr_val)
                    if bot_uid2 not in self._bot_user_ids:
                        self._bot_user_ids.add(bot_uid2)
                        self._debug(f"BOT用户ID记录(context.{attr_name}) | 新增user_id={bot_uid2}，当前已知BOT ID: {self._bot_user_ids}")
            except Exception:
                pass

        result = event.get_result()
        if not result or not result.chain:
            return

        # 优先使用分段前保存的完整回复文本
        # 分段模块会修改 result.chain 只保留最后一段，
        # 导致此处只能拿到部分文本，复读检测因此失效
        full_text = event.get_extra("full_response_text_before_split")
        if full_text:
            combined_text = full_text
        else:
            text_parts = []
            for comp in result.chain:
                if hasattr(comp, "text") and comp.text:
                    text_parts.append(comp.text)
            combined_text = " ".join(text_parts) if text_parts else ""

        if combined_text:
            # 过滤思考标签（兜底机制）
            if self.filter_thinking_tags:
                combined_text = self._filter_thinking_tags(combined_text)
            if combined_text:
                bot_name = self.bot_names[0] if self.bot_names else "Bot"
                buffer = self._get_buffer(group_id)
                buffer.append((bot_name, combined_text, int(time.time()), {"is_bot_message": True}))
                # 同步记录到对话历史，确保多轮对话记忆完整
                if self.conversation_memory_enabled:
                    self._record_assistant_message(group_id, combined_text)
                # 记录 BOT 最近一次回复文本和时间（供语义去重检测）
                self._last_bot_reply_text[group_id] = combined_text
                self._last_bot_reply_time[group_id] = time.time()

    # ─── LLM 请求钩子 ─────────────────────────────────────

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """在 LLM 请求前注入群聊上下文和自然唤醒提示"""
        if not event.get_extra("smart_wakeup_triggered"):
            return

        # 诊断（v1.7.3）：记录 LLM 请求开始时间，用于 _auto_clear_llm_flag 诊断
        _group_id_for_diag = event.message_obj.group_id
        if _group_id_for_diag:
            self._llm_request_started_at[_group_id_for_diag] = time.time()
            # 清除之前的响应记录（防止上次请求残留干扰诊断）
            self._llm_response_received_at.pop(_group_id_for_diag, None)

        from astrbot.core.agent.message import TextPart

        # ── 核心优化：绕过 AstrBot 内置上下文 ──
        # 清空 AstrBot 核心加载的对话历史，避免历史膨胀导致的 TOKEN 消耗问题。
        # 插件通过自己的消息缓冲区 + 增量注入 + 摘要压缩来管理上下文，
        # 比核心的"全量历史"方式高效得多。
        # 此操作仅影响唤醒触发的消息，正常 @ 对话仍使用核心的上下文机制。
        if self.bypass_core_context:
            original_count = len(req.contexts) if hasattr(req, 'contexts') and req.contexts else 0
            if original_count > 0:
                req.contexts = []
                self._debug(
                    f"[ContextBypass] 已清空核心对话历史 ({original_count}条)，"
                    f"使用插件自管理的上下文"
                )
                logger.info(
                    f"[ContextBypass] 群={event.message_obj.group_id} "
                    f"清空核心历史 {original_count}条 → 0条"
                )

        parts = []

        # 提前获取 group_id，供后续对话记忆和群聊上下文注入使用
        group_id = event.message_obj.group_id

        # 注入分层对话记忆（替代被清空的核心上下文）
        if self.conversation_memory_enabled and self.bypass_core_context and group_id:
            memory_text = self._format_conversation_memory(group_id)
            if memory_text:
                parts.append(TextPart(text=memory_text))
                self._debug(
                    f"[ConversationMemory] 注入对话记忆 "
                    f"群={group_id} 记忆长度={len(memory_text)}字符"
                )

        # 注入群聊上下文（支持增量注入和压缩）
        if group_id:
            context_text, new_count, old_count = self._format_context(group_id, incremental=True)
            if context_text:
                original_chars = len(context_text)
                compressed_text = context_text

                # 小模型摘要压缩
                if self.context_compression_enabled and original_chars > 200:
                    compressed_text = await self._compress_context(context_text, group_id)

                # 更新增量上下文时间戳
                buffer = self._msg_buffer.get(group_id)
                if buffer:
                    self._last_context_ts[group_id] = buffer[-1][2]

                # 构建上下文标签
                if new_count > 0 and self.incremental_context_enabled:
                    context_label = (
                        f"以下是自上次回复后的群聊消息"
                        f"（新增{new_count}条" +
                        (f"+补充{old_count}条" if old_count > 0 else "") +
                        f"）：\n"
                    )
                else:
                    context_label = "以下是最近的群聊消息记录（包括未直接 @ 你的消息）：\n"

                parts.append(
                    TextPart(
                        text=(
                            "<group_chat_context>\n"
                            + context_label
                            + compressed_text + "\n"
                            + "</group_chat_context>"
                        )
                    )
                )

                # 上下文注入诊断日志
                compression_ratio = ""
                if compressed_text != context_text:
                    ratio = len(compressed_text) / original_chars * 100 if original_chars > 0 else 100
                    compression_ratio = f" → 压缩={len(compressed_text)}字符({ratio:.0f}%)"
                self._debug(
                    f"[ContextInject] 群={group_id} 新增={new_count} 补充={old_count} "
                    f"原始={original_chars}字符{compression_ratio}"
                )

        # 注入聚合消息信息
        aggregated_text = event.get_extra("aggregated_text")
        aggregated_count = event.get_extra("aggregated_count") or 1

        if aggregated_count > 1 and aggregated_text:
            parts.append(
                TextPart(
                    text=(
                        "<aggregated_messages>\n"
                        f"用户在 {aggregated_count} 条连续消息中表达了以下内容（已聚合）：\n"
                        f"{aggregated_text}\n"
                        "请将以上内容视为一个完整的表述来回复。\n"
                        "</aggregated_messages>"
                    )
                )
            )

        # 根据唤醒类型注入不同提示
        wakeup_type = event.get_extra("wakeup_type") or "name_trigger"

        if wakeup_type == "name_trigger":
            matched_name = event.get_extra("matched_name") or ""
            parts.append(
                TextPart(
                    text=(
                        "<natural_wakeup_context>\n"
                        f"用户在群聊中自然提到了你的名称「{matched_name}」，"
                        f"你被自动唤醒参与对话。请自然地回复，像普通群成员一样参与话题，"
                        f"而不是以被命令的语气回应。\n"
                        "</natural_wakeup_context>"
                    )
                )
            )
        elif wakeup_type == "keyword_trigger":
            matched_keyword = event.get_extra("matched_keyword") or ""
            parts.append(
                TextPart(
                    text=(
                        "<natural_wakeup_context>\n"
                        f"群聊中提到了你关注的关键词「{matched_keyword}」，"
                        f"你被自动唤醒参与对话。请自然地加入话题，像普通群成员一样随意参与讨论，"
                        f"不要显得突兀。\n"
                        "</natural_wakeup_context>"
                    )
                )
            )
        elif wakeup_type == "probability_wakeup":
            parts.append(
                TextPart(
                    text=(
                        "<natural_wakeup_context>\n"
                        "你主动决定参与群聊对话。请自然地加入话题，"
                        "像普通群成员一样随意参与讨论，不要显得突兀。\n"
                        "</natural_wakeup_context>"
                    )
                )
            )
            # 轻量回应引导：允许 LLM 在无合适话题时输出简短附和
            if self.light_response_enabled:
                parts.append(
                    TextPart(
                        text=(
                            "<light_response_guidance>\n"
                            "如果你判断当前群聊中没有值得深度参与的话题，但保持沉默又显得不在场，\n"
                            "可以输出一句简短的自然附和——就像真人在群里偶尔冒泡一样。\n"
                            "这类回应的特征：不输出实质观点、不强行接话、在任何场合都不违和。\n"
                            "例如：简短的认同、语气词等。不要频繁使用同一句。\n"
                            "</light_response_guidance>"
                        )
                    )
                )
        elif wakeup_type == "dead_chat_rescue":
            parts.append(
                TextPart(
                    text=(
                        "<natural_wakeup_context>\n"
                        "群聊已经冷场了一段时间，你主动打破沉默。"
                        "请发起一个轻松的话题，或者对当前消息做出恰当的回应，"
                        "让群聊重新活跃起来。\n"
                        "</natural_wakeup_context>"
                    )
                )
            )
            # 轻量回应引导：冷场时也允许简短附和
            if self.light_response_enabled:
                parts.append(
                    TextPart(
                        text=(
                            "<light_response_guidance>\n"
                            "如果你觉得没有合适的话题可以发起，也可以输出一句简短的自然附和，\n"
                            "表达你在场的感受。不必强行找话题，自然就好。\n"
                            "</light_response_guidance>"
                        )
                    )
                )

        # 防重复提示：告知 LLM 最近一次回复内容，引导其主动避免重复
        # 仅对概率唤醒和冷场救场场景生效（直接呼叫是用户主动请求，不应限制）
        if wakeup_type in ("probability_wakeup", "dead_chat_rescue") and group_id:
            last_text = self._last_bot_reply_text.get(group_id, "")
            last_time = self._last_bot_reply_time.get(group_id, 0)
            now = time.time()
            if last_text and (now - last_time) <= self._SIMILARITY_WINDOW:
                # 截断过长的历史回复，避免注入过多 token
                display_text = last_text[:150] + ("..." if len(last_text) > 150 else "")
                parts.append(
                    TextPart(
                        text=(
                            "<anti_repeat_guidance>\n"
                            f"你刚刚在{int(now - last_time)}秒前说了：\n「{display_text}」\n"
                            "请避免重复相同的观点或表达方式。如果当前话题你已经表达过看法，"
                            "请换一个角度、补充新信息、或者选择不深入——"
                            "不要用不同措辞重复同一个意思。\n"
                            "</anti_repeat_guidance>"
                        )
                    )
                )
                self._debug(
                    f"[AntiRepeat] 注入防重复提示 | 群={group_id} "
                    f"距上次={now - last_time:.0f}秒 上次内容='{display_text[:40]}'"
                )

        # 在场感知提示：约束 BOT 只对在场用户说话
        parts.append(
            TextPart(
                text=(
                    "<presence_awareness>\n"
                    "注意：只对近期活跃的用户说话，不要对不在场的群友发起对话。"
                    "上下文中标注了[近期活跃]用户列表，仅对这些用户做出回应和互动。\n"
                    "</presence_awareness>"
                )
            )
        )

        # 省略主语提示：群聊中用户常省略主语，帮助 LLM 正确理解
        parts.append(
            TextPart(
                text=(
                    "<conversation_guidance>\n"
                    "群聊中用户常省略主语，省略主语的句子通常指说话者自己。"
                    "例如「怎么突然就变成XX了？」通常意为「我怎么突然就变成XX了？」，"
                    "而非指他人。请结合上下文对话关系标注（→ 回复/回应BOT）正确理解省略主语的句子。\n"
                    "注意：如果群聊中出现【调试定位】等方括号标记，这是用户在标记此前对话存在异常，"
                    "你应意识到标记之前的对话中可能存在发言者识别错误或回复对象错位的问题，"
                    "在后续回复中务必仔细核对发言者身份，避免延续错误。\n"
                    "</conversation_guidance>"
                )
            )
        )

        # 智能模型路由：根据消息特征决定使用大模型还是小模型
        # AstrBot 在 hook 之前已选定 provider，req.model 为 None，无法通过修改 req 切换模型。
        # 因此采用"直接调用小模型 + 阻断主请求"的方式实现路由。
        routing_model = self._determine_routing_model(event)
        if routing_model:
            try:
                provider = self.context.get_provider_by_id(routing_model)
                if provider:
                    # 构建简化的 prompt（系统提示 + 上下文 + 用户消息）
                    route_prompt = event.message_str or ""
                    route_system = req.system_prompt or ""
                    # 将注入的上下文也带上
                    context_parts_text = "\n".join(
                        p.text for p in parts if hasattr(p, 'text')
                    )
                    route_context = ""
                    if context_parts_text:
                        route_context = f"\n\n{context_parts_text}"

                    # 直接调用小模型
                    self._debug(f"[ModelRoute] 直接调用小模型: {routing_model}")
                    small_resp = await provider.text_chat(
                        prompt=f"{route_prompt}{route_context}",
                        session_id=f"route_{event.message_obj.group_id or 'dm'}_{int(time.time())}",
                        system_prompt=route_system,
                    )

                    # 提取回复文本
                    result_text = ""
                    if hasattr(small_resp, 'completion_text'):
                        result_text = small_resp.completion_text or ""
                    elif hasattr(small_resp, 'result'):
                        result_text = str(small_resp.result) if small_resp.result else ""

                    if result_text:
                        # 记录路由统计
                        event.set_extra("smart_wakeup_routed_model", routing_model)
                        event.set_extra("smart_wakeup_route_result", result_text)
                        self._stats["routing_stats"]["small_model_count"] += 1

                        # 估算小模型 token 消耗
                        est_prompt = self._estimate_tokens(route_system + route_context + route_prompt)
                        est_completion = self._estimate_tokens(result_text)
                        self._record_token_usage(
                            event=event,
                            prompt_tokens=est_prompt,
                            completion_tokens=est_completion,
                            model_name=routing_model,
                            is_estimated=True,
                        )

                        logger.info(
                            f"[ModelRoute] 路由成功: → {routing_model} "
                            f"唤醒={event.get_extra('wakeup_type')} "
                            f"消息长度={len(event.message_str or '')} "
                            f"回复长度={len(result_text)} "
                            f"估算tokens={est_prompt + est_completion}"
                        )

                        # 阻断主请求：清空上下文，最小化主模型消耗
                        # 主模型仍会被调用，但输入极小（~1K tokens），
                        # 在 on_decorating_result 中会用小模型回复替换主模型输出
                        req.contexts = []
                        req.system_prompt = "Reply with only: OK"
                        req.prompt = "OK"
                        req.extra_user_content_parts = []
                        event.set_extra("smart_wakeup_route_completed", True)
                        return
                    else:
                        logger.warning(f"[ModelRoute] 小模型返回为空，回退到主模型")
                        self._stats["routing_stats"]["glm47_count"] += 1
                else:
                    logger.warning(f"[ModelRoute] 未找到提供商 {routing_model}，回退到主模型")
                    self._stats["routing_stats"]["glm47_count"] += 1
            except asyncio.CancelledError:
                raise  # Pipeline被取消，向上传播
            except Exception as e:
                logger.warning(f"[ModelRoute] 小模型调用异常: {e}，回退到主模型")
                self._stats["routing_stats"]["glm47_count"] += 1
        else:
            self._stats["routing_stats"]["glm47_count"] += 1

        # 存储 prompt 文本长度，供 on_llm_response 估算 token 使用
        event.set_extra("smart_wakeup_prompt_len", len(event.message_str or ""))
        event.set_extra("smart_wakeup_system_prompt_len", len(req.system_prompt or ""))
        context_parts_len = sum(len(p.text) for p in parts if hasattr(p, 'text'))
        event.set_extra("smart_wakeup_context_len", context_parts_len)

        req.extra_user_content_parts.extend(parts)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp):
        """捕获 LLM 响应，记录 Token 消耗"""
        if not event.get_extra("smart_wakeup_triggered"):
            return

        # 诊断（v1.7.3）：记录 LLM 响应到达时间，用于 _auto_clear_llm_flag 诊断
        _group_id_for_diag = event.message_obj.group_id
        if _group_id_for_diag:
            self._llm_response_received_at[_group_id_for_diag] = time.time()
            _started = self._llm_request_started_at.get(_group_id_for_diag)
            if _started:
                _llm_elapsed = time.time() - _started
                logger.info(
                    f"[LLM诊断] 群={_group_id_for_diag} on_llm_response到达，"
                    f"LLM调用耗时{_llm_elapsed:.1f}秒"
                )

        # 如果路由已完成（小模型已直接回复），跳过主模型响应的记录
        if event.get_extra("smart_wakeup_route_completed"):
            return

        # 检测 tool_call：当 LLM 使用 send_message_to_user 时标记，防止重复输出
        finish_reason = getattr(resp, 'finish_reason', None)
        tool_calls = getattr(resp, 'tool_calls', None)
        if finish_reason == 'tool_calls' or (tool_calls and len(tool_calls) > 0):
            event.set_extra("smart_wakeup_has_tool_call", True)
            # 检查是否为 send_message_to_user 工具
            tool_names = []
            if tool_calls:
                for tc in tool_calls:
                    if hasattr(tc, 'function') and hasattr(tc.function, 'name'):
                        tool_names.append(tc.function.name)
                    elif isinstance(tc, dict):
                        func = tc.get('function', {})
                        if isinstance(func, dict):
                            tool_names.append(func.get('name', ''))
            logger.info(
                f"[ToolCallDetect] on_llm_response 检测到 tool_call | "
                f"finish_reason={finish_reason} tools={tool_names}"
            )

        # 兼容性提取 token usage - 多种尝试
        prompt_tokens = None
        completion_tokens = None
        total_tokens = None

        # 方式1: 直接属性
        prompt_tokens = getattr(resp, 'prompt_tokens', None)
        completion_tokens = getattr(resp, 'completion_tokens', None)
        total_tokens = getattr(resp, 'total_tokens', None)

        # 方式2: usage 子对象（对象属性或字典）
        if not prompt_tokens:
            usage = getattr(resp, 'usage', None)
            if usage:
                if isinstance(usage, dict):
                    prompt_tokens = usage.get('prompt_tokens') or None
                    completion_tokens = usage.get('completion_tokens') or None
                    total_tokens = usage.get('total_tokens') or None
                else:
                    prompt_tokens = getattr(usage, 'prompt_tokens', None)
                    completion_tokens = getattr(usage, 'completion_tokens', None)
                    total_tokens = getattr(usage, 'total_tokens', None)

        # 方式3-5: 深度搜索（保留但简化）
        if not prompt_tokens:
            # 遍历属性和 __dict__
            for attr_name in dir(resp):
                if 'usage' in attr_name.lower() or 'token' in attr_name.lower():
                    attr_val = getattr(resp, attr_name, None)
                    if attr_val and not callable(attr_val):
                        if isinstance(attr_val, dict) and 'prompt_tokens' in attr_val:
                            prompt_tokens = attr_val['prompt_tokens'] or None
                            completion_tokens = attr_val.get('completion_tokens') or None
                            total_tokens = attr_val.get('total_tokens') or None
                            break
                        elif hasattr(attr_val, 'prompt_tokens'):
                            prompt_tokens = getattr(attr_val, 'prompt_tokens') or None
                            completion_tokens = getattr(attr_val, 'completion_tokens') or None
                            total_tokens = getattr(attr_val, 'total_tokens') or None
                            break

        # 方式6: 本地估算（AstrBot 框架不暴露 usage 时的兜底方案）
        is_estimated = False
        if not prompt_tokens:
            completion_text = ""
            if hasattr(resp, 'completion_text'):
                completion_text = resp.completion_text or ""
            elif hasattr(resp, 'result'):
                completion_text = str(resp.result) if resp.result else ""

            # 从 event extra 中获取之前存储的文本长度
            prompt_text_len = event.get_extra("smart_wakeup_prompt_len") or 0
            system_prompt_len = event.get_extra("smart_wakeup_system_prompt_len") or 0
            context_len = event.get_extra("smart_wakeup_context_len") or 0

            prompt_tokens = self._estimate_tokens_by_len(system_prompt_len + prompt_text_len + context_len)
            completion_tokens = self._estimate_tokens_by_len(len(completion_text))
            total_tokens = prompt_tokens + completion_tokens
            is_estimated = True

        # 确保是整数
        try:
            prompt_tokens = int(prompt_tokens) if prompt_tokens else 0
            completion_tokens = int(completion_tokens) if completion_tokens else 0
            total_tokens = int(total_tokens) if total_tokens else 0
        except (ValueError, TypeError):
            return

        if total_tokens == 0:
            total_tokens = prompt_tokens + completion_tokens

        # 获取归因信息
        group_id = event.message_obj.group_id or "unknown"
        wakeup_type = event.get_extra("wakeup_type") or "name_trigger"
        model_name = getattr(resp, 'model', '') or ''
        est_tag = "📈" if is_estimated else "📊"

        # 使用统一的记录方法
        self._record_token_usage(
            event=event,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model_name=model_name,
            is_estimated=is_estimated,
        )

        # 调试日志
        logger.info(
            f"[TokenTracker] {est_tag} 模型={model_name or '?'} 唤醒={wakeup_type} "
            f"群={group_id} prompt={prompt_tokens} completion={completion_tokens} total={total_tokens}"
            + (" (估算)" if is_estimated else "")
        )
        self._debug(
            f"[TokenTracker] 累计: prompt={self._stats['total_prompt_tokens']} "
            f"completion={self._stats['total_completion_tokens']} "
            f"total={self._stats['total_tokens']} 调用={self._stats['llm_call_count']}次"
        )

        # P1-10 修复：移除未实现的级联升级逻辑
        # 原逻辑在 on_llm_response 中设置 smart_wakeup_cascade_upgrade 标记，
        # 但 on_decorating_result 从未检查该标记，功能未实现且统计虚高。
        # 保留 cascade_upgrade_enabled 配置项避免破坏用户配置结构，
        # 如需启用需在 on_decorating_result 中实现回退大模型重新调用的完整逻辑。
        # 保留 cascade_upgrade_count 统计字段（始终为 0）避免 KeyError

        # 定期检查异常（每10次调用检查一次，避免频繁计算）
        if self._stats["llm_call_count"] % 10 == 0:
            self._check_token_anomaly()

    @filter.on_using_llm_tool()
    async def on_using_llm_tool(self, event: AstrMessageEvent, tool, tool_args: dict):
        """检测 LLM 工具调用，拦截 send_message_to_user 的重复发送

        当 LLM 返回 finish_reason='tool_calls' 且同时包含 content 和 tool_calls 时，
        框架会先通过 on_decorating_result 发送 content，再通过 send_message_to_user 发送相同内容，
        导致用户看到重复消息。此方法在 tool 执行前拦截重复发送。

        防御机制（三层）：
        1. 设置 event flag，让后续 on_decorating_result 调用能检测到 tool_call
        2. 提取 send_message_to_user 的消息文本，与已发送内容比对
        3. 若重复，同时修改 tool_args 和 tool.handler，双管齐下阻止重复发送
           注意：tool 对象来自框架 tools_map 共享引用，handler 替换后必须恢复
        """
        if not event.get_extra("smart_wakeup_triggered"):
            return
        tool_name = getattr(tool, 'name', '') or ''
        if tool_name == 'send_message_to_user':
            group_id = event.message_obj.group_id
            event.set_extra("smart_wakeup_has_tool_call", True)

            # 提取 send_message_to_user 的消息文本
            tool_text = self._extract_tool_message_text(tool_args)

            # P0 修复：对 tool_args 中的文本统一过滤思考标签和上下文标签
            # ToolCall 场景下 LLM 可能在 tool_args 中混入思考标签，此处是唯一的过滤机会
            self._filter_tool_args_text(tool_args)

            # 重新提取过滤后的文本用于日志
            tool_text = self._extract_tool_message_text(tool_args)

            # P1-11 修复：移除原去重检查分支（依赖 _sent_content_cache，但 hook 执行顺序
            # 导致检查时缓存为空，整个分支是死代码）。
            # 原逻辑若生效会清空 tool_args 导致 tool_loop 不发送、用户无输出，
            # 与 v1.3.2 设计（on_decorating_result 清空 result.chain，让 tool_loop
            # 成为唯一发送通道）冲突。现保留 tool_args 不清空，由 on_decorating_result
            # 的 4 策略检测 tool_call 并清空 result.chain 即可保证不重复输出。
            logger.info(
                f"[ToolCallDetect] on_using_llm_tool 检测到 send_message_to_user | "
                f"群={group_id} 内容='{tool_text[:80]}' | tool_args 保留（tool_loop 为唯一发送通道）"
            )

    def _extract_tool_message_text(self, tool_args: dict) -> str:
        """从 send_message_to_user 的 tool_args 中提取纯文本内容"""
        messages = tool_args.get('messages', [])
        texts = []
        for msg in messages:
            if isinstance(msg, dict):
                text = msg.get('text', '')
                if text:
                    texts.append(text)
            elif isinstance(msg, str):
                texts.append(msg)
        return '\n'.join(texts)

    def _filter_tool_args_text(self, tool_args: dict):
        """对 tool_args 中的文本统一过滤思考标签和上下文标签

        ToolCall 场景下 LLM 可能在 send_message_to_user 的参数中混入
        思考标签或上下文标签，此处是唯一的过滤机会。
        """
        messages = tool_args.get('messages', [])
        for i, msg in enumerate(messages):
            if isinstance(msg, dict):
                text = msg.get('text', '')
                if text:
                    filtered = self._filter_context_tags(text)
                    if self.filter_thinking_tags:
                        filtered = self._filter_thinking_tags(filtered)
                    filtered = self._filter_duplicate_response(filtered)
                    if filtered != text:
                        msg['text'] = filtered
                        logger.info(
                            f"[ToolCallFilter] tool_args 文本已过滤 | "
                            f"原文 {len(text)} 字 → 过滤后 {len(filtered)} 字"
                        )
            elif isinstance(msg, str) and msg:
                filtered = self._filter_context_tags(msg)
                if self.filter_thinking_tags:
                    filtered = self._filter_thinking_tags(filtered)
                filtered = self._filter_duplicate_response(filtered)
                if filtered != msg:
                    messages[i] = filtered

    def _record_token_usage(self, event: AstrMessageEvent, prompt_tokens: int, completion_tokens: int,
                            model_name: str = "", is_estimated: bool = False):
        """统一的 Token 使用记录方法"""
        total_tokens = prompt_tokens + completion_tokens
        group_id = event.message_obj.group_id or "unknown"
        wakeup_type = event.get_extra("wakeup_type") or "name_trigger"

        # 更新总计
        self._stats["total_prompt_tokens"] += prompt_tokens
        self._stats["total_completion_tokens"] += completion_tokens
        self._stats["total_tokens"] += total_tokens
        self._stats["llm_call_count"] += 1

        # 按模型归因
        model_key = model_name if model_name else "unknown"
        if model_key not in self._stats["token_by_model"]:
            self._stats["token_by_model"][model_key] = {"prompt": 0, "completion": 0, "total": 0, "count": 0}
        self._stats["token_by_model"][model_key]["prompt"] += prompt_tokens
        self._stats["token_by_model"][model_key]["completion"] += completion_tokens
        self._stats["token_by_model"][model_key]["total"] += total_tokens
        self._stats["token_by_model"][model_key]["count"] += 1

        # 按唤醒类型归因
        if wakeup_type in self._stats["token_by_wakeup_type"]:
            self._stats["token_by_wakeup_type"][wakeup_type]["prompt"] += prompt_tokens
            self._stats["token_by_wakeup_type"][wakeup_type]["completion"] += completion_tokens
            self._stats["token_by_wakeup_type"][wakeup_type]["total"] += total_tokens
            self._stats["token_by_wakeup_type"][wakeup_type]["count"] += 1

        # 按群归因
        gid = str(group_id)
        if gid not in self._stats["token_by_group"]:
            self._stats["token_by_group"][gid] = {"prompt": 0, "completion": 0, "total": 0, "count": 0}
        self._stats["token_by_group"][gid]["prompt"] += prompt_tokens
        self._stats["token_by_group"][gid]["completion"] += completion_tokens
        self._stats["token_by_group"][gid]["total"] += total_tokens
        self._stats["token_by_group"][gid]["count"] += 1

        # 按小时统计
        hour_key = datetime.now().strftime("%Y-%m-%dT%H")
        if hour_key not in self._stats["hourly_tokens"]:
            self._stats["hourly_tokens"][hour_key] = {"prompt": 0, "completion": 0, "total": 0, "count": 0}
        self._stats["hourly_tokens"][hour_key]["prompt"] += prompt_tokens
        self._stats["hourly_tokens"][hour_key]["completion"] += completion_tokens
        self._stats["hourly_tokens"][hour_key]["total"] += total_tokens
        self._stats["hourly_tokens"][hour_key]["count"] += 1

        # 峰值追踪
        if prompt_tokens > self._stats["peak_prompt_tokens"]:
            self._stats["peak_prompt_tokens"] = prompt_tokens
            self._stats["peak_prompt_tokens_detail"] = (
                f"prompt={prompt_tokens} 群={gid} 唤醒={wakeup_type} "
                f"时间={datetime.now().strftime('%H:%M:%S')}"
                + (" (估算)" if is_estimated else "")
            )

    @staticmethod
    def _estimate_tokens_by_len(text_len: int) -> int:
        """根据文本字符数估算 token 数

        中文约 1.5 字符/token，英文约 4 字符/token，混合取约 2 字符/token。
        这只是粗略估算，实际值可能有 ±20% 误差，但足以用于监控和趋势分析。
        """
        return max(1, text_len // 2)

    def _estimate_tokens(self, text: str) -> int:
        """根据文本内容估算 token 数"""
        return self._estimate_tokens_by_len(len(text))

    # ─── 分层对话记忆 ───────────────────────────────────────

    def _get_conversation_history(self, group_id: str) -> deque:
        """获取指定群的对话历史，自动创建"""
        if group_id not in self._conversation_history:
            maxlen = self.summary_rounds_max * 2  # 每轮有user+assistant两条记录
            self._conversation_history[group_id] = deque(maxlen=maxlen)
        return self._conversation_history[group_id]

    def _record_user_message(self, group_id: str, text: str, sender_name: str = ""):
        """记录用户消息到对话历史"""
        history = self._get_conversation_history(group_id)
        history.append(("user", text.strip(), int(time.time()), sender_name))

    def _record_assistant_message(self, group_id: str, text: str):
        """记录Bot回复到对话历史"""
        history = self._get_conversation_history(group_id)
        history.append(("assistant", text.strip(), int(time.time()), ""))

        # 检查是否需要触发摘要压缩
        self._maybe_summarize_history(group_id)

    def _maybe_summarize_history(self, group_id: str):
        """检查并触发对话历史摘要压缩

        当对话历史超过 recent_rounds_keep * 2 条记录时，
        将较旧的记录压缩为摘要。
        """
        history = self._get_conversation_history(group_id)
        threshold = self.recent_rounds_keep * 2  # 每轮2条记录

        if len(history) <= threshold:
            return

        # P1-9 修复：并发去重，如果该群已有摘要任务在执行则跳过，防止竞态
        if group_id in self._summary_in_progress:
            self._debug(f"[ConversationMemory] 群={group_id} 摘要任务已在执行，跳过本次触发")
            return

        # 需要压缩的记录：除最近 recent_rounds_keep 轮外的所有记录
        records_to_summarize = list(history)[:-threshold]
        if not records_to_summarize:
            return

        # 构建待压缩的对话文本
        lines = []
        for record in records_to_summarize:
            role = record[0]
            text = record[1]
            sender = record[3] if len(record) > 3 else ""
            if role == "user":
                role_label = sender if sender else "用户"
            else:
                role_label = "Bot"
            lines.append(f"{role_label}: {text}")
        conversation_text = "\n".join(lines)

        # 异步触发摘要（通过 asyncio.create_task）
        try:
            import asyncio
            # P1-9 修复：标记该群摘要任务正在执行，防止并发触发
            self._summary_in_progress.add(group_id)
            asyncio.create_task(
                self._summarize_conversation(group_id, conversation_text)
            )
        except Exception as e:
            # 创建任务失败时立即清除标记
            self._summary_in_progress.discard(group_id)
            logger.warning(f"[ConversationMemory] 摘要任务创建失败: {e}")

    async def _summarize_conversation(self, group_id: str, conversation_text: str):
        """使用小模型对对话历史进行摘要压缩"""
        model = self.summary_model or self.compression_model
        if not model:
            # 没有配置摘要模型，使用当前提供者
            provider = self.context.get_using_provider()
        else:
            provider = self.context.get_provider_by_id(model)

        if not provider:
            logger.warning(f"[ConversationMemory] 未找到摘要模型，跳过摘要")
            return

        try:
            prompt = (
                "请将以下对话历史压缩为简洁摘要，要求：\n"
                "1. 保留所有关键信息和话题\n"
                "2. 保留决策和结论\n"
                "3. 去除寒暄和重复内容\n"
                "4. 摘要长度不超过原文的20%\n\n"
                f"对话历史：\n{conversation_text}"
            )

            resp = await provider.text_chat(
                prompt=prompt,
                session_id=f"summary_{group_id}_{int(time.time())}",
            )

            summary = ""
            if hasattr(resp, 'completion_text'):
                summary = resp.completion_text or ""
            elif hasattr(resp, 'result'):
                summary = str(resp.result) if resp.result else ""

            if summary:
                # P0-4 修复：限制摘要保留数量，防止 _conversation_summaries 无限增长
                # 原逻辑：每次摘要追加到 existing 后面，无上限，长期运行导致 prompt 膨胀
                # 新逻辑：最多保留最近 5 条摘要，超出时丢弃最旧的
                MAX_SUMMARIES = 5
                existing = self._conversation_summaries.get(group_id, "")
                if existing:
                    # 按 "---近期摘要---" 分隔，保留最近 MAX_SUMMARIES-1 条 + 新摘要
                    segments = existing.split("\n\n---近期摘要---\n")
                    if len(segments) >= MAX_SUMMARIES:
                        # 丢弃最旧的，保留最近 MAX_SUMMARIES-1 条
                        segments = segments[-(MAX_SUMMARIES - 1):]
                    self._conversation_summaries[group_id] = "\n\n---近期摘要---\n".join(segments + [summary])
                else:
                    self._conversation_summaries[group_id] = summary

                # 从历史中移除已摘要的记录
                history = self._get_conversation_history(group_id)
                threshold = self.recent_rounds_keep * 2
                while len(history) > threshold:
                    history.popleft()

                logger.info(
                    f"[ConversationMemory] 群={group_id} 摘要生成完成 "
                    f"原文={len(conversation_text)}字符 → 摘要={len(summary)}字符 "
                    f"剩余历史={len(history)}条"
                )
            else:
                logger.warning(f"[ConversationMemory] 群={group_id} 摘要生成失败，返回为空")

        except asyncio.CancelledError:
            # 诊断（v1.7.3）：记录完整 traceback，定位 CancelledError 真实来源
            import traceback as _tb
            logger.warning(
                f"[ConversationMemory] 群={group_id} 摘要被取消（CancelledError）\n"
                f"Traceback:\n{_tb.format_exc()}"
            )
        except Exception as e:
            logger.warning(f"[ConversationMemory] 群={group_id} 摘要异常: {e}")
        finally:
            # P1-9 修复：无论成功/失败/取消，都清除摘要任务标记，允许后续触发
            self._summary_in_progress.discard(group_id)

    def _format_conversation_memory(self, group_id: str) -> str:
        """格式化对话记忆，用于注入到 LLM 请求中

        返回格式：
        <conversation_memory>
        [历史摘要]
        ---近期摘要---
        ...

        [最近N轮原文对话]
        用户: xxx
        Bot: xxx
        </conversation_memory>
        """
        parts = []

        # 添加摘要
        summary = self._conversation_summaries.get(group_id, "")
        if summary:
            parts.append(f"<历史对话摘要>\n{summary}\n</历史对话摘要>")

        # 添加最近N轮原文
        history = self._get_conversation_history(group_id)
        if history:
            recent_lines = []
            for record in list(history):
                role = record[0]
                text = record[1]
                sender = record[3] if len(record) > 3 else ""
                if role == "user":
                    role_label = sender if sender else "用户"
                else:
                    role_label = "Bot"
                    # P1 根因修复：对 BOT 回复做要点化处理，避免 LLM 延续自己的原文措辞
                    # 当 LLM 看到自己说过的原文时，会倾向于复制相同的表达（如重复"<BOT_NAME>都看不下去了"），
                    # 只保留话题要点而非原文，从根源上切断 LLM 复制自身措辞的倾向
                    text = self._summarize_bot_reply_for_memory(text)
                # 截断过长的单条消息
                if len(text) > 200:
                    text = text[:100] + "..."
                # 消息内容歧义消除：同 _format_context 中的处理逻辑
                if re.match(r'^[\w\u4e00-\u9fff]+[:：]', text):
                    text = f"「{text}」"
                recent_lines.append(f"{role_label}: {text}")

            if recent_lines:
                parts.append(f"<近期对话>\n" + "\n".join(recent_lines) + "\n</近期对话>")

        if not parts:
            return ""

        return "<conversation_memory>\n" + "\n\n".join(parts) + "\n</conversation_memory>"

    def _summarize_bot_reply_for_memory(self, text: str) -> str:
        """将 BOT 的回复原文转为要点摘要，用于对话记忆注入

        根因修复：LLM 看到自己说过的原文时，会倾向于延续相同的措辞和表达方式，
        导致重复输出（如连续两次说"<BOT_NAME>都看不下去了"）。
        将原文转为要点摘要后，LLM 只知道"自己之前回应过某个话题"，
        但看不到原文措辞，从而不会复制自己的表达。

        策略：
        - 短回复（<=15字）：保留原文（简短附和本身不易重复）
        - 中等回复（16-60字）：提取首句 + 末句要点
        - 长回复（>60字）：提取首句要点 + 末句要点，中间省略
        """
        text = text.strip()
        if not text:
            return text

        # 短回复保留原文
        if len(text) <= 15:
            return text

        # 按句号/问号/感叹号/换行分割为句子
        sentences = re.split(r'(?<=[。？！\n])', text)
        sentences = [s.strip() for s in sentences if s.strip()]

        if len(sentences) <= 1:
            # 单句长回复：截取前半部分 + 省略标记
            if len(text) > 40:
                return text[:30] + "...(已回应此话题)"
            return text

        if len(sentences) == 2:
            # 两句：首句 + 末句要点
            first = sentences[0]
            last = sentences[-1]
            if len(first) > 30:
                first = first[:25] + "..."
            if len(last) > 30:
                last = last[:25] + "..."
            return f"{first}...{last}(已回应)"

        # 三句及以上：首句 + 末句要点
        first = sentences[0]
        last = sentences[-1]
        if len(first) > 25:
            first = first[:20] + "..."
        if len(last) > 25:
            last = last[:20] + "..."
        return f"{first}...(省略)...{last}(已回应)"

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """消息发送前拦截，过滤注入的上下文标签和思考标签，替换路由结果"""
        if not event.get_extra("smart_wakeup_triggered"):
            return

        # 清除 LLM 执行中标志（on_decorating_result 在 LLM 结果返回后触发，
        # 无论结果是否为空都会执行，比 after_message_sent 更可靠）
        group_id = event.message_obj.group_id
        if group_id and group_id in self._llm_running_groups:
            del self._llm_running_groups[group_id]
            # 取消主动超时定时器（正常路径已清除标志，无需等待超时）
            self._cancel_llm_flag_timer(group_id)

        # 如果小模型路由已完成，用小模型的回复替换主模型的输出
        route_result = event.get_extra("smart_wakeup_route_result")
        if route_result:
            result = event.get_result()
            if result and result.chain:
                # 替换主模型的输出为小模型的回复（Plain 已在文件顶部全局导入）
                result.chain = [Plain(route_result)]
                logger.info(
                    f"[ModelRoute] 已用小模型回复替换主模型输出 "
                    f"回复长度={len(route_result)}"
                )
            # 清除标记，避免重复替换
            event.set_extra("smart_wakeup_route_result", None)

        # P0-3 修复：删除此处对 _record_assistant_message 的调用
        # 原因：此处记录的是过滤前的 LLM 原始输出（可能含思考标签、上下文标签），
        # 而 after_message_sent 会再次记录过滤后的文本，导致同一回复被记录两次且内容不一致。
        # 未过滤的标签会进入 _conversation_history，下次注入时 LLM 看到标签可能模仿输出导致泄漏。
        # 现在仅在 after_message_sent 中记录（此时已过滤且确认发送）。

        # 过滤上下文标签和思考标签
        result = event.get_result()
        if not result or not result.chain:
            return

        # 首次运行时探测 result/event 的可用属性（仅记录一次）
        if not getattr(self, '_attr_probe_done', False):
            self._attr_probe_done = True
            _result_attrs = [a for a in dir(result) if not a.startswith('_') and not callable(getattr(result, a, None))]
            _event_llm_attrs = [a for a in dir(event) if 'llm' in a.lower() or 'resp' in a.lower() or 'tool' in a.lower()]
            logger.info(
                f"[ToolCallDetect] 属性探测 | result 属性={_result_attrs} | "
                f"event LLM相关属性={_event_llm_attrs}"
            )
            # 检查关键属性
            for attr in ['tool_calls', 'finish_reason', 'result', 'completion_text']:
                val = getattr(result, attr, 'NOT_FOUND')
                if val != 'NOT_FOUND':
                    logger.info(f"[ToolCallDetect] result.{attr} = {val}")
            for attr in ['_llm_response', 'llm_response', '_resp', 'resp']:
                val = getattr(event, attr, 'NOT_FOUND')
                if val != 'NOT_FOUND':
                    logger.info(f"[ToolCallDetect] event.{attr} type={type(val).__name__}")

        # 记录过滤前的文本状态（供分段调试）
        pre_filter_text = ""
        for comp in result.chain:
            if hasattr(comp, "text") and comp.text:
                pre_filter_text += comp.text

        for comp in result.chain:
            if hasattr(comp, "text") and comp.text:
                # 始终过滤注入的系统上下文标签（这些绝不应泄露给用户）
                filtered = self._filter_context_tags(comp.text)
                # 过滤 LLM 返回的重复回复（如 GLM 用 ``` 分隔的多版本）
                filtered = self._filter_duplicate_response(filtered)
                # 可选过滤思考标签（兜底机制）
                if self.filter_thinking_tags:
                    filtered = self._filter_thinking_tags(filtered)
                if filtered != comp.text:
                    comp.text = filtered

        # 分段调试：记录过滤后的文本状态，帮助排查 splitter 分段问题
        post_filter_text = ""
        for comp in result.chain:
            if hasattr(comp, "text") and comp.text:
                post_filter_text += comp.text
        if pre_filter_text != post_filter_text:
            self._debug(
                f"[OutputFilter] 过滤前={len(pre_filter_text)}字 → 过滤后={len(post_filter_text)}字 | "
                f"文本='{post_filter_text[:80]}'"
            )
        # 记录最终输出文本长度和分段点信息，供判断 splitter 是否能正确分段
        text_len = len(post_filter_text)
        newline_count = post_filter_text.count('\n')
        sentence_end_count = sum(1 for c in post_filter_text if c in '。？！!?;；')
        self._debug(
            f"[OutputInfo] 最终输出={text_len}字 换行={newline_count}处 句末标点={sentence_end_count}处 | "
            f"chain组件数={len(result.chain)}"
        )

        # ─── 输出去重：防止 LLM 工具调用或重复响应导致同一内容被多次发送 ───
        group_id = event.message_obj.group_id
        if group_id and post_filter_text:
            if self._is_duplicate_content(group_id, post_filter_text):
                logger.warning(
                    f"[Dedup] 检测到重复输出，已拦截 | 群={group_id} "
                    f"内容='{post_filter_text[:80]}'"
                )
                # P1-13 修复：更新 _last_bot_reply_text 为被拦截的内容，
                # 防止下次语义去重比较的是更早的回复导致误判或漏判
                self._last_bot_reply_text[group_id] = post_filter_text
                self._last_bot_reply_time[group_id] = time.time()
                # 设置 suppressed 标记，让 after_message_sent 跳过记录到对话记忆
                event.set_extra("smart_wakeup_suppressed", True)
                result.chain.clear()
                return
            # P1-14 修复：移除此处的提前记录，改为在确认不会被后续分支清空后才记录
            # 原逻辑在 tool_call/抑制/重新生成前就记录，导致实际发送内容与缓存不一致

        # ─── 语义去重：检测短时间内两次概率唤醒生成语义相近的重复回复 ───
        # 核心思路：两次独立的概率唤醒触发两次 LLM 调用，LLM 基于相似上下文
        # 可能生成语义重复的回复。检测到重复时，先尝试重新生成（注入防重复提示），
        # 重新生成仍重复才抑制——从根源解决"生成重复内容"的问题，而非堵住输出
        if group_id and post_filter_text:
            wakeup_type = event.get_extra("wakeup_type", "")
            is_direct_call = wakeup_type in ("name_trigger", "reply_to_bot", "keyword_trigger")
            # 仅对非直接呼叫场景检测（概率唤醒、冷场救场）
            # 直接呼叫是用户主动请求，即使内容相似也应回复
            if not is_direct_call:
                last_text = self._last_bot_reply_text.get(group_id, "")
                last_time = self._last_bot_reply_time.get(group_id, 0)
                now = time.time()
                # 仅在时间窗口内检测
                if last_text and (now - last_time) <= self._SIMILARITY_WINDOW:
                    similarity = self._calc_text_similarity(post_filter_text, last_text)
                    # 相似度阈值：0.55 以上视为语义重复
                    if similarity >= 0.55:
                        logger.info(
                            f"[SemanticDedup] 检测到语义重复回复 | 群={group_id} "
                            f"相似度={similarity:.2f} 距上次={now - last_time:.0f}秒 | "
                            f"新回复='{post_filter_text[:60]}' | 上次='{last_text[:60]}'"
                        )
                        # 尝试重新生成：注入防重复提示，让 LLM 换一个角度
                        regenerated = await self._regenerate_with_anti_repeat(
                            event, group_id, post_filter_text, last_text
                        )
                        if regenerated:
                            # 重新生成成功，替换原输出
                            result.chain = [Plain(regenerated)]
                            # 更新去重缓存中的记录
                            self._record_sent_content(group_id, regenerated)
                            # P1-14 修复：标记已记录，防止后续重复记录 post_filter_text（已更新为 regenerated）
                            event.set_extra("smart_wakeup_content_recorded", True)
                            logger.info(
                                f"[SemanticDedup] 重新生成成功 | 群={group_id} "
                                f"新内容='{regenerated[:60]}'"
                            )
                            # 更新 post_filter_text 供后续流程使用
                            post_filter_text = regenerated
                        else:
                            # 重新生成失败或仍相似，抑制该回复
                            logger.info(
                                f"[SemanticDedup] 重新生成未改善，抑制回复 | 群={group_id}"
                            )
                            self._suppress_reply(event, group_id, post_filter_text)
                            return
                    else:
                        self._debug(
                            f"[SemanticDedup] 相似度未达阈值 | 群={group_id} "
                            f"相似度={similarity:.2f}"
                        )

        # ─── 回复抑制：在输出去重之后、分段之前检测是否应拦截 ───
        if self.reply_suppression_enabled and group_id and post_filter_text:
            wakeup_type = event.get_extra("wakeup_type", "")
            # 判断是否为直接呼叫（名称匹配或回复BOT），直接呼叫时不应抑制
            is_direct_call = wakeup_type in ("name_trigger", "reply_to_bot", "keyword_trigger")

            suppressed = False

            # 方案A：关键词匹配拦截
            if self.reply_suppression_mode in ("keyword", "both"):
                if not is_direct_call:
                    keyword = self.reply_suppression_keyword
                    stripped = post_filter_text.strip()
                    # 匹配规则1：完全等于关键词
                    if stripped == keyword:
                        suppressed = True
                        event.set_extra("suppression_source", "keyword")
                        self._debug(f"[回复抑制] 关键词匹配拦截 | 群={group_id} 原始输出='{post_filter_text[:80]}'")
                    # 匹配规则2：去除空白后仅包含关键词
                    elif stripped.replace(' ', '').replace('\n', '').replace('\t', '') == keyword:
                        suppressed = True
                        event.set_extra("suppression_source", "keyword")
                        self._debug(f"[回复抑制] 关键词匹配拦截(含空白) | 群={group_id} 原始输出='{post_filter_text[:80]}'")
                    # 匹配规则3：以关键词开头（含混合输出，v1.9.8 加固）
                    elif stripped.startswith(keyword):
                        # v1.9.8 修复（2026-09-16 22:32 思考泄露事故）：
                        # GLM-5.2 等推理模型会把 "[SKIP] + 决策理由 + 偶发回复"
                        # 全部写进 content 正文（如 "[SKIP]\n\n想了想，我不确定...
                        # 简短接一句即可。\n\n啥游戏里的，怪物猎人？"），
                        # 原逻辑"后续无实质内容才拦截"导致混合体原样发出。
                        # 协议要求输出 [SKIP] 时不得输出任何其他内容；
                        # 违反协议的混合输出不可信——概率唤醒场景沉默无害、
                        # 泄露有害，安全优先：前缀命中即整体拦截。
                        suppressed = True
                        event.set_extra("suppression_source", "keyword_prefix")
                        remainder_preview = stripped[len(keyword):].strip()[:60]
                        self._debug(
                            f"[回复抑制] 关键词前缀拦截(含混合输出) | 群={group_id} "
                            f"[SKIP]后内容='{remainder_preview}'"
                        )
                    else:
                        self._debug(f"[回复抑制] 关键词检测未命中 | 群={group_id}")
                else:
                    self._debug(f"[回复抑制] 跳过检测（直接呼叫） | 群={group_id} 触发={wakeup_type}")

            # 方案B：独立小LLM合规判别
            if not suppressed and self.reply_suppression_mode in ("judge", "both"):
                if not is_direct_call:
                    try:
                        judge_result = await self._judge_reply_compliance(group_id, post_filter_text)
                        if judge_result and judge_result.get("verdict") == "block":
                            suppressed = True
                            event.set_extra("suppression_source", "judge")
                            reason = judge_result.get("reason", "未知")
                            self._debug(f"[回复抑制] 合规判别拦截 | 群={group_id} 原因={reason}")
                        else:
                            self._debug(f"[回复抑制] 合规判别通过 | 群={group_id}")
                    except Exception as e:
                        logger.warning(f"[回复抑制] 合规判别异常，保守放过 | 群={group_id} 错误={e}")
                        self._stats["suppression_stats"]["judge_errors"] += 1
                else:
                    self._debug(f"[回复抑制] 跳过判别（直接呼叫） | 群={group_id} 触发={wakeup_type}")

            # 执行拦截
            if suppressed:
                self._suppress_reply(event, group_id, post_filter_text)
                return

        # ─── tool_call 检测：防止 LLM 工具调用导致重复输出 ───
        # 当 LLM 使用 send_message_to_user 时，AstrBot 的 tool_loop 会独立发送完整消息，
        # 如果正常流程也发送，用户会看到重复。检测到 tool_call 时清空 result.chain，
        # 让 tool_loop 成为唯一发送通道。
        has_tool_call = False

        # 策略1: 检查 event flag（由 on_llm_response 或 on_using_llm_tool 设置）
        if event.get_extra("smart_wakeup_has_tool_call"):
            has_tool_call = True
            logger.info("[ToolCallDetect] on_decorating_result 检测到 tool_call flag（来自 hook）")

        # 策略2: 检查 result 对象的 tool_calls/finish_reason 属性
        if not has_tool_call:
            _result_tc = getattr(result, 'tool_calls', None)
            _result_fr = getattr(result, 'finish_reason', None)
            if _result_tc and len(_result_tc) > 0:
                has_tool_call = True
                logger.info(f"[ToolCallDetect] on_decorating_result 检测到 result.tool_calls={_result_tc}")
            elif _result_fr == 'tool_calls':
                has_tool_call = True
                logger.info("[ToolCallDetect] on_decorating_result 检测到 result.finish_reason='tool_calls'")

        # 策略3: 检查 event 对象的 LLM 响应相关属性
        if not has_tool_call:
            _event_resp = getattr(event, '_llm_response', None) or getattr(event, 'llm_response', None)
            if _event_resp:
                _event_fr = getattr(_event_resp, 'finish_reason', None)
                _event_tc = getattr(_event_resp, 'tool_calls', None)
                if _event_fr == 'tool_calls' or (_event_tc and len(_event_tc) > 0):
                    has_tool_call = True
                    logger.info(f"[ToolCallDetect] on_decorating_result 检测到 event LLM 响应中的 tool_call | finish_reason={_event_fr}")

        # 策略4: 深度探测 event 内部属性（遍历所有属性查找 tool_call/finish_reason 信息）
        if not has_tool_call:
            for attr_name in dir(event):
                if attr_name.startswith('_'):
                    continue
                try:
                    attr_val = getattr(event, attr_name, None)
                    if attr_val is None or callable(attr_val):
                        continue
                    # 检查属性值是否有 finish_reason='tool_calls'
                    fr = getattr(attr_val, 'finish_reason', None)
                    if fr == 'tool_calls':
                        has_tool_call = True
                        logger.info(
                            f"[ToolCallDetect] on_decorating_result 深度探测发现 tool_call | "
                            f"event.{attr_name}.finish_reason='tool_calls'"
                        )
                        break
                    # 检查属性值是否有 tool_calls 列表
                    tc = getattr(attr_val, 'tool_calls', None)
                    if tc and len(tc) > 0:
                        has_tool_call = True
                        logger.info(
                            f"[ToolCallDetect] on_decorating_result 深度探测发现 tool_call | "
                            f"event.{attr_name}.tool_calls={tc}"
                        )
                        break
                except Exception:
                    continue

        if has_tool_call:
            logger.info(
                f"[ToolCallDetect] 检测到 tool_call，清空 result.chain 防止重复发送 | "
                f"群={group_id} 内容='{post_filter_text[:80]}'"
            )
            # 用零宽空格替代输出（而非清空），防止 intelligent_retry 插件重试
            result.chain.clear()
            result.chain.append(Plain("\u200b"))
            # P1-12 修复：设置 suppressed 标记，让 after_message_sent 跳过记录零宽空格到对话记忆
            event.set_extra("smart_wakeup_suppressed", True)
            return

        # P1-14 修复：只在实际确认发送后才记录到去重缓存
        # 此时已通过所有拦截分支（去重/语义去重/回复抑制/tool_call），content 确认将被发送
        # 语义去重重新生成场景已通过 smart_wakeup_content_recorded 标记避免重复记录
        if group_id and post_filter_text and not event.get_extra("smart_wakeup_content_recorded"):
            self._record_sent_content(group_id, post_filter_text)

        # ─── 分段模块处理 ───
        # 仅对本插件主动触发的 LLM 回复做分段，其他插件的输出不应被分段
        if self.splitter_enabled and event.is_at_or_wake_command:
            await self._splitter_process(event)

    def _suppress_reply(self, event: AstrMessageEvent, group_id: str, response_text: str):
        """执行回复抑制：清空输出、撤销对话记忆、恢复精力

        注意：不能使用 result.chain.clear() 清空输出，因为 intelligent_retry 插件
        会将空回复视为 LLM 调用失败并触发重试，重试结果绕过 on_decorating_result
        直接发送，导致 [SKIP] 等抑制标记原样输出给用户。
        解决方案：用零宽空格替换输出内容，使 retry 插件判定为"非空回复"而跳过重试。
        """
        # ── 安全话语替代检查 ──
        # 当回复被抑制时，以一定概率输出简短附和语替代完全沉默
        # 仅对非直接呼叫场景生效（直接呼叫不应被抑制，也不应输出安全话语）
        if self.light_response_enabled and group_id:
            wakeup_type = event.get_extra("wakeup_type", "")
            is_direct_call = wakeup_type in ("name_trigger", "reply_to_bot", "keyword_trigger")
            if not is_direct_call:
                now = time.time()
                # 检查1：冷却时间
                cooldown = self._get_group_param(group_id, "light_response_cooldown", self.light_response_cooldown)
                last_light = self._light_response_last.get(group_id, 0)
                if now - last_light >= cooldown:
                    # 检查2：每小时上限（含小时重置逻辑）
                    hour_start = self._light_response_hour_reset.get(group_id, now)
                    if now - hour_start >= 3600:
                        # 进入新小时，重置计数器
                        self._light_response_count[group_id] = 0
                        self._light_response_hour_reset[group_id] = now
                    count = self._light_response_count.get(group_id, 0)
                    max_per_hour = self._get_group_param(group_id, "light_response_max_per_hour", self.light_response_max_per_hour)
                    if count < max_per_hour:
                        # 检查3：概率掷骰
                        prob = self._get_group_param(group_id, "light_response_prob", self.light_response_prob)
                        if random.random() < prob:
                            light_msg = random.choice(self._light_response_pool)
                            # 替换输出为安全话语（而非零宽空格）
                            result = event.get_result()
                            if result:
                                result.chain = [Plain(light_msg)]
                            # 标记为轻量回应（不设置 smart_wakeup_suppressed，让 after_message_sent 正常记录）
                            event.set_extra("smart_wakeup_light_response", True)
                            # 更新频率统计
                            self._light_response_last[group_id] = now
                            self._light_response_count[group_id] = count + 1
                            # 撤销对话记忆中的 LLM 原始输出（被抑制了）
                            if self.conversation_memory_enabled and group_id in self._conversation_history:
                                history = self._conversation_history[group_id]
                                if history and history[-1][0] == "assistant":
                                    removed = history.pop()
                                    self._debug(f"[安全话语] 撤销LLM原始输出 | 群={group_id} 内容='{removed[1][:50]}'")
                            # 注意：不恢复精力（BOT 确实发言了）
                            # 注意：不更新抑制统计（这不是抑制，是替代输出）
                            logger.info(
                                f"[安全话语] 替代沉默 | 群={group_id} 内容='{light_msg}' "
                                f"本小时第{count + 1}/{max_per_hour}次"
                            )
                            return  # 不执行后续抑制逻辑

        # 1. 用零宽空格替换输出（而非清空），防止 intelligent_retry 插件重试
        result = event.get_result()
        if result:
            result.chain = [Plain("\u200b")]

        # 标记事件为已抑制，after_message_sent 据此跳过记录
        event.set_extra("smart_wakeup_suppressed", True)

        # 2. 撤销对话记忆
        # P0-3 修复后 on_decorating_result 不再调用 _record_assistant_message，
        # assistant 消息由 after_message_sent 记录。_suppress_reply 在 on_decorating_result
        # 中调用，此时 history 最后一条是 user 消息（非 assistant），无需 pop。
        # suppressed 标记已确保 after_message_sent 不会记录被抑制的回复。

        # 3. 恢复精力值（精力在 _trigger_wake 后已消耗，需恢复）
        if group_id in self._energy_states:
            state = self._energy_states[group_id]
            decay_rate = self._get_group_param(group_id, "energy_decay_rate", self.energy_decay_rate)
            state.energy = min(1.0, state.energy + decay_rate)
            state.total_replies = max(0, state.total_replies - 1)
            self._debug(f"[回复抑制] 精力恢复 | 群={group_id} 精力恢复至 {state.energy:.2f}")

        # 4. 更新统计（根据实际拦截的方案更新对应计数器）
        suppression_source = event.get_extra("suppression_source", "")
        if suppression_source == "keyword":
            self._stats["suppression_stats"]["keyword_suppressed"] += 1
        elif suppression_source == "judge":
            self._stats["suppression_stats"]["judge_suppressed"] += 1

        logger.info(
            f"[回复抑制] 已拦截 | 群={group_id} 触发={event.get_extra('wakeup_type', '')} "
            f"来源={suppression_source} "
            f"内容='{response_text[:80]}'"
        )

    async def _judge_reply_compliance(self, group_id: str, reply_text: str) -> dict | None:
        """使用独立小模型判别回复是否适宜发出

        Returns:
            dict | None: 解析后的判别结果 {"verdict": "pass"/"block", "reason": "..."}，
                        调用失败返回 None
        """
        # 获取判别模型 provider
        judge_model_id = self.reply_suppression_judge_model
        if not judge_model_id:
            # 回退到摘要压缩模型
            judge_model_id = self.compression_model
        if not judge_model_id:
            # 回退到路由小模型
            judge_model_id = self.routing_small_model
        if not judge_model_id:
            logger.warning("[回复抑制] 未配置判别模型，跳过合规判别")
            return None

        provider = self.context.get_provider_by_id(judge_model_id)
        if not provider:
            logger.warning(f"[回复抑制] 未找到判别模型 {judge_model_id}，跳过合规判别")
            return None

        # 构造上下文摘要（最近5条消息）
        buffer = self._get_buffer(group_id)
        context_lines = []
        for sender, text, ts, meta in list(buffer)[-5:]:
            context_lines.append(f"{sender}: {text}")
        context_summary = "\n".join(context_lines) if context_lines else "（无上下文）"

        # 使用自定义或内置默认判别提示词
        if self.reply_suppression_judge_prompt:
            judge_prompt = self.reply_suppression_judge_prompt
        else:
            judge_prompt = _DEFAULT_JUDGE_PROMPT

        # 构造判别请求
        user_message = (
            f"<群聊上下文>\n{context_summary}\n</群聊上下文>\n\n"
            f"<BOT回复>\n{reply_text}\n</BOT回复>\n\n"
            f"{judge_prompt}"
        )

        try:
            resp = await provider.text_chat(
                prompt=user_message,
                session_id=f"judge_{group_id}_{int(time.time())}",
            )
            if not resp or not (hasattr(resp, 'completion_text') and resp.completion_text):
                return None

            # 解析 JSON 输出
            import json
            result_text = resp.completion_text.strip()
            # 尝试提取 JSON（可能被 markdown 代码块包裹）
            json_match = re.search(r'\{[^}]+\}', result_text)
            if json_match:
                result = json.loads(json_match.group())
                if "verdict" in result:
                    self._stats["suppression_stats"]["judge_passed" if result["verdict"] == "pass" else "judge_suppressed"] += 1
                    return result

            # 无法解析为 JSON，保守放过
            self._debug(f"[回复抑制] 判别结果无法解析 | 原始输出='{result_text[:100]}'")
            return {"verdict": "pass", "reason": "解析失败，保守放过"}

        except Exception as e:
            logger.warning(f"[回复抑制] 合规判别调用异常 | 错误={e}")
            self._stats["suppression_stats"]["judge_errors"] += 1
            return None

    # ─── 分段模块 ───

    def _calculate_segment_delay(self, text: str) -> float:
        """根据文本长度计算分段发送延迟，模拟真人输入节奏"""
        if self.delay_strategy == "random":
            return random.uniform(self.random_min, self.random_max)
        if self.delay_strategy == "log":
            return min(self.log_base + self.log_factor * math.log(len(text) + 1), 5.0)
        if self.delay_strategy == "linear":
            return self.linear_base + (len(text) * self.linear_factor)
        return self.fixed_delay

    def _trim_segment_blank_lines(self, segment: list) -> None:
        """清理段落首尾空行"""
        f_p = next((c for c in segment if isinstance(c, Plain)), None)
        l_p = next((c for c in reversed(segment) if isinstance(c, Plain)), None)
        if f_p and f_p.text:
            f_p.text = re.sub(r'^(?:[ \t]*\r?\n)+', '', f_p.text)
        if l_p and l_p.text:
            l_p.text = re.sub(r'(?:\r?\n[ \t]*)+$', '', l_p.text)

    def _strip_segment_trailing_punct(self, segment: list) -> None:
        """剔除段落末尾的指定标点，使分段更符合自然聊天习惯"""
        if not self.strip_trailing_punct_enabled or not self.strip_trailing_punct_chars:
            return
        l_p = next((c for c in reversed(segment) if isinstance(c, Plain)), None)
        if l_p and l_p.text:
            l_p.text = l_p.text.rstrip(self.strip_trailing_punct_chars)

    def _is_abbreviation_period(self, text: str, pos: int, delim_len: int) -> bool:
        """判断当前位置的句号是否属于缩写/小数/域名，而非句末标点。

        返回 True 表示"是缩写等，不应在此分段"。
        仅在分隔符包含英文句号时调用。
        """
        n = len(text)
        # 取分隔符前一个字符和分隔符后一个字符
        p_c = text[pos - 1] if pos > 0 else ""
        n_c = text[pos + delim_len] if pos + delim_len < n else ""

        # ── 规则1: 前后都是字母 → 缩写 (U.S.Army)
        if re.match(r"^[a-zA-Z]$", p_c) and re.match(r"^[a-zA-Z]$", n_c):
            return True

        # ── 规则2: 前为数字、后为数字 → 小数 (3.5, GPT 4.0)
        if re.match(r"^\d$", p_c) and re.match(r"^\d$", n_c):
            return True

        # ── 规则3: 前为字母、后为数字 → 版本号 (v2.0, GPT3.5)
        if re.match(r"^[a-zA-Z]$", p_c) and re.match(r"^\d$", n_c):
            return True

        # ── 规则4: 前为数字、后为字母 → 可能是缩写 (2nd.) 或版本号
        # 但数字后接字母+句号更可能是列表序号 "1.Hello"，需要分段
        # 这里保守处理：只有当后面紧跟空格+大写字母时才分段
        # 暂不拦截，让后续规则处理

        # ── 规则5: 句号在行首 → 域名 (.com, .org)
        if pos == 0 or (pos > 0 and text[pos - 1] in " \t\n"):
            return True

        # ── 规则6: 前为字母/数字、后为空格+非大写 → 可能是缩写结尾 (etc. , vs. )
        # 但 "etc. the" 也可能需要分段，所以仅当后接空格+小写字母时保守不分段
        # 实际上这种情况极少，暂不处理

        # ── 规则7: 后面紧跟句号 → 缩写链 (U.S.)
        if n_c == ".":
            return True

        # ── 规则8: 前面紧跟句号 → 缩写链 (.S.)
        if pos >= 2 and text[pos - 2] == ".":
            return True

        return False

    def _smart_split_text(self, text: str, pattern: str, segments: list,
                           buffer: list, start_w: int = 0, ideal: int = 0) -> int:
        """智能分段：避免在引号/成对符号/代码块内部切断，正确处理英文缩写和小数"""
        stack = []
        compiled = re.compile(pattern)
        i = 0
        n = len(text)
        chunk = ""
        weight = start_w

        while i < n:
            # 代码块保护
            if text.startswith("```", i):
                idx = text.find("```", i + 3)
                if idx != -1:
                    chunk += text[i:idx + 3]
                    weight += idx + 3 - i
                    i = idx + 3
                    continue
                else:
                    chunk += text[i:]
                    weight += n - i
                    break
            # think标签保护
            if text.startswith("<think>", i):
                idx = text.find("</think>", i + 7)
                if idx != -1:
                    chunk += text[i:idx + 8]
                    weight += idx + 8 - i
                    i = idx + 8
                    continue
                else:
                    chunk += text[i:]
                    weight += n - i
                    break

            # 省略号保护：... 或 …… 作为整体保留，不作为分段点
            if text.startswith("...", i):
                chunk += "..."
                weight += 3
                i += 3
                continue
            if text.startswith("……", i):
                chunk += "……"
                weight += 2
                i += 2
                continue

            match = compiled.match(text, pos=i)
            if match:
                delim = match.group()
                should = False
                if not stack or "\n" in delim:
                    should = True
                    # 均分模式：段长不足时不切
                    if ideal > 0 and weight < ideal * 0.4:
                        should = False
                    # 英文句号缩写检测：U.S.Army / 3.5 / .com 不切
                    if should and "." in delim and "\n" not in delim:
                        if self._is_abbreviation_period(text, i, len(delim)):
                            should = False
                if should:
                    chunk += delim
                    buffer.append(Plain(chunk))
                    segments.append(buffer[:])
                    buffer.clear()
                    chunk = ""
                    weight = 0
                    i += len(delim)
                else:
                    chunk += delim
                    weight += len(delim)
                    i += len(delim)
                continue

            # 均分模式：段长超上限时在次级标点处切分
            if ideal > 0 and weight >= ideal * 0.9 and not stack:
                sec = self._secondary_pattern.match(text, pos=i)
                if sec:
                    delim = sec.group()
                    chunk += delim
                    buffer.append(Plain(chunk))
                    segments.append(buffer[:])
                    buffer.clear()
                    chunk = ""
                    weight = 0
                    i += len(delim)
                    continue

            char = text[i]
            if char in self._quote_chars:
                if stack and stack[-1] == char:
                    stack.pop()
                else:
                    stack.append(char)
            elif not stack and char in self._pair_map:
                stack.append(char)
            elif stack and char == self._pair_map.get(stack[-1]):
                stack.pop()

            chunk += char
            i += 1
            weight += 1 if not char.isspace() else 0

        if chunk:
            buffer.append(Plain(chunk))
        return weight

    def _split_chain(self, chain: list, pattern: str, ideal: int = 0) -> list:
        """将消息链按标点分段，非文本组件跟随下一段"""
        segments = []
        buffer = []
        weight = 0

        for comp in chain:
            if isinstance(comp, Plain):
                if not comp.text:
                    continue
                if self.enable_smart_split:
                    weight = self._smart_split_text(
                        comp.text, pattern, segments, buffer, weight, ideal
                    )
                else:
                    # 简单正则分段
                    parts = re.split("({})".format(pattern), comp.text)
                    tmp = ""
                    for p in parts:
                        if not p:
                            continue
                        if re.fullmatch(pattern, p):
                            tmp += p
                            buffer.append(Plain(tmp))
                            segments.append(buffer[:])
                            buffer.clear()
                            tmp = ""
                        else:
                            tmp += p
                    if tmp:
                        buffer.append(Plain(tmp))
                    weight = 0
            else:
                # 非文本组件：图片单独一段，其他跟随下一段
                c_type = type(comp).__name__.lower()
                if "image" in c_type or "record" in c_type:
                    if buffer:
                        segments.append(buffer[:])
                        buffer.clear()
                    segments.append([comp])
                    weight = 0
                else:
                    # Reply/At/Face 等跟随下一段
                    if buffer:
                        segments.append(buffer[:])
                        buffer.clear()
                        weight = 0
                    buffer.append(comp)

        if buffer:
            segments.append(buffer)
        return [s for s in segments if s]

    async def _splitter_process(self, event: AstrMessageEvent):
        """分段模块核心处理，在 on_decorating_result 中调用"""
        result = event.get_result()
        if not result or not result.chain:
            return
        if getattr(result, "__lingxi_split_processed", False):
            return

        # 仅处理唤醒触发的消息
        if not event.get_extra("smart_wakeup_triggered"):
            return

        # tool_call 保护：当 LLM 使用 send_message_to_user 时跳过分段
        # 因为 tool_loop 会独立发送完整消息，分段会导致重复
        if event.get_extra("smart_wakeup_has_tool_call"):
            logger.info("[ToolCallDetect] _splitter_process 检测到 tool_call，跳过分段")
            return

        setattr(result, "__lingxi_split_processed", True)

        # 零宽空格脱敏
        for comp in result.chain:
            if isinstance(comp, Plain) and comp.text:
                if "\u200b" in comp.text:
                    comp.text = comp.text.replace("\u200b \u200b", "__ZWSP_D__").replace("\u200b", "__ZWSP_S__")

        # 计算理想段长（均分模式）
        ideal_length = 0
        if self.balanced_split_mode and self.max_segments > 0:
            text_weight = sum(len(c.text.replace(" ", "")) for c in result.chain if isinstance(c, Plain))
            if text_weight > 0:
                ideal_length = max(math.ceil(text_weight / self.max_segments), self.min_segment_length)

        # 执行切分
        segments = self._split_chain(result.chain, self.split_regex, ideal_length)

        # 在分段修改 chain 之前，保存完整回复文本供 after_message_sent 记录
        # 否则 after_message_sent 只能拿到最后一段，导致复读检测失效
        full_text_parts = []
        for comp in result.chain:
            if hasattr(comp, "text") and comp.text:
                full_text_parts.append(comp.text)
        if full_text_parts:
            event.set_extra("full_response_text_before_split", " ".join(full_text_parts))

        # 均分模式尾部合并：过短的末段并入前段
        if self.balanced_split_mode and len(segments) >= 2:
            last_text = "".join([c.text for c in segments[-1] if isinstance(c, Plain)]).strip()
            if 0 < len(last_text) < self.min_segment_length:
                if not any(not isinstance(c, Plain) for c in segments[-1]):
                    segments[-2].extend(segments.pop())

        # 后处理：清理空行 + 剔除末尾标点 + 恢复零宽空格
        for seg in segments:
            if self.trim_segment_edge_blank_lines:
                self._trim_segment_blank_lines(seg)
            if self.strip_trailing_punct_enabled:
                self._strip_segment_trailing_punct(seg)
            for comp in seg:
                if isinstance(comp, Plain) and comp.text:
                    comp.text = comp.text.replace("__ZWSP_D__", "\u200b \u200b").replace("__ZWSP_S__", "\u200b")

        # 只有一段，无需分段发送
        if len(segments) <= 1:
            final = segments[0] if segments else []
            result.chain.clear()
            result.chain.extend(final)
            return

        # 多段发送：前 N-1 段主动发送，最后一段交给正常流程
        sent_count = 0
        try:
            for i in range(len(segments) - 1):
                seg_chain = segments[i]
                text_content = "".join([c.text for c in seg_chain if isinstance(c, Plain)])
                if not text_content.strip(" \t\r\n\u200b") and not any(not isinstance(c, Plain) for c in seg_chain):
                    continue

                try:
                    debug_text = text_content[:60].replace('\n', '\\n')
                    self._debug(f"[分段] 第{i + 1}/{len(segments)}段: {debug_text}")
                    mc = MessageChain()
                    mc.chain = seg_chain
                    await self.context.send_message(event.unified_msg_origin, mc)
                    sent_count += 1
                    self._stats["splitter_stats"]["total_segments_sent"] += 1
                    await asyncio.sleep(self._calculate_segment_delay(text_content))
                except Exception as e:
                    logger.error(f"[分段] 发送失败: {e}")
        except asyncio.CancelledError:
            # P1-8 修复：取消时合并所有未发送段到 result.chain，而非仅取最后一段
            # 原逻辑 `last_seg = remaining[-1]` 会丢失中间未发送段（如 3 段已发 1 段被取消时，第 2 段丢失）
            remaining = segments[sent_count:] if sent_count > 0 else segments
            result.chain.clear()
            for seg_chain in remaining:
                result.chain.extend(seg_chain)
            logger.warning(f"[分段] 发送被取消，已发送{sent_count}段，剩余{len(remaining)}段合并到正常流程")
            return

        # 最后一段交给正常流程发送
        last_seg = segments[-1]
        result.chain.clear()
        result.chain.extend(last_seg)

        self._stats["splitter_stats"]["total_splits"] += 1
        logger.info(f"[分段] 完成: {len(segments)}段, 主动发送{sent_count}段, 最后1段交由正常流程")

    # ─── 调试指令 ──────────────────────────────────────────

    @filter.command("wakeup_status", alias={"唤醒状态"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_status(self, event: AstrMessageEvent):
        """查看插件运行状态

        显示插件配置、统计信息、缓冲区概览等。
        """
        now = int(time.time())
        uptime = now - self._stats["plugin_start_time"]
        uptime_str = self._format_duration(uptime)

        # 缓冲区概览
        buffer_summary = []
        total_buffered = 0
        for gid, buf in self._msg_buffer.items():
            count = len(buf)
            total_buffered += count
            if buf:
                oldest_age = now - buf[0][2]
                newest_age = now - buf[-1][2]
                buffer_summary.append(
                    f"  群 {gid}: {count} 条 | "
                    f"最早 {self._format_duration(oldest_age)}前 | "
                    f"最新 {self._format_duration(newest_age)}前"
                )
            else:
                buffer_summary.append(f"  群 {gid}: 空")

        last_cleanup = "从未"
        if self._stats["last_cleanup_time"] > 0:
            last_cleanup = f"{self._format_duration(now - self._stats['last_cleanup_time'])}前"

        # 精力概览
        energy_summary = []
        for gid, est in self._energy_states.items():
            self._recover_energy(gid)
            energy_summary.append(f"  群 {gid}: 精力 {est.energy:.2f} | 回复 {est.total_replies} 次")

        # 心流概览
        flow_summary = []
        for gid, fst in self._flow_states.items():
            flow_summary.append(f"  群 {gid}: {fst.state.value} | 活跃度 {fst.message_count_in_window} | 参与度 {fst.engagement:.2f}")

        # 冷场救场概览
        rescue_summary = []
        for gid, rs in self._rescue_states.items():
            rescue_summary.append(f"  群 {gid}: 救场 {rs.total_rescues} 次")

        lines = [
            "📋 灵犀 - 运行状态",
            "",
            f"⏱ 运行时长: {uptime_str}",
            f"🤖 机器人名称: {' | '.join(self.bot_names)}",
            f"🔑 关注关键词: {' | '.join(self.keywords) if self.keywords else '无'}",
            f"🔑 关键词概率: {self.keyword_reply_prob}",
            f"💬 上下文消息数: {self.context_messages_count}",
            f"🔒 私聊唤醒: {'启用' if self.enable_private_chat else '关闭'}",
            f"📝 白名单模式: {'启用' if self.whitelist_enabled else '关闭'}",
            f"✅ 白名单群: {self.enabled_groups or '无'}",
            f"🚫 黑名单群: {self.blocked_groups or '无'}",
            f"🎲 概率唤醒: {'启用' if self.probability_wakeup else '关闭'}",
            f"🎯 参与度衰减: {self.engagement_decay_per_minute}/分钟",
            f"🔄 回复刷新: +{self.engagement_refresh_on_reply}",
            f"😴 疲劳系数: {self.fatigue_coefficient}/轮 (上限{self.fatigue_max_multiplier}x)",
            f"🛟 冷场救场: {'启用' if self.rescue_enabled else '关闭'}",
            f"⏳ 防抖: {'启用' if self.debounce_enabled else '关闭'} | "
            f"强制防抖: {'启用' if self.force_debounce else '关闭'} | "
            f"复读抑制: {'启用' if self.repeat_suppress_enabled else '关闭'}(系数={self.repeat_suppress_factor} 检测=近{self.recent_rounds_keep}轮) | "
            f"🧹 思考过滤: {'启用' if self.filter_thinking_tags else '关闭'}",
            f"⚙️ 群组覆盖: {len(self.group_overrides)} 个群 | "
            f"👤 用户概率覆盖: {len(self.user_prob_overrides)} 个用户",
            "",
            "📊 统计:",
            f"  记录消息总数: {self._stats['total_messages_recorded']}",
            f"  唤醒总次数: {self._stats['total_wakeups']}",
            f"  清理总次数: {self._stats['total_cleanups']}",
            f"  上次清理: {last_cleanup}",
            "",
            f"💾 缓冲区: {len(self._msg_buffer)} 个群 | 共 {total_buffered} 条消息",
        ]
        lines.extend(buffer_summary if buffer_summary else ["  (无缓冲区数据)"])

        lines.extend(["", f"⚡ 精力系统: {len(self._energy_states)} 个群"])
        lines.extend(energy_summary if energy_summary else ["  (无数据)"])

        lines.extend(["", f"🔥 心流系统: {len(self._flow_states)} 个群"])
        lines.extend(flow_summary if flow_summary else ["  (无数据)"])

        lines.extend(["", f"🛟 冷场救场: {len(self._rescue_states)} 个群"])
        lines.extend(rescue_summary if rescue_summary else ["  (无数据)"])

        lines.extend([
            "",
            "📊 唤醒统计:",
            f"  名称触发: {self._stats['name_trigger_wakeups']}",
            f"  关键词触发: {self._stats.get('keyword_trigger_wakeups', 0)}",
            f"  概率唤醒: {self._stats['probability_wakeups']}",
            f"  冷场救场: {self._stats['rescue_wakeups']}",
            f"  概率检查: {self._stats['probability_checks']} 次",
            f"  概率通过: {self._stats['probability_passed']} 次",
            f"  防抖触发: {self._stats['debounce_fired']}",
            f"  防抖取消: {self._stats['debounce_cancelled']}",
            f"  思考过滤: {self._stats['thinking_filtered']}",
        ])

        # Token 消耗统计
        if self._stats["llm_call_count"] > 0:
            total_prompt = self._stats["total_prompt_tokens"]
            total_completion = self._stats["total_completion_tokens"]
            total = self._stats["total_tokens"]
            prompt_pct = (total_prompt / total * 100) if total > 0 else 0
            lines.extend([
                "",
                "📊 Token 消耗:",
                f"  总计: {self._fmt_tokens(total)} (输入 {prompt_pct:.1f}%)",
                f"  调用: {self._stats['llm_call_count']}次 | "
                f"峰值: {self._fmt_tokens(self._stats['peak_prompt_tokens'])}",
                f"  详情: /wakeup_token",
            ])
        else:
            lines.extend([
                "",
                "📊 Token 消耗: (暂无数据)",
            ])

        yield event.plain_result("\n".join(lines))

    @filter.command("wakeup_buffer", alias={"唤醒缓冲"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_buffer(self, event: AstrMessageEvent, count: int = 10):
        """查看当前群的消息缓冲区内容

        用法: /wakeup_buffer [数量]
        默认显示最近 10 条，最多 50 条。
        """
        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result("此指令仅在群聊中可用")
            return

        count = min(max(count, 1), 50)
        buffer = self._msg_buffer.get(group_id)

        if not buffer:
            yield event.plain_result(f"群 {group_id} 的缓冲区为空")
            return

        now = int(time.time())
        messages = list(buffer)[-count:]

        lines = [f"💾 群 {group_id} 缓冲区 (最近 {len(messages)}/{len(buffer)} 条):", ""]
        for sender, text, ts in messages:
            age = self._format_duration(now - ts)
            # 截断过长的消息
            display_text = text[:60] + "..." if len(text) > 60 else text
            lines.append(f"[{sender}] ({age}前): {display_text}")

        yield event.plain_result("\n".join(lines))

    @filter.command("wakeup_clear", alias={"唤醒清理"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_clear(self, event: AstrMessageEvent, target: str = ""):
        """清理消息缓冲区

        用法:
          /wakeup_clear        - 清理当前群的缓冲区
          /wakeup_clear all    - 清理所有群的缓冲区
          /wakeup_clear expire - 清理所有过期数据
        """
        if target == "all":
            # 清理所有缓冲区
            total = sum(len(buf) for buf in self._msg_buffer.values())
            group_count = len(self._msg_buffer)
            self._msg_buffer.clear()
            self._stats["total_cleanups"] += 1
            self._stats["last_cleanup_time"] = int(time.time())
            logger.info(f"手动清理: 已清除所有缓冲区 ({group_count} 个群, {total} 条消息)")
            yield event.plain_result(
                f"已清理所有缓冲区: {group_count} 个群, {total} 条消息"
            )
        elif target == "expire":
            # 清理过期数据
            before_groups = len(self._msg_buffer)
            before_total = sum(len(buf) for buf in self._msg_buffer.values())
            self._cleanup_expired_buffers()
            after_groups = len(self._msg_buffer)
            after_total = sum(len(buf) for buf in self._msg_buffer.values())
            yield event.plain_result(
                f"过期清理完成:\n"
                f"群数: {before_groups} → {after_groups}\n"
                f"消息数: {before_total} → {after_total}"
            )
        else:
            # 清理当前群
            group_id = event.message_obj.group_id
            if not group_id:
                yield event.plain_result("此指令仅在群聊中可用")
                return

            buffer = self._msg_buffer.get(group_id)
            if not buffer:
                yield event.plain_result(f"群 {group_id} 的缓冲区已经为空")
                return

            count = len(buffer)
            del self._msg_buffer[group_id]
            logger.info(f"手动清理: 已清除群 {group_id} 的缓冲区 ({count} 条消息)")
            yield event.plain_result(f"已清理群 {group_id} 的缓冲区: {count} 条消息")

    @filter.command("wakeup_groups", alias={"唤醒群组"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_groups(self, event: AstrMessageEvent):
        """查看群白名单/黑名单状态及当前群信息"""
        group_id = event.message_obj.group_id

        lines = [
            "📋 群组过滤状态",
            "",
            f"白名单模式: {'启用' if self.whitelist_enabled else '关闭'}",
            f"白名单群: {self.enabled_groups or '无'}",
            f"黑名单群: {self.blocked_groups or '无'}",
        ]

        if group_id:
            is_blocked = group_id in self.blocked_groups
            is_whitelisted = group_id in self.enabled_groups
            is_allowed = self._is_group_allowed(group_id)

            lines.extend([
                "",
                f"当前群: {group_id}",
                f"  在黑名单中: {'是' if is_blocked else '否'}",
                f"  在白名单中: {'是' if is_whitelisted else '否'}",
                f"  唤醒状态: {'允许' if is_allowed else '禁止'}",
            ])

        yield event.plain_result("\n".join(lines))

    @filter.command("wakeup_energy", alias={"唤醒精力"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_energy(self, event: AstrMessageEvent):
        """查看当前群的精力状态"""
        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result("此指令仅在群聊中可用")
            return

        energy = self._get_energy(group_id)
        self._recover_energy(group_id)  # 先恢复

        now = time.time()
        last_reply_ago = self._format_duration(int(now - energy.last_reply_time)) if energy.last_reply_time > 0 else "从未"

        # 获取群组覆盖参数
        decay_rate = self._get_group_param(group_id, "energy_decay_rate", self.energy_decay_rate)
        recovery_rate = self._get_group_param(group_id, "energy_recovery_rate", self.energy_recovery_rate)

        # 计算恢复到满精力需要的时间
        if energy.energy < 1.0 and recovery_rate > 0:
            deficit = 1.0 - energy.energy
            minutes_to_full = deficit / recovery_rate
            time_to_full = f"{minutes_to_full:.0f}分钟"
        else:
            time_to_full = "已满"

        # 检查是否有群组覆盖
        overrides = self.group_overrides.get(str(group_id), {})
        override_info = ""
        if overrides:
            override_items = [f"{k}: {v}" for k, v in overrides.items()]
            override_info = f"\n群组覆盖参数: {', '.join(override_items)}"

        lines = [
            f"⚡ 群 {group_id} 精力状态",
            "",
            f"当前精力: {energy.energy:.2f} / 1.0",
            f"消耗速率: {decay_rate} / 次" + (" (覆盖)" if "energy_decay_rate" in overrides else ""),
            f"恢复速率: {recovery_rate} / 分钟" + (" (覆盖)" if "energy_recovery_rate" in overrides else ""),
            f"上次回复: {last_reply_ago}前",
            f"回复次数: {energy.total_replies}",
            f"恢复至满精力: {time_to_full}",
            override_info,
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("wakeup_flow", alias={"唤醒心流"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_flow(self, event: AstrMessageEvent):
        """查看当前群的心流状态"""
        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result("此指令仅在群聊中可用")
            return

        flow = self._get_flow(group_id)
        energy = self._get_energy(group_id)
        self._recover_energy(group_id)

        now = time.time()
        time_in_state = self._format_duration(int(now - flow.state_enter_time))

        state_emoji = {
            FlowState.BYSTANDER: "😴",
            FlowState.ATTENTIVE: "👀",
            FlowState.FLOW: "🔥",
            FlowState.FATIGUED: "😫",
        }

        # 获取群组覆盖参数
        bystander_prob = self._get_group_param(group_id, "flow_bystander_prob", self.flow_bystander_prob)
        attentive_prob = self._get_group_param(group_id, "flow_attentive_prob", self.flow_attentive_prob)
        flow_prob = self._get_group_param(group_id, "flow_flow_prob", self.flow_flow_prob)

        # 检查是否有群组覆盖
        overrides = self.group_overrides.get(str(group_id), {})

        lines = [
            f"{state_emoji.get(flow.state, '❓')} 群 {group_id} 心流状态",
            "",
            f"当前状态: {flow.state.value}",
            f"停留时长: {time_in_state}",
            f"活跃度: {flow.message_count_in_window} 条/5分钟",
            f"话题相关度: {flow.relevance_score:.2f}",
            f"参与度: {flow.engagement:.2f}",
            f"对话轮数: {flow.conversation_turns}",
            f"当前精力: {energy.energy:.2f}",
            "",
            "各状态基础概率:",
            f"  旁观: {bystander_prob}" + (" (覆盖)" if "flow_bystander_prob" in overrides else ""),
            f"  关注: {attentive_prob}" + (" (覆盖)" if "flow_attentive_prob" in overrides else ""),
            f"  心流: {flow_prob}" + (" (覆盖)" if "flow_flow_prob" in overrides else ""),
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("wakeup_debounce", alias={"唤醒防抖"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_debounce(self, event: AstrMessageEvent):
        """查看当前群的防抖状态"""
        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result("此指令仅在群聊中可用")
            return

        state = self._debounce_states.get(group_id)
        now = time.time()

        lines = [
            f"⏳ 群 {group_id} 防抖状态",
            "",
            f"防抖启用: {'是' if self.debounce_enabled else '否'}",
            f"强制防抖: {'是' if self.force_debounce else '否'}",
            f"名称触发等待: {self.debounce_wait_name}秒",
            f"概率唤醒等待: {self.debounce_wait_prob}秒",
            f"冷场救场等待: {self.debounce_wait_rescue}秒",
        ]

        if state and state.pending_messages:
            lines.extend([
                "",
                f"暂存消息数: {len(state.pending_messages)}",
                f"最近消息: {state.last_msg_sender} ({self._format_duration(int(now - state.last_msg_time))}前)",
            ])
        else:
            lines.extend(["", "暂存消息: 无"])

        lines.extend([
            "",
            "📊 统计:",
            f"  防抖触发: {self._stats['debounce_fired']}",
            f"  防抖取消: {self._stats['debounce_cancelled']}",
        ])

        yield event.plain_result("\n".join(lines))

    @filter.command("wakeup_token", alias={"唤醒token", "唤醒Token"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_token(self, event: AstrMessageEvent, sub: str = ""):
        """查看 Token 消耗统计

        用法:
          /wakeup_token          - 总览
          /wakeup_token model    - 按模型分布
          /wakeup_token group    - 按群分布
          /wakeup_token hourly   - 按小时趋势
          /wakeup_token compress - 压缩效果
          /wakeup_token route    - 路由效果
          /wakeup_token anomaly  - 异常检测
        """
        if sub == "model":
            yield event.plain_result(self._format_token_model_report())
        elif sub == "group":
            yield event.plain_result(self._format_token_group_report())
        elif sub == "hourly":
            yield event.plain_result(self._format_token_hourly_report())
        elif sub == "compress":
            yield event.plain_result(self._format_token_compress_report())
        elif sub == "route":
            yield event.plain_result(self._format_token_route_report())
        elif sub == "anomaly":
            yield event.plain_result(self._format_token_anomaly_report())
        else:
            yield event.plain_result(self._format_token_overview())

    # ─── 主动发言调试指令（Phase 3 新增，v1.7.0）──────────────

    @filter.command("wakeup_proactive", alias={"主动发言触发"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_proactive_trigger(self, event: AstrMessageEvent, category: str = ""):
        """手动触发一次主动发言

        跳过冷却和概率检查，但仍检查 UMO 有效性、并发互斥、退让状态（仅提示不阻止）。
        用法:
            /wakeup_proactive               # 加权随机选择话题
            /wakeup_proactive 科技资讯       # v1.8.0 强制指定资讯类话题
            /wakeup_proactive 沙雕新闻       # 同上，可选: 科技资讯/游戏八卦/沙雕新闻/热点事件
        """
        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result("❌ 此指令只能在群聊中使用")
            return

        if not self._proactive_enabled:
            yield event.plain_result("❌ 主动发言功能未启用（请在配置中开启 proactive_enabled）")
            return

        # 检查 UMO 缓存
        if not self._is_umo_valid(group_id):
            yield event.plain_result(
                f"❌ 群 {group_id} 的 UMO 缓存无效或不存在\n"
                f"请先在群内发送一条消息以建立 UMO 缓存"
            )
            return

        # 检查并发互斥
        if group_id in self._llm_running_groups:
            yield event.plain_result("❌ 当前群正在处理其他 LLM 请求，请稍后再试")
            return

        # 检查调度器状态
        if not self._proactive_task or self._proactive_task.done():
            yield event.plain_result("❌ 主动发言调度器未运行，请检查插件状态")
            return

        # v1.8.0 校验指定的 category 参数
        category_override = None
        if category:
            category_stripped = category.strip()
            # 允许的类别：配置中的话题类别 ∪ 资讯类（资讯类可能不在配置中，需显式并入）
            # 注：_proactive_topic_categories 默认不含资讯类，| _NEWS_CATEGORIES 是必要的补充而非冗余
            allowed = set(self._proactive_topic_categories) | self._NEWS_CATEGORIES
            if category_stripped in allowed:
                category_override = category_stripped
            else:
                yield event.plain_result(
                    f"❌ 不识别的话题类别: '{category_stripped}'\n"
                    f"允许的类别: {', '.join(sorted(allowed))}"
                )
                return

        # 检查退让状态（提示但不阻止，管理员可测试退让状态下的行为）
        # v1.8.0 修复（B1 三次审查 m1）：退让状态分支也显示 trigger_hint，让用户确认指定话题已生效
        trigger_hint = f"（指定话题: {category_override}）" if category_override else ""
        retreat_info = self._proactive_retreat.get(group_id)
        if retreat_info and time.time() < retreat_info["until"]:
            remaining = int(retreat_info["until"] - time.time())
            yield event.plain_result(
                f"⚠️ 群 {group_id} 当前处于退让状态\n"
                f"剩余: {self._format_duration(remaining)} | 原因: {retreat_info['reason']}\n"
                f"正在强制触发{trigger_hint}..."
            )
        else:
            yield event.plain_result(f"🚀 正在为群 {group_id} 手动触发主动发言{trigger_hint}...")

        # P1-2 修复（v1.7.0）：记录触发前计数，区分"已触发"和"已发言"
        today = datetime.now().strftime("%Y-%m-%d")
        count_before = self._proactive_daily_count.get(group_id, {}).get(today, 0)

        try:
            await self._proactive_speak(group_id, category_override=category_override)
        except Exception as e:
            yield event.plain_result(f"❌ 主动发言触发失败: {e}")
            return

        # 根据计数变化判断是否实际发言
        count_after = self._proactive_daily_count.get(group_id, {}).get(today, 0)
        if count_after > count_before:
            # v1.8.1：资讯类话题已有 SKIP 保护，此处不再需要降级提示
            # （v1.8.0 的降级机制已在 v1.8.1 改为 SKIP，不再出现"已降级但仍发言"的情况）
            yield event.plain_result(
                f"✅ 主动发言成功\n"
                f"今日已发言: {count_after}/{self._proactive_daily_limit}"
            )
        else:
            # v1.8.1 新增：检查 SKIP 原因，给出具体反馈而非笼统的"未实际发言"
            skip_reason = getattr(self, '_last_proactive_skip_reason', {}).get(group_id, "")
            if skip_reason:
                yield event.plain_result(
                    f"⏭️ 主动发言已 SKIP\n"
                    f"原因: {skip_reason}\n"
                    f"今日已发言: {count_after}/{self._proactive_daily_limit}\n"
                    f"提示：资讯类话题无可用资讯时不降级编造，请稍后重试或检查 Fetcher 配置"
                )
            else:
                yield event.plain_result(
                    f"⚠️ 已触发但未实际发言（可能被 SKIP/去重/发送失败/LLM 占用）\n"
                    f"今日已发言: {count_after}/{self._proactive_daily_limit}\n"
                    f"详情请查看日志"
                )

    @filter.command("wakeup_proactive_status", alias={"主动发言状态"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_proactive_status(self, event: AstrMessageEvent):
        """查看主动发言运行状态

        显示全局配置、各群退让/冷却/追踪状态等。
        """
        now = time.time()
        scheduler_running = bool(self._proactive_task and not self._proactive_task.done())
        lines = [
            "🚀 主动发言 - 运行状态",
            "",
            "⚙️ 全局配置:",
            f"  启用: {'✅' if self._proactive_enabled else '❌'}",
            f"  检查间隔: {self._proactive_check_interval}s",
            f"  发言冷却: {self._proactive_cooldown}s",
            f"  触发概率: {self._proactive_probability}",
            f"  每日上限: {self._proactive_daily_limit}",
            f"  最小精力: {self._proactive_min_energy}",
            f"  调度器: {'运行中' if scheduler_running else '未运行'}",
            "",
            f"📊 各群状态（{len(self._group_umo)} 个群有 UMO 缓存）:",
        ]

        # 收集所有有数据的群
        all_groups = set(self._group_umo.keys()) | set(self._proactive_last_speak.keys()) | \
                     set(self._proactive_retreat.keys()) | set(self._proactive_response_tracker.keys())

        if not all_groups:
            lines.append("  (无数据)")
        else:
            today = datetime.now().strftime("%Y-%m-%d")
            for gid in sorted(all_groups):
                # UMO 有效性
                umo_valid = self._is_umo_valid(gid)
                umo_str = "✅" if umo_valid else "❌"

                # 退让状态
                retreat_info = self._proactive_retreat.get(gid)
                if retreat_info and now < retreat_info["until"]:
                    retreat_remaining = int(retreat_info["until"] - now)
                    retreat_str = f"退让中({self._format_duration(retreat_remaining)}, {retreat_info['reason']})"
                else:
                    retreat_str = "正常"

                # 冷却剩余
                last_speak = self._proactive_last_speak.get(gid, 0)
                if last_speak > 0:
                    cooldown_remaining = max(0, int(self._proactive_cooldown - (now - last_speak)))
                    cooldown_str = f"冷却剩余{self._format_duration(cooldown_remaining)}" if cooldown_remaining > 0 else "可发言"
                else:
                    cooldown_str = "从未发言"

                # 今日已发言次数
                daily_counts = self._proactive_daily_count.get(gid, {})
                today_count = daily_counts.get(today, 0)

                # 无人回应追踪
                tracker = self._proactive_response_tracker.get(gid)
                if tracker and not tracker.get("checked"):
                    elapsed = int(now - tracker.get("speak_time", 0))
                    tracker_str = f"追踪中({self._format_duration(elapsed)})"
                elif tracker and tracker.get("checked"):
                    tracker_str = "已检查"
                else:
                    tracker_str = "无"
                # v1.8.4 新增：显示递进退让状态
                consecutive_count = tracker.get("consecutive_no_response_count", 0) if tracker else 0
                cooldown_mult = tracker.get("cooldown_multiplier", 1.0) if tracker else 1.0
                if consecutive_count > 0 or cooldown_mult > 1.0:
                    tracker_str += f" | 连续无回应={consecutive_count} | 冷却倍率×{cooldown_mult}"

                lines.append(f"  群 {gid}:")
                lines.append(f"    UMO: {umo_str} | {retreat_str}")
                lines.append(f"    {cooldown_str} | 今日 {today_count}/{self._proactive_daily_limit}")
                lines.append(f"    追踪: {tracker_str}")

        # 统计总览
        lines.extend([
            "",
            "📈 统计总览:",
            f"  尝试: {self._proactive_stats['total_attempts']}",
            f"  成功: {self._proactive_stats['total_success']}",
            f"  跳过: {self._proactive_stats['total_skip']}",
            f"  重复: {self._proactive_stats['total_duplicate']}",
            f"  发送失败: {self._proactive_stats['total_send_fail']}",
            f"  退让触发: {self._proactive_stats['total_retreats']}",
        ])

        yield event.plain_result("\n".join(lines))

    @filter.command("wakeup_proactive_metrics", alias={"主动发言指标"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_proactive_metrics(self, event: AstrMessageEvent):
        """查看主动发言评估指标

        显示 CPS、采纳率、退让信号触发次数等评估数据。
        用法: /wakeup_proactive_metrics（显示全部群汇总）
        """
        lines = ["📊 主动发言 - 评估指标", ""]

        # 退让信号触发明细
        lines.extend([
            "🚫 退让信号触发次数:",
            f"  ④ 自说自话: {self._proactive_stats['retreat_self_talk']}",
            f"  ⑤ 连续SKIP: {self._proactive_stats['retreat_skip']}",
            f"  ① 活跃激增: {self._proactive_stats['retreat_active_surge']}",
            f"  ② 无人回应: {self._proactive_stats['retreat_no_response']}",
            f"  ③ 厌烦关键词: {self._proactive_stats['retreat_annoyed']}",
            f"  总计: {self._proactive_stats['total_retreats']}",
            "",
        ])

        # 各群 CPS 和采纳率
        if not self._proactive_outcomes:
            lines.append("📈 各群效果指标: (暂无数据)")
            lines.append("  需主动发言后 30 分钟才会生成效果记录")
        else:
            lines.append("📈 各群效果指标:")
            for gid, outcomes in self._proactive_outcomes.items():
                if not outcomes:
                    continue
                total_cps = sum(cps for _, cps, _ in outcomes)
                adopted_count = sum(1 for _, _, adopted in outcomes if adopted)
                avg_cps = total_cps / len(outcomes) if outcomes else 0
                adoption_rate = (adopted_count / len(outcomes) * 100) if outcomes else 0
                lines.append(f"  群 {gid}:")
                lines.append(f"    样本数: {len(outcomes)} | 平均CPS: {avg_cps:.1f} | 采纳率: {adoption_rate:.0f}%")
                # 最近 5 次记录
                recent = outcomes[-5:]
                recent_str = " | ".join(f"CPS={cps}({'✅' if adopted else '❌'})" for _, cps, adopted in recent)
                lines.append(f"    近{len(recent)}次: {recent_str}")

        yield event.plain_result("\n".join(lines))

    # ─── 工具方法 ──────────────────────────────────────────

    def _format_token_overview(self) -> str:
        """Token 消耗总览"""
        now = int(time.time())
        uptime = now - self._stats["plugin_start_time"]
        uptime_str = self._format_duration(uptime)

        total_prompt = self._stats["total_prompt_tokens"]
        total_completion = self._stats["total_completion_tokens"]
        total = self._stats["total_tokens"]
        call_count = self._stats["llm_call_count"]

        # prompt/completion 比率
        prompt_pct = (total_prompt / total * 100) if total > 0 else 0
        completion_pct = (total_completion / total * 100) if total > 0 else 0

        # 平均每次调用
        avg_prompt = total_prompt // call_count if call_count > 0 else 0
        avg_completion = total_completion // call_count if call_count > 0 else 0

        # 按唤醒类型汇总
        type_lines = []
        for wtype, data in self._stats["token_by_wakeup_type"].items():
            if data["count"] > 0:
                type_name = {
                    "name_trigger": "🔔 名称触发",
                    "keyword_trigger": "🔑 关键词",
                    "probability_wakeup": "🎲 概率唤醒",
                    "dead_chat_rescue": "🛟 冷场救场",
                }.get(wtype, wtype)
                type_lines.append(f"  {type_name}: {data['count']}次 total={self._fmt_tokens(data['total'])}")

        lines = [
            "📊 Token 使用统计",
            "━━━━━━━━━━━━━━━━━━━━",
            f"运行时长: {uptime_str}",
            f"总消耗: {self._fmt_tokens(total)}",
            f"  输入: {self._fmt_tokens(total_prompt)} ({prompt_pct:.1f}%) | 输出: {self._fmt_tokens(total_completion)} ({completion_pct:.1f}%)",
            f"调用次数: {call_count} 次",
            f"平均每次: prompt={self._fmt_tokens(avg_prompt)} completion={self._fmt_tokens(avg_completion)}",
            "",
            "按唤醒类型:",
        ]
        lines.extend(type_lines if type_lines else ["  (暂无数据)"])

        # 峰值
        if self._stats["peak_prompt_tokens"] > 0:
            lines.extend([
                "",
                f"⚠️ 峰值: {self._stats['peak_prompt_tokens_detail']}",
            ])

        # 提示
        if prompt_pct > 90 and call_count > 3:
            lines.extend([
                "",
                f"💡 提示: prompt 占比 {prompt_pct:.1f}%，建议优化上下文注入",
            ])

        lines.extend([
            "",
            "子命令: model | group | hourly | compress | route | anomaly",
        ])

        return "\n".join(lines)

    def _format_token_model_report(self) -> str:
        """按模型分布"""
        model_data = self._stats["token_by_model"]
        if not model_data:
            return "📊 Token 按模型分布\n━━━━━━━━━━━━━━━━━━━━\n(暂无数据)"

        lines = [
            "📊 Token 按模型分布",
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        for model_name, data in sorted(model_data.items(), key=lambda x: x[1]["total"], reverse=True):
            lines.append(
                f"  {model_name}: {data['count']}次 "
                f"prompt={self._fmt_tokens(data['prompt'])} "
                f"completion={self._fmt_tokens(data['completion'])} "
                f"total={self._fmt_tokens(data['total'])}"
            )
        return "\n".join(lines)

    def _format_token_group_report(self) -> str:
        """按群分布"""
        group_data = self._stats["token_by_group"]
        if not group_data:
            return "📊 Token 按群分布\n━━━━━━━━━━━━━━━━━━━━\n(暂无数据)"

        lines = [
            "📊 Token 按群分布",
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        for gid, data in sorted(group_data.items(), key=lambda x: x[1]["total"], reverse=True):
            lines.append(
                f"  群 {gid}: {data['count']}次 "
                f"prompt={self._fmt_tokens(data['prompt'])} "
                f"total={self._fmt_tokens(data['total'])}"
            )
        return "\n".join(lines)

    def _format_token_hourly_report(self) -> str:
        """按小时趋势"""
        hourly_data = self._stats["hourly_tokens"]
        if not hourly_data:
            return "📊 Token 按小时趋势\n━━━━━━━━━━━━━━━━━━━━\n(暂无数据)"

        lines = [
            "📊 Token 按小时趋势",
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        # 按时间排序，取最近24小时
        sorted_hours = sorted(hourly_data.items(), reverse=True)[:24]
        max_total = max(d["total"] for _, d in sorted_hours) if sorted_hours else 1

        for hour_key, data in sorted_hours:
            bar_len = int(data["total"] / max_total * 20) if max_total > 0 else 0
            bar = "█" * bar_len
            lines.append(f"  {hour_key}: {self._fmt_tokens(data['total'])} {bar}")

        return "\n".join(lines)

    def _format_token_compress_report(self) -> str:
        """压缩效果统计"""
        cs = self._stats["compression_stats"]
        if cs["compression_count"] == 0:
            return "📊 压缩效果统计\n━━━━━━━━━━━━━━━━━━━━\n(暂无压缩数据，可能未启用压缩)"

        avg_ratio = (cs["total_compressed_chars"] / cs["total_original_chars"] * 100) if cs["total_original_chars"] > 0 else 0
        saved_pct = 100 - avg_ratio

        lines = [
            "📊 压缩效果统计",
            "━━━━━━━━━━━━━━━━━━━━",
            f"压缩次数: {cs['compression_count']}",
            f"原始总字符: {self._fmt_tokens(cs['total_original_chars'])}",
            f"压缩后总字符: {self._fmt_tokens(cs['total_compressed_chars'])}",
            f"平均压缩率: {avg_ratio:.1f}% (节省 {saved_pct:.1f}%)",
        ]
        return "\n".join(lines)

    def _format_token_route_report(self) -> str:
        """路由效果统计"""
        rs = self._stats["routing_stats"]
        total = rs["glm47_count"] + rs["small_model_count"]
        if total == 0:
            return "📊 路由效果统计\n━━━━━━━━━━━━━━━━━━━━\n(暂无路由数据，可能未启用路由)"

        glm_pct = rs["glm47_count"] / total * 100
        small_pct = rs["small_model_count"] / total * 100

        lines = [
            "📊 路由效果统计",
            "━━━━━━━━━━━━━━━━━━━━",
            f"GLM4.7: {rs['glm47_count']}次 ({glm_pct:.1f}%)",
            f"小模型: {rs['small_model_count']}次 ({small_pct:.1f}%)",
            # P1-10: 级联升级展示行已移除（功能未实现，统计始终为 0）
        ]
        return "\n".join(lines)

    def _format_token_anomaly_report(self) -> str:
        """异常检测报告"""
        hourly_data = self._stats["hourly_tokens"]
        if len(hourly_data) < 2:
            return "🔍 Token 异常检测\n━━━━━━━━━━━━━━━━━━━━\n(数据不足，需要至少2小时的数据)"

        import statistics
        totals = [d["total"] for d in hourly_data.values()]
        mean = statistics.mean(totals)
        std = statistics.stdev(totals) if len(totals) >= 2 else 0

        # 当前小时
        current_hour = datetime.now().strftime("%Y-%m-%dT%H")
        current_total = hourly_data.get(current_hour, {}).get("total", 0)

        lines = [
            "🔍 Token 异常检测",
            "━━━━━━━━━━━━━━━━━━━━",
            f"当前小时消耗: {self._fmt_tokens(current_total)}",
            f"历史小时均值: {self._fmt_tokens(int(mean))}",
        ]

        if std > 0:
            z_score = (current_total - mean) / std
            deviation_pct = ((current_total - mean) / mean * 100) if mean > 0 else 0
            if abs(z_score) > 3:
                status = "🔴 严重异常"
            elif abs(z_score) > 2:
                status = "⚠️ 超出正常范围"
            else:
                status = "✅ 正常"
            lines.append(f"偏差: {deviation_pct:+.1f}% ({status})")
            lines.append(f"Z-score: {z_score:.2f}")

        # 峰值信息
        if self._stats["peak_prompt_tokens"] > 0:
            lines.extend([
                "",
                f"峰值记录: {self._stats['peak_prompt_tokens_detail']}",
            ])

        # 建议
        total_prompt = self._stats["total_prompt_tokens"]
        total_all = self._stats["total_tokens"]
        if total_all > 0 and total_prompt / total_all > 0.95 and self._stats["llm_call_count"] > 3:
            lines.extend([
                "",
                "建议:",
                "  1. 检查 context_messages_count 是否过高",
                "  2. 考虑启用增量上下文注入",
                "  3. 考虑启用上下文摘要压缩",
            ])

        return "\n".join(lines)

    @staticmethod
    def _fmt_tokens(n: int) -> str:
        """格式化 token 数量为可读字符串"""
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        elif n >= 1_000:
            return f"{n / 1_000:.1f}K"
        else:
            return str(n)

    def _filter_context_tags(self, text: str) -> str:
        """过滤注入的系统上下文标签，防止泄露给用户

        过滤的标签：
        - <group_chat_context>...</group_chat_context>
        - <natural_wakeup_context>...</natural_wakeup_context>
        - <aggregated_messages>...</aggregated_messages>
        - <system_reminder>...</system_reminder>
        - <conversation_memory>...</conversation_memory>
        - <历史对话摘要>...</历史对话摘要>
        - <近期对话>...</近期对话>
        """
        tags = [
            "group_chat_context",
            "natural_wakeup_context",
            "aggregated_messages",
            "system_reminder",
            "conversation_memory",
            "历史对话摘要",
            "近期对话",
        ]
        filtered = text
        for tag in tags:
            pattern = rf'<{tag}>[\s\S]*?</{tag}>'
            filtered = re.sub(pattern, '', filtered)
        filtered = filtered.strip()
        if filtered != text.strip():
            logger.info(
                f"上下文标签过滤: 已移除泄露内容（原文 {len(text)} 字 → 过滤后 {len(filtered)} 字）"
            )
        return filtered

    def _filter_thinking_tags(self, text: str) -> str:
        """过滤思考标签包裹的思考内容

        支持两种格式：
        - AstrBot 格式：<think()>...</think()>
        - GLM 等模型格式：<think>...</think>
        同时处理只有闭合标签 </think> 的情况（模型在 content 中先输出草稿再输出最终版）。

        作为兜底机制：AstrBot 核心通常会过滤思考标签，
        但某些情况下可能未拦截，此方法确保思考内容不会泄露给用户。
        """
        original = text.strip()
        # 模式1：完整的思考标签 <think>...</think> 或 <think()>...</think()>
        text = re.sub(r'<think\(\)>[\s\S]*?</think\(\)>', '', text)
        text = re.sub(r'<think>[\s\S]*?</think>', '', text)
        # 模式1.5：孤立的 </think> 标签（无对应开标签 <think>）
        # GLM-4 有时在 content 中输出"实际回复</think>实际回复"的模式：
        #   模型先输出回复内容，然后输出一个孤立的 </think>，再重复输出相同内容。
        #   此时 </think> 之前的内容属于"草稿区"，之后的内容才是最终版。
        #   策略：移除最后一个 </think> 及其之前的所有内容，仅保留之后的部分；
        #   若 </think> 后无内容则仅移除标签本身（保留标签前的内容）。
        if '</think>' in text and '<think>' not in text:
            last_close_idx = text.rfind('</think>')
            after_tag = text[last_close_idx + len('</think>'):].strip()
            if after_tag:
                # </think> 后有内容：保留之后的部分（草稿在前，最终版在后）
                text = after_tag
            else:
                # </think> 后无内容：仅移除孤立标签，保留标签前的内容
                text = text[:last_close_idx].strip()
        # 模式2：移除所有 " response" 之前的内容，仅保留最终版
        # GLM 模型有时会在 content 中输出多段草稿，用 " response" 分隔：
        #   草稿1 response 草稿2（含元数据回显） response 最终版
        # 注意：使用负向前瞻 (?!\w) 避免 "few responses" 等英文常见词汇被误匹配
        if re.search(r" response(?!\w)", text):
            text = re.sub(r'^[\s\S]*? response(?!\w)\s*', '', text)
        if re.search(r" response(?!\w)", text):
            text = re.sub(r'^[\s\S]* response(?!\w)\s*', '', text)
        # 移除模型可能回显的上下文元数据（如 LLMPerception 注入的 [发送时间:...] 等）
        text = re.sub(r'\[发送时间:[^\]]*\]', '', text)
        text = re.sub(r'\[平台:[^\]]*\]', '', text)
        # 兜底清理：移除末尾残留的 ``` 或 `` 标记（GLM-4 草稿分隔符残留）
        # 匹配文本末尾的 2-3 个连续反引号（前后可能有空白），这些是 GLM 草稿分隔符残留
        text = re.sub(r'\s*```\s*$', '', text)
        text = re.sub(r'\s*``\s*$', '', text)
        text = text.strip()
        if text != original:
            self._stats["thinking_filtered"] += 1
            logger.info(f"思考标签过滤: 已移除思考内容（原文 {len(original)} 字 → 过滤后 {len(text)} 字）")
        return text

    @staticmethod
    def _filter_duplicate_response(text: str) -> str:
        """过滤 LLM 返回的重复回复

        某些模型（如 GLM）会在回复中生成两个版本，用 ``` 或 " response" 分隔。
        仅当分隔符符合特定模式时才判定为版本分隔符，避免误切正常内容。
        """
        # 处理 GLM 模型用 " response" 分隔的多段草稿（如推理过程中混入 content 的多个版本）
        # 取最后一个 " response" 之后的最终版
        # 注意：使用正则而非简单 split，避免 "few responses" 等英文常见词汇中的 " response" 子串被误匹配
        # GLM 草稿分隔符格式为 " response\n" 或 " response "，后面不紧跟单词字符
        if re.search(r" response(?!\w)", text):
            parts = re.split(r" response(?!\w)\s*", text)
            if len(parts) > 1:
                filtered = parts[-1].strip()
                logger.info(f"重复回复过滤(GLMs): 检测到 {len(parts)} 个版本，保留最终版本（原文 {len(text)} 字 → 过滤后 {len(filtered)} 字）")
                return filtered

        # 匹配 ``` 独占一行的情况：
        # - 前后有换行
        # - ``` 后面只有空白和换行（不是代码语言如 python，也不是颜文字如 (QAQ)）
        # P0-2 修复：原逻辑仅凭 len(parts) > 1 就切分，会误切合法代码块
        # （如 "解释\n```\ncode\n```\n结论" 被切成 3 段，前两段丢失）
        # 新逻辑：仅当段数≥3（至少 2 个 ``` 分隔符，即草稿模式）且最后一段不像代码时才切分
        parts = re.split(r'\n```[ \t]*\n', text)
        if len(parts) >= 3:
            # 取最后一个版本（模型的最终修订）
            filtered = parts[-1].strip()
            # 安全检查：最后一段不应以 ``` 开头（否则可能是代码块内部）
            if not filtered.startswith('```'):
                logger.info(f"重复回复过滤: 检测到 {len(parts)} 个版本，保留最终版本（原文 {len(text)} 字 → 过滤后 {len(filtered)} 字）")
                return filtered

        # 匹配 ``` 直接跟在文字后面的情况（GLM-4 新模式）：
        # 例如："这么有创意```[SKIP]```\n[SKIP]"
        # 此时 ``` 不在独立行上，而是紧跟在文本末尾
        # 安全策略：仅当 ``` 后紧跟 [SKIP] 或另一个 ``` 时才判定为草稿分隔符，
        # 避免误切合法代码块（```python 等）
        if re.search(r'```(?:\[SKIP\]|```)', text):
            parts = re.split(r'```', text)
            if len(parts) > 1:
                # 检查最后一个部分是否是有效的最终版本（非空且不是 [SKIP] 等标记）
                last_part = parts[-1].strip()
                # 如果最后一段为空或仅是 [SKIP] 标记，则取第一段（正式回复）
                if not last_part or re.match(r'^\[SKIP\]', last_part):
                    filtered = parts[0].strip()
                else:
                    filtered = last_part
                # 清理末尾可能残留的 ``` 标记
                filtered = filtered.rstrip('`').strip()
                if filtered != text.strip():
                    logger.info(f"重复回复过滤(```内联): 检测到 {len(parts)} 段，保留最终版本（原文 {len(text)} 字 → 过滤后 {len(filtered)} 字）")
                    return filtered
        return text

    @staticmethod
    def _format_duration(seconds: int) -> str:
        """将秒数格式化为人类可读的时长"""
        if seconds < 60:
            return f"{seconds}秒"
        elif seconds < 3600:
            return f"{seconds // 60}分钟"
        elif seconds < 86400:
            return f"{seconds // 3600}小时"
        else:
            return f"{seconds // 86400}天"

    # ─── 生命周期 ──────────────────────────────────────────

    async def initialize(self):
        """框架初始化钩子，启动主动发言调度器（v1.5.0 新增）

        总是启动调度器，让单群覆盖的 proactive_enabled 能生效。
        若全局关闭且无单群覆盖开启，调度器仅空转（每 30 分钟遍历一次，资源消耗极小）。
        实际开关检查在 _should_proactive_speak 第 0 步执行。

        框架接口验证：
        - initialize 是 AstrBot 标准生命周期钩子（bishoujo L242 验证）
        - 在所有插件加载完成后由框架自动调用
        - 不会与 smart_wakeup 现有代码冲突（当前无 initialize）
        """
        # v1.7.1：加载持久化状态（UMO + 防重复数据），避免重载后沉默群无法被调度 + 防重复失效
        self._load_proactive_state()
        self._proactive_task = asyncio.create_task(self._proactive_speak_loop())
        logger.info("[主动发言] 调度器已启动")

    # ─── 主动发言（v1.5.0 新增） ──────────────────────────
    # 基于三要素模型（Anticipation-Initiation-Planning）设计
    # 理论依据：ACM TOIS 2025 综述 + MII/AIF 状态机 + ProCoT 三步法

    # AIF 状态机常量（v1.5.0 仅使用两态）
    _PROACTIVE_STATE_PASSIVE = "PASSIVE_MONITORING"  # 被动监听，等待时机
    _PROACTIVE_STATE_AGENT = "AGENT_DOMINANT"        # Agent 主导对话（发言后短暂期间）

    # 话题类别引导 prompt（对应 CoI 三种临场感）
    # v1.8.0 扩展：新增 4 个资讯类话题（需 Fetcher 支持）
    _TOPIC_CATEGORY_PROMPTS = {
        # 原有 5 个话题类别
        "分享想法": "分享一个你自己的想法或观点，可以是对某个事物的看法或感受",
        "提问讨论": "提出一个有趣的问题来引发群友讨论",
        "回忆过去": "回忆之前群里讨论过的某个话题，延续或回顾那个话题",
        "关注某人": "对某个群友近期的发言或状态表达关心或看法",
        "活跃气氛": "说点轻松有趣的内容来活跃群里的气氛",
        # 资讯类 4 个（v1.8.0 新增，需 Fetcher 支持，无资讯时降级为活跃气氛）
        "科技资讯": "基于最新科技资讯，用你的风格转述事实并加上犀利吐槽",
        "游戏八卦": "基于最新游戏行业八卦，用你的风格转述事实并调侃",
        "沙雕新闻": "基于最新沙雕新闻，用你的风格转述事实并吐槽",
        "热点事件": "基于当前热点事件，用你的风格转述事实并发表看法",
    }

    # 资讯类话题集合（需调用 Fetcher 获取外部资讯）
    _NEWS_CATEGORIES = {"科技资讯", "游戏八卦", "沙雕新闻", "热点事件"}

    # 资讯类话题的 ProCoT 生成阶段指令（覆盖默认指令，强调"事实转述+吐槽"模式）
    _NEWS_CATEGORY_PROMPTS = {
        "科技资讯": "基于外部科技资讯，用你的风格转述事实并加上犀利吐槽",
        "游戏八卦": "基于外部游戏行业八卦，用你的风格转述事实并调侃",
        "沙雕新闻": "基于外部沙雕新闻，用你的风格转述事实并吐槽",
        "热点事件": "基于当前热点事件，用你的风格转述事实并发表看法",
    }

    async def _proactive_speak_loop(self):
        """主动发言主调度循环

        每隔 proactive_check_interval 秒遍历所有已缓存群，
        评估触发条件并按概率发起主动发言。
        """
        try:
            while True:
                await asyncio.sleep(self._proactive_check_interval)
                # v1.8.0 诊断日志：证明调度器循环执行了（排查"主动发言不触发"问题）
                target_groups = self._get_proactive_target_groups()
                logger.info(
                    f"[主动发言] 调度器检查: 目标群={len(target_groups)} 个, "
                    f"UMO缓存={len(self._group_umo)} 个, "
                    f"LLM运行中={list(self._llm_running_groups.keys()) if hasattr(self, '_llm_running_groups') else []}"
                )
                # Phase 2 退让信号②：检查无人回应追踪（每次循环都检查，不依赖 _should_proactive_speak）
                self._check_no_response_all_groups()
                # v1.7.1：遍历配置群列表 ∪ UMO 缓存群列表，避免沉默群无法被调度
                for group_id in target_groups:
                    try:
                        if self._should_proactive_speak(group_id):
                            # 概率触发（避免可预测性）
                            prob = self._get_group_param(
                                group_id, "proactive_probability", self._proactive_probability
                            )
                            if random.random() < prob:
                                logger.info(f"[主动发言] 群={group_id} 触发! prob={prob:.2f}")
                                await self._proactive_speak(group_id)
                            else:
                                logger.info(f"[主动发言] 群={group_id} 概率未命中: random>={prob:.2f}")
                        # else 分支的失败原因由 _should_proactive_speak 内部 DBUG 日志输出
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning(f"[主动发言] 群={group_id} 触发失败: {e}")
        except asyncio.CancelledError:
            logger.info("[主动发言] 调度循环已停止")
        except Exception as e:
            # v1.8.0 修复：捕获非 CancelledError 异常，避免任务静默死亡（排查根因）
            logger.error(f"[主动发言] 调度循环异常退出: {type(e).__name__}: {e}", exc_info=True)

    # ─── v1.8.4 新增：时段检查工具函数（支持跨天） ───

    @staticmethod
    def _is_in_time_period(current: str, start: str, end: str) -> bool:
        """检查当前时间是否在 [start, end] 时段内（支持跨天）

        Args:
            current: 当前时间 "HH:MM"
            start: 开始时间 "HH:MM"
            end: 结束时间 "HH:MM"

        Returns:
            True 表示在时段内

        Examples:
            _is_in_time_period("14:00", "09:00", "23:00") → True
            _is_in_time_period("02:00", "23:00", "07:00") → True（跨天）
            _is_in_time_period("10:00", "23:00", "07:00") → False（跨天外）
        """
        if start <= end:
            # 不跨天：start <= current <= end（如 09:00-23:00）
            return start <= current <= end
        else:
            # 跨天：current >= start 或 current <= end（如 23:00-07:00）
            return current >= start or current <= end

    def _parse_quiet_hours(self, quiet_hours_str: str) -> list:
        """解析静默时段字符串为 (start, end) 元组列表

        Args:
            quiet_hours_str: 静默时段字符串，如 "23:00-07:00,13:00-14:00"
                             空字符串或 "none" 返回空列表

        Returns:
            [("23:00", "07:00"), ("13:00", "14:00")]
        """
        if not quiet_hours_str or not quiet_hours_str.strip():
            return []
        if quiet_hours_str.strip().lower() == "none":
            return []
        result = []
        for segment in quiet_hours_str.split(","):
            segment = segment.strip()
            if not segment or "-" not in segment:
                continue
            parts = segment.split("-", 1)
            if len(parts) != 2:
                continue
            start, end = parts[0].strip(), parts[1].strip()
            # 简单格式校验：HH:MM
            if len(start) == 5 and len(end) == 5 and start[2] == ":" and end[2] == ":":
                result.append((start, end))
        return result

    def _is_in_quiet_hours(self, group_id: str, current_time: str) -> bool:
        """检查当前时间是否在指定群的静默时段内

        支持单群覆盖（group_overrides.proactive_quiet_hours）：
        - null/留空：使用全局配置
        - "none"：禁用此群的静默时段
        - "23:00-07:00,13:00-14:00"：覆盖为指定时段
        """
        quiet_hours_str = self._get_group_param(
            group_id, "proactive_quiet_hours", self._proactive_quiet_hours
        )
        # null 表示使用全局值（_get_group_param 已处理）
        if quiet_hours_str is None or quiet_hours_str == "":
            quiet_hours_str = self._proactive_quiet_hours
        periods = self._parse_quiet_hours(quiet_hours_str) if quiet_hours_str else []
        for start, end in periods:
            if self._is_in_time_period(current_time, start, end):
                return True
        return False

    def _should_proactive_speak(self, group_id: str) -> bool:
        """评估是否适合对指定群主动发言（三要素之 Anticipation）

        返回 True 表示所有条件满足，可以按概率触发。
        v1.8.0 诊断日志：每个 return False 路径都记录原因（DBUG 级别），便于排查"主动发言不触发"
        """
        # 0. 主动发言开关检查（支持单群覆盖，可单独关闭某群）
        if not self._get_group_param(group_id, "proactive_enabled", self._proactive_enabled):
            logger.debug(f"[主动发言] 群={group_id} 跳过原因[0]: proactive_enabled=False")
            return False

        # 1. 退让状态检查（MII Human_Dominant 退让）
        if self._is_in_retreat(group_id):
            logger.debug(f"[主动发言] 群={group_id} 跳过原因[1]: 退让状态中")
            return False

        # 2. 群过滤检查
        if not self._is_group_allowed(group_id):
            logger.debug(f"[主动发言] 群={group_id} 跳过原因[2]: 群未在允许列表")
            return False

        # 3. UMO 有效性检查（v2.0 改进：有效期检查）
        if not self._is_umo_valid(group_id):
            logger.debug(f"[主动发言] 群={group_id} 跳过原因[3]: UMO无效或不存在")
            return False

        # 4. 时段检查（v1.8.4 修复：使用跨天工具函数，原字符串字典序无法处理 23:00-07:00 跨天）
        now = datetime.now()
        current_time = now.strftime("%H:%M")
        time_window_start = self._get_group_param(
            group_id, "proactive_time_window_start", self._proactive_time_window_start
        )
        time_window_end = self._get_group_param(
            group_id, "proactive_time_window_end", self._proactive_time_window_end
        )
        if not self._is_in_time_period(current_time, time_window_start, time_window_end):
            logger.debug(f"[主动发言] 群={group_id} 跳过原因[4]: 时段外({current_time} 不在 {time_window_start}-{time_window_end})")
            return False

        # 4.5 静默时段检查（v1.8.4 新增：避免深夜打扰，支持单群覆盖）
        if self._is_in_quiet_hours(group_id, current_time):
            logger.debug(f"[主动发言] 群={group_id} 跳过原因[4.5]: 静默时段内")
            return False

        # 5. 冷却检查（支持单群覆盖 + 安全下限 + 退让信号②惩罚倍率）
        cooldown = self._get_group_param(
            group_id, "proactive_cooldown", self._proactive_cooldown
        )
        # Phase 2 退让信号②：读取无人回应的冷却惩罚倍率
        # v1.8.4 修复：consecutive_no_response_count 查表计算 cooldown（原方案漏洞 1.1 修复）
        # 2 次→2h / 3 次→6h / 4 次→24h（封顶 24h）
        tracker = self._proactive_response_tracker.get(group_id)
        if tracker:
            consecutive = tracker.get("consecutive_no_response_count", 0)
            if consecutive >= 4:
                cooldown = 86400  # 24h 封顶
                logger.debug(f"[主动发言] 群={group_id} 连续{consecutive}次无回应，应用最大冷却24h")
            elif consecutive == 3:
                cooldown = 21600  # 6h
                logger.debug(f"[主动发言] 群={group_id} 连续3次无回应，应用冷却6h")
            elif consecutive == 2:
                cooldown = 7200  # 2h
                logger.debug(f"[主动发言] 群={group_id} 连续2次无回应，应用冷却2h")
            # 兼容旧逻辑：cooldown_multiplier（向后兼容，新逻辑以 consecutive 为主）
            elif tracker.get("cooldown_multiplier", 1.0) > 1.0:
                cooldown *= tracker["cooldown_multiplier"]
                logger.debug(f"[主动发言] 群={group_id} 应用无人回应惩罚: 冷却×{tracker['cooldown_multiplier']}")
        last_speak = self._proactive_last_speak.get(group_id, 0)
        elapsed = time.time() - last_speak
        if elapsed < cooldown:
            remaining = int(cooldown - elapsed)
            logger.debug(f"[主动发言] 群={group_id} 跳过原因[5]: 冷却中(已过{int(elapsed)}s/{int(cooldown)}s, 剩余{remaining}s)")
            return False

        # 6. 每日上限检查（支持单群覆盖 + 安全下限）
        today = now.strftime("%Y-%m-%d")
        daily_counts = self._proactive_daily_count.get(group_id, {})
        today_count = daily_counts.get(today, 0)
        daily_limit = self._get_group_param(
            group_id, "proactive_daily_limit", self._proactive_daily_limit
        )
        if today_count >= daily_limit:
            logger.debug(f"[主动发言] 群={group_id} 跳过原因[6]: 每日上限({today_count}/{daily_limit})")
            return False

        # 7. LLM 空闲检查（防止与被动回复并发）
        if group_id in self._llm_running_groups:
            logger.debug(f"[主动发言] 群={group_id} 跳过原因[7]: LLM运行中")
            return False

        # 8. 上下文充足检查
        buffer = self._msg_buffer.get(group_id)
        if not buffer or len(buffer) < self._proactive_min_context_messages:
            buf_len = len(buffer) if buffer else 0
            logger.debug(f"[主动发言] 群={group_id} 跳过原因[8]: 上下文不足({buf_len}/{self._proactive_min_context_messages})")
            return False

        # 9. 精力检查（支持单群覆盖）
        energy_state = self._get_energy(group_id)
        min_energy = self._get_group_param(
            group_id, "proactive_min_energy", self._proactive_min_energy
        )
        if energy_state.energy < min_energy:
            logger.debug(f"[主动发言] 群={group_id} 跳过原因[9]: 精力不足({energy_state.energy:.2f}/{min_energy:.2f})")
            return False

        # 10. 心流非疲劳检查
        flow = self._flow_states.get(group_id)
        if flow and flow.state == FlowState.FATIGUED:
            logger.debug(f"[主动发言] 群={group_id} 跳过原因[10]: 心流疲劳")
            return False

        # 10.5 退让信号①：用户活跃讨论激增检测（Phase 2 新增）
        # 检测最近 5 分钟消息数是否超过阈值，若活跃激增则触发退让 1 小时
        if self._check_active_surge(group_id):
            # v1.9.7 修正 M1：退却时长从硬编码 3600 改为可配置
            self._trigger_retreat(group_id, self._retreat_active_surge_secs, "active_surge")
            logger.debug(f"[主动发言] 群={group_id} 跳过原因[10.5]: 活跃激增触发退让")
            return False

        # 11. 四维度冷场检测
        conv_state = self._detect_conversation_state(group_id)
        if conv_state == "SELF_TALK":
            # v1.7.0 修复：不再触发退让（避免死循环），仅跳过本次
            # 当有新用户消息冲淡 Bot 占比后，自动恢复正常发言
            logger.debug(f"[主动发言] 群={group_id} 跳过原因[11a]: 自说自话检测触发（Bot占比超过阈值）")
            return False
        if conv_state == "ACTIVE":
            # 群内活跃，不主动发言（MII Human_Dominant）
            logger.debug(f"[主动发言] 群={group_id} 跳过原因[11b]: 群内活跃（Human_Dominant）")
            return False

        logger.debug(f"[主动发言] 群={group_id} 所有检查通过，准备按概率触发")
        return True

    def _detect_conversation_state(self, group_id: str) -> str:
        """四维度冷场检测，返回对话状态

        返回值：
        - "COLD"：适合主动发言（冷场）
        - "ACTIVE"：群内活跃，不主动发言
        - "SELF_TALK"：Bot 自说自话，跳过本次（不触发退让，等新用户消息冲淡占比后自动恢复）
        - "INSUFFICIENT"：数据不足，无法判断
        """
        buffer = self._get_buffer(group_id)
        if not buffer or len(buffer) < 5:
            return "INSUFFICIENT"

        recent_msgs = list(buffer)[-10:]

        # 维度 1：时间（距最近一条消息的时间）
        last_msg_time = recent_msgs[-1][2] if recent_msgs else 0
        idle_secs = time.time() - last_msg_time
        is_idle = idle_secs > self._proactive_idle_threshold

        # 维度 2：内容（平均消息长度过短或信息量低）
        user_msgs = [m for m in recent_msgs if not m[3].get("is_bot_message")]
        avg_len = sum(len(m[1]) for m in user_msgs) / max(len(user_msgs), 1)
        is_low_content = avg_len < 10  # 平均长度 < 10 字符

        # 维度 3：重复（最近消息相似度过高，无新话题）
        if len(user_msgs) >= 2:
            similarities = [
                self._calc_text_similarity(user_msgs[i][1], user_msgs[i + 1][1])
                for i in range(len(user_msgs) - 1)
            ]
            avg_sim = sum(similarities) / len(similarities) if similarities else 0
            is_repetitive = avg_sim > 0.7
        else:
            is_repetitive = False

        # 维度 4：自说自话检测（v1.7.0 可配置化，使用时间窗口+占比阈值）
        # 不再触发退让，只是返回 SELF_TALK 状态让 _should_proactive_speak 跳过本次
        # 当有新用户消息进来冲淡占比后，自动恢复正常发言
        # v1.7.0 修复：在整个 buffer 上按时间窗口过滤，而非先取10条再过滤
        now = time.time()
        window_secs = self._proactive_self_talk_hours * 3600
        if window_secs > 0:
            # 在整个 buffer 上按时间窗口过滤
            all_msgs = list(buffer)
            window_msgs = [m for m in all_msgs if now - m[2] < window_secs]
        else:
            # 时间窗口=0，看全部缓冲区
            window_msgs = list(buffer)

        if window_msgs:
            bot_count = sum(1 for m in window_msgs if m[3].get("is_bot_message"))
            bot_ratio = bot_count / len(window_msgs)
            is_self_talk = bot_ratio >= self._proactive_self_talk_ratio
        else:
            is_self_talk = False

        if is_self_talk:
            return "SELF_TALK"
        if is_idle or is_low_content or is_repetitive:
            return "COLD"
        return "ACTIVE"

    async def _proactive_speak(self, group_id: str, category_override: str = None):
        """对指定群执行一次主动发言（三要素之 Initiation）

        完整流程：设置标志 → 构建ProCoT prompt → 调用LLM → 过滤去重 → 发送 → 记录 → 更新状态

        Args:
            group_id: 目标群 ID
            category_override: 可选，强制指定话题类别（如"科技资讯"）。None 时走加权随机逻辑。
                v1.8.0 新增：支持手动触发时指定资讯类话题，避免随机命中非资讯类。
        """
        # v1.9.7 新增 M5：调用前再次检查退却状态
        # 原问题：管理员手动触发（/wakeup_proactive）可绕过 _should_proactive_speak 的退却检查
        # 修复：在 _proactive_speak 入口处也检查退却状态，防止手动触发绕过退却
        if self._is_in_retreat(group_id):
            retreat_info = self._proactive_retreat.get(group_id, {})
            reason = retreat_info.get("reason", "unknown")
            logger.info(
                f"[主动发言] 群={group_id} 处于退却状态（原因: {reason}），跳过本次触发"
            )
            return

        # === 1. 设置 LLM 执行标志（防止与被动回复并发）===
        # P1-1 修复（v1.7.0）：检查是否已有 LLM 请求进行中（防止 TOCTOU 并发风险）
        if group_id in self._llm_running_groups:
            logger.warning(f"[主动发言] 群={group_id} 已有 LLM 请求进行中，跳过本次触发")
            return
        self._llm_running_groups[group_id] = time.time()
        self._start_llm_flag_timer(group_id)
        self._proactive_stats["total_attempts"] += 1  # Phase 2 统计

        # 状态机：进入 AGENT_DOMINANT
        self._proactive_states[group_id] = self._PROACTIVE_STATE_AGENT

        try:
            # === 2. 构建上下文 ===
            memory_text = self._format_conversation_memory(group_id) if self.conversation_memory_enabled else ""
            # _format_context 返回 (formatted_text, new_msg_count, old_msg_count) 三元组
            context_text, _, _ = self._format_context(group_id, incremental=False)

            # v1.8.1 修复 P0 根因 2：过滤 context 中的命令文本
            # 问题：用户发送 /wakeup_proactive 科技资讯 后，命令文本被 _record_message 写入 _msg_buffer，
            # _format_context 读取后注入 <group_chat_context>，即使 category 降级为"活跃气氛"，
            # LLM 仍从 context 看到命令文本并编造"刚刷了一下科技资讯..."
            # 修复：移除 context_text 中包含命令特征的行
            if context_text:
                context_text = self._filter_command_lines_from_context(context_text)

            # === 3. 选择话题类别（Phase 2 改进：按群活跃度加权选择）===
            # v1.8.0 新增：支持 category_override 强制指定话题（手动触发用）
            # v1.8.0 修复（B1 三次审查 m2/C1）：规范化 + Fetcher 状态检查 + 降级保护
            if category_override:
                # 防御性规范化（B1 三次审查 m2）：调用方可能传入带空格的字符串
                category_override = category_override.strip()
                # 校验 override 值是否在配置允许的类别列表中
                # 注：cmd_proactive_trigger 已硬校验，此处为公共方法防御性校验，
                # 当前调用路径下不会触发 warning 分支，但保护未来新增的调用点
                if category_override in self._proactive_topic_categories or category_override in self._NEWS_CATEGORIES:
                    category = category_override
                    logger.info(f"[主动发言] 群={group_id} 使用指定话题类别: {category}")
                else:
                    logger.warning(f"[主动发言] 群={group_id} 指定的话题类别 '{category_override}' 不在配置中，回退到加权随机")
                    category = self._select_topic_category_weighted(group_id)
            else:
                category = self._select_topic_category_weighted(group_id)
            category_instruction = self._TOPIC_CATEGORY_PROMPTS.get(category, "自然地发起一段对话")

            # === 3.5（v1.8.0 新增）：资讯类话题调用 Fetcher 获取外部资讯 ===
            # 将 Bot 从"信息索求者"转变为"有趣信息的分享者与锐评人"
            news_context = ""
            news_items_used = []  # v1.8.0 修复（B1 审查问题 7）：记录本次使用的资讯，发送成功后标记
            # v1.8.1 修复 P0 根因 5：资讯类话题无可用资讯时，SKIP 本次发言并通知用户，不降级为"活跃气氛"
            # 原行为（v1.8.0）：降级为活跃气氛 → LLM 从 context 命令文本推断话题编造
            # 新行为（v1.8.1）：SKIP 本次发言，通过 _last_proactive_skip_reason 通知用户
            # 适用场景：①category_override 显式指定资讯类但 Fetcher 未启用/无可用资讯/异常
            #          ②加权随机选中资讯类但无可用资讯（同样不应降级编造）
            # 提前初始化 _last_proactive_category 和 _last_proactive_skip_reason，供 SKIP 分支使用
            if not hasattr(self, '_last_proactive_category'):
                self._last_proactive_category = {}
            if not hasattr(self, '_last_proactive_skip_reason'):
                self._last_proactive_skip_reason = {}
            # 清空上次的 skip 原因（新一轮发言开始）
            self._last_proactive_skip_reason[group_id] = ""

            # 降级分支 1：Fetcher 未启用
            if category in self._NEWS_CATEGORIES and not self._fetcher:
                logger.warning(f"[主动发言] 群={group_id} 资讯类话题={category} 但 Fetcher 未启用，SKIP 本次发言")
                self._last_proactive_category[group_id] = category
                self._last_proactive_skip_reason[group_id] = f"资讯类话题 '{category}' 需 Fetcher 但未启用"
                self._proactive_stats["total_skip"] += 1
                return
            if category in self._NEWS_CATEGORIES and self._fetcher:
                try:
                    # v1.8.0 修复（B1 审查问题 6）：使用配置项而非硬编码
                    fetcher_max = self.config.get("fetcher", {}).get("fetcher_max_items_per_fetch", 3)
                    # v1.8.1 诊断日志（排查 fetch 返回空的问题）：记录 fetch 调用前的状态
                    logger.info(
                        f"[主动发言][Fetcher诊断] 准备调用 fetch: "
                        f"category={category}, group_id={group_id}, max_items={fetcher_max}, "
                        f"fetcher.enabled={self._fetcher.enabled}, "
                        f"fetchers={len(self._fetcher.fetchers)}, "
                        f"pool_size={self._fetcher.pool.pool_size}"
                    )
                    # v1.8.1 修复 P0 根因 1：fetch 增加 group_id 参数，per-group 去重
                    news_items = await self._fetcher.fetch(category, group_id=group_id, max_items=fetcher_max)
                    # v1.8.0 诊断日志：记录 fetch 返回结果
                    logger.info(
                        f"[主动发言][Fetcher诊断] fetch 返回 {len(news_items)} 条资讯"
                    )
                    if news_items:
                        news_context = self._format_news_context(news_items)
                        news_items_used = news_items  # 记录下来，发送成功后用于 mark_sent
                        # 资讯类话题强制使用"事实转述+吐槽"模式
                        category_instruction = self._NEWS_CATEGORY_PROMPTS.get(category, category_instruction)
                    else:
                        # 降级分支 2：资讯类话题无可用资讯（池子为空 + 数据源拉取失败/为空）
                        # v1.8.1 修复 P0 根因 5：改为 SKIP 并通知，不降级为"活跃气氛"
                        logger.warning(f"[主动发言] 群={group_id} 资讯类话题={category} 无可用资讯，SKIP 本次发言")
                        self._last_proactive_category[group_id] = category
                        self._last_proactive_skip_reason[group_id] = (
                            f"资讯类话题 '{category}' 无可用资讯"
                            f"（可能原因：池子已耗尽 / 数据源拉取失败 / 网络异常）"
                        )
                        self._proactive_stats["total_skip"] += 1
                        return
                except Exception as e:
                    # 降级分支 3：Fetcher 异常
                    # v1.8.1 修复 P0 根因 5：异常时也 SKIP，不降级为"活跃气氛"
                    logger.warning(f"[主动发言] 群={group_id} Fetcher 获取资讯失败: {e}，SKIP 本次发言")
                    self._last_proactive_category[group_id] = category
                    self._last_proactive_skip_reason[group_id] = f"Fetcher 异常: {e}"
                    self._proactive_stats["total_skip"] += 1
                    return

            # v1.8.0 修复（B1 三次审查 M2）：记录实际使用的 category，供调用方对比并通知用户
            # v1.8.1：SKIP 分支已在上方提前记录并 return，此处仅记录非 SKIP 的最终 category
            self._last_proactive_category[group_id] = category

            # === 4. 检查自定义话题 ===
            custom_topic = None
            if self._proactive_custom_topics and random.random() < 0.3:
                custom_topic = random.choice(self._proactive_custom_topics)

            # === 5. 构建 ProCoT prompt（三步法，传入 news_context）===
            prompt = self._build_proactive_prompt(memory_text, context_text, category_instruction, custom_topic, news_context, group_id)

            # === 6. 调用 LLM（必须用 provider.text_chat，避免 on_llm_request 钩子污染）===
            provider = self._get_proactive_provider()
            resp = await provider.text_chat(
                prompt=prompt,
                session_id=f"proactive_{group_id}_{int(time.time())}"
            )
            text = ""
            if hasattr(resp, 'completion_text'):
                text = resp.completion_text or ""
            elif hasattr(resp, 'result'):
                text = resp.result or ""
            text = text.strip()

            if not text:
                logger.warning(f"[主动发言] 群={group_id} LLM 返回空内容")
                self._last_proactive_skip_reason[group_id] = "LLM 返回空内容"
                return

            # === 7. 从 ProCoT 响应中提取最终发言 ===
            text = self._extract_proactive_output(text)

            # === 7b. 发送前安全验证：检测思考内容外泄 ===
            # 如果提取后仍包含思考格式特征，说明 LLM 输出异常，跳过本次发言
            if self._contains_thinking_patterns(text):
                logger.warning(f"[主动发言] 群={group_id} 检测到思考内容外泄特征，跳过本次发言")
                logger.warning(f"[主动发言] 外泄内容前200字: {text[:200]}")
                self._proactive_stats["total_skip"] += 1
                self._last_proactive_skip_reason[group_id] = "LLM 思考内容外泄（输出异常）"
                return

            # === 8. 过滤思考标签 + 去重 ===
            text = self._filter_thinking_tags(text)
            text = self._filter_duplicate_response(text)
            text = text.strip()

            # v1.9.8 加固：前缀匹配 [SKIP]（含混合输出）
            # 原因：推理模型可能输出 "[SKIP] + 决策理由 + 偶发回复" 混合体
            # （详见 2026-09-16 22:32 思考泄露事故），精确匹配会漏过导致泄露。
            # 前缀命中即视为 LLM 判断不宜发言，跳过本次主动发言。
            if not text or text.strip().startswith("[SKIP]"):
                logger.info(f"[主动发言] 群={group_id} LLM 判断不宜发言，跳过")
                self._proactive_stats["total_skip"] += 1  # Phase 2 统计
                # 连续 [SKIP] 检查（退让信号 ⑤）
                self._proactive_skip_streak[group_id] = self._proactive_skip_streak.get(group_id, 0) + 1
                if self._proactive_skip_streak[group_id] >= 3:
                    # v1.9.4 修复：consecutive_skip 退却从 6h 缩短为 1h
                    # 原因：6h 过于激进，LLM 判断不宜发言不应长时间阻塞概率唤醒
                    # v1.9.7 修正 M1：退却时长改为可配置
                    self._trigger_retreat(group_id, self._retreat_consecutive_skip_secs, "consecutive_skip")
                    self._proactive_skip_streak[group_id] = 0
                self._last_proactive_skip_reason[group_id] = "LLM 主动判断不宜发言（输出 [SKIP]）"
                return

            # 发言成功，重置 SKIP 计数
            self._proactive_skip_streak[group_id] = 0

            # === 9. 去重检查（新鲜度指标验证）===
            if self._is_duplicate_content(group_id, text):
                logger.info(f"[主动发言] 群={group_id} 内容与近期发送重复，跳过")
                self._proactive_stats["total_duplicate"] += 1  # Phase 2 统计
                self._last_proactive_skip_reason[group_id] = "内容与近期发送重复（新鲜度检查）"
                return

            # === 10. 语义去重检查 ===
            last_reply = self._last_bot_reply_text.get(group_id, "")
            last_reply_time = self._last_bot_reply_time.get(group_id, 0)
            if last_reply and (time.time() - last_reply_time) < self._SIMILARITY_WINDOW:
                similarity = self._calc_text_similarity(text, last_reply)
                if similarity >= 0.55:
                    logger.info(f"[主动发言] 群={group_id} 与上次回复语义相似({similarity:.2f})，跳过")
                    self._last_proactive_skip_reason[group_id] = f"与上次回复语义相似（相似度 {similarity:.2f}）"
                    return

            # === 10.5 话题去重检查（Phase 2 新增：避免7天内重复话题）===
            if self._is_topic_duplicate(group_id, text):
                logger.info(f"[主动发言] 群={group_id} 话题与近期重复，跳过")
                self._proactive_stats["total_duplicate"] += 1
                self._last_proactive_skip_reason[group_id] = "话题与近期重复（7天内话题去重）"
                return

            # === 10.6 敏感词过滤（Phase 2 新增：5指标后处理——接受度验证）===
            if self._filter_sensitive_words(text):
                logger.warning(f"[主动发言] 群={group_id} 发言含敏感词，跳过")
                self._last_proactive_skip_reason[group_id] = "发言含敏感词"
                return

            # === 11. 发送消息（分段发送，复用 splitter 逻辑模拟真人节奏）===
            umo_str, _ = self._group_umo[group_id]

            # 将文本按标点切分成多段（复用 splitter 的 _split_chain）
            full_chain = [Plain(text)]
            segments = self._split_chain(full_chain, self.split_regex, 0)

            # 后处理：清理空行 + 剔除末尾标点（与被动回复一致）
            for seg in segments:
                if self.trim_segment_edge_blank_lines:
                    self._trim_segment_blank_lines(seg)
                if self.strip_trailing_punct_enabled:
                    self._strip_segment_trailing_punct(seg)

            sent_count = 0
            try:
                if len(segments) <= 1:
                    # 单段直接发送（使用处理后 segments[0]，与多段一致）
                    mc = MessageChain()
                    mc.chain = segments[0] if segments else [Plain(text)]
                    await self.context.send_message(umo_str, mc)
                    sent_count = 1
                else:
                    # 多段发送：前 N-1 段主动发送，每段之间有延迟
                    logger.info(f"[主动发言] 群={group_id} 分段发送: {len(segments)}段")
                    for i in range(len(segments) - 1):
                        seg_chain = segments[i]
                        text_content = "".join([c.text for c in seg_chain if isinstance(c, Plain)])
                        if not text_content.strip(" \t\r\n\u200b") and not any(not isinstance(c, Plain) for c in seg_chain):
                            continue
                        mc = MessageChain()
                        mc.chain = seg_chain
                        await self.context.send_message(umo_str, mc)
                        sent_count += 1
                        await asyncio.sleep(self._calculate_segment_delay(text_content))
                    # 最后一段
                    mc = MessageChain()
                    mc.chain = segments[-1]
                    await self.context.send_message(umo_str, mc)
                    sent_count += 1
            except asyncio.CancelledError:
                # 多段发送被取消：记录已发送段数，记忆中标记为部分发送
                logger.warning(f"[主动发言] 群={group_id} 分段发送被取消，已发送{sent_count}/{len(segments)}段")
                # 记录实际已发送的文本（而非完整 text），避免记忆与实际不一致
                sent_text_parts = []
                for i in range(sent_count):
                    if i < len(segments):
                        sent_text_parts.append("".join([c.text for c in segments[i] if isinstance(c, Plain)]))
                text = "\n".join(sent_text_parts) if sent_text_parts else text
                # v1.8.0 修复（B1 二次审查 m8）：部分发送的资讯也应标记为已发送
                # 多段发送时无法判断哪些资讯已在已发送的段中，全部标记避免重复发送
                # v1.8.1 修复 P0 根因 1：mark_sent 增加 group_id 参数，per-group 标记
                if sent_count > 0 and news_items_used and self._fetcher:
                    for item in news_items_used:
                        self._fetcher.mark_sent(group_id, item.title, category)
                    logger.debug(
                        f"[主动发言] 群={group_id} 部分发送取消，仍标记 {len(news_items_used)} 条资讯为已发送"
                    )
                raise  # 重新抛出，让外层 CancelledError 处理
            except Exception as send_err:
                # UMO 可能已失效（群解散/Bot 被踢）
                logger.error(f"[主动发言] 群={group_id} 发送失败，清除 UMO 缓存: {send_err}")
                self._group_umo.pop(group_id, None)
                self._proactive_stats["total_send_fail"] += 1  # Phase 2 统计
                self._last_proactive_skip_reason[group_id] = f"发送失败（UMO 可能失效）: {send_err}"
                return

            # === 12. 记录到记忆系统（与被动回复统一）===
            # 12a. 写入消息缓冲区
            # 使用 _get_buffer 保持 maxlen 一致（max(ctx*2, 40)），meta 必须含 is_bot_message
            bot_name = self.bot_names[0] if self.bot_names else "Bot"
            buffer = self._get_buffer(group_id)
            buffer.append((bot_name, text, int(time.time()), {"is_bot_message": True}))

            # 12b. 写入对话历史（仅在启用分层记忆时记录）
            if self.conversation_memory_enabled:
                self._record_assistant_message(group_id, text)

            # 12c. 更新去重缓存
            self._record_sent_content(group_id, text)

            # v1.8.0 修复（B1 审查问题 7 + 二次审查 M1）：资讯类发言成功后，标记已发送的资讯
            # 防止同一资讯在短期内反复发送（如 LLM 把多条资讯合并发言时全部标记）
            # v1.8.1 修复 P0 根因 1：mark_sent 增加 group_id 参数，per-group 标记
            # v1.8.1 修复 P0 根因 4：mark_sent 不再清除池子缓存（池子跨群复用）
            if news_items_used and self._fetcher:
                for item in news_items_used:
                    self._fetcher.mark_sent(group_id, item.title, category)
                logger.debug(
                    f"[主动发言] 群={group_id} 标记 {len(news_items_used)} 条资讯为已发送 (类别={category})"
                )

            # 12d. 更新语义去重基准
            self._last_bot_reply_text[group_id] = text
            self._last_bot_reply_time[group_id] = time.time()

            # === 13. 消耗精力 + 更新心流 ===
            self._consume_energy(group_id)
            flow = self._flow_states.get(group_id)
            if flow:
                # 与被动回复一致：使用 _get_group_param 支持单群覆盖
                flow.engagement = min(1.0, flow.engagement + self._get_group_param(
                    group_id, "engagement_refresh_on_reply", self.engagement_refresh_on_reply
                ))
                flow.conversation_turns += 1
                flow.engagement_last_update = time.time()

            # === 14. 更新主动发言统计 ===
            today = datetime.now().strftime("%Y-%m-%d")
            if group_id not in self._proactive_daily_count:
                self._proactive_daily_count[group_id] = {}
            self._proactive_daily_count[group_id][today] = \
                self._proactive_daily_count[group_id].get(today, 0) + 1
            self._proactive_last_speak[group_id] = time.time()

            # === 15. 启动退让信号②追踪（Phase 2 新增）===
            # 记录发言时间戳，30分钟后由调度循环检查是否有用户回复
            # v1.8.4 修复：发言后重置 cooldown_multiplier，但保留 consecutive_no_response_count（原方案漏洞 1.1 修复）
            # consecutive_no_response_count 只在用户有效回复时重置（在 on_group_message 中处理）
            # 这样递进退让机制在多次发言后依然生效
            _prev_tracker = self._proactive_response_tracker.get(group_id, {})
            _prev_consecutive = _prev_tracker.get("consecutive_no_response_count", 0) if isinstance(_prev_tracker, dict) else 0
            self._proactive_response_tracker[group_id] = {
                "speak_time": time.time(),
                "checked": False,
                "cooldown_multiplier": 1.0,  # 重置：本次发言前的惩罚已生效
                "consecutive_no_response_count": _prev_consecutive,  # 保留递进退让记忆
                "speak_text": text[:100],  # 保留摘要用于 CPS 判定
            }

            # === 16. 记录话题历史（Phase 2 话题去重）===
            if group_id not in self._proactive_topic_history:
                self._proactive_topic_history[group_id] = deque(maxlen=10)
            self._proactive_topic_history[group_id].append((time.time(), text[:80]))

            # v1.8.4 新增：记录开头到去重队列（用于资讯类 prompt 避免重复开头模式）
            # 提取第一句话作为开头标识（前 30 字符）
            _first_sentence = text.split("。")[0].split("！")[0].split("？")[0].split("\n")[0][:30]
            if _first_sentence:
                if group_id not in self._proactive_recent_openers:
                    self._proactive_recent_openers[group_id] = deque(maxlen=5)
                self._proactive_recent_openers[group_id].append(_first_sentence)

            # v1.7.1：发言成功后立即持久化防重复数据，避免重载后短时间内重复发言
            self._save_proactive_state()

            # === 17. 更新统计 ===
            self._proactive_stats["total_success"] += 1

            # 状态机：回到 PASSIVE_MONITORING（等待用户反应）
            self._proactive_states[group_id] = self._PROACTIVE_STATE_PASSIVE

            logger.info(f"[主动发言] 群={group_id} 发言成功: {text[:80]}")

        except asyncio.CancelledError:
            logger.info(f"[主动发言] 群={group_id} 发言被取消")
            raise
        except Exception as e:
            logger.error(f"[主动发言] 群={group_id} 异常: {e}", exc_info=True)
        finally:
            # === 15. 清除 LLM 执行标志 ===
            self._llm_running_groups.pop(group_id, None)
            self._cancel_llm_flag_timer(group_id)

    def _build_proactive_prompt(
        self, memory_text: str, context_text: str,
        category_instruction: str, custom_topic: "str | None",
        news_context: str = "",
        group_id: str = ""
    ) -> str:
        """构建 ProCoT 三步法 prompt

        ProCoT（Proactive Chain-of-Thought）三步：思考 → 决策 → 生成
        来源：EMNLP 2023《Proactive Chain-of-Thought》
        v1.8.0 新增 news_context 参数：资讯类话题时注入外部资讯
        v1.8.4 新增 group_id 参数：用于开头去重机制（记录最近 5 次开头，避免重复模式）
        """
        parts = []

        if memory_text:
            parts.append(f"<conversation_memory>\n{memory_text}\n</conversation_memory>")

        if context_text:
            parts.append(f"<group_chat_context>\n{context_text}\n</group_chat_context>")

        # 外部资讯上下文（v1.8.0 新增：资讯类话题时注入）
        if news_context:
            parts.append(f"<external_news_context>\n{news_context}\n</external_news_context>")

        # ProCoT 三步法
        prococt_lines = [
            "<proactive_think>",
            "[思考阶段] 请分析以上群聊上下文和你的记忆，识别 2-3 个可切入的话题方向。",
            "考虑：",
            "- 哪些话题与近期讨论相关但可以独立成立（不需要前文铺垫也能理解）",
            "- 哪些话题对群友可能有趣",
            "- 哪些话题你未曾深入分享过",
            "",
            "输出格式：",
            "1. 候选话题A：<简述>",
            "2. 候选话题B：<简述>",
            "3. 候选话题C：<简述>",
            "</proactive_think>",
            "",
            "<proactive_decide>",
            "[决策阶段] 基于以下 5 个指标对每个候选话题评分（0-10）：",
            "- 自然切入度：能否自然地接入当前对话，不突兀",
            "- 新鲜度：近期是否已讨论过类似话题",
            "- 兴趣度：对群友的潜在吸引力",
            "- 独立性：不需要前文铺垫也能独立成立",
            "- 接受度：话题的适宜性和不冒犯性",
            "",
            f"选择总分最高的一个话题方向：{category_instruction}",
        ]

        if custom_topic:
            prococt_lines.append(f"具体话题参考：{custom_topic}（可以基于此展开，但不要照搬）")

        prococt_lines.extend([
            "</proactive_decide>",
            "",
            "<proactive_generate>",
        ])

        if news_context:
            # v1.8.0 资讯类话题：事实转述 + 犀利吐槽模式（不向群友提问）
            # v1.8.1 修复 P0 根因 3：强化硬约束，防止 LLM 扭曲真实资讯事实
            # v1.8.2 改进（基于实测反馈）：多段结构，第一段新闻梗概（含时间/地点/人物/来源），后续段锐评吐槽
            #   原问题：LLM 直接吐槽但未提及新闻内容，群友不知道在说什么；即便提及也过于简短
            #   改进：明确要求第一段必须包含新闻要素和来源，让群友知道是真实新闻而非编造
            # v1.8.4 重大改进（基于实测反馈：模板化严重）：对齐用户原话"以第一条原新闻消息的形式发出来"
            #   原问题：每次发言都以"刚在 XXX 上看到/刷到..."开头，机械且缺乏真人交流感
            #   改进：
            #   1. 第一段定位为"原文关键片段引用"（直接以新闻核心事实开头，不加"刚在 XXX 看到前缀）
            #   2. 删除模板化示例（"刚刷到"/"据 BBC 报道"等），改为提供多样化开头模式池
            #   3. 引入 Few-Shot Examples（正面+反面示例）替代纯指令
            #   4. 开头去重机制：记录最近 5 次开头，prompt 显式排除
            #   5. 硬约束 1/2 降级为软约束（"自然融入"而非"必须包含"），保留硬约束 3（空则 SKIP）
            import random as _random
            # 6 种开头模式随机选 1 种注入（避免 LLM 套用固定模板）
            _OPENER_PATTERNS = [
                "直接事实型：直接陈述新闻核心事实（如\"OpenAI 刚刚发布了 GPT-5...\"）",
                "引用型：引用新闻中的关键数字或数据（如\"1.2 亿美元，这是 Anthropic 上轮融资的金额...\"）",
                "反问型：用反问句引发好奇（如\"谁能想到，Valve 居然开始做硬件了？...\"）",
                "场景化型：用场景化描述代入新闻（如\"想象一下，你打开 Steam 发现...\"）",
                "对比型：用对比反差突出新闻（如\"昨天还在说 AI 泡沫，今天 OpenAI 就...\"）",
                "数字震撼型：用数字或规模震撼群友（如\"47 亿美元，字节跳动今年的 AI 投入...\"）",
            ]
            _chosen_pattern = _random.choice(_OPENER_PATTERNS)
            # 开头去重：获取该群最近 5 次开头，prompt 显式排除
            _recent_openers_str = ""
            if group_id and group_id in self._proactive_recent_openers:
                recent_list = list(self._proactive_recent_openers[group_id])
                if recent_list:
                    _recent_openers_str = "【开头去重】以下是你最近 5 次发言的开头模式，本次必须避免类似开头：\n- " + "\n- ".join(recent_list)
            prococt_lines.extend([
                "[生成阶段] 基于外部资讯，用你的人设和说话风格，将资讯包装成多段群聊发言。",
                "",
                "【核心定位·对齐真人分享习惯】",
                "想象你在群里看到一条新闻，想分享给群友——你会先把新闻的核心内容发出来（引用关键事实片段），",
                "然后再围绕这条新闻发表你的吐槽和锐评。而不是每次都通过\"刚在 XXX 上看到\"的方式转述说明自己看到了什么。",
                "",
                "输出结构要求（严格按多段格式，每段独立成消息发送）：",
                "",
                "【第一段：原文关键片段引用】直接以新闻核心事实开头，不要加\"刚在...看到\"前缀",
                "- 直接引用或概括新闻关键事实（一两句，让群友知道发生了什么）",
                "- 如果原文有时间、地点、人物、机构等要素，自然融入（不要干巴巴罗列）",
                "- 新闻来源可以用自然方式提及（让群友知道是真实新闻），但不要每次都套用\"据 XX 报道\"模式",
                "- 禁止用\"某公司\"\"某人\"等模糊指代替代具体名称",
                "",
                "【第二段及以后：锐评吐槽】针对这条新闻进行犀利吐槽、调侃或锐评：",
                "- 要有态度，但不要冒犯他人",
                "- 保持你的人设和说话风格",
                "- 不要向群友提问，你是分享者不是索取者",
                "",
                "【本次开头模式建议】" + _chosen_pattern,
                "（这是建议不是强制，你可以灵活选择，但要避免每次都用同一种开头）",
                _recent_openers_str if _recent_openers_str else "",
                "【Few-Shot 示例】",
                "正面示例 1（直接事实型）：",
                "  OpenAI 今天凌晨发布了 GPT-5，号称推理能力比 GPT-4 提升了 47%。",
                "  价格嘛，API 涨了 30%，Sam Altman 说是\"为了可持续运营\"——翻译一下就是割韭菜。",
                "",
                "正面示例 2（数字震撼型）：",
                "  1.2 亿美元，Anthropic 上轮融资的金额。刚拿到手就全砸进 Claude 3.5 的训练成本里了。",
                "  这就是为什么他们最近疯狂推 Claude for Work——回本压力大啊。",
                "",
                "反面示例（禁止这样写）：",
                "  刚在 TechCrunch 上看到一条新闻，说是 OpenAI 又融资了。具体多少来着，反正是很多钱。",
                "  哈哈这个公司真有钱。（← 错误：套用模板化开头 + 信息模糊 + 吐槽太短）",
                "",
                "格式要求：",
                "- 每段用句号或感叹号结束",
                "- 段落之间用换行分隔（splitter 会按句号/换行拆分为多条消息发送）",
                "- 第一段必须包含新闻核心事实，不得跳过直接吐槽",
                "- 【重要】每句话必须使用正常中文标点符号（。！？）结束，禁止用空格分隔句子",
                "- 【重要】禁止使用账号 ID、用户名或英文昵称直接称呼群友",
                "",
                "【硬约束】",
                "- 【硬约束 1】必须严格基于上方 <external_news_context> 标签中的资讯事实转述，"
                "禁止添加任何外部信息、编造数据、虚构事件细节或臆测因果关系",
                "- 【硬约束 2】转述时必须保留原始资讯的关键要素（人物、机构、事件、时间），"
                "不得用'某公司''某研究'等模糊指代替代具体名称",
                "- 【硬约束 3】如果 <external_news_context> 标签为空或不存在，必须输出 [SKIP]，"
                "禁止编造任何资讯内容",
                "- 【硬约束 4】禁止使用\"刚在 XXX 上看到/刷到\"\"据 XXX 报道\"\"XXX 上说\"等模板化开头",
                "",
                "如果你觉得此刻不适合发言，可以输出 [SKIP]。",
                "不要直接暴露思考过程，只输出最终发言。",
            ])
        else:
            # 非资讯类话题：原有生成指令
            prococt_lines.extend([
                "[生成阶段] 用你的人设和说话风格，将选定的话题方向转化为一段自然的群聊发言。",
                "要求：",
                "- 自然切入，不要生硬地宣布\"我要发起话题\"",
                "- 简短自然，像朋友间随口一提",
                "- 不要分析性或总结性的语气，像真人聊天而非做报告",
                "- 不要接着之前的话题深入分析，而是自然地开启一个相关的新角度",
                "- 保持你的人设和说话风格",
                "- 【重要】每句话必须使用正常中文标点符号（。！？）结束，禁止用空格分隔句子。例如：\"今天天气不错。要不要出去走走？\" 而不是 \"今天天气不错 要不要出去走走\"",
                "- 如果是多段发言，每段用句号或感叹号结束，换行分隔",
                "- 【重要】禁止使用账号 ID、用户名或英文昵称直接称呼群友（如 MagicalYu、ruruao 等）。真人群聊不会指着对方账号 ID 说话。应使用自然称呼（如\"各位\"、\"大家\"）或不带称呼",
                "- 如果你觉得此刻不适合发言，可以输出 [SKIP]",
                "- 不要直接暴露思考过程，只输出最终发言",
            ])

        prococt_lines.extend([
            "",
            "【格式提醒】你的回复必须包含在 <proactive_generate></proactive_generate> 标签中。",
            "不要输出 <proactive_think> 或 <proactive_decide> 标签的内容，那些已经在前面完成了。",
            "只在此处输出最终的发言文本本身。",
            "</proactive_generate>",
        ])

        parts.append("\n".join(prococt_lines))

        return "\n\n".join(parts)

    def _format_news_context(self, news_items: list) -> str:
        """格式化资讯为 prompt 上下文（v1.8.0 新增）

        Args:
            news_items: NewsItem 列表（来自 Fetcher 模块）
        Returns:
            格式化的资讯文本，用于注入 ProCoT prompt 的 <external_news_context> 块
        """
        lines = []
        for i, item in enumerate(news_items, 1):
            # 安全地格式化时间（容错处理）
            try:
                pub_time = item.published_at.strftime("%Y-%m-%d %H:%M") if item.published_at else "未知时间"
            except Exception:
                pub_time = "未知时间"
            # v1.8.0 修复（B1 审查问题 1）：清洗外部内容，防止 prompt injection
            # 移除 < > 字符，防止攻击者注入 XML 标签破坏 prompt 结构
            source = self._sanitize_for_prompt(item.source)
            title = self._sanitize_for_prompt(item.title)
            summary = self._sanitize_for_prompt(item.summary)
            url = self._sanitize_for_prompt(item.url)
            lines.append(
                f"{i}. 【{source}】{title}\n"
                f"   摘要：{summary}\n"
                f"   时间：{pub_time}\n"
                f"   链接：{url}"
            )
        return "\n\n".join(lines)

    @staticmethod
    def _sanitize_for_prompt(text: str) -> str:
        """清洗外部文本，防止 prompt injection（v1.8.0 新增）

        移除 < > 字符，防止攻击者注入 XML 标签破坏 prompt 结构。
        限制长度，避免过长内容消耗 token。
        """
        if not text:
            return ""
        # 移除 < > 字符（替换为全角，保留可读性）
        text = str(text).replace("<", "＜").replace(">", "＞")
        # 限制长度（防止超长内容消耗 token）
        if len(text) > 500:
            text = text[:500] + "..."
        return text

    def _extract_proactive_output(self, text: str) -> str:
        """从 ProCoT 响应中提取最终发言

        ProCoT prompt 生成 <proactive_generate> 块中的内容即为最终发言。
        若 LLM 未遵循格式，使用多层过滤确保思考内容不泄露给用户。

        注意：main.py 的 _filter_thinking_tags 只过滤 think 标签，不过滤 ProCoT 标签，必须在此处理
        """
        # 优先：提取 <proactive_generate> 块内容
        match = re.search(r"<proactive_generate>\s*(.*?)\s*</proactive_generate>", text, re.DOTALL)
        if match:
            return match.group(1).strip()

        # 回退 1：移除所有 ProCoT 标签块（防止 think/decide 思考内容泄露给用户）
        text = re.sub(r'<proactive_think>[\s\S]*?</proactive_think>', '', text)
        text = re.sub(r'<proactive_decide>[\s\S]*?</proactive_decide>', '', text)
        text = re.sub(r'</?proactive_generate>', '', text)

        # 回退 2：LLM 未使用标签格式，移除思考决策格式的行
        # ProCoT 思考阶段常见模式：候选话题、选项话题、评分、SKIP 判定等
        lines = text.split('\n')
        filtered_lines = []
        for line in lines:
            stripped = line.strip()
            # 跳过空行
            if not stripped:
                continue
            # 跳过明显的思考决策行
            if re.match(r'^[\d.]*\s*候选话题', stripped) or \
               re.match(r'^[\d.]*\s*选项话题', stripped) or \
               re.match(r'^[\d.]*\s*话题[A-C]', stripped) or \
               re.match(r'^[\d.]*\s*[A-C][.、:：]', stripped) or \
               re.match(r'^[-\d.]*\s*(自然切入度|新鲜度|兴趣度|独立性|接受度|关联性|热度|评分|总分)', stripped) or \
               re.match(r'^选择(总分最高|话题)', stripped) or \
               re.match(r'^SKIP', stripped):
                continue
            filtered_lines.append(line)

        text = '\n'.join(filtered_lines).strip()

        # 回退 3：如果过滤后内容为空或过短（<5字），返回 [SKIP] 避免发送空消息
        if len(text) < 5:
            logger.warning(f"[主动发言] ProCoT 输出过滤后内容过短或为空，返回 SKIP")
            return "[SKIP]"

        return text

    def _contains_thinking_patterns(self, text: str) -> bool:
        """检测文本是否包含 ProCoT 思考内容外泄特征

        在发送前作为最后一道安全网，防止思考内容泄露到群聊。
        检测以下特征：
        - ProCoT 标签残留
        - 候选话题/选项话题格式
        - 评分指标格式
        - 过多分段（>15段，正常发言不会这么多段）
        """
        # 1. ProCoT 标签残留
        if re.search(r'</?proactive_(think|decide|generate)>', text):
            return True

        # 2. 候选话题/选项话题格式（ProCoT 思考阶段特征）
        if re.search(r'候选话题|选项话题', text):
            return True

        # 3. 评分指标格式（ProCoT 决策阶段特征）
        if re.search(r'(自然切入度|新鲜度|兴趣度|独立性|接受度|关联性|热度)[:：]\s*\d', text):
            return True
        if re.search(r'选择总分最高|选择话题', text):
            return True

        # 4. 过多分段（正常发言通常 1-5 段，超过 15 段说明思考内容被切分）
        segments = re.split(r'[。？！?!.\n…]+', text)
        non_empty_segments = [s.strip() for s in segments if s.strip()]
        if len(non_empty_segments) > 15:
            return True

        return False

    def _select_topic_category(self) -> str:
        """随机选择话题类别"""
        if not self._proactive_topic_categories:
            return "分享想法"
        return random.choice(self._proactive_topic_categories)

    def _get_proactive_provider(self):
        """获取主动发言使用的 LLM Provider

        必须返回 Provider 实例（用于 provider.text_chat），
        不能返回 context（避免误用 llm_generate 触发 on_llm_request 钩子）。
        """
        if self._proactive_model:
            provider = self.context.get_provider_by_id(self._proactive_model)
            if provider:
                return provider
            logger.warning(f"[主动发言] 配置的模型 {self._proactive_model} 不可用，回退到默认")
        return self.context.get_using_provider()

    def _is_umo_valid(self, group_id: str) -> bool:
        """检查 UMO 缓存是否仍在有效期内"""
        umo_info = self._group_umo.get(group_id)
        if not umo_info:
            return False
        _, cached_time = umo_info
        return (time.time() - cached_time) < self.UMO_VALIDITY_PERIOD

    # ─── v1.7.1 持久化：UMO + 防重复数据 ──────────────────────────
    # 解决插件重载后状态清空导致的问题：
    #   1. 沉默群 UMO 未填充 → 调度器不遍历 → 永远无法触发主动发言
    #   2. 防重复数据清空 → 重载后短时间内可能重复发言
    # 持久化文件：data/proactive_state.json（插件目录下）

    def _get_proactive_state_path(self) -> str:
        """获取主动发言持久化状态文件路径"""
        return os.path.join(os.path.dirname(__file__), "data", "proactive_state.json")

    def _get_proactive_target_groups(self) -> set:
        """返回主动发言调度器应遍历的群列表

        v1.9.1 修复：白名单语义。
        - 如果配置了 proactive_target_groups（非空），则只遍历配置的群（白名单模式）。
        - 如果未配置（留空），则遍历所有有 UMO 缓存的群（旧行为，向后兼容）。

        这样用户可以通过配置目标群列表来限制主动发言的范围，
        避免在未配置的群内主动发言。
        """
        config_groups_str = self.config.get("proactive_speak", {}).get("proactive_target_groups", "")
        if config_groups_str:
            # 白名单模式：只遍历配置的群
            target_groups = set()
            for gid in re.split(r"[,，]", config_groups_str):
                gid = gid.strip()
                if gid:
                    target_groups.add(gid)
            return target_groups
        else:
            # 旧行为：遍历所有有 UMO 缓存的群
            return set(self._group_umo.keys())

    def _load_proactive_state(self):
        """从磁盘加载主动发言持久化状态

        在 initialize 中调用，恢复以下数据：
        - UMO 缓存（_group_umo）：避免重载后沉默群无法被调度
        - 去重缓存（_sent_content_cache）：避免重载后短时间内重复发言
        - 语义去重基准（_last_bot_reply_text / _last_bot_reply_time）
        - 话题历史（_proactive_topic_history）：避免重载后话题重复

        每段独立 try/except，单段损坏不影响其他段恢复。
        """
        state_path = self._get_proactive_state_path()
        if not os.path.exists(state_path):
            logger.debug("[主动发言] 无持久化状态文件，跳过加载")
            return
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception as e:
            logger.warning(f"[主动发言] 持久化文件解析失败，跳过加载: {e}")
            return
        now = time.time()
        # 1. 恢复 UMO 缓存（过滤已过期条目）
        try:
            umo_cache = state.get("umo_cache", {})
            restored_umo = 0
            for gid, item in umo_cache.items():
                # JSON 列表格式 → 运行时元组
                if isinstance(item, list) and len(item) == 2:
                    umo_str, cached_time = item[0], item[1]
                    if (now - cached_time) < self.UMO_VALIDITY_PERIOD:
                        self._group_umo[gid] = (umo_str, cached_time)
                        restored_umo += 1
        except Exception as e:
            logger.warning(f"[主动发言] 恢复 UMO 缓存失败: {e}")
            restored_umo = 0
        # 2. 恢复去重缓存（过滤已过期条目）
        try:
            dedup_cache = state.get("dedup_cache", {})
            for gid, items in dedup_cache.items():
                cache = deque(maxlen=50)
                # 按 ts 排序后 append，确保 deque 时间升序
                sorted_items = sorted(
                    [(fp, ts) for fp, ts in items if (now - ts) < self._DEDUP_WINDOW],
                    key=lambda x: x[1]
                )
                for fp, ts in sorted_items:
                    cache.append((fp, ts))
                if cache:
                    self._sent_content_cache[gid] = cache
        except Exception as e:
            logger.warning(f"[主动发言] 恢复去重缓存失败: {e}")
        # 3. 恢复语义去重基准（过滤已过期条目）
        try:
            last_reply = state.get("last_bot_reply", {})
            for gid, item in last_reply.items():
                if isinstance(item, list) and len(item) == 2:
                    text_val, ts = item[0], item[1]
                    if (now - ts) < self._SIMILARITY_WINDOW:
                        self._last_bot_reply_text[gid] = text_val
                        self._last_bot_reply_time[gid] = ts
        except Exception as e:
            logger.warning(f"[主动发言] 恢复语义去重基准失败: {e}")
        # 4. 恢复话题历史（过滤超过 24h 的旧话题）
        try:
            topic_history = state.get("topic_history", {})
            topic_max_age = 86400  # 24h
            for gid, items in topic_history.items():
                history = deque(maxlen=10)
                for item in items:
                    # JSON 列表格式 [ts, topic] → 运行时元组 (ts, topic)
                    if isinstance(item, list) and len(item) == 2:
                        ts, topic = item[0], item[1]
                        if (now - ts) < topic_max_age:
                            history.append((ts, topic))
                if history:
                    self._proactive_topic_history[gid] = history
        except Exception as e:
            logger.warning(f"[主动发言] 恢复话题历史失败: {e}")
        # 5. v1.8.4 新增：恢复 response_tracker（含 consecutive_no_response_count）
        # 过滤 speak_time 超过 24h 的过期条目
        try:
            response_tracker = state.get("response_tracker", {})
            restored_tracker = 0
            for gid, tracker in response_tracker.items():
                speak_time = tracker.get("speak_time", 0)
                if speak_time and (now - speak_time) > 86400:
                    continue  # 过期条目跳过
                # v1.8.4 修复 M4：重载后 buffer 未持久化（为空），若 checked=False 且 speak_time 已过 30 分钟，
                # _check_no_response_all_groups 会因 buffer 为空误判为"无用户回复"导致 consecutive 错误累加
                # 修复：重载时若 buffer 为空，将 checked 标记为 True 跳过本次检查（损失一次检查机会，避免误判）
                _checked = tracker.get("checked", False)
                if not _checked and not self._msg_buffer:
                    _checked = True
                self._proactive_response_tracker[gid] = {
                    "speak_time": speak_time,
                    "checked": _checked,
                    "cooldown_multiplier": tracker.get("cooldown_multiplier", 1.0),
                    "consecutive_no_response_count": tracker.get("consecutive_no_response_count", 0),
                }
                restored_tracker += 1
        except Exception as e:
            logger.warning(f"[主动发言] 恢复 response_tracker 失败: {e}")
        # 6. v1.8.4 新增：恢复 last_speak（每群上次发言时间戳）
        try:
            last_speak = state.get("last_speak", {})
            for gid, ts in last_speak.items():
                if (now - ts) < 86400:  # 过滤超过 24h 的过期条目
                    self._proactive_last_speak[gid] = ts
        except Exception as e:
            logger.warning(f"[主动发言] 恢复 last_speak 失败: {e}")
        # 7. v1.8.4 新增：恢复 daily_count（每群每日发言计数）
        try:
            daily_count = state.get("daily_count", {})
            for gid, counts in daily_count.items():
                if isinstance(counts, dict):
                    self._proactive_daily_count[gid] = dict(counts)
        except Exception as e:
            logger.warning(f"[主动发言] 恢复 daily_count 失败: {e}")
        # 8. v1.8.4 新增：恢复 retreat（退让状态，过滤已过期）
        try:
            retreat = state.get("retreat", {})
            for gid, r in retreat.items():
                until_ts = r.get("until", 0)
                if until_ts > now:  # 只恢复未过期的退让状态
                    self._proactive_retreat[gid] = {"until": until_ts, "reason": r.get("reason", "")}
        except Exception as e:
            logger.warning(f"[主动发言] 恢复 retreat 失败: {e}")
        # 9. v1.8.4 新增：恢复 skip_streak（连续 SKIP 计数）
        try:
            skip_streak = state.get("skip_streak", {})
            for gid, count in skip_streak.items():
                if isinstance(count, int) and count > 0:
                    self._proactive_skip_streak[gid] = count
        except Exception as e:
            logger.warning(f"[主动发言] 恢复 skip_streak 失败: {e}")
        logger.info(
            f"[主动发言] 持久化状态已加载 | "
            f"UMO={len(self._group_umo)}, 去重={len(self._sent_content_cache)}, "
            f"语义={len(self._last_bot_reply_text)}, 话题={len(self._proactive_topic_history)}, "
            f"tracker={len(self._proactive_response_tracker)}, last_speak={len(self._proactive_last_speak)}, "
            f"daily_count={len(self._proactive_daily_count)}, retreat={len(self._proactive_retreat)}"
        )

    def _save_proactive_state(self):
        """保存主动发言持久化状态到磁盘（原子写入）

        在以下场景调用：
        - terminate（插件卸载/重载）
        - on_group_message 更新 UMO 后（延迟保存）
        - _proactive_speak 发言成功后

        使用 tempfile + os.replace() 实现原子写入，避免崩溃导致状态文件损坏。
        """
        state_path = self._get_proactive_state_path()
        data_dir = os.path.dirname(state_path)
        os.makedirs(data_dir, exist_ok=True)
        try:
            now = time.time()
            # 序列化 UMO 缓存（只保存未过期条目，避免文件无限增长）
            umo_cache = {}
            for gid, (umo_str, cached_time) in self._group_umo.items():
                if (now - cached_time) < self.UMO_VALIDITY_PERIOD:
                    umo_cache[gid] = [umo_str, cached_time]
            # 序列化去重缓存（deque → list）
            dedup_cache = {}
            for gid, cache in self._sent_content_cache.items():
                dedup_cache[gid] = [[fp, ts] for fp, ts in cache]
            # 序列化语义去重基准
            last_reply = {}
            for gid in self._last_bot_reply_text:
                if gid in self._last_bot_reply_time:
                    last_reply[gid] = [self._last_bot_reply_text[gid], self._last_bot_reply_time[gid]]
            # 序列化话题历史（deque → list，元组顺序调整）
            topic_history = {}
            for gid, history in self._proactive_topic_history.items():
                topic_history[gid] = [[ts, topic] for ts, topic in history]
            # v1.8.4 新增：序列化主动发言运行时状态（避免重载后冷却惩罚归零）
            # response_tracker：包含 consecutive_no_response_count / cooldown_multiplier / speak_time
            # last_speak：每群上次发言时间戳（冷却控制依赖）
            # daily_count：每群每日发言计数（每日上限检查依赖）
            # retreat：退让状态（避免重载后退让状态丢失）
            # skip_streak：连续 SKIP 计数（避免重载后递进退让丢失）
            response_tracker = {}
            for gid, tracker in self._proactive_response_tracker.items():
                # 过滤 speak_time 超过 24h 的过期条目，避免文件无限增长
                speak_time = tracker.get("speak_time", 0)
                if speak_time and (now - speak_time) > 86400:
                    continue
                response_tracker[gid] = {
                    "speak_time": tracker.get("speak_time", 0),
                    "checked": tracker.get("checked", False),
                    "cooldown_multiplier": tracker.get("cooldown_multiplier", 1.0),
                    "consecutive_no_response_count": tracker.get("consecutive_no_response_count", 0),
                }
            last_speak = {}
            for gid, ts in self._proactive_last_speak.items():
                if (now - ts) < 86400:  # 过滤超过 24h 的过期条目
                    last_speak[gid] = ts
            daily_count = {}
            today_str = datetime.now().strftime("%Y-%m-%d")
            for gid, counts in self._proactive_daily_count.items():
                # 只保存今天的计数，昨天的清零
                if today_str in counts:
                    daily_count[gid] = {today_str: counts[today_str]}
            retreat = {}
            for gid, r in self._proactive_retreat.items():
                until_ts = r.get("until", 0)
                if until_ts > now:  # 只保存未过期的退让状态
                    retreat[gid] = {"until": until_ts, "reason": r.get("reason", "")}
            skip_streak = {}
            for gid, count in self._proactive_skip_streak.items():
                if count > 0:
                    skip_streak[gid] = count
            state = {
                "umo_cache": umo_cache,
                "dedup_cache": dedup_cache,
                "last_bot_reply": last_reply,
                "topic_history": topic_history,
                # v1.8.4 新增字段
                "response_tracker": response_tracker,
                "last_speak": last_speak,
                "daily_count": daily_count,
                "retreat": retreat,
                "skip_streak": skip_streak,
                "saved_at": now,
            }
            # 原子写入：先写临时文件，再 os.replace 覆盖目标文件
            import tempfile
            fd, tmp_path = tempfile.mkstemp(dir=data_dir, suffix=".tmp", prefix="proactive_state_")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, state_path)
            except Exception:
                # 写入失败时清理临时文件
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.warning(f"[主动发言] 保存持久化状态失败: {e}")

    def _maybe_save_proactive_state(self):
        """延迟持久化：距离上次成功保存超过 60 秒才保存

        用于 on_group_message 等高频场景，避免每次消息都触发磁盘 IO。
        发言成功后直接调用 _save_proactive_state() 立即保存。
        时间戳在保存成功后才更新，失败时下次仍会重试。
        """
        now = time.time()
        if (now - self._last_save_proactive_state_ts) > 60:
            self._save_proactive_state()
            # 仅在保存成功（无异常被吞掉）后更新时间戳
            # 注意：_save_proactive_state 内部 try/except 吞掉异常，无法直接判断成功
            # 采用乐观策略：即使失败也更新时间戳，避免高频重试（失败大概率是权限/磁盘问题，重试也无益）
            self._last_save_proactive_state_ts = now

    def _is_in_retreat(self, group_id: str) -> bool:
        """检查群是否处于退让状态（MII Human_Dominant 退让）"""
        retreat_info = self._proactive_retreat.get(group_id)
        if not retreat_info:
            return False
        if time.time() < retreat_info["until"]:
            return True
        del self._proactive_retreat[group_id]
        return False

    def _trigger_retreat(self, group_id: str, duration: int, reason: str):
        """触发退让"""
        self._proactive_retreat[group_id] = {
            "until": time.time() + duration,
            "reason": reason
        }
        # Phase 2 统计：记录退让触发
        self._proactive_stats["total_retreats"] += 1
        retreat_key = f"retreat_{reason}"
        if retreat_key in self._proactive_stats:
            self._proactive_stats[retreat_key] += 1
        logger.info(f"[主动发言] 群={group_id} 进入退让状态 {duration}s，原因：{reason}")

    # ─── Phase 2 退让信号①②③ 实现（v1.6.0） ───

    def _check_active_surge(self, group_id: str) -> bool:
        """退让信号①：检测用户活跃讨论激增

        检查最近 N 分钟（默认5分钟）内的用户消息数是否超过阈值。
        若活跃激增说明用户正在热烈讨论，Bot 应退让（MII Human_Dominant）。

        阈值和窗口通过 __init__ 的 _proactive_active_surge_threshold / _proactive_active_surge_window 配置。
        """
        buffer = self._msg_buffer.get(group_id)
        if not buffer:
            return False
        now = time.time()
        cutoff = now - self._proactive_active_surge_window
        # 统计窗口内的用户消息（排除 Bot 自己的消息）
        user_msg_count = sum(
            1 for entry in buffer
            if entry[2] >= cutoff and not entry[3].get("is_bot_message", False)
        )
        return user_msg_count > self._proactive_active_surge_threshold

    def _check_no_response_all_groups(self):
        """退让信号②：检查所有群的无人回应追踪

        在每次调度循环开始时调用，检查是否有群在主动发言后30分钟内无用户回复。
        若无人回应，设置冷却×2 惩罚（避免下次过早发言）。
        """
        now = time.time()
        expired_trackers = []
        for group_id, tracker in self._proactive_response_tracker.items():
            if tracker["checked"]:
                continue
            elapsed = now - tracker["speak_time"]
            if elapsed >= self._proactive_no_response_window:
                # 30分钟已到，检查是否有用户回复
                buffer = self._msg_buffer.get(group_id)
                has_user_response = False
                if buffer:
                    for entry in buffer:
                        if (entry[2] > tracker["speak_time"]
                                and not entry[3].get("is_bot_message", False)):
                            has_user_response = True
                            break
                if not has_user_response:
                    # v1.8.4 修复：累加 consecutive_no_response_count（原方案漏洞 1.1 修复）
                    # 原 Bug：tracker 重置时 cooldown_multiplier 归零，惩罚只生效一次
                    # 新逻辑：累加 consecutive_no_response_count，递进式退让
                    #   2 次→冷却2h / 3 次→冷却6h / 4+ 次→冷却24h（封顶）
                    # 同时累乘 cooldown_multiplier（向后兼容），上限 24.0
                    prev_consecutive = tracker.get("consecutive_no_response_count", 0)
                    new_consecutive = prev_consecutive + 1
                    tracker["consecutive_no_response_count"] = new_consecutive
                    # 累乘 cooldown_multiplier（上限 24.0，避免无限放大）
                    prev_mult = tracker.get("cooldown_multiplier", 1.0)
                    new_mult = min(prev_mult * self._proactive_no_response_penalty, 24.0)
                    tracker["cooldown_multiplier"] = new_mult
                    # 软退让分层：连续 2 次无回应时进入硬退让（设置退让状态 1 小时）
                    # 连续 1 次只是冷却延长，不进入退让状态（保留探测）
                    if new_consecutive >= 2:
                        # 硬退让：连续 2 次以上无回应，触发退让
                        # v1.9.7 修正 M1+M2：退却时长与 cooldown 协调，且可配置
                        # 原问题：retreat = 3600*N（1h/2h/3h...），cooldown 查表（2h/6h/24h），
                        #   两者不协调——retreat 过早过期后 cooldown 仍阻塞，行为不可预测
                        # 修复：retreat_duration = cooldown 查表值，受 _retreat_no_response_max_secs 限制
                        #   这样 retreat 和 cooldown 同步过期，行为可预测
                        if new_consecutive >= 4:
                            retreat_duration = min(86400, self._retreat_no_response_max_secs)
                        elif new_consecutive == 3:
                            retreat_duration = min(21600, self._retreat_no_response_max_secs)
                        else:  # new_consecutive == 2
                            retreat_duration = min(7200, self._retreat_no_response_max_secs)
                        self._trigger_retreat(group_id, retreat_duration, "consecutive_no_response")
                        logger.info(
                            f"[主动发言] 群={group_id} 主动发言后30分钟无人回应，"
                            f"连续{new_consecutive}次无回应，冷却×{new_mult}（递进退让{retreat_duration}秒）"
                        )
                    else:
                        logger.info(
                            f"[主动发言] 群={group_id} 主动发言后30分钟无人回应，"
                            f"连续{new_consecutive}次（软退让，仅延长冷却）"
                        )
                    self._proactive_stats["retreat_no_response"] += 1
                # 记录效果（Phase 3 评估指标）
                cps = self._count_post_speak_responses(group_id, tracker["speak_time"])
                adopted = cps > 0
                self._record_proactive_outcome(group_id, cps, adopted)
                tracker["checked"] = True
                expired_trackers.append(group_id)
        # 清理已检查的追踪记录（保留30分钟后清理，避免内存泄漏）
        for gid in expired_trackers:
            # 保留 cooldown_multiplier 到下次发言时使用
            pass  # 不立即删除，在下次 _should_proactive_speak 的冷却检查中读取

    def _count_post_speak_responses(self, group_id: str, speak_time: float) -> int:
        """统计主动发言后30分钟内的用户回复数（CPS 计算）"""
        buffer = self._msg_buffer.get(group_id)
        if not buffer:
            return 0
        cutoff = speak_time + self._proactive_no_response_window
        return sum(
            1 for entry in buffer
            if speak_time < entry[2] <= cutoff
            and not entry[3].get("is_bot_message", False)
        )

    def _check_annoyed_keywords(self, group_id: str, message_str: str):
        """退让信号③：检测厌烦关键词

        检测到"别说了"/"闭嘴"/"吵"等关键词时触发退让24小时。
        在 on_group_message 入口处调用，不依赖唤醒触发。
        """
        # 仅在主动发言功能启用时检测
        if not self._proactive_enabled:
            return
        # v1.9.6 修正：黑名单/非白名单群不检测厌烦关键词
        # 原问题：_check_annoyed_keywords 在白名单检查前调用（L2959 vs L3078），
        # 黑名单群也能触发 24h 退却并持久化，后续加入白名单后退却仍生效
        if not self._is_group_allowed(group_id):
            return
        # 快速检查：消息是否包含任何厌烦关键词
        msg_lower = message_str.lower()
        for keyword in self._proactive_annoyed_keywords:
            if keyword in message_str or keyword.lower() in msg_lower:
                # 检查是否已在退让中（避免重复触发）
                if not self._is_in_retreat(group_id):
                    # v1.9.7 修正 M1：退却时长从硬编码 86400 改为可配置
                    self._trigger_retreat(group_id, self._retreat_annoyed_secs, "annoyed")
                    logger.info(f"[主动发言] 群={group_id} 检测到厌烦关键词 '{keyword}'，退让{self._retreat_annoyed_secs}秒")
                return

    # ─── Phase 2 话题系统增强（v1.6.0） ───

    def _select_topic_category_weighted(self, group_id: str) -> str:
        """按群活跃度加权选择话题类别（CoI 临场感平衡 + Fetcher 资讯类）

        v1.8.0 改进：
        - 新增资讯类话题（科技资讯/游戏八卦/沙雕新闻/热点事件）
        - 降低"提问讨论"权重（避免冷场）
        - 冷清群资讯类权重高（给群友喂乐子）
        - 心流群分享想法权重恢复
        """
        categories = self._proactive_topic_categories
        if not categories:
            return "活跃气氛"

        # 如果 Fetcher 未启用，或已启用但无可用数据源，从候选中移除资讯类话题
        # v1.8.0 修复（B1 二次审查 m3）：原仅检查 enabled，但即使 enabled=True，
        # 若 RSS 未开启且 API Key 缺失，或依赖未安装，fetchers 列表会为空，
        # 选中资讯类话题后必在 _proactive_speak 中降级，造成无效循环
        if not self._fetcher or not self._fetcher.enabled or not self._fetcher.fetchers:
            categories = [c for c in categories if c not in self._NEWS_CATEGORIES]
            if not categories:
                return "活跃气氛"

        # 根据心流状态判断群活跃度
        flow = self._flow_states.get(group_id)
        if not flow:
            # 无心流状态，均匀随机
            return random.choice(categories)

        # 根据心流状态构建权重
        # v1.8.0 调整：资讯类权重高，提问讨论权重低（Bot 从索取者→分享者）
        weights = {}
        if flow.state == FlowState.BYSTANDER:
            # 冷清：资讯类 > 活跃气氛 > 其他（给群友喂乐子）
            weights = {
                "活跃气氛": 2.0, "沙雕新闻": 3.0, "游戏八卦": 2.5,
                "科技资讯": 2.0, "热点事件": 1.5,
                "关注某人": 1.5, "分享想法": 1.0, "提问讨论": 0.3, "回忆过去": 0.5
            }
        elif flow.state == FlowState.ATTENTIVE:
            # 关注：均衡，资讯类仍占优
            weights = {
                "活跃气氛": 1.5, "沙雕新闻": 2.0, "游戏八卦": 2.0,
                "科技资讯": 1.5, "热点事件": 1.0,
                "关注某人": 1.5, "分享想法": 2.0, "提问讨论": 1.0, "回忆过去": 1.5
            }
        elif flow.state == FlowState.FLOW:
            # 心流：分享想法权重恢复，资讯类仍可用
            weights = {
                "活跃气氛": 1.0, "沙雕新闻": 1.5, "游戏八卦": 1.0,
                "科技资讯": 1.0, "热点事件": 1.0,
                "关注某人": 1.0, "分享想法": 3.0, "提问讨论": 1.5, "回忆过去": 2.0
            }
        else:
            # FATIGUED 或其他：均匀
            return random.choice(categories)

        # 构建加权列表
        weighted = []
        for cat in categories:
            weight = weights.get(cat, 1.0)
            weighted.extend([cat] * int(weight * 10))
        return random.choice(weighted) if weighted else random.choice(categories)

    def _is_topic_duplicate(self, group_id: str, text: str) -> bool:
        """话题去重：检查发言是否与近期主动发言话题重复

        使用文本相似度检查，避免7天内重复发起相同话题（新鲜度指标）。
        """
        history = self._proactive_topic_history.get(group_id)
        if not history:
            return False
        now = time.time()
        # 仅检查最近7天的话题
        for ts, topic_text in history:
            if now - ts > 604800:  # 7天
                continue
            similarity = self._calc_text_similarity(text, topic_text)
            if similarity >= 0.6:
                logger.info(f"[主动发言] 群={group_id} 话题与历史重复(相似度={similarity:.2f})")
                return True
        return False

    def _filter_sensitive_words(self, text: str) -> bool:
        """5指标评分后处理——接受度验证：敏感词过滤

        返回 True 表示包含敏感词（应跳过），False 表示安全。
        """
        text_lower = text.lower()
        for word in self._proactive_sensitive_words:
            if word in text or word.lower() in text_lower:
                logger.warning(f"[主动发言] 敏感词检测: '{word}'，跳过发言")
                return True
        return False

    # ─── Phase 3 评估指标（v1.6.0） ───

    def _record_proactive_outcome(self, group_id: str, cps: int, adopted: bool):
        """记录主动发言效果（在发言后30分钟由调度器回调）

        数据结构：{group_id: [(timestamp, cps, adopted), ...]}，仅保留最近20条
        """
        if group_id not in self._proactive_outcomes:
            self._proactive_outcomes[group_id] = []
        self._proactive_outcomes[group_id].append((time.time(), cps, adopted))
        # 仅保留最近20条
        if len(self._proactive_outcomes[group_id]) > 20:
            self._proactive_outcomes[group_id] = self._proactive_outcomes[group_id][-20:]
        logger.debug(f"[主动发言] 群={group_id} 效果记录: CPS={cps}, adopted={adopted}")

    def _get_proactive_metrics(self, group_id: "str | None" = None) -> dict:
        """获取主动发言评估指标

        参数 group_id 为 None 时返回全局统计，否则返回指定群的统计。
        """
        if group_id is None:
            # 全局统计
            stats = dict(self._proactive_stats)
            # 计算全局 CPS 和采纳率
            all_outcomes = []
            for gid, outcomes in self._proactive_outcomes.items():
                all_outcomes.extend(outcomes)
            if all_outcomes:
                total_cps = sum(o[1] for o in all_outcomes)
                adopted_count = sum(1 for o in all_outcomes if o[2])
                stats["avg_cps"] = round(total_cps / len(all_outcomes), 2)
                stats["adoption_rate"] = round(adopted_count / len(all_outcomes) * 100, 1)
                stats["total_outcomes"] = len(all_outcomes)
            else:
                stats["avg_cps"] = 0
                stats["adoption_rate"] = 0
                stats["total_outcomes"] = 0
            return stats
        else:
            # 单群统计
            outcomes = self._proactive_outcomes.get(group_id, [])
            if not outcomes:
                return {"avg_cps": 0, "adoption_rate": 0, "total": 0}
            total_cps = sum(o[1] for o in outcomes)
            adopted_count = sum(1 for o in outcomes if o[2])
            return {
                "avg_cps": round(total_cps / len(outcomes), 2),
                "adoption_rate": round(adopted_count / len(outcomes) * 100, 1),
                "total": len(outcomes),
                "recent": outcomes[-5:],  # 最近5条
            }

    async def terminate(self):
        """插件卸载/停用时调用，清理所有内存数据"""
        # ─── v1.5.0 主动发言：先取消调度器任务，避免其在清理过程中访问已清空的状态 ───
        proactive_task_count = 0
        if self._proactive_task is not None and not self._proactive_task.done():
            self._proactive_task.cancel()
            proactive_task_count = 1
            try:
                await self._proactive_task
            except asyncio.CancelledError:
                pass
        self._proactive_task = None

        # v1.9.7 新增 M3：关闭 FetcherManager 中的持久 aiohttp session
        if hasattr(self, '_fetcher_manager') and self._fetcher_manager is not None:
            try:
                await self._fetcher_manager.close()
            except Exception as e:
                logger.warning(f"[terminate] 关闭 FetcherManager 失败: {e}")

        # v1.7.1：在清理状态前保存持久化数据（UMO + 防重复），避免重载后丢失
        self._save_proactive_state()

        buffer_count = sum(len(buf) for buf in self._msg_buffer.values())
        group_count = len(self._msg_buffer)
        energy_count = len(self._energy_states)
        flow_count = len(self._flow_states)
        rescue_count = len(self._rescue_states)
        conv_count = len(self._conversation_history)
        summary_count = len(self._conversation_summaries)

        # 取消所有防抖计时器
        for state in self._debounce_states.values():
            if state.timer_task is not None and not state.timer_task.done():
                state.timer_task.cancel()
        debounce_count = len(self._debounce_states)
        self._debounce_states.clear()

        self._msg_buffer.clear()
        self._energy_states.clear()
        self._flow_states.clear()
        self._rescue_states.clear()
        # 取消所有 LLM 标志定时器
        for task in self._llm_flag_timers.values():
            if not task.done():
                task.cancel()
        self._llm_flag_timers.clear()
        self._llm_running_groups.clear()
        # 诊断（v1.7.3）：同步清理诊断数据，防止孤儿键
        self._llm_request_started_at.clear()
        self._llm_response_received_at.clear()
        self._conversation_history.clear()
        self._conversation_summaries.clear()
        self._summary_checkpoint.clear()
        self._sent_content_cache.clear()
        self._bot_user_ids.clear()
        self._last_context_ts.clear()
        # ─── v1.5.0 主动发言：清理所有主动发言状态 ───
        umo_count = len(self._group_umo)
        proactive_last_count = len(self._proactive_last_speak)
        proactive_daily_count = len(self._proactive_daily_count)
        proactive_retreat_count = len(self._proactive_retreat)
        proactive_states_count = len(self._proactive_states)
        proactive_skip_count = len(self._proactive_skip_streak)
        self._group_umo.clear()
        self._proactive_last_speak.clear()
        self._proactive_daily_count.clear()
        self._proactive_retreat.clear()
        self._proactive_states.clear()
        self._proactive_skip_streak.clear()
        # ─── v1.6.0 Phase 2/3 新增数据结构清理 ───
        response_tracker_count = len(self._proactive_response_tracker)
        topic_history_count = len(self._proactive_topic_history)
        outcomes_count = len(self._proactive_outcomes)
        self._proactive_response_tracker.clear()
        self._proactive_topic_history.clear()
        self._proactive_outcomes.clear()
        logger.info(
            f"灵犀插件已卸载 | "
            f"已释放 {group_count} 个群缓冲区({buffer_count} 条消息), "
            f"{energy_count} 个精力状态, {flow_count} 个心流状态, {rescue_count} 个救场状态, "
            f"{debounce_count} 个防抖状态, {conv_count} 个对话历史, {summary_count} 个摘要, "
            f"{proactive_task_count} 个主动发言调度器, {umo_count} 个 UMO 缓存, "
            f"{proactive_last_count} 个发言时间戳, {proactive_daily_count} 个日计数, "
            f"{proactive_retreat_count} 个退让状态, {proactive_states_count} 个状态, "
            f"{proactive_skip_count} 个跳过计数, "
            f"{response_tracker_count} 个回应追踪, {topic_history_count} 个话题历史, "
            f"{outcomes_count} 个效果记录"
        )
