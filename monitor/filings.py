"""Exchange filings from CNINFO (巨潮资讯), the official disclosure site.

Filings are the highest-signal source available: earnings pre-announcements,
contracts, buybacks, insider buying/selling, placements and regulatory action
all appear here first. Most of the volume is boilerplate (legal opinions,
subsidiary guarantees, HK daily return forms), so every title is classified by
rule into a tier:

  1  alert now        — could change the investment case or the price
  2  weekly brief     — worth knowing, not worth a ping
  0  noise            — dropped
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta

import akshare as ak

logger = logging.getLogger(__name__)


@dataclass
class Filing:
    id: str
    code: str
    company: str
    title: str
    published: str   # YYYY-MM-DD
    url: str
    category: str
    tier: int
    tone: str        # positive / negative / neutral

    @property
    def pdf_url(self) -> str:
        return f"http://static.cninfo.com.cn/finalpage/{self.published}/{self.id}.PDF"

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def pdf_text(filing: Filing, max_chars: int = 4000) -> str:
    """First part of the filing's text, so the summary can quote real figures."""
    import io

    import requests
    from pypdf import PdfReader

    try:
        resp = requests.get(filing.pdf_url, timeout=30)
        resp.raise_for_status()
        if not resp.content.startswith(b"%PDF"):
            return ""
        reader = PdfReader(io.BytesIO(resp.content))
        text = ""
        for page in reader.pages[:4]:
            text += (page.extract_text() or "") + "\n"
            if len(text) >= max_chars:
                break
        return " ".join(text.split())[:max_chars]
    except Exception:
        logger.warning("could not read PDF for filing %s", filing.id)
        return ""


