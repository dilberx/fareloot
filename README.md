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

`.github/workflows/watch.yml` runs `fareloot poll` on GitHub Actions for free.
Add these as repository secrets (Settings -> Secrets and variables -> Actions),
then trigger it once manually from the Actions tab to confirm it works:

| Secret | Purpose |
|---|---|
| `FARELOOT_TP_TOKEN` | Travelpayouts API token. Without it the poll exits immediately. |
| `FARELOOT_TELEGRAM_BOT_TOKEN` | Your @BotFather bot token. |
| `FARELOOT_TELEGRAM_CHAT_ID` | Where alerts get sent. |
| `FARELOOT_WATCHLIST_YAML` | Optional. The full contents of your `config/watchlist.yaml`. |

`config/watchlist.yaml` is gitignored, so your home airports and destination
wishlist stay off a public repo. The workflow writes the file from
`FARELOOT_WATCHLIST_YAML` before polling. With that secret unset it falls back to
the built-in defaults and says so in the run log. To update your watchlist in the
deployed watcher, paste the edited file into that secret.

Price history is kept on a separate `fareloot-data` branch, not in `main`'s
commit history, so the repo doesn't grow forever.

A route sends no alerts until it has price history. To build it quickly, run the
workflow manually with mode **seed** (Actions -> watch -> Run workflow), or:

```bash
gh workflow run watch.yml -f mode=seed
```

To check the Telegram secrets without waiting for a real deal, send one test
message:

```bash
gh workflow run watch.yml -f mode=test-alert
```

Each seed run covers the next slice of routes within the API budget and logs how
many runs remain. Repeat until it reports that every route has been seeded,
about 8 runs with the default watchlist. Scheduled runs keep adding history on
their own either way.

Two things about this setup are worth knowing. Scheduled runs are best-effort:
observed cadence is closer to every 7 or 8 hours than the hourly cron requests,
because GitHub heavily throttles `schedule` on free public repos. And the repo
has to stay public, because branch protection on a private repo requires GitHub
Pro. Actions minutes are not the constraint either way, since a month of polling
costs about 60 of the 2,000 free private minutes.

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
