"""Chinese financial news from Eastmoney's search (via AKShare).

Searching by ticker code — what the previous version did — mostly returns
screener spam ("188 stocks form a death cross", "82 stocks hit limit-up at
noon") in which the ticker is one row of a table. Searching by the company's
short Chinese name and requiring that name in the headline returns real
stories. Theme searches (e.g. 钍基熔盐堆) require a theme term in the headline.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import akshare as ak

logger = logging.getLogger(__name__)

JUNK = re.compile(
    r"\d+只|只个?股|附股|名单|主力资金|资金流|净流入|净流出|龙虎榜|均线|死叉|金叉|融资融券|两融|杠杆资金"
    r"|大宗交易|北向|特大单|早报|晚报|午评|收评|盘前|盘中|开盘|收盘|快报|速递|概念(涨|跌)|板块|ETF"
)


@dataclass
class Article:
    id: str
    query: str
    title: str
    snippet: str
    source: str
    published: str  # "YYYY-MM-DD HH:MM"
    url: str

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _search(keyword: str) -> list[Article]:
    try:
        df = ak.stock_news_em(symbol=keyword)
    except Exception:
        logger.exception("news search failed for %r", keyword)
        return []
    out = []
    for _, r in df.iterrows():
        title = str(r.get("新闻标题") or "").strip()
        url = str(r.get("新闻链接") or "")
        out.append(
            Article(
                id=hashlib.sha1((url or title).encode()).hexdigest()[:16],
                query=keyword,
                title=title,
                snippet=" ".join(str(r.get("新闻内容") or "").split())[:300],
                source=str(r.get("文章来源") or ""),
                published=str(r.get("发布时间") or "")[:16],
                url=url,
            )
        )
    return out


def relevant(article: Article, must_contain: list[str], max_age_days: int, now: datetime | None = None) -> bool:
    if not article.title or JUNK.search(article.title):
        return False
    if not any(term in article.title for term in must_contain):
        return False
    try:
        published = datetime.strptime(article.published[:10], "%Y-%m-%d")
    except ValueError:
        return False
    return published >= (now or datetime.now()) - timedelta(days=max_age_days)


def _dedupe(articles: list[Article]) -> list[Article]:
    """Drop repeats of the same story (same headline prefix) syndicated across outlets."""
    seen, out = set(), []
    for a in sorted(articles, key=lambda a: a.published, reverse=True):
        key = re.sub(r"\W", "", a.title)[:18]
        if a.id in seen or key in seen:
            continue
        seen.update({a.id, key})
        out.append(a)
    return out


def company(short_zh: str, max_age_days: int = 7) -> list[Article]:
    return _dedupe([a for a in _search(short_zh) if relevant(a, [short_zh], max_age_days)])


def theme(queries: list[str], title_terms: list[str], max_age_days: int = 14) -> list[Article]:
    found: list[Article] = []
    for q in queries:
        found.extend(a for a in _search(q) if relevant(a, title_terms, max_age_days))
        time.sleep(0.5)
    return _dedupe(found)
