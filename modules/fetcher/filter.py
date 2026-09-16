"""资讯过滤模块

提供去重、敏感词过滤、有效性检查。
"""

from typing import List

# v1.8.0 修复（日志可见性）：优先使用 AstrBot loguru logger，回退到标准 logging
try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)


class NewsFilter:
    """资讯过滤器

    提供去重、敏感词过滤、有效性检查。
    """

    # 默认敏感词列表（可配置）
    DEFAULT_SENSITIVE_KEYWORDS = [
        "政治敏感",
        "色情",
        "暴力",
        "赌博",
        "毒品",
        " suicide ",
        " kill yourself",
    ]

    def __init__(self, sensitive_keywords: List[str] = None):
        """初始化过滤器

        Args:
            sensitive_keywords: 自定义敏感词列表，None 则使用默认
        """
        self.sensitive_keywords = sensitive_keywords or self.DEFAULT_SENSITIVE_KEYWORDS

    def filter(self, items: list, category: str = "") -> list:
        """过滤资讯

        Args:
            items: NewsItem 列表
            category: 资讯类别（用于日志）
        Returns:
            过滤后的 NewsItem 列表
        """
        if not items:
            return []

        filtered = []
        seen_titles = set()
        seen_urls = set()

        for item in items:
            # 去重：标题
            if item.title in seen_titles:
                logger.debug(f"[NewsFilter] 去重（标题重复）: {item.title[:30]}")
                continue
            seen_titles.add(item.title)

            # 去重：URL
            if item.url and item.url in seen_urls:
                logger.debug(f"[NewsFilter] 去重（URL 重复）: {item.url}")
                continue
            if item.url:
                seen_urls.add(item.url)

            # 敏感词过滤
            if self._contains_sensitive(item):
                logger.warning(f"[NewsFilter] 敏感词过滤: {item.title[:30]}")
                continue

            # 有效性检查
            if not item.title or len(item.title) < 5:
                logger.debug(f"[NewsFilter] 标题过短: {item.title}")
                continue

            if not item.summary:
                item.summary = "（无摘要）"

            filtered.append(item)

        logger.info(f"[NewsFilter] 过滤完成: {len(items)} → {len(filtered)} (类别={category})")
        return filtered

    def _contains_sensitive(self, item) -> bool:
        """检查是否包含敏感词"""
        text = (item.title + " " + item.summary).lower()
        for kw in self.sensitive_keywords:
            if kw.lower() in text:
                return True
        return False
