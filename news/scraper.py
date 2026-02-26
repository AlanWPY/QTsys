"""新闻爬虫 - 抓取主流金融新闻网站"""
import asyncio
import re
from datetime import datetime
from dataclasses import dataclass
from typing import Optional
import httpx
from bs4 import BeautifulSoup


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


async def _fetch(client: httpx.AsyncClient, url: str, encoding="utf-8") -> Optional[str]:
    try:
        resp = await client.get(url, headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
        resp.encoding = encoding
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    return None


def _clean(text: str) -> str:
    text = re.sub(r'\s+', ' ', text).strip()
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)


# ===== 新浪财经 (JSON API) =====

async def scrape_sina(client: httpx.AsyncClient) -> list[NewsItem]:
    items = []
    params = {"pageid": "153", "lid": "2516", "num": "30", "page": "1"}
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
        pass
    return items


# ===== 东方财富 (JSON API) =====

async def scrape_eastmoney(client: httpx.AsyncClient) -> list[NewsItem]:
    items = []
    params = {"columns": "74", "pageSize": "30", "pageIndex": "0", "req_trace": "1"}
    try:
        resp = await client.get("https://np-listapi.eastmoney.com/comm/web/getNewsByColumns",
                                params=params, headers=HEADERS,
                                timeout=TIMEOUT, follow_redirects=True)
        data = resp.json()
        for it in data.get("data", {}).get("list", []):
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
        pass
    return items


# ===== 财联社电报 (JSON API) =====

async def scrape_cls(client: httpx.AsyncClient) -> list[NewsItem]:
    items = []
    params = {"app": "CailianpressWeb", "os": "web", "sv": "7.7.5"}
    try:
        resp = await client.get("https://www.cls.cn/nodeapi/updateTelegraphList",
                                params=params, headers=HEADERS,
                                timeout=TIMEOUT, follow_redirects=True)
        data = resp.json()
        for it in data.get("data", {}).get("roll_data", []):
            title = _clean(it.get("title", "") or it.get("brief", ""))
            if not title:
                raw = it.get("content", "")
                soup = BeautifulSoup(raw, "html.parser")
                title = _clean(soup.get_text())[:80]
            if not title:
                continue
            try:
                pub = datetime.fromtimestamp(int(it.get("ctime", 0))).strftime("%Y-%m-%d %H:%M")
            except Exception:
                pub = ""
            items.append(NewsItem(
                title=title[:120],
                url=f"https://www.cls.cn/detail/{it.get('id', '')}",
                source="财联社", publish_time=pub,
                summary=title[:200], category="快讯",
            ))
    except Exception:
        pass
    return items


# ===== 同花顺 (HTML) =====

async def scrape_10jqka(client: httpx.AsyncClient) -> list[NewsItem]:
    items = []
    html = await _fetch(client, "https://news.10jqka.com.cn/cjzx_list/", encoding="utf-8")
    if not html:
        return items
    try:
        soup = BeautifulSoup(html, "html.parser")
        for li in soup.select("ul.list-con li")[:30]:
            a = li.select_one("a")
            if not a:
                continue
            title = _clean(a.get_text())
            href = a.get("href", "")
            if not title or not href:
                continue
            if href.startswith("//"):
                href = "https:" + href
            span = li.select_one("span.arc-title")
            pub = _clean(span.get_text()) if span else ""
            items.append(NewsItem(
                title=title, url=href, source="同花顺",
                publish_time=pub, summary="", category="财经",
            ))
    except Exception:
        pass
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
        # 移除脚本和样式
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        # 尝试常见正文容器
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
        # 回退: 取body中最长的文本块
        body = soup.find("body")
        if body:
            text = _clean(body.get_text())
            if len(text) > 100:
                return text[:5000]
    except Exception:
        pass
    return ""


# ===== 主入口 =====

SCRAPERS = {
    "新浪财经": scrape_sina,
    "东方财富": scrape_eastmoney,
    "财联社": scrape_cls,
    "同花顺": scrape_10jqka,
}


async def scrape_all(sources: list[str] = None) -> list[NewsItem]:
    """并发抓取所有新闻源, 返回按时间倒序排列的新闻列表"""
    if sources is None:
        sources = list(SCRAPERS.keys())

    async with httpx.AsyncClient() as client:
        tasks = []
        for name in sources:
            if name in SCRAPERS:
                tasks.append(SCRAPERS[name](client))
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_items = []
    for r in results:
        if isinstance(r, list):
            all_items.extend(r)

    # 去重 (按标题)
    seen = set()
    unique = []
    for item in all_items:
        key = item.title.strip()[:50]
        if key not in seen:
            seen.add(key)
            unique.append(item)

    # 按时间倒序
    unique.sort(key=lambda x: x.publish_time or "", reverse=True)
    return unique
