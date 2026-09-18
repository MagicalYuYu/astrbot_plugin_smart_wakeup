"""Fetcher 模块入口

提供 FetcherManager 统一管理多个数据源。

v1.8.1 重构（P0 LLM 编造新闻问题修复）：
- fetch 增加 group_id 参数，支持 per-group 去重
- mark_sent 增加 group_id 参数，且不再清除缓存（池子要跨群复用）
- 使用 NewsPool 替代原 sent_titles 全局集合 + NewsCache 缓存
- fetch 时先检查池子，池子命中则 per-group 过滤返回未发送过的
- 池子未命中或过期时从 RSSFetcher 拉取 pool_size 条填充池子
"""

# 日志必须且只能从 astrbot.api 导入（插件市场合规要求，v2.0.1 移除标准 logging 回退）
from astrbot.api import logger

from typing import List

from .base import BaseFetcher, NewsItem
from .cache import NewsCache, NewsPool
from .filter import NewsFilter


class FetcherManager:
    """Fetcher 模块管理器

    统一调度多个数据源（RSS、API），提供池子和过滤。

    v1.8.1 架构变更：
    - 引入 NewsPool 替代原 sent_titles + NewsCache 组合
    - pool_size 条资讯填充池子，跨群共享
    - per-group 去重，每个群独立记录已发送标题
    - mark_sent 不再清除池子（池子跨群复用）
    """

    def __init__(self, config: dict):
        """初始化 Fetcher 管理器

        Args:
            config: 配置字典，包含：
                - fetcher_enabled: 是否启用
                - fetcher_rss_feeds: RSS 源配置
                - fetcher_api_provider: API 提供商
                - fetcher_api_key: API Key
                - fetcher_api_key_env: API Key 环境变量名
                - fetcher_cache_ttl: 池子 TTL（v1.8.1 默认 21600 秒 = 6 小时）
                - fetcher_pool_size: 池子大小（v1.8.1 默认 15 条）
        """
        self.config = config
        self.enabled = config.get("fetcher_enabled", False)
        self.fetchers: List[BaseFetcher] = []

        # v1.8.1 资讯池子（核心重构：替代原 NewsCache + sent_titles 组合）
        cache_ttl = config.get("fetcher_cache_ttl", 21600)
        pool_size = config.get("fetcher_pool_size", 15)
        self.pool = NewsPool(ttl_seconds=cache_ttl, pool_size=pool_size)

        # v1.8.1 保留 NewsCache 供 API 数据源使用（API 数据源可能不需要池子模式）
        self.cache = NewsCache(ttl_seconds=cache_ttl)
        self.filter = NewsFilter()

        # v1.8.1 兼容性：保留 sent_titles 属性供旧代码诊断日志访问
        # 实际逻辑已迁移到 NewsPool._sent_per_group
        # 通过 property 动态返回所有群的已发送总数
        # 注意：此属性为只读，直接修改不会影响 NewsPool 内部状态

        if self.enabled:
            self._init_fetchers()
            logger.info(
                f"[FetcherManager] 初始化完成，{len(self.fetchers)} 个数据源，"
                f"池子大小={pool_size}, TTL={cache_ttl}s"
            )
        else:
            logger.info("[FetcherManager] 未启用 Fetcher 模块")

    @property
    def sent_titles(self) -> set:
        """v1.8.1 兼容性属性：返回所有群的已发送标题合并集合

        仅供诊断日志读取（如 main.py 中的 FetcherManager 诊断日志）。
        修改此返回值不会影响 NewsPool 内部状态。
        """
        merged = set()
        for sent_set in self.pool._sent_per_group.values():
            merged.update(sent_set)
        return merged

    def _init_fetchers(self):
        """根据配置初始化数据源"""
        # RSS 数据源（主）
        # v1.8.0 修复（B1 二次审查 m2）：允许通过 fetcher_rss_enabled 关闭 RSS，
        # 适用于只想用 API 数据源的场景（默认开启，向后兼容）
        rss_enabled = self.config.get("fetcher_rss_enabled", True)
        if rss_enabled:
            try:
                from .rss_fetcher import RSSFetcher

                self.fetchers.append(RSSFetcher(self.config))
                logger.debug("[FetcherManager] RSS 数据源已加载")
            except ImportError as e:
                logger.warning(f"[FetcherManager] RSS 数据源加载失败: {e}")
            except Exception as e:
                logger.warning(f"[FetcherManager] RSS 数据源初始化失败: {e}")

        # API 数据源（辅）
        api_provider = self.config.get("fetcher_api_provider", "")
        if api_provider:
            try:
                from .api_fetcher import APIFetcher

                self.fetchers.append(APIFetcher(self.config))
                logger.debug(
                    f"[FetcherManager] API 数据源已加载 ({api_provider})"
                )
            except ImportError as e:
                logger.warning(f"[FetcherManager] API 数据源加载失败: {e}")
            except Exception as e:
                logger.warning(f"[FetcherManager] API 数据源初始化失败: {e}")

    async def close(self):
        """v1.9.7 新增 M3：关闭所有 fetcher 的持久资源（如 aiohttp session）

        应在插件 terminate 或 FetcherManager 销毁时调用。
        """
        for fetcher in self.fetchers:
            if hasattr(fetcher, 'close'):
                try:
                    await fetcher.close()
                except Exception as e:
                    logger.warning(f"[FetcherManager] 关闭 fetcher 失败: {e}")

    async def fetch(self, category: str, group_id: str, max_items: int = 3) -> List[NewsItem]:
        """获取指定类别的资讯（v1.8.1 重构）

        v1.8.1 核心变更：
        - 增加 group_id 参数，支持 per-group 去重
        - 优先从池子取该群未发送过的资讯
        - 池子未命中或过期时从数据源拉取 pool_size 条填充池子
        - 不再使用全局 sent_titles 过滤

        流程：
        1. 检查池子是否命中（该类别是否已拉取过且未过期）
        2. 池子命中：per-group 过滤后返回 max_items 条
        3. 池子未命中：从数据源拉取 pool_size 条，填充池子，再 per-group 过滤返回

        Args:
            category: 资讯类别（科技资讯/游戏八卦/沙雕新闻/热点事件）
            group_id: 目标群 ID（v1.8.1 新增，用于 per-group 去重）
            max_items: 最大返回条数
        Returns:
            NewsItem 列表，空列表表示无可用资讯
        """
        if not self.enabled or not self.fetchers:
            logger.info(
                f"[FetcherManager] fetch 提前返回空: "
                f"enabled={self.enabled}, fetchers={len(self.fetchers)} (类别={category})"
            )
            return []

        # v1.8.1 诊断日志：记录 fetch 调用
        pool_stats = self.pool.get_stats()
        logger.info(
            f"[FetcherManager] fetch 开始: category={category}, group_id={group_id}, "
            f"max_items={max_items}, fetchers={len(self.fetchers)}, "
            f"pool_categories={pool_stats['pool_categories']}, "
            f"pool_total_items={pool_stats['pool_total_items']}, "
            f"group_sent={pool_stats['sent_per_group'].get(group_id, 0)}"
        )

        # v1.8.1 核心：优先从池子取该群未发送过的
        unsent = self.pool.get_unsent_for_group(category, group_id, max_items)
        if unsent:
            logger.info(
                f"[FetcherManager] 池子命中+per-group 过滤: 返回 {len(unsent)} 条 "
                f"(类别={category}, 群={group_id})"
            )
            return unsent

        # v1.8.1 修复 B1 审查 Major 2：区分"池子未命中"与"池子命中但该群已用尽"两种场景
        # - 池子未命中（TTL 过期/从未拉取）：从数据源拉取并覆盖池子
        # - 池子命中但该群已用尽：不覆盖池子（保护其他群未发送的资讯），直接返回空触发上层 SKIP
        # 原行为缺陷：群 1 用尽池子后重新拉取会覆盖池子，导致群 2 未发送的旧资讯丢失
        pool_exists = self.pool.get_pool(category) is not None
        if pool_exists:
            logger.info(
                f"[FetcherManager] 池子命中但群={group_id} 已用尽该类别={category} 的可用资讯，"
                f"不覆盖池子（保护其他群未发送资讯），返回空触发 SKIP"
            )
            return []

        # 池子未命中（TTL 过期或从未拉取）：从数据源拉取 pool_size 条填充池子
        logger.info(
            f"[FetcherManager] 池子未命中（TTL 过期/首次拉取），从数据源拉取 "
            f"(类别={category}, pool_size={self.pool.pool_size})"
        )

        # 2. 从数据源获取（拉取 pool_size 条填充池子）
        pool_size = self.pool.pool_size
        all_items = []
        for fetcher in self.fetchers:
            try:
                logger.info(
                    f"[FetcherManager] 调用 {fetcher.__class__.__name__}.fetch("
                    f"category={category}, max_items={pool_size})"
                )
                items = await fetcher.fetch(category, max_items=pool_size)
                all_items.extend(items)
                logger.info(
                    f"[FetcherManager] {fetcher.__class__.__name__} 返回 {len(items)} 条"
                )
            except Exception as e:
                logger.warning(
                    f"[FetcherManager] {fetcher.__class__.__name__} 获取失败: {e}"
                )

        logger.info(
            f"[FetcherManager] 数据源获取完成: 共 {len(all_items)} 条 (类别={category})"
        )

        # 3. 过滤（去重低质量内容等）
        filtered = self.filter.filter(all_items, category)

        logger.info(
            f"[FetcherManager] 过滤完成: {len(all_items)} → {len(filtered)} (类别={category})"
        )

        # 4. 填充池子
        # v1.8.1 修复 B1 审查 Major 1：注释与代码行为一致
        # - 非空结果：写入池子（带 TTL 6 小时）
        # - 空结果：不写入池子，下次 fetch 会重新拉取（依赖主动发言冷却控制频率，默认 30 分钟）
        #   不做"负缓存"以避免复杂度增加，当前 RSS 源较稳定
        if filtered:
            self.pool.set_pool(category, filtered)
        else:
            logger.warning(
                f"[FetcherManager] 数据源返回空，池子未填充 (类别={category})，"
                f"下次 fetch 将重新拉取"
            )
            return []

        # 5. per-group 过滤后返回 max_items 条
        result = self.pool.get_unsent_for_group(category, group_id, max_items)
        logger.info(
            f"[FetcherManager] fetch 完成: 返回 {len(result)} 条 (类别={category}, 群={group_id})"
        )
        return result

    def mark_sent(self, group_id: str, title: str, category: str = "") -> None:
        """标记一条资讯为某群已发送（v1.8.1 重构）

        v1.8.1 核心变更：
        - 增加 group_id 参数，per-group 标记
        - 不再清除池子缓存（池子要跨群复用）
        - 委托给 NewsPool.mark_sent

        Args:
            group_id: 目标群 ID
            title: 资讯标题
            category: 资讯类别（v1.8.1 保留参数但不再用于清除缓存）
        """
        self.pool.mark_sent(group_id, title, category)

    def seed_sent(self, group_id: str, titles: set) -> None:
        """将持久化的已发送标题种子进池子（v2.0.3，防跨天重复推送）

        委托给 NewsPool.seed_sent；由插件启动时调用。
        """
        self.pool.seed_sent(group_id, titles)

    def clear_cache(self) -> None:
        """清空所有缓存（v1.8.1：同时清空 NewsCache 和 NewsPool）"""
        self.cache.clear()
        self.pool.clear_pool()

    async def refresh(self) -> None:
        """刷新所有数据源"""
        for fetcher in self.fetchers:
            try:
                await fetcher.refresh()
            except Exception as e:
                logger.warning(
                    f"[FetcherManager] {fetcher.__class__.__name__} 刷新失败: {e}"
                )


__all__ = ["FetcherManager", "BaseFetcher", "NewsItem"]
