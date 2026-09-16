"""资讯缓存与池子模块

提供两种机制：
1. NewsCache：TTL 缓存（v1.8.0 原有，单次调用结果缓存）
2. NewsPool：资讯池子（v1.8.1 新增，跨群共享 + per-group 去重）

v1.8.1 重构说明（P0 LLM 编造新闻问题修复）：
- 原 NewsCache 仅缓存 max_items 条（默认 3 条），且 mark_sent 时清除缓存，
  导致群 1 用掉后群 2 拿不到资讯（sent_titles 全局集合跨群去重根因）。
- 新增 NewsPool 类：拉取 pool_size 条（默认 15）填充池子，
  跨群共享池子，但每个群独立记录已发送标题，从池子中取该群未发送过的。
- 池子 TTL 默认 6 小时（21600 秒），过期后重新拉取 RSS/API。
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

# v1.8.0 修复（日志可见性）：优先使用 AstrBot loguru logger，回退到标准 logging
try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)


class NewsCache:
    """资讯缓存（TTL 机制）

    缓存按类别存储，超过 TTL 后自动失效。
    v1.8.1 起主要供 API 数据源使用，RSS 池子逻辑由 NewsPool 接管。
    """

    def __init__(self, ttl_seconds: int = 1800):
        """初始化缓存

        Args:
            ttl_seconds: 缓存有效期（秒），默认 1800（30 分钟）
        """
        self.ttl = timedelta(seconds=ttl_seconds)
        self._cache: dict[str, tuple[list, datetime]] = {}

    def get(self, category: str) -> Optional[list]:
        """获取缓存

        Args:
            category: 资讯类别
        Returns:
            缓存的 NewsItem 列表，未命中或已过期返回 None
        """
        if category in self._cache:
            items, ts = self._cache[category]
            if datetime.now() - ts < self.ttl:
                logger.debug(f"[NewsCache] 缓存命中: {category} ({len(items)} 条)")
                return items
            # 过期，删除
            logger.debug(f"[NewsCache] 缓存过期: {category}")
            del self._cache[category]
        return None

    def set(self, category: str, items: list) -> None:
        """写入缓存

        Args:
            category: 资讯类别
            items: NewsItem 列表
        """
        self._cache[category] = (items, datetime.now())
        logger.debug(f"[NewsCache] 写入缓存: {category} ({len(items)} 条)")

    def clear(self) -> None:
        """清空所有缓存"""
        self._cache.clear()
        logger.debug("[NewsCache] 已清空所有缓存")

    def remove(self, category: str) -> None:
        """删除指定类别的缓存"""
        self._cache.pop(category, None)


class NewsPool:
    """资讯池子（v1.8.1 新增）

    跨群共享的资讯池子，per-group 去重。

    核心机制：
    1. 拉取阶段：RSSFetcher 拉取 pool_size 条填充池子（按类别存储）
    2. 取用阶段：每个群从池子中取 max_items 条该群未发送过的
    3. 标记阶段：群发送成功后调用 mark_sent(group_id, title) 标记该群已发送
    4. 过期阶段：池子 TTL 到期后重新拉取（默认 6 小时）

    与 v1.8.0 的关键区别：
    - sent_titles 从全局 set 改为 per-group dict[str, set]
    - mark_sent 不再清除缓存（池子要跨群复用）
    - 拉取 pool_size 条而非 max_items 条
    """

    def __init__(self, ttl_seconds: int = 21600, pool_size: int = 15):
        """初始化资讯池子

        Args:
            ttl_seconds: 池子有效期（秒），默认 21600（6 小时）
            pool_size: 池子大小，默认 15 条
        """
        self.ttl = timedelta(seconds=ttl_seconds)
        self.pool_size = pool_size
        # 池子按类别存储：{category: (items, fetched_at)}
        self._pool: Dict[str, tuple[list, datetime]] = {}
        # per-group 已发送标题：{group_id: set(titles)}
        # v1.8.1 修复 P0 根因 1：原为全局 set 导致跨群去重
        self._sent_per_group: Dict[str, Set[str]] = {}
        # 限制每个群的 sent 集合大小，防止内存泄漏
        self._sent_max_per_group = 500

    def get_pool(self, category: str) -> Optional[List]:
        """获取池子内容（不过滤已发送）

        Args:
            category: 资讯类别
        Returns:
            池子中的 NewsItem 列表，未命中或已过期返回 None
        """
        if category in self._pool:
            items, fetched_at = self._pool[category]
            if datetime.now() - fetched_at < self.ttl:
                logger.debug(
                    f"[NewsPool] 池子命中: {category} ({len(items)} 条, "
                    f"age={int((datetime.now() - fetched_at).total_seconds())}s)"
                )
                return items
            # 过期，删除
            logger.debug(f"[NewsPool] 池子过期: {category}")
            del self._pool[category]
        return None

    def set_pool(self, category: str, items: list) -> None:
        """填充池子

        Args:
            category: 资讯类别
            items: NewsItem 列表（通常为 pool_size 条）
        """
        self._pool[category] = (items, datetime.now())
        logger.info(
            f"[NewsPool] 填充池子: {category} ({len(items)} 条, "
            f"TTL={int(self.ttl.total_seconds())}s)"
        )

    def get_unsent_for_group(self, category: str, group_id: str, max_items: int = 3) -> List:
        """从池子中取出该群未发送过的资讯

        v1.8.1 核心方法：per-group 过滤，跨群共享池子。

        Args:
            category: 资讯类别
            group_id: 目标群 ID
            max_items: 最大返回条数
        Returns:
            该群未发送过的 NewsItem 列表，空列表表示池子空或全部已发送
        """
        pool_items = self.get_pool(category)
        if not pool_items:
            return []

        sent_titles = self._sent_per_group.get(group_id, set())
        if not sent_titles:
            return pool_items[:max_items]

        # per-group 过滤
        unsent = [item for item in pool_items if item.title not in sent_titles]
        before_count = len(pool_items)
        after_count = len(unsent)
        if before_count != after_count:
            logger.info(
                f"[NewsPool] per-group 过滤: 群={group_id} 类别={category} "
                f"{before_count} → {after_count} (该群已发送 {before_count - after_count} 条)"
            )
        return unsent[:max_items]

    def mark_sent(self, group_id: str, title: str, category: str = "") -> None:
        """标记一条资讯为某群已发送

        v1.8.1 修复 P0 根因 1：从全局 set 改为 per-group set。
        v1.8.1 修复 P0 根因 4：不再清除池子缓存（池子要跨群复用）。

        Args:
            group_id: 目标群 ID
            title: 资讯标题
            category: 资讯类别（可选，保留参数为向后兼容，但不再用于清除缓存）
        """
        if not title or not group_id:
            return

        if group_id not in self._sent_per_group:
            self._sent_per_group[group_id] = set()

        sent_set = self._sent_per_group[group_id]
        sent_set.add(title)

        # 限制大小防止内存泄漏
        if len(sent_set) > self._sent_max_per_group:
            logger.debug(
                f"[NewsPool] 群={group_id} sent 集合超过 {self._sent_max_per_group} 条，清空重置"
            )
            sent_set.clear()
            sent_set.add(title)

        # v1.8.1 关键变更：不再清除池子缓存（与 v1.8.0 行为不同）
        # 池子要跨群复用，清除缓存会导致其他群拿不到资讯

    def get_sent_count(self, group_id: str) -> int:
        """获取某群已发送的资讯数量（诊断用）"""
        return len(self._sent_per_group.get(group_id, set()))

    def clear_all_sent(self, group_id: str = None) -> None:
        """清除已发送记录

        Args:
            group_id: 指定群 ID 清除，None 则清除所有群
        """
        if group_id:
            self._sent_per_group.pop(group_id, None)
            logger.debug(f"[NewsPool] 清除群={group_id} 的 sent 记录")
        else:
            self._sent_per_group.clear()
            logger.debug("[NewsPool] 清除所有群的 sent 记录")

    def clear_pool(self, category: str = None) -> None:
        """清除池子

        Args:
            category: 指定类别清除，None 则清除所有
        """
        if category:
            self._pool.pop(category, None)
            logger.debug(f"[NewsPool] 清除池子: {category}")
        else:
            self._pool.clear()
            logger.debug("[NewsPool] 清除所有池子")

    def get_stats(self) -> dict:
        """获取池子统计信息（诊断用）"""
        return {
            "pool_categories": list(self._pool.keys()),
            "pool_total_items": sum(len(items) for items, _ in self._pool.values()),
            "groups_tracked": len(self._sent_per_group),
            "sent_per_group": {gid: len(s) for gid, s in self._sent_per_group.items()},
        }
