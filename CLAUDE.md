# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                 # install deps
uv run fareloot init    # create config/watchlist.yaml and data/fareloot.db
uv run fareloot seed --provider mock   # bootstrap baselines (swap mock for travelpayouts once you have a token)
uv run fareloot poll --provider mock --notify console   # full pipeline, no credentials needed
uv run pytest           # full suite
uv run pytest tests/test_scoring.py::test_expensive_return_leg_blocks_the_push  # a single test
uv run ruff check .
```

`--provider mock` works everywhere without a `.env` file and is the fastest way to exercise a
change. Real runs need `FARELOOT_TP_TOKEN` (Travelpayouts) in `.env`; Telegram alerts additionally
need `FARELOOT_TELEGRAM_BOT_TOKEN` and `FARELOOT_TELEGRAM_CHAT_ID`.

## Architecture

The pipeline is: **provider → store → score → watcher → notify**, and each stage is a separate,
independently testable module. `watcher.py` is pure wiring; all judgement calls live in
`scoring.py` and `baseline.py`.

### The core idea a reader needs before anything else makes sense

A price is only ever judged against **that specific route's own history** (median + MAD, not
mean/stdev, since fare distributions are skewed) — there is no absolute "cheap" threshold anywhere
in the codebase. And **the unit of a deal is a round trip, not a leg**: a crashed outbound with a
gouging return is explicitly not a deal. Both of these are enforced in `scoring.score()`, and
`tests/test_scoring.py`'s first two tests exist specifically to pin that behavior down.

### Where the return-leg check actually gets its data (the non-obvious part)

Travelpayouts prices a round trip as a single total, never itemised legs, so "is the return leg
overpriced" can't be read off that number directly. `watcher.poll()` separately fetches the
*reverse-direction one-way* price (destination → origin) for every wishlist route, and
`_return_leg_z()` scores that against **its own** one-way history (also keyed by route in
`store.py`, with `trip_length IS NULL` distinguishing it from round-trip history). `seed()` has to
populate that reverse one-way baseline too, not just the forward direction — `MockProvider` and
`fareloot seed` both do this deliberately; forgetting the reverse seed silently disables the
return-leg gate (it did, once, during development — see the git history around the fix).

### Hotness scoring (`scoring.py`)

A weighted blend of five components (depth below baseline, rarity, price-per-km value, absolute
rupees saved, reach penalty) is multiplied by three gates that can only ever pull the score *down*:
`return_health` (the trap veto above), `confidence` (discounts thin history so a 3-sample baseline
can't manufacture a jackpot), and `staleness` (discounts quotes near `expires_at`). Weights and gate
constants live at the top of the file as named constants, not magic numbers inline.

### Two hunting arms, one promotion path (`watcher.poll`)

- **Wishlist**: named destinations in `config/watchlist.yaml`, priced as proper round trips.
- **Discovery**: broad, cheap-in-calls scanning (`city_directions`, `latest`) across all
  destinations, to catch places nobody thought to list.

Anything discovery finds above `DEAL` tier gets written to the `discovered` table and is folded
into the wishlist arm on the *next* poll (`store.promoted()`), so nothing pushes to Telegram off a
single unconfirmed discovery quote.

### The origin isn't a fixed airport (`journey.py`, `config.Watchlist`)

`config/watchlist.yaml` lists every airport reachable from Kerala with what it costs to get there
(train fare, hours, mode). Every quote is ranked by `flight + 2×reach.cost_inr +
2×hours×train_hour_value_inr` — the time penalty exists purely to keep ordering honest (a ₹400
saving shouldn't win against a 28-hour round trip on rail) and is never shown as real money.

### Dedup is calendar-day, not timestamp (`store.py`)

`observations` dedups on `(route, price, observed_date)` — the *date*, not the full timestamp.
Two provider calls in the same poll stamp microsecond-different `observed_at` values, so keying on
the raw timestamp would never catch anything; this was a real bug caught by
`test_duplicate_observation_not_double_counted`.

### Provider abstraction (`providers/`)

`Provider` (a `Protocol` in `base.py`) has exactly four methods. `TravelpayoutsProvider` and
`MockProvider` both implement it; nothing else in the codebase imports either concretely except
`cli.py`. `MockProvider` isn't a test double bolted on afterward — it's a deterministic fake with
two scripted routes (a genuine crash and a return-leg trap) that the entire pipeline can be
exercised against with zero credentials, and it's what most of the test suite and manual smoke
testing runs against.

### Deployment

`.github/workflows/watch.yml` runs `fareloot poll` hourly via GitHub Actions cron (free only on
**public** repos on the free plan — see README for why). Price history persists across runs on an
orphan `fareloot-data` branch (force-pushed each run) rather than in `main`'s commit history, to
keep the repo from growing forever with a binary SQLite file.
