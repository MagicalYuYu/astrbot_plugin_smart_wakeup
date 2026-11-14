"""资讯过滤模块

提供去重、敏感词过滤、有效性检查。
"""

import re
from typing import List

# 日志必须且只能从 astrbot.api 导入（插件市场合规要求，v2.0.1 移除标准 logging 回退）
from astrbot.api import logger


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

            # v2.4.9 新增：图片板类空条目拦截（煎蛋无聊图类源的典型输出）
            # 特征：标题以冒号结尾、冒号前是短作者名（如「杯面骑士:」「HeHeDa:」），
            # 且摘要剥离 HTML 后无文字（纯图）。此类条目无任何可转述事实，
            # LLM 只能编造「有新消息但不说是什么」的空话。默认源已下线煎蛋，
            # 此处为通用防线：用户自定义 RSS 填入同类图片板源时不再复发。
            title_stripped = item.title.rstrip()
            title_core = title_stripped.rstrip("：: ")
            summary_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", item.summary or "")).strip()
            if (
                len(title_core) < len(title_stripped)
                and re.fullmatch(r"[\w\u4e00-\u9fff·\- ]{1,12}", title_core)
                and len(summary_text) < 10
            ):
                logger.debug(f"[NewsFilter] 图片板空条目（仅作者名无正文）: {item.title}")
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
