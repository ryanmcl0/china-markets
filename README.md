# China Energy Watchlist Monitor

Telegram alerts and a weekly brief for a watchlist of Chinese energy-transition
stocks (thorium/nuclear supply chain, solar, wind, storage, grid, rare earths).
Runs on GitHub Actions on a schedule — or, if you prefer, in a container on a NAS.

Python gathers the facts — prices, official exchange filings, Chinese news —
and decides when something is worth a message. Headless Claude Code (Sonnet 5.5,
on a Claude subscription) reads them, searches the web for the *why*, and writes
the message. Numbers in the scoreboards come from the data, not the model.

## What you get

| Message | When | What's in it |
|---|---|---|
| **China close** | Weekdays after the HK close (~09:45 UK), only if something triggered | Limit crossings, ±5% days, new 52-week lows, volume spikes — each attributed as market-wide, sector-wide or stock-specific, with the likely cause and what it means for an entry |
| **New filing** | Within ~2 hours of publication | Material CNINFO filings (earnings pre-announcements, buybacks, insider buying/selling, placements, contracts, regulatory action), translated and read from the PDF |
| **Weekly brief** | Saturday ~09:00 UK | What drove the market, what changed per company, an entry radar against your limits, thesis check (incl. thorium/TMSR news), next week's events, scoreboard |
| **Sector update** | Sunday ~09:00 UK | The whole China nuclear & renewables market, not just the watchlist: approvals and installations, solar/wind/storage prices, grid build-out, NEA/NDRC policy, with sources — plus a line on what it means for the watchlist |
| **Replies** | Within ~20 minutes of messaging the bot | Commands below, or any free-text question |

Telegram commands: `/now` scoreboard · `/stock hailu` one stock in detail ·
`/filings [name]` material filings, 14 days · `/brief` weekly brief now ·
`/sector` sector update now ·
`/limit hailu 7.0` change a buy limit · `/pause 4h` / `/resume` · `/status` · `/help`.

## Why it's built this way

The first version (local Qwen 3B on the NAS, scoring every news item) sent 67
alerts in three months; 66 were price drops forced to "10/10", and real news —
Zhefu's +121–156% H1 profit guidance, CATL's ¥20bn buyback — scored 1/10 and
was dropped. Two causes:

- **Input**: Eastmoney searched *by ticker code* returns mostly screener spam
  ("188 stocks form a death cross"). Now: official filings from CNINFO,
  classified by rule (`monitor/filings.py`), plus news searched by company
  name with spam filtered out (`monitor/news.py`).
- **Reasoning**: a 3B model can't explain anything. Now: deterministic signals
  decide *whether* to message (`monitor/signals.py`); Claude explains *why*.

## Layout

```
monitor/
  __main__.py   plan / run / serve / context
  schedule.py   which tasks are due (stdlib only — runs before deps install)
  state.py      JSON state: what's been alerted, last bars seen, pause, /limit overrides
  telegram.py   Bot API via urllib; Markdown→Telegram HTML; chunking
  config.py     config.yaml → Stock objects
  market.py     Sina daily prices (A-shares, HK, CSI 300, Hang Seng) → metrics
  filings.py    CNINFO filings + rule-based classifier + PDF text
  news.py       Eastmoney news by company name / theme, spam filter
  signals.py    price signals + market/sector/stock attribution
  report.py     scoreboard, fallbacks, the data bundle Claude reads
  claude.py     runs `claude -p` (web search/fetch only)
  jobs.py       close, filings, weekly, sector, inbox
prompts/        what Claude is asked to write, per message type
.github/workflows/monitor.yml
Dockerfile, docker-compose.yml   NAS option
```

## Setup — GitHub Actions

Repository secrets:

| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | from @BotFather |
| `TELEGRAM_CHAT_ID` | your chat ID(s), comma-separated |
| `MONITOR_CONFIG` | the whole of your `config.yaml` |
| `CLAUDE_CODE_OAUTH_TOKEN` | from `claude setup-token` (Pro/Max subscription) |

```bash
cp config.yaml.example config.yaml        # edit your watchlist
gh secret set MONITOR_CONFIG < config.yaml
claude setup-token                        # copy the token it prints, then:
gh secret set CLAUDE_CODE_OAUTH_TOKEN
gh workflow run monitor -f task=weekly    # try it
```

Without `CLAUDE_CODE_OAUTH_TOKEN` everything still runs; messages are just the
facts without the analysis.

**Changing the watchlist or sector topics** (`watchlist`, `themes`, `sector_themes`): edit `config.yaml`, then
`gh secret set MONITOR_CONFIG < config.yaml`. Buy limits can also be changed
from Telegram with `/limit` (stored in the bot's state).

**The repo is public**, so run logs are public. The code never logs message
content, config or state, and `--dry-run` (which prints messages) must not be
used in the workflow.

## Setup — NAS instead

```bash
cp .env.example .env                      # Telegram + CLAUDE_CODE_OAUTH_TOKEN
cp config.yaml.example config.yaml
gh variable set MONITOR_RUNNER --body nas # pause the Actions copy first
docker compose up -d --build
```

Switch back with `docker compose down` and `gh variable delete MONITOR_RUNNER`.
Never run both: alerts would be sent twice and they'd race for Telegram messages.

## Local development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/pytest -q
.venv/bin/python -m monitor run weekly --dry-run   # prints instead of sending; uses your local Claude login
.venv/bin/python -m monitor context weekly         # the data bundle Claude sees
MONITOR_NO_CLAUDE=1 .venv/bin/python -m monitor run close --dry-run   # facts-only fallback
```

Data: [AKShare](https://github.com/akfamily/akshare) → Sina (prices), CNINFO
(filings), Eastmoney (news). All three work from GitHub's US runners.
