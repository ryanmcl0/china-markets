from datetime import datetime, timezone

import pandas as pd
import pytest

from monitor import schedule, telegram
from monitor.config import Stock, find
from monitor.filings import classify
from monitor.market import compute, rsi
from monitor.news import Article, relevant
from monitor.signals import attribute, evaluate


# ── Filing classifier — real titles from CNINFO, Jul–Oct 2026 ────────────────

@pytest.mark.parametrize("title,tier,tone", [
    ("北方稀土2026年前三季度业绩预增公告", 1, "positive"),
    ("2026年半年度业绩预告", 1, "neutral"),
    ("上海电气关于筹划向特定对象发行A股股票的提示性公告", 1, "negative"),
    ("长江电力关于控股股东增持股份结果的公告", 1, "positive"),
    ("关于公司副总裁兼董事会秘书增持股份公告", 1, "positive"),
    ("国电南瑞关于董事长提议回购公司部分股份的提示性公告", 1, "positive"),
    ("关于回购公司股份方案的公告暨回购股份报告书", 1, "positive"),
    ("关于首次回购公司A股股份的公告", 2, "positive"),
    ("关于回购公司股份的进展公告", 2, "positive"),
    ("上海电气关于子公司重大诉讼的进展公告", 2, "neutral"),
    ("上海电气关于计提减值准备的公告", 2, "negative"),
    ("关于持股5%以上股东及其一致行动人权益变动触及1%整数倍的公告", 2, "neutral"),
    ("关于发行股份及支付现金购买资产并募集配套资金暨关联交易事项的进展公告", 2, "neutral"),
    ("2026年半年度报告", 2, "neutral"),
    ("北方稀土关于2026年第四季度稀土精矿交易价格的公告", 2, "neutral"),
    ("长江电力2026年三季度发电量完成情况公告", 2, "neutral"),
    # noise
    ("H股公告（翌日披露报表）", 0, "neutral"),
    ("2026年半年度报告摘要", 0, "neutral"),
    ("关于全资子公司金风国际为其全资子公司金风越南提供担保的公告", 0, "neutral"),
    ("上海市通力律师事务所关于宁德时代新能源科技股份有限公司2026年第一次临时股东会的法律意见书", 0, "neutral"),
    ("关于参与投资的基金完成私募投资基金备案的公告", 0, "neutral"),
    ("《2026年A股第二期员工持股计划（草案）》", 0, "neutral"),
    ("关于控股股东部分股份解除质押的公告", 0, "neutral"),
    ("关于2026年度第八期绿色科技创新债券发行完成的公告", 0, "neutral"),
    ("第七届董事会第七次会议决议公告", 0, "neutral"),
    ("关于增加2026年度日常关联交易预计额度的公告", 0, "neutral"),
])
def test_classify(title, tier, tone):
    _, got_tier, got_tone = classify(title)
    assert (got_tier, got_tone) == (tier, tone), title


# ── Metrics / signals ────────────────────────────────────────────────────────

def _stock(**kw):
    base = dict(ticker="002255", exchange="XSHE", name_en="Hailu", short_en="Hailu", short_zh="海陆重工", currency="CNY",
                limit=10.5, tier="core", status="watching", thesis="", filings_code="002255")
    base.update(kw)
    return Stock(**base)


def _bars(closes, volumes=None):
    idx = pd.bdate_range(end="2026-10-16", periods=len(closes))
    return pd.DataFrame({"close": closes, "volume": volumes or [1000] * len(closes)}, index=idx)


def _bench(n, last_move=0.0):
    vals = [100.0] * (n - 1) + [100.0 * (1 + last_move / 100)]
    return pd.Series(vals, index=pd.bdate_range(end="2026-10-16", periods=n))


def test_compute_basic():
    closes = [12.0] * 260 + [11.0, 10.4]
    m = compute("002255", _bars(closes), _bench(262, -1.0), "CSI 300")
    assert m.close == 10.4 and m.prev_close == 11.0
    assert m.chg_1d == pytest.approx((10.4 / 11 - 1) * 100)
    assert m.bench_1d == pytest.approx(-1.0)
    assert m.new_52w_low  # 10.4 < previous low 11.0
    assert m.vs_limit(10.5) == pytest.approx((10.4 / 10.5 - 1) * 100)


def test_rsi_bounds():
    up = pd.Series(range(1, 40), dtype=float)
    assert rsi(up) == 100.0
    assert rsi(pd.Series([1.0, 2.0])) is None


def test_evaluate_limit_cross_and_once_per_bar():
    s = _stock()
    m = compute("002255", _bars([12.0] * 260 + [11.0, 10.4]), _bench(262, -0.5), "CSI 300")
    last_eval, last_signal = {}, {}
    sigs = evaluate([s], {s.ticker: m}, last_eval, last_signal)
    assert len(sigs) == 1
    assert set(sigs[0].kinds) == {"limit_cross", "big_move", "new_low"}
    assert sigs[0].attribution == "stock-specific"
    # Same bar again: nothing.
    assert evaluate([s], {s.ticker: m}, last_eval, last_signal) == []


