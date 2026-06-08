import asyncio
import hashlib
import math
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
    "1.2.0",
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
        # _conversation_history: {group_id: deque of (role, text, timestamp)}
        # role: "user" or "assistant"
        self._conversation_history: dict[str, deque] = {}
        # _conversation_summaries: {group_id: summary_text}
        self._conversation_summaries: dict[str, str] = {}
        # _summary_checkpoint: {group_id: number of rounds already summarized}
        self._summary_checkpoint: dict[str, int] = {}

        # 输出去重缓存：防止 LLM 工具调用或重复响应导致同一内容被多次发送
        # _sent_content_cache: {group_id: deque of (fingerprint, timestamp)}
        self._sent_content_cache: dict[str, deque] = {}
        self._DEDUP_WINDOW = 30  # 去重时间窗口（秒）

        # 三大系统状态
        self._energy_states: dict[str, ChatEnergy] = {}
        self._flow_states: dict[str, ChatFlowState] = {}
        self._rescue_states: dict[str, ChatRescueState] = {}

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
            f"指令前缀跳过: {'启用' if self.command_prefix_enabled else '关闭'}(前缀='{self.command_prefix}') | "
            f"唤醒前缀: '{self.wake_command_prefix}' | "
            f"调试模式: {'启用' if self.debug_mode else '关闭'} | "
            f"群组覆盖: {len(self.group_overrides)} 个群 | "
            f"用户概率覆盖: {len(self.user_prob_overrides)} 个用户 | "
            f"分段: {'启用' if self.splitter_enabled else '关闭'}"
        )

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
            self._sent_content_cache[group_id] = deque()
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
            self._sent_content_cache[group_id] = deque()

        self._sent_content_cache[group_id].append((fingerprint, now))

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
                elif isinstance(comp, Plain):
                    text = getattr(comp, "text", "")
                    if text.startswith("Sticker:"):
                        sticker_emoji = True
                    elif text.strip():
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
                self._record_user_message(group_id, event.message_str or "")

    def _get_user_prob(self, sender_id: str) -> float:
        """获取用户概率乘数

        返回值范围 0.0~1.0：
        - 1.0 = 默认，不影响原始概率
        - 0.0 = 永不回复该用户
        - 中间值 = 作为最终概率的乘数
        """
        return self.user_prob_overrides.get(str(sender_id), 1.0)

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
        if event.message_obj and event.message_obj.message:
            for comp in event.message_obj.message:
                comp_type = type(comp).__name__
                if comp_type == "Image":
                    has_image = True
                    # 尝试获取图片 URL
                    image_url = getattr(comp, "url", None) or getattr(comp, "image_url", None) or getattr(comp, "file", None)
                    break

        if has_image and (not text or not text.strip()):
            # 纯图片消息：分配唯一编码，尝试从 message_str 提取框架生成的图片描述
            image_id = f"img_{uuid.uuid4().hex[:8]}"
            meta = meta or {}
            meta["image_id"] = image_id
            if image_url:
                meta["image_url"] = image_url

            # 框架在分发图片消息时，message_str 已包含 [Image: 描述内容] 格式
            image_desc = ""
            image_match = re.search(r'\[Image:\s*(.+?)\]', text) if text else None
            if image_match:
                image_desc = image_match.group(1).strip()

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
            # 尝试从 message_str 提取框架生成的图片描述
            image_desc = ""
            image_match = re.search(r'\[Image:\s*(.+?)\]', text) if text else None
            if image_match:
                image_desc = image_match.group(1).strip()
                meta["image_pending"] = False
                meta["image_description"] = image_desc
            else:
                meta["image_pending"] = True
                if image_url:
                    meta["image_url"] = image_url
                # 自定义模型模式：异步调用多模态模型识别图片
                if self.image_context_custom_model and image_url:
                    asyncio.ensure_future(self._describe_image_custom(group_id, sender, image_url, image_id, buffer))
            self._debug(f"图片记录(带文字) | 群={group_id} 发送者={sender} image_id={image_id} pending={meta.get('image_pending', False)}")

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
            # 过滤过短消息
            if self.context_truncation_enabled and len(text.strip()) < self.context_min_length:
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

            lines.append(f"[{display_name}]{relation_tag}: {text}")

        return "\n".join(lines), new_msg_count, old_msg_count

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
                "4. 摘要长度不超过原文的30%\n\n"
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
            # Pipeline 被取消（如新消息到达），压缩中断，返回原文
            logger.debug(f"[ContextCompress] 群={group_id} 压缩被取消（Pipeline中断），使用原文")
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
            logger.info(f"缓冲区清理: 已删除群 {group_id} 的过期缓冲区")

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
        """
        overrides = self.group_overrides.get(str(group_id), {})
        if param_name in overrides:
            return overrides[param_name]
        return default_value

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
        for msg_sender, msg_text, _ts, _evt in messages:
            # 跳过复读消息：如果该消息是复读（与缓冲区中其他用户的消息相同/相似），
            # 则不应因复读内容包含名称而触发唤醒
            if self.repeat_suppress_enabled and self._is_repeat_message(group_id, msg_sender, msg_text)[0]:
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
        for msg_sender, msg_text, _ts, _evt in messages:
            # 跳过复读消息：复读内容包含关键词时不应触发关键词唤醒
            if self.repeat_suppress_enabled and self._is_repeat_message(group_id, msg_sender, msg_text)[0]:
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

        # 2.5 复读抑制：检查所有暂存消息是否为复读
        # 防抖聚合了多条消息，复读可能出现在任意一条中，不能只检查最后一条
        has_repeat = False
        repeat_info = ""
        if self.repeat_suppress_enabled and messages:
            for sender, text, _ts, _evt in messages:
                is_repeat, match_info = self._is_repeat_message(group_id, sender, text)
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
        message_str = event.message_str or ""
        sender_name = event.get_sender_name()
        sender_id = str(getattr(event.message_obj.sender, "user_id", ""))

        # 1. 记录消息到缓冲区
        self._record_message(event)
        self._debug(f"收到群消息 | 群={group_id} 发送者={sender_name}({sender_id}) 内容='{message_str[:50]}'")

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

    # ─── LLM 请求钩子 ─────────────────────────────────────

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """在 LLM 请求前注入群聊上下文和自然唤醒提示"""
        if not event.get_extra("smart_wakeup_triggered"):
            return

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

        # 如果路由已完成（小模型已直接回复），跳过主模型响应的记录
        if event.get_extra("smart_wakeup_route_completed"):
            return

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

        # 级联升级：检查小模型回复质量，不足时升级到大模型
        if self.cascade_upgrade_enabled and event.get_extra("smart_wakeup_routed_model"):
            completion_text = ""
            if hasattr(resp, 'completion_text'):
                completion_text = resp.completion_text or ""
            elif hasattr(resp, 'result'):
                completion_text = str(resp.result) if resp.result else ""

            needs_upgrade = False
            upgrade_reason = ""
            if len(completion_text.strip()) < 10:
                needs_upgrade = True
                upgrade_reason = f"回复过短({len(completion_text.strip())}字符)"
            elif any(kw in completion_text for kw in ["我不知道", "不确定", "无法回答", "不太清楚"]):
                needs_upgrade = True
                upgrade_reason = "包含不确定关键词"

            if needs_upgrade:
                self._stats["routing_stats"]["cascade_upgrade_count"] += 1
                original_model = event.get_extra("smart_wakeup_original_model") or ""
                logger.info(
                    f"[ModelRoute] 级联升级: {event.get_extra('smart_wakeup_routed_model')} → {original_model} "
                    f"原因={upgrade_reason}"
                )
                event.set_extra("smart_wakeup_cascade_upgrade", True)
                event.set_extra("smart_wakeup_cascade_reason", upgrade_reason)

        # 定期检查异常（每10次调用检查一次，避免频繁计算）
        if self._stats["llm_call_count"] % 10 == 0:
            self._check_token_anomaly()

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

    def _record_user_message(self, group_id: str, text: str):
        """记录用户消息到对话历史"""
        history = self._get_conversation_history(group_id)
        history.append(("user", text.strip(), int(time.time())))

    def _record_assistant_message(self, group_id: str, text: str):
        """记录Bot回复到对话历史"""
        history = self._get_conversation_history(group_id)
        history.append(("assistant", text.strip(), int(time.time())))

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

        # 需要压缩的记录：除最近 recent_rounds_keep 轮外的所有记录
        records_to_summarize = list(history)[:-threshold]
        if not records_to_summarize:
            return

        # 构建待压缩的对话文本
        lines = []
        for role, text, ts in records_to_summarize:
            role_label = "用户" if role == "user" else "Bot"
            lines.append(f"{role_label}: {text}")
        conversation_text = "\n".join(lines)

        # 异步触发摘要（通过 asyncio.create_task）
        try:
            import asyncio
            asyncio.create_task(
                self._summarize_conversation(group_id, conversation_text)
            )
        except Exception as e:
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
                # 合并到已有摘要
                existing = self._conversation_summaries.get(group_id, "")
                if existing:
                    self._conversation_summaries[group_id] = f"{existing}\n\n---近期摘要---\n{summary}"
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
            logger.debug(f"[ConversationMemory] 群={group_id} 摘要被取消（Pipeline中断）")
        except Exception as e:
            logger.warning(f"[ConversationMemory] 群={group_id} 摘要异常: {e}")

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
            for role, text, ts in list(history):
                role_label = "用户" if role == "user" else "Bot"
                # 截断过长的单条消息
                if len(text) > 200:
                    text = text[:100] + "..."
                recent_lines.append(f"{role_label}: {text}")

            if recent_lines:
                parts.append(f"<近期对话>\n" + "\n".join(recent_lines) + "\n</近期对话>")

        if not parts:
            return ""

        return "<conversation_memory>\n" + "\n\n".join(parts) + "\n</conversation_memory>"

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """消息发送前拦截，过滤注入的上下文标签和思考标签，替换路由结果"""
        if not event.get_extra("smart_wakeup_triggered"):
            return

        # 如果小模型路由已完成，用小模型的回复替换主模型的输出
        route_result = event.get_extra("smart_wakeup_route_result")
        if route_result:
            result = event.get_result()
            if result and result.chain:
                # 替换主模型的输出为小模型的回复
                from astrbot.core.agent.message import Plain
                result.chain = [Plain(route_result)]
                logger.info(
                    f"[ModelRoute] 已用小模型回复替换主模型输出 "
                    f"回复长度={len(route_result)}"
                )
            # 清除标记，避免重复替换
            event.set_extra("smart_wakeup_route_result", None)

        # 记录Bot回复到对话历史
        if self.conversation_memory_enabled:
            group_id = event.message_obj.group_id
            if group_id:
                result = event.get_result()
                if result and result.chain:
                    response_text = ""
                    for comp in result.chain:
                        if hasattr(comp, "text") and comp.text:
                            response_text += comp.text
                    if response_text:
                        self._record_assistant_message(group_id, response_text)

        # 过滤上下文标签和思考标签
        result = event.get_result()
        if not result or not result.chain:
            return

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
                result.chain.clear()
                return
            self._record_sent_content(group_id, post_filter_text)

        # ─── 分段模块处理 ───
        # 仅对本插件主动触发的 LLM 回复做分段，其他插件的输出不应被分段
        if self.splitter_enabled and event.is_at_or_wake_command:
            await self._splitter_process(event)

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
            remaining = segments[sent_count:] if sent_count > 0 else segments
            last_seg = remaining[-1]
            result.chain.clear()
            result.chain.extend(last_seg)
            logger.warning(f"[分段] 发送被取消，已发送{sent_count}段")
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
            f"级联升级: {rs['cascade_upgrade_count']}次",
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
        # 模式2：只有闭合标签 </think>，说明前面的内容是思考/草稿，应移除
        # 如 "行吧，那你忙你的。</think>好，那你先忙你的。" → "好，那你先忙你的。"
        text = re.sub(r'^[\s\S]*?</think>\s*', '', text)
        text = text.strip()
        if text != original:
            self._stats["thinking_filtered"] += 1
            logger.info(f"思考标签过滤: 已移除思考内容（原文 {len(original)} 字 → 过滤后 {len(text)} 字）")
        return text

    @staticmethod
    def _filter_duplicate_response(text: str) -> str:
        """过滤 LLM 返回的重复回复

        某些模型（如 GLM）会在回复中生成两个版本，用 ``` 分隔。
        仅当 ``` 独占一行（前后为换行，且 ``` 后面不紧跟代码语言标识或颜文字标记）
        时才判定为版本分隔符，避免误切代码块和颜文字标记。
        """
        # 匹配 ``` 独占一行的情况：
        # - 前后有换行
        # - ``` 后面只有空白和换行（不是代码语言如 python，也不是颜文字如 (QAQ)）
        parts = re.split(r'\n```[ \t]*\n', text)
        if len(parts) > 1:
            # 取最后一个版本（模型的最终修订）
            filtered = parts[-1].strip()
            logger.info(f"重复回复过滤: 检测到 {len(parts)} 个版本，保留最终版本（原文 {len(text)} 字 → 过滤后 {len(filtered)} 字）")
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

    async def terminate(self):
        """插件卸载/停用时调用，清理所有内存数据"""
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
        self._conversation_history.clear()
        self._conversation_summaries.clear()
        self._summary_checkpoint.clear()
        self._sent_content_cache.clear()
        self._bot_user_ids.clear()
        self._last_context_ts.clear()
        logger.info(
            f"灵犀插件已卸载 | "
            f"已释放 {group_count} 个群缓冲区({buffer_count} 条消息), "
            f"{energy_count} 个精力状态, {flow_count} 个心流状态, {rescue_count} 个救场状态, "
            f"{debounce_count} 个防抖状态, {conv_count} 个对话历史, {summary_count} 个摘要"
        )
