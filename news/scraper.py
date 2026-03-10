"""新闻爬虫 - 抓取主流金融新闻网站"""
import asyncio
import re
from datetime import datetime
from dataclasses import dataclass
from typing import Optional
from difflib import SequenceMatcher
import httpx
from bs4 import BeautifulSoup
from logging_config import get_logger

logger = get_logger("qtsys.news.scraper")


@dataclass
class NewsItem:
    title: str
    url: str
    source: str
    publish_time: str = ""
    summary: str = ""
    content: str = ""
    category: str = ""


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT}
TIMEOUT = httpx.Timeout(15.0, connect=10.0)

# 重试配置
MAX_RETRIES = 3
RETRY_BACKOFF = [1, 2, 4]  # 指数退避秒数
RETRYABLE_STATUS = {500, 502, 503, 504, 408, 429}


async def _fetch(client: httpx.AsyncClient, url: str, encoding="utf-8") -> Optional[str]:
    """带指数退避重试的HTTP请求"""
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.get(url, headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
            if resp.status_code == 200:
                resp.encoding = encoding
                return resp.text
            if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF[attempt]
                logger.warning(f"HTTP {resp.status_code} from {url}, 重试 {attempt+1}/{MAX_RETRIES} (等待{wait}s)")
                await asyncio.sleep(wait)
                continue
            # 4xx等不可重试错误
            logger.warning(f"HTTP {resp.status_code} from {url}, 不可重试")
            return None
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF[attempt]
                logger.warning(f"请求超时/连接失败 {url}: {e}, 重试 {attempt+1}/{MAX_RETRIES}")
                await asyncio.sleep(wait)
            else:
                logger.error(f"请求最终失败 {url}: {e}")
        except Exception:
            logger.exception(f"请求异常 {url}")
            break
    return None


def _clean(text: str) -> str:
    text = re.sub(r'\s+', ' ', text).strip()
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)


# ===== 新浪财经 (JSON API, 支持分页) =====

async def scrape_sina(client: httpx.AsyncClient, pages: int = 3) -> list[NewsItem]:
    items = []
    for page in range(1, pages + 1):
        params = {"pageid": "153", "lid": "2516", "num": "30", "page": str(page)}
        try:
            resp = await client.get("https://feed.mix.sina.com.cn/api/roll/get",
                                    params=params, headers=HEADERS,
                                    timeout=TIMEOUT, follow_redirects=True)
            data = resp.json()
            for it in data.get("result", {}).get("data", []):
                title = it.get("title", "").strip()
                if not title:
                    continue
                try:
                    pub = datetime.fromtimestamp(int(it.get("ctime", ""))).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pub = ""
                items.append(NewsItem(
                    title=title, url=it.get("url", ""), source="新浪财经",
                    publish_time=pub,
                    summary=_clean(it.get("summary", "") or it.get("intro", ""))[:200],
                    category="财经",
                ))
        except Exception:
            logger.exception(f"新浪财经第{page}页抓取失败")
    logger.info(f"新浪财经: 抓取{len(items)}条")
    return items


# ===== 东方财富 (JSON API, 支持分页) =====

async def scrape_eastmoney(client: httpx.AsyncClient, pages: int = 3) -> list[NewsItem]:
    items = []
    for page in range(pages):
        params = {"columns": "74", "pageSize": "30", "pageIndex": str(page), "req_trace": "1"}
        try:
            resp = await client.get("https://np-listapi.eastmoney.com/comm/web/getNewsByColumns",
                                    params=params, headers=HEADERS,
                                    timeout=TIMEOUT, follow_redirects=True)
            data = resp.json()
            if not data or not isinstance(data, dict):
                continue
            inner = data.get("data")
            if not inner or not isinstance(inner, dict):
                continue
            for it in inner.get("list", []):
                title = it.get("title", "").strip()
                if not title:
                    continue
                items.append(NewsItem(
                    title=title, url=it.get("url", ""), source="东方财富",
                    publish_time=it.get("showTime", "")[:16],
                    summary=_clean(it.get("digest", ""))[:200],
                    category="财经",
                ))
        except Exception:
            logger.exception(f"东方财富第{page+1}页抓取失败")
    logger.info(f"东方财富: 抓取{len(items)}条")
    return items


# ===== 财联社电报 (JSON API, 支持分页) =====

