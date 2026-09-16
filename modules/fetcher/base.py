"""Fetcher 模块基础数据结构

定义 NewsItem 数据类和 BaseFetcher 抽象基类。
所有数据源（RSS、API）都继承 BaseFetcher。
"""

from dataclasses import dataclass, field
from datetime import datetime
from abc import ABC, abstractmethod
from typing import List


@dataclass
class NewsItem:
    """资讯条目数据类

    表示一条从外部数据源获取的资讯。
    """

    title: str  # 标题
    summary: str  # 摘要（RSS description / API description）
    url: str  # 原文链接
    source: str  # 来源（如 The Verge、HackerNews）
    published_at: datetime  # 发布时间
    category: str  # 类别（科技资讯/游戏八卦/沙雕新闻/热点事件）
    fetched_at: datetime = field(default_factory=datetime.now)  # 抓取时间

    def __str__(self) -> str:
        return f"[{self.source}] {self.title}"


class BaseFetcher(ABC):
    """Fetcher 抽象基类

    所有数据源（RSS、API）都继承此类，实现 fetch 和 refresh 方法。
    """

    @abstractmethod
    async def fetch(self, category: str, max_items: int = 3) -> List[NewsItem]:
        """获取指定类别的资讯

        Args:
            category: 资讯类别（科技资讯/游戏八卦/沙雕新闻/热点事件）
            max_items: 最大条数
        Returns:
            NewsItem 列表，空列表表示无可用资讯
        """
        pass

    @abstractmethod
    async def refresh(self) -> None:
        """刷新资讯池（定时调用）"""
        pass
