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

### Live-checking alerts

Travelpayouts prices come from a search cache and can be hours old. With a free
[SerpApi](https://serpapi.com) key in `FARELOOT_SERPAPI_KEY`, each alert is
priced live on Google Flights first. Within 10% of the cached price, it is sent
with the live price. More than 10% higher, it is dropped as stale. Anything else
(no results, an error, or the monthly cap of 200 searches reached) still sends,
labelled as not live-checked. Check the key with
`gh workflow run watch.yml -f mode=live-check`, which prices one route as a round
trip and one way and reports whether the round-trip figure covers both legs.

### Routes you fly often

Normally a route alerts only on a big drop: 25% and Rs 3,000 below its usual
price. For destinations you fly regularly, a `lowest_fare_rules` entry in your
watchlist also alerts when a round trip is the cheapest seen in the last N days,
with looser minimums. See the commented example in the template written by
`fareloot init`. The return leg still has to be checked and healthy, so those
destinations must be in `destinations:`. Each route needs 20 prices across at
least 7 days before it can fire, and it alerts at most once per route per run,
then again only on a 10% further drop or after the 48-hour cooldown.

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
| `FARELOOT_SERPAPI_KEY` | Optional. Live-checks each alert on Google Flights before sending (free tier: 250 searches/month). |
| `FARELOOT_DATA_KEY` | Encrypts the stored price history. Required; runs refuse to start without it. |

`config/watchlist.yaml` is gitignored, so your home airports and destination
wishlist stay off a public repo. The workflow writes the file from
`FARELOOT_WATCHLIST_YAML` before polling. With that secret unset it falls back to
the built-in defaults and says so in the run log. To update your watchlist in the
deployed watcher, paste the edited file into that secret.

Price history is kept on a separate `fareloot-data` branch, not in `main`'s
commit history, so the repo doesn't grow forever.

That branch is public, and the database records every route polled, which is
enough to rebuild your watchlist. So it is stored encrypted as `fareloot.db.enc`,
using `FARELOOT_DATA_KEY`. Generate the key once in PowerShell and keep a copy in a
password manager. GitHub never shows a secret again, and without the key the
stored history cannot be read or restored:

```powershell
$k = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)); $k | Set-Clipboard; $k | gh secret set FARELOOT_DATA_KEY
```

To inspect the history locally, with `FARELOOT_DATA_KEY` set in your shell:

```bash
git fetch origin fareloot-data
git show origin/fareloot-data:fareloot.db.enc > fareloot.db.enc
openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -pass env:FARELOOT_DATA_KEY -in fareloot.db.enc -out data/fareloot.db
```

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

`.github/workflows/keepalive.yml` checks once a day that this keeps working, and
messages you on Telegram if it won't:

- **From day 45 without a commit to `main`.** GitHub disables scheduled workflows
  in a public repo after 60 days without activity, and only commits to the
  default branch appear to count. `main` is protected, so nothing automated can
  commit there. Merging any PR resets the clock, even a one-line README edit.
- **When the watcher hasn't succeeded for 36 hours,** for whatever reason.
- **When the watch workflow is disabled.** The keepalive also re-enables it.

Run it any time with `gh workflow run keepalive.yml`, or locally:
`uv run fareloot health --last-main-commit 2026-10-01T00:00:00Z`.

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
