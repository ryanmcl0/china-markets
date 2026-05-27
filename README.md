# China Energy Market Monitor

Self-hosted personal market monitor for a Chinese stock watchlist. Polls
AKShare (Eastmoney/HK news), Reddit, and RSS feeds, scores each story with a
local LLM, and delivers actionable Telegram alerts — every notification
answers: **what happened, does it matter, and what should I do?**

Everything runs in a **single Docker container** on a NAS or home server.
No cloud APIs, no subscriptions, no data leaving your machine except Telegram delivery.

---

## How it works

```
AKShare (Eastmoney/HK) + Reddit + RSS
              │
        queue.Queue  ←  in-process dedup + dissemination scoring
              │
     Qwen2.5:7B via Ollama  ←  translate · score · summarise · recommend
              │
          SQLite  ←  history for /summary and /watchlist
              │
     Telegram bot  →  you
```

Four threads run inside one container: **poller**, **consumer**, **digest**, **bot**. Ollama runs as a subprocess on `localhost:11434`. No Redis, no Postgres, no inter-container networking.

---

## Requirements

- Docker + Docker Compose
- ~4 GB RAM free (Qwen2.5:3B Q4 needs ~2 GB)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Optional: Reddit API credentials (free) for Reddit polling

Tested on a UGreen DXP4800 Plus (Intel N100, 8 GB DDR5). Any x86 machine with enough RAM works.

---

## Quick start

### 1. Clone and configure

```bash
git clone <repo-url>
cd china-markets

cp .env.example .env
# Edit .env — add your Telegram bot token and chat ID
```

### 2. Create your config

```bash
cp config.yaml.example config.yaml
# Edit config.yaml — add your watchlist, GTC limits, investor profile
```

### 3. Build and start

```bash
docker compose up -d --build cn-market-monitor
docker compose logs -f cn-market-monitor
```

On first start the container pulls the LLM model (~2 GB). This takes a few minutes and only happens once — the model is cached in `./data/ollama/`.

When you see `telegram bot reachable` in the logs, send `/help` to your bot to confirm everything is working.

---

## Configuration

All behaviour is controlled by `config.yaml` (gitignored — never committed). Key sections:

```yaml
watchlist:
  - ticker: "002255"
    name_en: "Suzhou Hailu Heavy Industry"
    exchange: "XSHE"          # XSHE / XSHG / HKEX
    currency: "CNY"
    gtc_limit: 10.50          # your good-till-cancelled buy limit
    portfolio_weight: 0.15    # used in LLM context
    status: "watching"        # watching / holding / exited
    thesis: "..."             # injected into every LLM prompt for this stock

investor:
  type: "long-term retail investor"
  country: "UK"
  horizon: "5-10 years"
  bias: "buy dips, do not chase spikes"
  risk_tolerance: "medium-high"

notifications:
  score_immediate: 9      # alert any time, day or night
  score_market_hours: 7   # alert during market hours only
  score_digest_only: 5    # included in /summary only
  score_discard: 4        # stored in DB, never notified
  daily_digest_time: "08:00"

llm:
  model: "qwen2.5:7b-instruct-q4_K_M"
  temperature: 0.2
  max_tokens: 1024
```

Changes to `config.yaml` take effect on `docker compose restart cn-market-monitor`. Most day-to-day changes (limits, positions, adding tickers) can be made by chatting to the bot directly.

---

## Telegram bot commands

| Command | What it does |
|---|---|
| `/status` | Queue depth, posts today, last poll time per source |
| `/summary` | All posts scored ≥ digest threshold in last 24h |
| `/watchlist` | All tickers, limits, positions, last signal per ticker |
| `/pause 2h` | Silence notifications for N hours (`2h`, `30m`) |
| `/add TICKER name limit` | Add a ticker to the watchlist |
| `/updatelimit TICKER LIMIT` | Update a GTC limit |
| `/acted TICKER ACTION notes` | Log that you acted on a recommendation |

Any message without a `/` prefix is routed to the LLM with your full portfolio context preloaded — ask questions or record trades in plain English:

