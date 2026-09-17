# fareloot

Catches abnormally cheap round-trip flights from airports reachable from Kerala
(Kochi, Trivandrum, or an overnight train to Bangalore/Chennai/etc.), and pushes
a Telegram alert when one crashes below its normal price.

A price is only ever compared against that specific route's own history, and a
cheap outbound with a gouging return never counts as a deal - see
[CLAUDE.md](CLAUDE.md) for how the scoring works.

## Setup

```bash
uv sync
uv run fareloot init
```

This writes `config/watchlist.yaml` (your reachable airports and destinations
to watch) and creates the local SQLite database.

### Try it with no credentials

```bash
uv run fareloot poll --provider mock --notify console
```

Runs the full pipeline against fake but deterministic data, so you can see the
scoring and tiers work before signing up for anything.

### Get real data

1. Register at [travelpayouts.com](https://www.travelpayouts.com/programs/100/tools/api)
   and grab a free API token.
2. Copy `.env.example` to `.env` and set `FARELOOT_TP_TOKEN`.
3. Bootstrap baselines instantly instead of waiting weeks for history to build up:

   ```bash
   uv run fareloot seed
   uv run fareloot deals
   ```

### Get alerts on Telegram

1. Message [@BotFather](https://t.me/BotFather) on Telegram, `/newbot`, and
   copy the token it gives you.
2. Send your new bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and read your
   chat id out of the JSON.
3. Add both to `.env` as `FARELOOT_TELEGRAM_BOT_TOKEN` and `FARELOOT_TELEGRAM_CHAT_ID`.
4. `uv run fareloot test-alert` - confirm it reaches your phone.

## Running it continuously

`.github/workflows/watch.yml` polls hourly on GitHub Actions for free (the repo
needs to be public for cron to run on the free plan). Push this repo to GitHub,
add `FARELOOT_TP_TOKEN`, `FARELOOT_TELEGRAM_BOT_TOKEN`, and
`FARELOOT_TELEGRAM_CHAT_ID` as repository secrets, then trigger the workflow
once manually (Actions tab -> watch -> Run workflow) to confirm it works.

Price history is kept on a separate `fareloot-data` branch rather than in the
commit history of `main`, so the repo doesn't grow forever.

## Commands

| Command | What it does |
|---|---|
| `fareloot init` | Create `config/watchlist.yaml` and the database |
| `fareloot seed` | Bootstrap baselines with a month of prices per route |
| `fareloot poll` | Fetch fresh prices, score them, alert on anything hot |
| `fareloot deals` | Show currently scored deals, no new API calls |
| `fareloot test-alert` | Send a test message to Telegram |

Run any command with `--help` for its options.

## Development

```bash
uv run pytest
uv run ruff check .
```
