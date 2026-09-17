"""RSS 数据源

从 RSS feeds 抓取资讯。

v1.8.2 改进（基于实测反馈）：
- 不再使用 feedparser.parse(url) 的内置 HTTP 请求（在 AstrBot 进程内会卡死）
- 改用 aiohttp 异步拉取 RSS 内容（字符串），再传给 feedparser.parse(content) 解析
- 这样完全控制超时、UA、不依赖线程池，避免 AstrBot 事件循环干扰
"""

import asyncio
from datetime import datetime
from typing import Dict, List

# 日志必须且只能从 astrbot.api 导入（插件市场合规要求，v2.0.1 移除标准 logging 回退）
from astrbot.api import logger

try:
    import feedparser
except ImportError:
    feedparser = None
    logger.warning("[RSSFetcher] feedparser 未安装，RSS 数据源不可用")

# v1.8.2：使用 aiohttp 异步拉取（插件已依赖 aiohttp）
try:
    import aiohttp
except ImportError:
    aiohttp = None
    logger.warning("[RSSFetcher] aiohttp 未安装，v1.8.2 异步拉取机制不可用，将回退到 feedparser.parse(url)")

from .base import BaseFetcher, NewsItem


# v1.8.2：浏览器 User-Agent，避免被站点限流
_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _get_windows_proxy() -> str:
    """获取系统代理设置（v1.8.2 改进版）

    v1.8.2 修复（基于实测反馈）：
    - 服务器配置了 Clash 代理（127.0.0.1:7897），DNS 被劫持到 Facebook IP
    - urllib/feedparser 自动读 Windows 代理，能正常工作
    - aiohttp 默认不读 Windows 代理，直连 Facebook IP 必然失败
    - 本函数尝试多种方式获取代理，供 aiohttp 使用

    v1.8.2 深度修复（基于二次实测反馈）：
    - 在 AstrBot 进程内直接读 winreg 返回 None（原因未明，可能权限/用户模拟）
    - 新增 urllib.request.getproxies() 作为优先获取方式
    - urllib 在 AstrBot 进程内能正常获取代理（因为科技资讯 feedparser.parse(url) 成功过）

    Returns:
        代理字符串（如 "http://127.0.0.1:7897"），无代理时返回 None
    """
    # 方式 1：urllib.request.getproxies()（Python 标准库自动读取系统代理）
    # 这个方式最可靠，因为它和 urllib/feedparser 用同一套机制
    try:
        import urllib.request
        proxies = urllib.request.getproxies()
        if proxies:
            # 优先 https 代理，因为大部分 RSS 源是 https
            for key in ["https", "http"]:
                if key in proxies:
                    proxy_url = proxies[key]
                    # urllib 返回的格式已经是 "http://127.0.0.1:7897"
                    if not proxy_url.startswith("http"):
                        proxy_url = f"http://{proxy_url}"
                    return proxy_url
            # 退而求其次，取第一个
            first_proxy = next(iter(proxies.values()))
            if not first_proxy.startswith("http"):
                first_proxy = f"http://{first_proxy}"
            return first_proxy
    except Exception:
        pass

    # 方式 2：从环境变量获取（部分程序通过环境变量配置代理）
    for key in ["HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy",
                "ALL_PROXY", "all_proxy"]:
        val = None
        try:
            import os
            val = os.environ.get(key)
        except Exception:
            pass
        if val:
            if not val.startswith("http"):
                val = f"http://{val}"
            return val

    # 方式 3：直接读 Windows 注册表（HKCU）
    # 在独立进程中有效，但 AstrBot 进程内可能返回 None（原因未明）
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        )
        try:
            proxy_enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not proxy_enable:
                winreg.CloseKey(key)
                return None
            proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
            winreg.CloseKey(key)
            # ProxyServer 格式可能是 "127.0.0.1:7897" 或 "http=127.0.0.1:7897;https=127.0.0.1:7897"
            if "=" in proxy_server:
                for part in proxy_server.split(";"):
                    if part.startswith("https="):
                        return f"http://{part[6:]}"
                    elif part.startswith("http="):
                        return f"http://{part[5:]}"
                first = proxy_server.split(";")[0]
                if "=" in first:
                    first = first.split("=", 1)[1]
                return f"http://{first}"
            else:
                return f"http://{proxy_server}"
        except FileNotFoundError:
            winreg.CloseKey(key)
            return None
    except ImportError:
        pass
    except Exception:
        pass

    return None