```
"CATL just announced solid-state batteries — what does that mean for my position?"
"Just bought 200 shares of 002255 at ¥10.82"
"Raise my CATL limit to HK$645"
```

Config changes and position updates are confirmed before writing, and a timestamped backup is created in `./data/config_backups/` before every write.

---

## Notification format

```
🔴 HIGH SIGNAL — eastmoney | Score: 9/10 | 2 source(s) in 2hrs

📊 STOCK: $002255 Suzhou Hailu Heavy Industry
💬 SENTIMENT: 🟢 Bullish | 🏷️ CONTRACT NEWS
📡 DISSEMINATION: Spreading across sources

📈 TECHNICAL: RSI 38.4 (oversold) | −4.1% vs MA20 (below)
🔀 DIVERGENCE: sentiment_leads_price — bullish news, price flat

📝 ANALYST
New procurement contract confirmed for TMSR-LF1 Phase 2 heat
exchanger system. Credibility: High. New information: Yes.

━━━━━━━━━━━━━━━━━━━━
⚡ PORTFOLIO MANAGER
━━━━━━━━━━━━━━━━━━━━
🟢 ADD TO POSITION
Confidence: High | Horizon: Weeks | Urgency: HIGH

Direct thesis confirmation. RSI oversold + price below MA20
is classic accumulation setup. GTC limit ¥10.50 is within range.

⚠️ Rules: No spike — price flat. Thesis confirmed.
━━━━━━━━━━━━━━━━━━━━

⚠️ AI-generated. Not financial advice. Your call.
```

---

## Repository layout

```
.
├── Dockerfile               # Python 3.12 + Ollama binary
├── entrypoint.sh            # starts ollama, pulls model if needed, starts app
├── docker-compose.yml       # single service: cn-market-monitor
├── requirements.txt         # all Python dependencies
├── config.yaml.example      # template — copy to config.yaml and edit
├── .env.example             # template — copy to .env and add credentials
│
├── poller/
│   ├── akshare_poller.py    # AKShare news + RSI/MA20 technical context
│   ├── reddit_poller.py     # Reddit via PRAW (optional)
│   ├── rss_poller.py        # RSS feeds via feedparser (optional)
│   └── post_queue.py        # in-process queue, dedup, caches, bot session state
│
├── processor/
│   ├── worker.py            # entry point — starts all four threads
│   ├── llm.py               # Ollama prompt builder + caller
│   ├── notifier.py          # Telegram formatting + send
│   ├── bot.py               # slash commands + conversational handler
│   ├── intent.py            # classifies free-text messages before routing
│   ├── config_writer.py     # safe config.yaml writes with backup
│   └── db.py                # SQLite schema + queries
│
└── data/                    # volume-mounted, gitignored
    ├── china.db             # SQLite — post history, notifications, actions
    ├── ollama/              # model weights (~4.5 GB)
    └── config_backups/      # timestamped backups before every bot config write
```

---

## LLM choice

**Qwen2.5:7B** is the right default for Chinese financial content — trained natively on Chinese text, understands stock codes and company names in both characters and pinyin. Llama or Mistral models are significantly weaker on Chinese news.

| Model | RAM | Fits 8 GB? | Quality |
|---|---|---|---|
| `qwen2.5:7b-instruct-q4_K_M` | ~4.5 GB | ✅ comfortable | very good |
| `qwen2.5:7b-instruct-q3_K_M` | ~3.5 GB | ✅ headroom | good |
| `qwen2.5:14b` | ~10–12 GB | ❌ needs 16 GB | excellent |

To upgrade: change `llm.model` in `config.yaml`, then `docker compose restart cn-market-monitor`. The new model is pulled automatically on next start.

---

## Day-to-day commands

```bash
docker compose logs -f cn-market-monitor     # live logs
docker compose restart cn-market-monitor     # after config.yaml changes
docker compose up -d --build cn-market-monitor  # after code changes
docker compose exec cn-market-monitor sqlite3 /app/data/china.db ".tables"
```

---

## Adapting to a different sector

The watchlist, investor profile, and investing rules in `config.yaml` are plain text injected into every LLM prompt. To monitor a different market or sector, update those fields — no code changes required.