def test_new_low_cooldown():
    s = _stock(limit=None)
    m = compute("002255", _bars([12.0] * 260 + [11.9, 11.8]), _bench(262), "CSI 300")
    last_signal = {"002255:new_low": "2026-10-10"}
    sigs = evaluate([s], {s.ticker: m}, {}, last_signal)
    assert sigs == []  # small move, low already flagged 6 days ago


def test_attribution():
    m = compute("002255", _bars([10.0] * 30 + [9.4]), _bench(31, -5.0), "CSI 300")
    assert attribute(m, peer_median_1d=-5.5) == "market-wide"
    m2 = compute("002255", _bars([10.0] * 30 + [9.4]), _bench(31, -1.0), "CSI 300")
    assert attribute(m2, peer_median_1d=-5.5) == "sector-wide"
    assert attribute(m2, peer_median_1d=-0.5) == "stock-specific"


# ── Schedule ─────────────────────────────────────────────────────────────────

def _utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_schedule_close_weekdays_once():
    fri = _utc(2026, 10, 16, 9, 0)
    assert "close" in schedule.due_tasks(fri, {})
    assert "close" not in schedule.due_tasks(fri, {"close": "2026-10-16"})
    assert "close" not in schedule.due_tasks(_utc(2026, 10, 16, 8, 30), {})
    assert "close" not in schedule.due_tasks(_utc(2026, 10, 17, 9, 0), {})  # Saturday


def test_schedule_filings_every_two_hours():
    now = _utc(2026, 10, 16, 12, 0)
    assert "filings" in schedule.due_tasks(now, {"filings": "2026-10-16T09:59+00:00"})
    assert "filings" not in schedule.due_tasks(now, {"filings": "2026-10-16T10:30+00:00"})
    assert "filings" not in schedule.due_tasks(_utc(2026, 10, 16, 23, 0), {})


def test_schedule_weekly_saturday_once():
    sat = _utc(2026, 10, 17, 8, 15)
    assert "weekly" in schedule.due_tasks(sat, {})
    assert "weekly" not in schedule.due_tasks(sat, {"weekly": schedule.week_key(sat)})


# ── Telegram rendering ───────────────────────────────────────────────────────

def test_render_escapes_and_formats():
    out = telegram.render("**CATL** <b>x</b> & [link](https://a.com/?a=1&b=2) `600111`")
    assert out.startswith("<b>CATL</b> &lt;b&gt;x&lt;/b&gt; &amp; ")
    assert '<a href="https://a.com/?a=1&amp;b=2">link</a>' in out
    assert "<code>600111</code>" in out


def test_chunk_respects_limit():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    parts = telegram.chunk(text, limit=1000)
    assert all(len(p) <= 1000 for p in parts)
    assert "\n".join(parts) == text


# ── News filter / lookup ─────────────────────────────────────────────────────

def _article(title, published="2026-10-15 10:00"):
    return Article(id="x", query="", title=title, snippet="", source="", published=published, url="")


def test_news_junk_filter():
    now = datetime(2026, 10, 17)
    assert relevant(_article("北方稀土：预计前三季度净利润同比增长135%至139%"), ["北方稀土"], 7, now)
    assert not relevant(_article("短线防风险 188只个股短期均线现死叉"), ["海陆重工"], 7, now)
    assert not relevant(_article("12.37亿主力资金净流入，北方稀土涨2.88%"), ["北方稀土"], 7, now)
    assert not relevant(_article("北方稀土：业绩说明会", "2026-09-01 10:00"), ["北方稀土"], 7, now)


def test_find_stock():
    stocks = [_stock(), _stock(ticker="03750", name_en="CATL (H)", short_en="CATL-H", short_zh="宁德时代", exchange="HKEX")]
    assert find(stocks, "hailu").ticker == "002255"
    assert find(stocks, "3750").ticker == "03750"
    assert find(stocks, "catl").ticker == "03750"
    assert find(stocks, "宁德时代").ticker == "03750"
    assert find(stocks, "nope") is None


def test_chunk_never_splits_pre_block():
    table = "<pre>" + "\n".join(f"row {i:02d} " + "y" * 30 for i in range(30)) + "</pre>"
    text = "\n".join(["<b>Brief</b>"] + [f"para {i} " + "x" * 80 for i in range(30)] + [table, "<i>legend</i>", "<i>footer</i>"])
    parts = telegram.chunk(text, limit=1500)
    assert all(len(p) <= 1500 for p in parts)
    for p in parts:
        assert p.count("<pre>") == p.count("</pre>")
    assert "\n".join(parts) == text


def test_schedule_sector_sunday_once():
    sun = _utc(2026, 10, 18, 8, 30)
    assert "sector" in schedule.due_tasks(sun, {})
    assert "sector" not in schedule.due_tasks(sun, {"sector": schedule.week_key(sun)})
    assert "sector" not in schedule.due_tasks(_utc(2026, 10, 17, 9, 0), {})  # Saturday is the watchlist brief


def test_news_drops_etf_promos():
    now = datetime(2026, 10, 17)
    assert not relevant(_article("光伏ETF富国(560230)跟踪指数中证光伏产业指数上涨1.80%"), ["光伏"], 7, now)
    assert relevant(_article("多晶硅库存继续累积 减产执行落地影响后续供需"), ["多晶硅"], 7, now)