async def scrape_cls(client: httpx.AsyncClient, pages: int = 3) -> list[NewsItem]:
    items = []
    last_time = ""
    for page in range(pages):
        params = {"app": "CailianpressWeb", "os": "web", "sv": "7.7.5"}
        if last_time:
            params["last_time"] = last_time
        try:
            resp = await client.get("https://www.cls.cn/nodeapi/updateTelegraphList",
                                    params=params, headers=HEADERS,
                                    timeout=TIMEOUT, follow_redirects=True)
            data = resp.json()
            roll_data = data.get("data", {}).get("roll_data", [])
            for it in roll_data:
                title = _clean(it.get("title", "") or it.get("brief", ""))
                if not title:
                    raw = it.get("content", "")
                    soup = BeautifulSoup(raw, "html.parser")
                    title = _clean(soup.get_text())[:80]
                if not title:
                    continue
                try:
                    ctime = int(it.get("ctime", 0))
                    pub = datetime.fromtimestamp(ctime).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pub = ""
                items.append(NewsItem(
                    title=title[:120],
                    url=f"https://www.cls.cn/detail/{it.get('id', '')}",
                    source="财联社", publish_time=pub,
                    summary=title[:200], category="快讯",
                ))
            # 用最后一条的时间做分页游标
            if roll_data:
                last_time = str(roll_data[-1].get("ctime", ""))
        except Exception:
            logger.exception(f"财联社第{page+1}页抓取失败")
    logger.info(f"财联社: 抓取{len(items)}条")
    return items


# ===== 同花顺 (HTML, 多选择器容错) =====

async def scrape_10jqka(client: httpx.AsyncClient) -> list[NewsItem]:
    items = []
    html = await _fetch(client, "https://news.10jqka.com.cn/cjzx_list/", encoding="utf-8")
    if not html:
        logger.warning("同花顺: 页面获取失败")
        return items
    try:
        soup = BeautifulSoup(html, "html.parser")
        # 多个备选CSS选择器，应对页面改版
        selector_configs = [
            ("ul.list-con li", "a", "span.arc-title"),
            ("div.list-content li", "a", "span.time"),
            ("div.news-list li", "a", "span.date"),
            ("ul.news-list li", "a", "span"),
        ]
        found = False
        for li_sel, a_sel, time_sel in selector_configs:
            lis = soup.select(li_sel)[:30]
            if not lis:
                continue
            found = True
            for li in lis:
                a = li.select_one(a_sel)
                if not a:
                    continue
                title = _clean(a.get_text())
                href = a.get("href", "")
                if not title or not href:
                    continue
                if href.startswith("//"):
                    href = "https:" + href
                span = li.select_one(time_sel)
                pub = _clean(span.get_text()) if span else ""
                items.append(NewsItem(
                    title=title, url=href, source="同花顺",
                    publish_time=pub, summary="", category="财经",
                ))
            break
        if not found:
            logger.info("同花顺: 所有 CSS 选择器均未匹配，页面可能已改版")
    except Exception:
        logger.exception("同花顺抓取异常")
    logger.info(f"同花顺: 抓取{len(items)}条")
    return items


# ===== 华尔街见闻 (JSON API) =====

async def scrape_wallstreetcn(client: httpx.AsyncClient) -> list[NewsItem]:
    items = []
    params = {"channel": "global-channel", "limit": "50"}
    try:
        resp = await client.get(
            "https://api-one.wallstcn.com/apiv1/content/lives",
            params=params, headers=HEADERS,
            timeout=TIMEOUT, follow_redirects=True,
        )
        data = resp.json()
        for it in data.get("data", {}).get("items", []):
            title = _clean(it.get("title", "") or it.get("content_text", ""))[:120]
            if not title:
                continue
            try:
                pub = datetime.fromtimestamp(
                    it.get("display_time", 0)
                ).strftime("%Y-%m-%d %H:%M")
            except Exception:
                pub = ""
            uri = it.get("uri", "") or str(it.get("id", ""))
            items.append(NewsItem(
                title=title,
                url=f"https://wallstreetcn.com/live/{uri}",
                source="华尔街见闻", publish_time=pub,
                summary=title[:200], category="快讯",
            ))
    except Exception:
        logger.exception("华尔街见闻抓取失败")
    logger.info(f"华尔街见闻: 抓取{len(items)}条")
    return items


# ===== 金十数据 (JSON API) =====

async def scrape_jin10(client: httpx.AsyncClient) -> list[NewsItem]:
    items = []
    try:
        resp = await client.get(
            "https://flash-api.jin10.com/get_flash_list",
            params={"channel": "-8200", "max_time": "", "vip": "0"},
            headers={**HEADERS, "x-app-id": "bVBF4FyRTn5NJF5n",
                     "x-version": "1.0.0"},
            timeout=TIMEOUT, follow_redirects=True,
        )
        data = resp.json()
        for it in data.get("data", []):
            raw = it.get("data", {})
            title = _clean(raw.get("title", "") or raw.get("content", ""))[:120]
            if not title:
                continue
            pub = it.get("time", "")[:16]
            items.append(NewsItem(
                title=title,
                url=f"https://www.jin10.com/flash_detail/{it.get('id', '')}",
                source="金十数据", publish_time=pub,
                summary=title[:200], category="快讯",
            ))
    except Exception:
        logger.exception("金十数据抓取失败")
    logger.info(f"金十数据: 抓取{len(items)}条")
    return items


