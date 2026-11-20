# Avito Hunter

> 💡 **Экономика парсинга**: себестоимость **1 поискового запроса с обновлением 1 раз в секунду составляет всего ~15 рублей в месяц**.
> Для работы не требуются дорогие мобильные или резидентские прокси — можно использовать обычные датацентровые IPv4 прокси (например, от **Webshare**).

# Avito ToS forbids bots/crawlers. This is a personal, low-frequency tool:
# no phone/name/address collection, no resale, home IP, one request at a time.
# You accept the remaining account/IP risk.

## Setup

```bash
cd ~/Projects/avito-hunter
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chrome   # or: playwright install chromium
cp .env.example .env
```

Fill `.env`:

- `TELEGRAM_BOT_TOKEN` from @BotFather
- `TELEGRAM_CHAT_ID` — write the bot first, then get your id (`@userinfobot` or similar)

VPN: use it **only** for Telegram. Keep Chrome/Avito on the home ISP (split tunnel). A system-wide VPN is worse for access, not better.

## Watchlist (several products at once)

Edit `config/watchlists/headphones.yaml` and `laptops.yaml`. Each row is one search:

- `max_price` — hard cap for this model. Above it the listing is skipped (`above_max_price`). This is a cutoff, not a sort: radar still uses date-sorted URLs.
- `global_max_price` in `config/settings.yaml` — optional second cap on top of every model (leave `null` to disable).
- `search_urls` — copy the URL from the browser **after** filters (city/delivery, private sellers, sort by **date**, max price slider).
- `poll_minutes` — how often this row is fetched. Hot items more often; the daily request budget is shared.

Several products = several watchlist rows in **one** process, one Chrome profile. Headphones and laptops run together.

Placeholder `PASTE_AVITO_SEARCH_URL` is ignored so a first launch does not hit the site.

## Run

First launch: log into a **separate** Avito account once in the Chrome window that opens. The profile is stored in `data/browser_profile/`. Do not use your main account.

```bash
hunter status          # watchlists, budget, telegram
hunter run             # collector + Round 2 worker (visible Chrome)
hunter run --dash      # plus dashboard at http://127.0.0.1:8765
hunter explain <id>    # inspect listing events and evaluations
hunter round2 <id>     # run full Round 2 (Vision + OCR + LLM Judge) on listing
hunter search "текст"  # full-text FTS5 search across listings
hunter sync-catalog    # sync YAML catalogs into SQLite products table
hunter eval --title "Sony XM5" --price 17000 --watch sony_xm5
hunter backfill        # re-run Round 1 on saved HTML
hunter pause | resume  # pause/resume collector loop
```

`notify_mode` in `config/settings.yaml`:

- `new` — Telegram alerts for listings passing hard filters (cold start)
- `deals` — only after enough market stats (expected value / discount alerts)
- `silent` — collect only

Default cold start: collect ~7 days, then switch to `deals` if you want fewer pings.

Telegram cards include the watchlist id, price cap, draft message, inspection checklist, and buttons: норм / фейк / дорого / не та модель.

Request budget: 600/day, 20–90s between requests, 45 min pause on 429/403. Captcha: solve it in the open window, then `hunter resume`.

## Round 2 & Vision Pipeline

Round 2 runs in a background async queue without blocking the Playwright collector:

- **LLM Router**: GigaChat (free cloud 365M tokens/yr for individuals), fallback to local Ollama (`qwen3:8b`), fallback to OpenRouter.
- **Vision**: Downloads listing photos (compressed to 1024px), computes pHash and checks for duplicate photos across sellers, runs Tesseract OCR for serial numbers and battery stats, calls VLM for physical defects.
- **Judge**: Assembles compact dossier (market stats, seller profile, defects, OCR findings) and returns structured verdict (`buy_now`, `ask_questions`, `watch`, `skip`), tailored seller questions, in-person inspection checklist, and maximum bargain price.

## systemd (user)

```bash
mkdir -p ~/.config/systemd/user
cp scripts/hunter.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hunter.service
```

Keep the laptop plugged in and do not let it sleep while the collector runs.