class RSSFetcher(BaseFetcher):
    """RSS 数据源

    从预定义或自定义的 RSS feeds 抓取资讯。
    """

    # 预定义 RSS 源（按类别）
    # v1.9.5 更新（2026-07-31）：全部替换为国内可直连 RSS 源
    #   原因：服务器无代理运行，国际 RSS 源（Verge/TechCrunch/Onion 等）DNS 被劫持无法访问
    #   导致 4/9 主动发言话题类别频繁 SKIP → 连续 3 次 SKIP 触发退却 → 概率唤醒被阻塞
    #   验证方式：Invoke-WebRequest -NoProxy 模拟无代理环境，全部 HTTP 200 + RSS 格式有效
    #   注：rsshub.rssforever.com 和 plink.anyfeeder.com 为国内可直连的 RSS 聚合镜像
    DEFAULT_FEEDS: Dict[str, List[str]] = {
        "科技资讯": [
            # 原生 RSS，响应快（< 1s），条目多
            "https://www.ithome.com/rss/",          # IT之家 - 60条目，0.26s
            "https://36kr.com/feed",                 # 36氪 - 30条目，0.47s，创业科技
            "https://sspai.com/feed",                # 少数派 - 10条目，0.30s，数字生活
            "https://www.ifanr.com/feed",            # 爱范儿 - 20条目，0.57s，科技媒体
            "https://rss.huxiu.com/",                # 虎嗅 - 58条目，0.31s，商业科技
        ],
        "游戏八卦": [
            # 原生 RSS + RSSHub 镜像
            "https://www.yystv.cn/rss/feed",         # 游研社 - 12条目，0.32s，游戏文化
            "https://www.gcores.com/rss",            # 机核 - 20条目，0.29s，游戏综合
            "http://www.chuapp.com/feed",            # 触乐 - 30条目，3.43s，游戏行业
            "https://rsshub.rssforever.com/3dmgame/news",  # 3DM - 20条目，4.69s (RSSHub镜像)
        ],
        "沙雕新闻": [
            # 国内可直连的荒诞/奇趣新闻源极度稀缺
            # 主源：煎蛋热榜（奇趣图片+短文）
            "https://rsshub.rssforever.com/jandan/top",  # 煎蛋热榜 - 30条目，4.08s (RSSHub镜像)
            # v1.9.6 备份源：小众软件（趣味软件推荐，非完美匹配但避免单点故障）
            # 长期方案：服务器自建 RSSHub 实例（Docker）可解锁糗事百科/抽屉等更多路由
            "https://www.appinn.com/feed/",  # 小众软件 - 10条目，0.39s，趣味软件
        ],
        "热点事件": [
            # plink.anyfeeder.com 响应快（< 1.5s），rsshub.rssforever.com 稍慢
            "https://plink.anyfeeder.com/thepaper",          # 澎湃新闻 - 18条目，1.15s
            "https://plink.anyfeeder.com/zhihu/daily",       # 知乎日报 - 30条目，0.76s
            "https://rsshub.rssforever.com/zhihu/hot",       # 知乎热榜 - 30条目，0.97s (RSSHub镜像)
            "https://plink.anyfeeder.com/zaobao/realtime/china",  # 联合早报中国 - 24条目，0.29s
        ],
    }

    def __init__(self, config: dict):
        """初始化 RSS 数据源

        Args:
            config: 配置字典，支持 fetcher_rss_feeds（自定义 RSS 源）
        """
        if feedparser is None:
            raise ImportError("feedparser 未安装，请运行 pip install feedparser")

        self.config = config
        # 自定义 RSS 源（覆盖默认源）
        custom_feeds_str = config.get("fetcher_rss_feeds", "")
        if custom_feeds_str:
            self.feeds = self._parse_custom_feeds(custom_feeds_str)
            self._is_default_feeds = False
        else:
            self.feeds = self.DEFAULT_FEEDS.copy()
            self._is_default_feeds = True

        # v1.8.3 新增：显式配置代理（解决 NSSM 服务 Session 0 无法读取用户代理的问题）
        # 优先级：fetcher_proxy 配置项 > _get_windows_proxy() 探测
        # 实测：AstrBot 作为 NSSM 服务运行在 SYSTEM 账户，winreg.HKEY_CURRENT_USER
        # 指向 SYSTEM 而非当前用户，导致 winreg/urllib.getproxies 均返回空
        # v1.9.6 修正：默认国内源不使用代理，避免国内 RSS 源走代理绕路或被识别为异常流量
        self._explicit_proxy = (config.get("fetcher_proxy") or "").strip() or None
        if self._is_default_feeds:
            logger.info(f"[RSSFetcher] 初始化完成，{len(self.feeds)} 个类别，使用默认国内源（直连不走代理）")
        elif self._explicit_proxy:
            logger.info(f"[RSSFetcher] 初始化完成，{len(self.feeds)} 个类别，使用显式配置代理: {self._explicit_proxy}")
        else:
            logger.info(f"[RSSFetcher] 初始化完成，{len(self.feeds)} 个类别，未配置显式代理（将尝试自动探测）")

        # v1.9.7 新增 M3：复用 aiohttp.ClientSession，避免每次重试新建连接池
        # 原问题：_fetch_single_feed 每次重试都 async with ClientSession(...)，连接池无法复用
        # 修复：实例级懒加载 session，跨 fetch() 调用复用，降低 TCP 连接开销
        self._session: aiohttp.ClientSession = None  # type: ignore

    async def _get_session(self) -> aiohttp.ClientSession:
        """v1.9.7 新增 M3：获取或创建持久的 aiohttp.ClientSession

        懒加载模式：首次调用时创建 session，后续复用。
        session 在实例生命周期内持久存在，连接池跨 fetch() 调用复用。
        """
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15, connect=8, sock_read=10)
            headers = {"User-Agent": _BROWSER_UA}
            connector = aiohttp.TCPConnector(
                force_close=False,
                enable_cleanup_closed=True,
                limit_per_host=5,  # v1.9.7：限制单主机连接数
            )
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers=headers,
                connector=connector,
            )
        return self._session

    async def close(self):
        """v1.9.7 新增 M3：关闭持久 session，释放连接池资源

        应在插件 terminate 或 Fetcher 销毁时调用。
        """
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            logger.debug("[RSSFetcher] 持久 session 已关闭")

    def _parse_custom_feeds(self, feeds_str: str) -> Dict[str, List[str]]:
        """解析自定义 RSS 源配置

        支持三种格式（v1.8.0 修复 B1 二次审查 M2：兼容冒号和竖线两种分隔符）：
        1. 分类格式（冒号）：类别:URL1,URL2;类别:URL1,URL2
        2. 分类格式（竖线）：类别|URL1,URL2;类别|URL1,URL2
        3. 简化格式（每行一个 URL，分配到"通用"类别）
        """
        feeds = {}
        feeds_str = feeds_str.strip()

        # v1.8.0 修复（B1 二次审查 M2）：同时支持冒号和竖线作为类别分隔符
        # 与 _conf_schema.json hint 描述（"类别:URL"）保持一致
        has_categories = ("|" in feeds_str and ";" in feeds_str) or \
                         (":" in feeds_str and ";" in feeds_str) or \
                         ("|" in feeds_str and "\n" not in feeds_str) or \
                         (":" in feeds_str and "\n" not in feeds_str)

        if has_categories:
            # 分类格式：按 ; 分割各组，每组按 | 或 : 分割类别和 URL
            for category_part in feeds_str.split(";"):
                category_part = category_part.strip()
                if not category_part:
                    continue
                # 优先尝试 | 分隔符，再尝试 : 分隔符
                if "|" in category_part:
                    category, urls = category_part.split("|", 1)
                elif ":" in category_part:
                    category, urls = category_part.split(":", 1)
                else:
                    continue
                category = category.strip()
                if not category:
                    continue
                feeds[category] = [u.strip() for u in urls.split(",") if u.strip()]
        else:
            # 简化格式：每行一个 URL
            urls = [u.strip() for u in feeds_str.split("\n") if u.strip()]
            if urls:
                feeds["通用"] = urls

        return feeds

    async def fetch(self, category: str, max_items: int = 3) -> List[NewsItem]:
        """获取指定类别的资讯

        v1.8.1 变更：max_items 参数现在由 FetcherManager 传入 pool_size（默认 15），
        用于填充池子。RSSFetcher 从多个源拉取，汇总后按时间排序返回 max_items 条。

        Args:
            category: 资讯类别
            max_items: 最大条数（v1.8.1：通常为 pool_size=15，由 FetcherManager 传入）
        Returns:
            NewsItem 列表
        """
        feeds = self._get_feeds(category)
        if not feeds:
            logger.warning(f"[RSSFetcher] 类别 {category} 无可用 RSS 源")
            return []

        all_items = []

        # 并发获取多个 RSS 源（v1.8.0 修复 B1 二次审查 m7：传入 max_items）
        # v1.8.1：max_items 现在是 pool_size，每个源拉取 max_items*2 条以填充池子
        tasks = [self._fetch_single_feed(feed_url, category, max_items) for feed_url in feeds]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"[RSSFetcher] 获取 RSS 源失败: {result}")
                continue
            all_items.extend(result)

        # 按发布时间排序（最新在前）
        all_items.sort(key=lambda x: x.published_at, reverse=True)

        # 截取最大条数（v1.8.1：通常为 pool_size=15）
        return all_items[:max_items]

    async def _fetch_single_feed(self, feed_url: str, category: str, max_items: int = 3) -> List[NewsItem]:
        """获取单个 RSS 源（v1.8.2 重构）

        Args:
            feed_url: RSS 源 URL
            category: 资讯类别
            max_items: 单源最大条数（v1.8.0 修复 B1 二次审查 m7：与外层 max_items 解耦，
                      取 max_items*2 适度冗余，避免过度抓取）

        v1.8.2 改进（基于实测反馈）：
        - 核心变更：不再使用 feedparser.parse(url) 的内置 HTTP 请求
          实测发现：在 AstrBot 进程内，feedparser.parse(url) 通过 run_in_executor 调用
          会导致 15s 超时（所有源全部失败），但独立进程中同样代码 1-2s 成功
          原因：AstrBot 进程内的某些环境因素（可能 SSL 上下文/socket 配置/事件循环状态）
          干扰了 feedparser 内部的 urllib HTTP 请求
        - 新方案：用 aiohttp 异步拉取 RSS 文本内容 → 传给 feedparser.parse(content) 解析
          - aiohttp 是异步库，不需要 run_in_executor，不占用线程池
          - 完全控制超时、UA、代理
          - feedparser 只负责 XML 解析，不再发起 HTTP 请求
        - 保留重试机制（应对临时网络抖动）
        - 保留错误分类日志（[TIMEOUT]/[NETWORK]/[SSL]/[HTTP]/[PARSE]）
        """
        items = []

        # v1.8.2：如果 aiohttp 不可用，回退到旧的 feedparser.parse(url) 方式
        if aiohttp is None:
            logger.warning(
                f"[RSSFetcher] aiohttp 未安装，回退到 feedparser.parse(url) 方式（可能在 AstrBot 进程内卡死）"
            )
            return await self._fetch_single_feed_legacy(feed_url, category, max_items)

        # v1.8.2：使用 aiohttp 异步拉取
        max_retries = 2  # 总尝试次数 = 2（首次 + 1 次重试）
        last_error_type = "unknown"
        last_error_detail = ""

        # v1.8.3 关键修复：优先使用显式配置的代理（解决 NSSM Session 0 问题）
        # 优先级：fetcher_proxy 配置项 > _get_windows_proxy() 探测
        # 实测：AstrBot 作为 NSSM 服务运行在 SYSTEM 账户，winreg/urllib.getproxies 均失效
        # 必须通过配置项显式指定代理
        # v1.9.6 修正：默认国内源不使用代理，避免国内 RSS 源走代理绕路或被识别为异常流量
        if self._is_default_feeds:
            proxy_url = None
            logger.debug("[RSSFetcher] 使用默认国内源，直连模式")
        elif self._explicit_proxy:
            proxy_url = self._explicit_proxy
            logger.info(f"[RSSFetcher] 使用显式配置代理: {proxy_url}")
        else:
            # 回退：尝试自动探测系统代理（独立进程有效，NSSM 服务无效）
            proxy_url = _get_windows_proxy()
            if proxy_url:
                logger.info(f"[RSSFetcher] 使用自动探测的系统代理: {proxy_url}")
            else:
                logger.debug("[RSSFetcher] 未检测到代理，直连模式（可能因 DNS 劫持失败）")

        # v1.9.7 修正 M3：使用共享 session 替代每次重试新建
        session = await self._get_session()

        for attempt in range(max_retries):
            try:
                start_time = datetime.now()
                async with session.get(feed_url, proxy=proxy_url) as resp:
                    if resp.status != 200:
                        error_detail = f"HTTP {resp.status} {resp.reason}"
                        if resp.status == 429:
                            last_error_type = "http_429"
                            # v1.9.6 修正：读取 Retry-After 头，按服务器指示等待
                            retry_after = resp.headers.get("Retry-After", "")
                            try:
                                retry_after_secs = int(retry_after)
                            except (ValueError, TypeError):
                                retry_after_secs = 0
                            if retry_after_secs > 30:
                                # 服务器要求等待太久，直接放弃
                                last_error_detail = f"HTTP 429 Too Many Requests（站点限流，Retry-After={retry_after_secs}s，放弃重试）"
                                logger.warning(f"[RSSFetcher] {feed_url} [{last_error_type}] {last_error_detail}")
                                return items
                            else:
                                last_error_detail = f"HTTP 429 Too Many Requests（站点限流，Retry-After={retry_after_secs}s）"
                        elif resp.status == 403:
                            last_error_type = "http_403"
                            last_error_detail = f"HTTP 403 Forbidden（被站点屏蔽）"
                            # v1.9.6 修正：403 是确定性错误，不重试
                            logger.warning(f"[RSSFetcher] {feed_url} [{last_error_type}] {last_error_detail}（不重试）")
                            return items
                        elif resp.status == 404:
                            last_error_type = "http_404"
                            last_error_detail = f"HTTP 404 Not Found（源已失效）"
                            # v1.9.6 修正：404 是确定性错误，不重试
                            logger.warning(f"[RSSFetcher] {feed_url} [{last_error_type}] {last_error_detail}（不重试）")
                            return items
                        else:
                            last_error_type = "http"
                            last_error_detail = error_detail

                        if attempt < max_retries - 1:
                            logger.warning(
                                f"[RSSFetcher] {feed_url} 第 {attempt+1} 次拉取失败 [{last_error_type}]，重试中: {last_error_detail}"
                            )
                            await asyncio.sleep(1)
                            continue
                        else:
                            logger.warning(
                                f"[RSSFetcher] {feed_url} 拉取失败（已重试 {max_retries} 次）: "
                                f"[{last_error_type.upper()}] {last_error_detail}"
                            )
                            return items

                    # 读取内容（bytes，让 feedparser 自动检测编码）
                    content = await resp.read()
                    fetch_elapsed = (datetime.now() - start_time).total_seconds()

                    # 用 feedparser 解析内容（不发起 HTTP 请求）
                    # 注意：feedparser.parse 接受 bytes 时会自动检测编码
                    feed = feedparser.parse(content)

                    if feed.bozo:
                        logger.warning(
                            f"[RSSFetcher] RSS 源解析警告: {feed_url} - {feed.bozo_exception}"
                        )

                    # v1.8.0 修复（B1 二次审查 m7）：每个源最多取 max_items*2 条（适度冗余）
                    per_feed_limit = max(max_items * 2, 5)
                    for entry in feed.entries[:per_feed_limit]:
                        # 解析发布时间
                        published_at = datetime.now()
                        if hasattr(entry, "published_parsed") and entry.published_parsed:
                            try:
                                published_at = datetime(*entry.published_parsed[:6])
                            except Exception:
                                pass
                        elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                            try:
                                published_at = datetime(*entry.updated_parsed[:6])
                            except Exception:
                                pass

                        # 获取摘要（v1.8.0 修复 B1 二次审查 m6：处理 None 情况）
                        summary = ""
                        if hasattr(entry, "summary"):
                            summary = (entry.summary or "")[:200]
                        elif hasattr(entry, "description"):
                            summary = (entry.description or "")[:200]

                        # v1.8.0 修复（B1 二次审查 m4）：source 表达式拆为显式 if/else 提升可读性
                        if hasattr(feed, "feed"):
                            source = feed.feed.get("title") or feed_url
                        else:
                            source = feed_url

                        items.append(
                            NewsItem(
                                title=(entry.get("title") or "").strip(),
                                summary=summary,
                                url=entry.get("link") or "",
                                source=source,
                                published_at=published_at,
                                category=category,
                                fetched_at=datetime.now(),
                            )
                        )

                    logger.info(
                        f"[RSSFetcher] {feed_url} 获取 {len(items)} 条 "
                        f"(HTTP 拉取 {fetch_elapsed:.2f}s, 解析后 {len(feed.entries)} 条)"
                    )
                    return items  # 成功直接返回，不再重试

            except asyncio.TimeoutError:
                last_error_type = "timeout"
                last_error_detail = f"15s 超时未响应"
                if attempt < max_retries - 1:
                    logger.warning(
                        f"[RSSFetcher] {feed_url} 第 {attempt+1} 次拉取超时（15s），重试中..."
                    )
                    await asyncio.sleep(1)
                    continue
                else:
                    logger.warning(
                        f"[RSSFetcher] {feed_url} 拉取失败（已重试 {max_retries} 次）: "
                        f"[TIMEOUT] {last_error_detail}"
                    )
                    return items

            except asyncio.CancelledError:
                # 任务被取消，不重试，直接返回已获取的内容
                logger.warning(f"[RSSFetcher] {feed_url} 任务被取消")
                return items

            except aiohttp.ClientConnectorError as e:
                last_error_type = "network"
                last_error_detail = f"连接错误: {e}"
                if attempt < max_retries - 1:
                    logger.warning(
                        f"[RSSFetcher] {feed_url} 第 {attempt+1} 次拉取失败 [NETWORK]，重试中: {last_error_detail}"
                    )
                    await asyncio.sleep(1)
                    continue
                else:
                    logger.warning(
                        f"[RSSFetcher] {feed_url} 拉取失败（已重试 {max_retries} 次）: "
                        f"[NETWORK] {last_error_detail}"
                    )
                    return items

            except aiohttp.ClientSSLError as e:
                last_error_type = "ssl"
                last_error_detail = f"SSL/TLS 错误: {e}"
                if attempt < max_retries - 1:
                    logger.warning(
                        f"[RSSFetcher] {feed_url} 第 {attempt+1} 次拉取失败 [SSL]，重试中: {last_error_detail}"
                    )
                    await asyncio.sleep(1)
                    continue
                else:
                    logger.warning(
                        f"[RSSFetcher] {feed_url} 拉取失败（已重试 {max_retries} 次）: "
                        f"[SSL] {last_error_detail}"
                    )
                    return items

            except Exception as e:
                # v1.8.2 改进：区分异常类型，输出更明确信息
                error_type_name = type(e).__name__
                last_error_type = "parse"
                last_error_detail = f"{error_type_name}: {e}"

                if attempt < max_retries - 1:
                    logger.warning(
                        f"[RSSFetcher] {feed_url} 第 {attempt+1} 次拉取失败 [{last_error_type}]，重试中: {last_error_detail}"
                    )
                    await asyncio.sleep(1)
                    continue
                else:
                    logger.warning(
                        f"[RSSFetcher] {feed_url} 拉取失败（已重试 {max_retries} 次）: "
                        f"[{last_error_type.upper()}] {last_error_detail}"
                    )
                    return items

        return items

    async def _fetch_single_feed_legacy(self, feed_url: str, category: str, max_items: int = 3) -> List[NewsItem]:
        """旧的拉取方式（feedparser.parse(url)），仅作回退

        v1.8.2：仅在 aiohttp 不可用时使用
        实测在 AstrBot 进程内会卡死，仅保留为兜底
        """
        items = []
        try:
            # 修改 feedparser 默认 User-Agent
            if feedparser is not None and getattr(feedparser, "USER_AGENT", "").startswith("feedparser/"):
                feedparser.USER_AGENT = _BROWSER_UA

            loop = asyncio.get_running_loop()
            feed = await asyncio.wait_for(
                loop.run_in_executor(None, feedparser.parse, feed_url),
                timeout=15
            )

            if feed.bozo:
                logger.warning(
                    f"[RSSFetcher] RSS 源解析警告: {feed_url} - {feed.bozo_exception}"
                )

            per_feed_limit = max(max_items * 2, 5)
            for entry in feed.entries[:per_feed_limit]:
                published_at = datetime.now()
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    try:
                        published_at = datetime(*entry.published_parsed[:6])
                    except Exception:
                        pass
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    try:
                        published_at = datetime(*entry.updated_parsed[:6])
                    except Exception:
                        pass

                summary = ""
                if hasattr(entry, "summary"):
                    summary = (entry.summary or "")[:200]
                elif hasattr(entry, "description"):
                    summary = (entry.description or "")[:200]

                if hasattr(feed, "feed"):
                    source = feed.feed.get("title") or feed_url
                else:
                    source = feed_url

                items.append(
                    NewsItem(
                        title=(entry.get("title") or "").strip(),
                        summary=summary,
                        url=entry.get("link") or "",
                        source=source,
                        published_at=published_at,
                        category=category,
                        fetched_at=datetime.now(),
                    )
                )

            logger.info(f"[RSSFetcher] {feed_url} 获取 {len(items)} 条 (legacy)")
        except Exception as e:
            logger.warning(f"[RSSFetcher] 解析 {feed_url} 失败 (legacy): {type(e).__name__}: {e}")

        return items

    def _get_feeds(self, category: str) -> List[str]:
        """获取指定类别的 RSS 源列表"""
        # 优先匹配类别，降级匹配"通用"
        return self.feeds.get(category) or self.feeds.get("通用", [])

    async def refresh(self) -> None:
        """刷新资讯池（RSS 模式下无需预刷新，实时获取）"""
        pass
