"""新闻 API 数据源

从新闻 API（如 APITube、NewsData）获取资讯。
"""

import asyncio
import os
from datetime import datetime
from typing import List, Optional

# v1.8.0 修复（日志可见性）：优先使用 AstrBot loguru logger，回退到标准 logging
try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

try:
    import aiohttp
except ImportError:
    aiohttp = None
    logger.warning("[APIFetcher] aiohttp 未安装，API 数据源不可用")

from .base import BaseFetcher, NewsItem


class APIFetcher(BaseFetcher):
    """新闻 API 数据源

    支持 APITube、NewsData 等新闻 API。
    """

    # API 端点
    APITUBE_ENDPOINT = "https://api.apitube.io/v1/news/articles"
    NEWSDATA_ENDPOINT = "https://newsdata.io/api/1/news"

    # 类别映射到 API 关键词
    CATEGORY_KEYWORDS = {
        "科技资讯": "technology OR AI OR startup",
        "游戏八卦": "gaming OR video game OR esports",
        "沙雕新闻": "odd news OR weird news",
        "热点事件": "breaking news OR trending",
    }

    def __init__(self, config: dict):
        """初始化 API 数据源

        Args:
            config: 配置字典，包含：
                - fetcher_api_provider: API 提供商（apitube/newsdata）
                - fetcher_api_key: API Key（直接配置）
                - fetcher_api_key_env: API Key 环境变量名（可选）
        """
        if aiohttp is None:
            raise ImportError("aiohttp 未安装，请运行 pip install aiohttp")

        self.config = config
        self.provider = config.get("fetcher_api_provider", "")
        self.api_key = self._get_api_key()

        # v1.8.0 修复（B1 二次审查 m5）：复用 aiohttp.ClientSession
        # 避免每次请求新建 session（连接池无法复用，TCP 握手开销大）
        # lazy create：aiohttp.ClientSession 必须在 event loop 内创建，
        # 因此 __init__ 中仅占位为 None，首次 _get_session 时才真正创建
        self._session: Optional["aiohttp.ClientSession"] = None

        if not self.api_key:
            logger.warning("[APIFetcher] 未配置 API Key，API 数据源不可用")

        logger.info(f"[APIFetcher] 初始化完成，提供商: {self.provider}")

    async def _get_session(self) -> "aiohttp.ClientSession":
        """获取或创建复用的 aiohttp.ClientSession

        v1.8.0 修复（B1 二次审查 m5）：session 复用机制
        - 首次调用时创建 session 并缓存
        - 后续调用直接复用，避免重复 TCP 握手
        - session 关闭后（refresh 中）会自动重新创建
        """
        if self._session is None or self._session.closed:
            # 设置默认超时和连接池限制
            timeout = aiohttp.ClientTimeout(total=10)
            connector = aiohttp.TCPConnector(
                limit=10,  # 最大连接数
                limit_per_host=5,  # 单 host 最大连接数
                ttl_dns_cache=300,  # DNS 缓存 5 分钟
            )
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
            )
            logger.debug("[APIFetcher] 创建新的 aiohttp.ClientSession")
        return self._session

    def _get_api_key(self) -> str:
        """获取 API Key

        优先从环境变量读取，降级从配置直接读取。
        """
        env_name = self.config.get("fetcher_api_key_env", "")
        if env_name:
            key = os.environ.get(env_name, "")
            if key:
                logger.debug(f"[APIFetcher] 从环境变量 {env_name} 读取 API Key")
                return key

        return self.config.get("fetcher_api_key", "")

    async def fetch(self, category: str, max_items: int = 3) -> List[NewsItem]:
        """获取指定类别的资讯

        Args:
            category: 资讯类别
            max_items: 最大条数
        Returns:
            NewsItem 列表
        """
        if not self.api_key:
            return []

        keywords = self.CATEGORY_KEYWORDS.get(category, category)

        try:
            if self.provider == "apitube":
                items = await self._fetch_apitube(keywords, category, max_items)
            elif self.provider == "newsdata":
                items = await self._fetch_newsdata(keywords, category, max_items)
            else:
                logger.warning(f"[APIFetcher] 不支持的提供商: {self.provider}")
                return []

            logger.info(
                f"[APIFetcher] {self.provider} 获取 {len(items)} 条 (类别={category})"
            )
            return items
        except Exception as e:
            logger.warning(f"[APIFetcher] 获取失败: {e}")
            return []

    async def _fetch_apitube(
        self, keywords: str, category: str, max_items: int
    ) -> List[NewsItem]:
        """从 APITube 获取资讯"""
        params = {
            "api_key": self.api_key,
            "q": keywords,
            "limit": max_items,
            "lang": "en",
        }

        return await self._fetch_with_http(
            self.APITUBE_ENDPOINT, params, category, max_items, "apitube"
        )

    async def _fetch_newsdata(
        self, keywords: str, category: str, max_items: int
    ) -> List[NewsItem]:
        """从 NewsData 获取资讯"""
        params = {
            "apikey": self.api_key,
            "q": keywords,
            "size": max_items,
            "language": "en",
        }

        return await self._fetch_with_http(
            self.NEWSDATA_ENDPOINT, params, category, max_items, "newsdata"
        )

    async def _fetch_with_http(
        self,
        endpoint: str,
        params: dict,
        category: str,
        max_items: int,
        provider: str,
    ) -> List[NewsItem]:
        """通用 HTTP 请求

        v1.8.0 修复（B1 二次审查 m5）：复用 session，避免每次请求新建连接池
        """
        items = []

        try:
            # v1.8.0 修复（B1 二次审查 m5）：复用 session 替代 async with aiohttp.ClientSession()
            session = await self._get_session()
            async with session.get(
                endpoint, params=params
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"[APIFetcher] {provider} 返回状态码 {resp.status}"
                    )
                    return []

                data = await resp.json()
        except asyncio.TimeoutError:
            logger.warning(f"[APIFetcher] {provider} 请求超时")
            return []
        except Exception as e:
            # v1.8.0 修复（B1 审查问题 9）：脱敏异常日志，避免 API Key 通过 URL 泄露
            error_msg = str(e)
            if self.api_key and self.api_key in error_msg:
                error_msg = error_msg.replace(self.api_key, "[REDACTED_KEY]")
            logger.warning(f"[APIFetcher] {provider} 请求异常: {type(e).__name__}: {error_msg}")
            return []

        # 解析响应（APITube 和 NewsData 格式略不同）
        articles = data.get("articles", data.get("results", []))

        for article in articles[:max_items]:
            # 解析发布时间
            published_at = datetime.now()
            published_str = article.get("published_at") or article.get("pubDate", "")
            if published_str:
                try:
                    # ISO 格式：2026-07-17T12:00:00Z
                    published_at = datetime.fromisoformat(
                        published_str.replace("Z", "+00:00")
                    )
                except Exception:
                    pass

            items.append(
                NewsItem(
                    title=(article.get("title") or "").strip(),
                    summary=(
                        article.get("description") or article.get("content") or ""
                    )[:200],
                    url=article.get("url") or article.get("link") or "",
                    source=article.get("source_name")
                    or article.get("source_id")
                    or provider,
                    published_at=published_at,
                    category=category,
                    fetched_at=datetime.now(),
                )
            )

        return items

    async def refresh(self) -> None:
        """刷新资讯池（API 模式下无需预刷新，实时获取）

        v1.8.0 修复（B1 二次审查 m5）：同时关闭复用的 session，
        下次 _get_session 时会重新创建。定期调用 refresh 可以释放闲置连接。
        """
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None
            logger.debug("[APIFetcher] 已关闭复用的 aiohttp.ClientSession")
