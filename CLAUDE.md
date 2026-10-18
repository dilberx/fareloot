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

### Round trips pool by trip band (`config.Watchlist.trip_bands`)

Real fare data arrives in dozens of trip lengths, including 0-2 day "round trips" nobody takes. A
round trip is kept only if its length falls inside a configured band (default 3-6, 7-10, 11-21
days), and its baseline is every observation in that band (`Store.route_history(..., band=...)`),
not its exact length. `watcher.usable_quotes` drops out-of-band quotes before they are recorded or
scored. Without it, same-day returns get their own cheap-looking baselines and can alert. It also
drops past departures: cached prices live for days, so a fare for a flight that has already left
can still come back from the provider.

### Lowest-fare rules are a second way in, not a change to scoring

`Watchlist.lowest_fare_rules` lets chosen destinations alert when a round trip is the cheapest in
a window (default 30 days), with looser floors. It lives in `watcher._apply_lowest_rule`, after
`scoring.score()`, and only ever promotes a deal to STEAL; it never demotes or touches the gates.
It requires a *verified* healthy return (`gates["return_health"] == 1.0`, so discovery quotes with
no reverse fetch can't qualify), at least `min_history` prices spanning `min_history_days`, and a
price strictly below `Store.route_low(..., before=run_started)`, which excludes the current run's
own rows. Its cooldown is per route (`alert_key` returns `LOWEST_KEY`), and `poll` keeps only the
cheapest qualifying deal per route per run; keyed per date, every date dipping under the old low
would alert separately. Each of these is pinned by a test in `tests/test_lowest_fare.py`.

### Live check before sending (`confirm.py`)

With `FARELOOT_SERPAPI_KEY` set, the CLI prices each alert on Google Flights via SerpApi just
before sending. Confirmed (within `STALE_TOLERANCE`, 10%) sends with the live price; stale drops
it and records it with `record_alerts` so the dead cache entry cools down instead of spending a
search every run; anything else (no key, error, no results, monthly cap in `meta`
`live_checks:YYYY-MM`) sends labelled unconfirmed. A checker fault must never cost a real deal.
The key is a URL query parameter, so request exceptions are re-raised without the URL, as in
`notify/telegram.py`. `fareloot live-check` verified on live data that a round-trip `price` is
the total for both legs (round trip / one way = 1.88 on COK-DXB). Confirmed alerts also carry
Google's `price_insights` verdict (level and typical range), shown but never used as a gate.

`fareloot analyze` (workflow mode `analyze`) reports where history comes from: yield per origin,
wishlist destinations bucketed by usable round trips, lowest-fare readiness. The public part names
no destination; the per-destination detail goes to Telegram.

Every poll prints a closeness line (`cli._closeness`): fares scored per tier and the best hotness.
It exists so a quiet run reads as quiet, not broken, and it never names routes.

### Hotness scoring (`scoring.py`)

A weighted blend of five components (depth below baseline, rarity, price-per-km value, absolute
rupees saved, reach penalty) is multiplied by three gates that can only ever pull the score *down*:
`return_health` (the trap veto above; a return is a trap above `RETURN_UNHEALTHY_Z` = 2.0, measured: 1.0 wrongly blocked ~15% of healthy returns), `confidence` (discounts thin history so a 3-sample baseline
can't manufacture a jackpot), and `staleness` (discounts quotes near `expires_at`). Weights and gate
constants live at the top of the file as named constants, not magic numbers inline.

### Travelpayouts endpoints (`providers/travelpayouts.py`)

`cheap()` calls `/aviasales/v3/prices_for_dates`, which Travelpayouts recommends over the deprecated
`/v1/prices/cheap`. Until v3 is proven on live data it falls back to `cheap_v1` on any failure,
including a response that isn't a `data` list, and skips v3 for the rest of a run after
`V3_MAX_FAILURES`, so a dead endpoint doesn't double every call. Budget exhaustion is re-raised,
never hidden by the fallback. v3 quotes take their route codes from the request, not the response,
which names cities and airports separately. `fareloot provider-check` (workflow mode
`provider-check`) compares v1, v3 and markets on one route; `FARELOOT_TP_MARKET` forces a market.

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

`.github/workflows/watch.yml` runs `fareloot poll` on GitHub Actions cron. Price history persists
across runs on an orphan `fareloot-data` branch (force-pushed each run), not in `main`'s commit
history, to keep the repo from growing forever with a binary SQLite file.

Three things here are easy to break:

- **`config/watchlist.yaml` is gitignored.** The workflow writes it from the
  `FARELOOT_WATCHLIST_YAML` secret before polling, so a personal wishlist is never public. That
  means the built-in `DEFAULT_WATCHLIST_YAML` in `config.py` is the only version-controlled copy,
  and `tests/test_config.py` exists to stop it rotting.
- **The restore step must create `data/` before redirecting into it,** and must use an explicit
  refspec when fetching `fareloot-data`. Both were wrong originally, and because the failures were
  swallowed by `|| true` the step reported success while restoring nothing, silently resetting every
  baseline each run. Keep that step loud.
- **A run prices a rotating slice of the wishlist, not all of it.** The full origin x destination
  product is several hundred calls; the provider limiter allows 150/hour. `_wishlist_slice` resumes
  from a cursor in the `meta` table each run. Walking in a fixed order instead starves every origin
  after the first few forever, including BLR and MAA. Discovery spends its calls first for the same
  reason. `test_small_budget_still_reaches_every_origin_across_runs` pins this.
- **Months 3+ rotate on their own cursor** (`FAR_CURSOR_KEY`) with `far_month_share` of the
  per-run budget; months 1-2 keep `_wishlist_slice`. Each far item fetches its own reverse month.
  Defaults: ~3.6 days per near pass, ~20 days per far month. Raising the horizon or the share
  stretches one or the other; there is no free lunch under the 150/hour limiter.
- **Baselines exclude the current run** (`route_history(..., before=run_started)`). `poll` records
  before scoring, and a route crashing on every date at once would otherwise dilute its own
  baseline; `test_run_does_not_count_its_own_prices_as_baseline` pins it.
- **Seeding is a `mode` input on watch.yml, not its own workflow.** It must share the
  `fareloot-watch` concurrency group and the restore/persist guards; a second workflow
  force-pushing `fareloot-data` concurrently would drop one run's data. `watcher.seed` rotates
  with its own `seed_cursor` for the same starvation reason as poll.
- **`fareloot-data` holds `fareloot.db.enc`, never plaintext.** The branch is public and the
  observations table names every polled route, enough to rebuild the gitignored watchlist. Restore
  decrypts with `FARELOOT_DATA_KEY` and runs SQLite's integrity check (CBC is unauthenticated, so a
  wrong key can occasionally "decrypt"); Persist encrypts and proves the round trip with `cmp`
  before pushing. A missing key aborts the run. Public logs follow the same rule: in CI,
  `cli._report_errors` groups provider errors by cause because they name routes, and
  `load_watchlist` raises `WatchlistError` with field locations but never values.
- **Alerts are recorded only after delivery** (`watcher.record_alerts`, called by the CLI with
  what `send_deals` actually delivered). Recording inside `poll` let a failed send mark a deal as
  sent, and the cooldown then hid it.
- **A run where every provider call failed exits non-zero** and leaves both cursors where they
  were (`PollResult/SeedResult.all_calls_failed`). Before this a revoked token produced green runs.
- **Persist runs even when Poll fails**, so a notification error doesn't discard collected prices,
  but only after a successful restore and never with a hot SQLite journal present.
- **Nothing automated keeps the schedule alive; `keepalive.yml` makes a person do it.** Reports
  say only default-branch commits count as activity for GitHub's 60-day rule, and `main` is
  protected, so a bot commit is impossible by design. The keepalive re-enables workflows through
  the API (undocumented whether that resets the clock, so nothing relies on it) and runs
  `fareloot health`, which warns on Telegram from day 45. Decision logic lives in `health.py` so
  it is testable; the workflow only gathers facts, and missing facts are warnings, never healthy.
- **The repo must stay public.** Branch protection on a private repo needs GitHub Pro; the API
  returns 403 and `main` reverts to unprotected. Scheduled runs are also best-effort and land every
  7-8 hours in practice, not hourly.