# Order matters: first match wins. Noise rules come first so boilerplate that
# mentions a significant word (e.g. a legal opinion *on* a buyback) is dropped.
# (pattern, category, tier, tone)
RULES: list[tuple[str, str, int, str]] = [
    # ── Noise ──
    (r"法律意见书|核查意见|独立财务顾问|专项说明|鉴证报告|审计报告|内部控制", "Boilerplate", 0, "neutral"),
    (r"细则|制度|章程|规则|声明与承诺|候选人声明|提名人声明|对照表|管理办法", "Governance documents", 0, "neutral"),
    (r"翌日披露报表|证券变动月报表|董事会会议召开日期|H股市场公告|H股公告$", "HK routine return", 0, "neutral"),
    (r"风险持续评估|资金占用|募集资金|现金管理|闲置", "Treasury / funds admin", 0, "neutral"),
    (r"业绩说明会|股东会.*(通知|决议|材料)|股东大会|会议决议|独立董事.*会议", "Meetings", 0, "neutral"),
    (r"报告摘要|半年度报告摘要|年度报告摘要", "Report summary", 0, "neutral"),
    (r"工商变更|营业执照|公司网址|注册资本|私募投资基金备案|参与投资.*(基金|合伙)", "Admin", 0, "neutral"),
    (r"限制性股票|股票期权|激励计划|员工持股|自主行权|归属", "Employee incentives", 0, "neutral"),
    (r"解除质押", "Pledge released", 0, "neutral"),
    (r"担保", "Guarantee for subsidiary", 0, "neutral"),
    (r"(绿色|科技创新|中期票据|公司)?债券.*(发行|注册|结果)|接受注册通知书|境外债券", "Bond issuance", 0, "neutral"),
    (r"会计政策变更|可转债转股结果", "Accounting / technical", 0, "neutral"),
    (r"日常关联交易|关联采购|接受关联方", "Routine related-party", 0, "neutral"),
    # ── Tier 1 ──
    (r"业绩预告.*(预增|扭亏|增长)|预增|扭亏", "Earnings pre-announcement (up)", 1, "positive"),
    (r"业绩预告.*(预减|预亏|首亏|续亏|下降)|预减|预亏|首亏|续亏", "Earnings pre-announcement (down)", 1, "negative"),
    (r"业绩预告|业绩快报", "Earnings pre-announcement", 1, "neutral"),
    (r"可交换公司债", "Major holder exchangeable bond", 2, "neutral"),
    (r"向特定对象发行|非公开发行|定向增发|配股|增发", "Share placement (dilution)", 1, "negative"),
    (r"减持", "Major holder / insider selling", 1, "negative"),
    (r"增持", "Major holder / insider buying", 1, "positive"),
    (r"回购.*(方案|报告书)|提议回购", "Buyback announced", 1, "positive"),
    (r"立案|行政处罚|监管函|警示函|问询函|关注函|责令改正|纪律处分", "Regulatory action / enquiry", 1, "negative"),
    (r"(重组|购买资产|收购).*进展", "Deal progress", 2, "neutral"),
    (r"停牌|重大资产重组|发行股份.*购买资产", "Restructuring / suspension", 1, "neutral"),
    (r"重大合同|中标|重大项目|签订.*(合同|协议)", "Major contract", 1, "positive"),
    (r"风险提示|退市|\*ST", "Risk warning", 1, "negative"),
    # ── Tier 2 ──
    (r"回购.*进展|首次回购|回购股份.*用途", "Buyback progress", 2, "positive"),
    (r"诉讼|仲裁", "Litigation / arbitration", 2, "neutral"),
    (r"减值", "Impairment", 2, "negative"),
    (r"异常波动", "Abnormal trading notice", 2, "neutral"),
    (r"年度报告|季度报告|中期报告", "Periodic report", 2, "neutral"),
    (r"利润分配|权益分派|分红", "Dividend", 2, "positive"),
    (r"发电量|产销|经营数据|经营情况", "Operating data", 2, "neutral"),
    (r"交易价格", "Pricing update", 2, "neutral"),
    (r"辞职|聘任|选举|换届|董事长|总经理", "Management / board change", 2, "neutral"),
    (r"质押", "Major holder pledged shares", 2, "negative"),
    (r"权益变动", "Major holder stake change", 2, "neutral"),
    (r"转股价格", "Convertible bond terms", 2, "neutral"),
    (r"挂牌转让|出售|收购|合资|设立", "M&A / disposal", 2, "neutral"),
    (r"关联交易", "Related-party transaction", 2, "neutral"),
]
_COMPILED = [(re.compile(p), c, t, tone) for p, c, t, tone in RULES]


def classify(title: str) -> tuple[str, int, str]:
    for rx, category, tier, tone in _COMPILED:
        if rx.search(title):
            return category, tier, tone
    return "Other filing", 2, "neutral"


_ANN_ID = re.compile(r"announcementId=(\d+)")


def fetch(code: str, days: int = 7, today: date | None = None) -> list[Filing]:
    end = today or date.today()
    start = end - timedelta(days=days)
    try:
        df = ak.stock_zh_a_disclosure_report_cninfo(
            symbol=code, market="沪深京", start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")
        )
    except Exception:
        logger.exception("CNINFO fetch failed for %s", code)
        return []
    out = []
    for _, r in df.iterrows():
        title = str(r["公告标题"]).strip()
        url = str(r["公告链接"])
        m = _ANN_ID.search(url)
        category, tier, tone = classify(title)
        out.append(
            Filing(
                id=m.group(1) if m else f"{code}:{r['公告时间']}:{title}",
                code=code,
                company=str(r["简称"]),
                title=title,
                published=str(r["公告时间"])[:10],
                url=url,
                category=category,
                tier=tier,
                tone=tone,
            )
        )
    return out


def fetch_all(codes: list[str], days: int = 7) -> list[Filing]:
    out: list[Filing] = []
    for code in codes:
        out.extend(fetch(code, days))
        time.sleep(0.5)
    # Newest first; drop exact duplicates CNINFO occasionally returns.
    seen, uniq = set(), []
    for f in sorted(out, key=lambda f: f.published, reverse=True):
        if f.id not in seen:
            seen.add(f.id)
            uniq.append(f)
    return uniq