# ===== 央行官网 (HTML) =====

async def scrape_pbc(client: httpx.AsyncClient) -> list[NewsItem]:
    items = []
    html = await _fetch(client, "https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html")
    if not html:
        logger.warning("央行官网: 页面获取失败")
        return items
    try:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select("a[href]")[:30]:
            title = _clean(a.get_text())
            if len(title) < 6:
                continue
            href = a.get("href", "")
            if not href or "index" in href:
                continue
            if href.startswith("/"):
                href = "https://www.pbc.gov.cn" + href
            span = a.find_next_sibling("span")
            pub = _clean(span.get_text()) if span else ""
            items.append(NewsItem(
                title=title, url=href, source="央行",
                publish_time=pub, summary="", category="货币政策",
            ))
    except Exception:
        logger.exception("央行官网抓取异常")
    logger.info(f"央行: 抓取{len(items)}条")
    return items


# ===== 证监会官网 (HTML) =====

async def scrape_csrc(client: httpx.AsyncClient) -> list[NewsItem]:
    items = []
    html = await _fetch(client, "https://www.csrc.gov.cn/csrc/c100028/common_list.shtml")
    if not html:
        logger.warning("证监会: 页面获取失败")
        return items
    try:
        soup = BeautifulSoup(html, "html.parser")
        for li in soup.select("ul.list_con li, div.list-content li")[:30]:
            a = li.select_one("a")
            if not a:
                continue
            title = _clean(a.get_text())
            if len(title) < 6:
                continue
            href = a.get("href", "")
            if not href:
                continue
            if href.startswith("/"):
                href = "https://www.csrc.gov.cn" + href
            span = li.select_one("span")
            pub = _clean(span.get_text()) if span else ""
            items.append(NewsItem(
                title=title, url=href, source="证监会",
                publish_time=pub, summary="", category="监管动态",
            ))
    except Exception:
        logger.exception("证监会抓取异常")
    logger.info(f"证监会: 抓取{len(items)}条")
    return items


# ===== 抓取新闻正文 =====

async def fetch_article_content(url: str) -> str:
    """抓取新闻正文内容"""
    if not url:
        return ""
    async with httpx.AsyncClient() as client:
        html = await _fetch(client, url)
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        selectors = [
            "div.article-content", "div.article_content", "div.art_content",
            "div.article-body", "div#artibody", "div.post_body",
            "article", "div.content", "div.text", "div.detail-content",
            "div.article", "div.news-content", "div.main-content",
        ]
        for sel in selectors:
            el = soup.select_one(sel)
            if el and len(el.get_text(strip=True)) > 50:
                paragraphs = el.find_all("p")
                if paragraphs:
                    return "\n\n".join(_clean(p.get_text()) for p in paragraphs if p.get_text(strip=True))
                return _clean(el.get_text())
        body = soup.find("body")
        if body:
            text = _clean(body.get_text())
            if len(text) > 100:
                return text[:5000]
    except Exception:
        logger.exception(f"正文抓取异常: {url}")
    return ""


# ===== 主入口 =====

SCRAPERS = {
    "新浪财经": scrape_sina,
    "东方财富": scrape_eastmoney,
    "财联社": scrape_cls,
    "同花顺": scrape_10jqka,
    "华尔街见闻": scrape_wallstreetcn,
    "金十数据": scrape_jin10,
    "央行": scrape_pbc,
    "证监会": scrape_csrc,
}


def _fuzzy_dedup(items: list[NewsItem], threshold: float = 0.75) -> list[NewsItem]:
    """模糊去重 - 基于SequenceMatcher相似度"""
    unique = []
    seen_titles = []
    for item in items:
        title = item.title.strip()
        is_dup = False
        for prev in seen_titles:
            if SequenceMatcher(None, title, prev).ratio() > threshold:
                is_dup = True
                break
        if not is_dup:
            unique.append(item)
            seen_titles.append(title)
    return unique


async def scrape_all(sources: list[str] = None) -> list[NewsItem]:
    """并发抓取所有新闻源, 返回按时间倒序排列的去重新闻列表"""
    if sources is None:
        sources = list(SCRAPERS.keys())

    async with httpx.AsyncClient() as client:
        tasks = []
        for name in sources:
            if name in SCRAPERS:
                tasks.append(SCRAPERS[name](client))
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_items = []
    for i, r in enumerate(results):
        if isinstance(r, list):
            all_items.extend(r)
        elif isinstance(r, Exception):
            logger.error(f"爬虫异常: {r}")

    # 模糊去重
    unique = _fuzzy_dedup(all_items)
    logger.info(f"总计抓取{len(all_items)}条, 去重后{len(unique)}条")

    # 按时间倒序
    unique.sort(key=lambda x: x.publish_time or "", reverse=True)
    return unique
