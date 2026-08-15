# Biotech desk — house rules

Personal end-of-day research system for a small-cap pharma watchlist. Runs
unattended on weekdays after the US close and writes a report to `reports/`.

Research support for the owner's own decisions. Not financial advice; every
order is placed by hand.

**Read `STRATEGY.local.md` first for anything involving sizing, routing or
whether to buy.** It is gitignored and holds the personal layer: objective, risk
posture, what each bucket means, broker preferences, constraints. This file
holds only the engineering, and is safe to make public.

## Architecture

Facts and judgement are deliberately separated:

```
scripts/fetch.py         -> data/latest.json      deterministic; prices, filings, XBRL, trials
scripts/signals.py       -> data/signals.json     fixed arithmetic; indicators, tiers, vetoes
scripts/propose_zones.py -> watchlist.toml        entry zones from each name's own range
scripts/detail.py                                 per-ticker drill-down for the analysis pass
prompts/daily.md         -> reports/YYYY-MM-DD.md the analysis (judgement lives here)
scripts/notify.py                                 ntfy push + email
run_daily.sh                                      orchestration, invoked by a systemd timer
```

An LLM must never be the source of a price, a share count or a cash balance.
Those come from `data/latest.json` or they do not appear in the report.

**Never read `data/latest.json` wholesale during analysis** — at 60+ names it is
several megabytes. Read `data/signals.json` (compact, all names) and then use
`python3 scripts/detail.py TICKER` for the few names that warrant a close look.
The triage rules in `prompts/daily.md` cap deep-dives at 12 per run.

## Data sources (free, keyless)

| What | Source | Notes |
|---|---|---|
| Daily OHLCV | Nasdaq API, Yahoo fallback | Yahoo rate-limits by IP and will 429 a whole run |
| Filings | SEC EDGAR submissions | includes 8-K **item codes** — the veto layer depends on these |
| Liquidity / burn / shares | SEC EDGAR XBRL companyconcept | see the traps below |
| Trials | ClinicalTrials.gov v2 | catalyst clock; sponsor derived from the SEC company name |
| News, PDUFA, AdCom | WebSearch/WebFetch at report time | not available from any structured feed |

SEC requires a descriptive User-Agent with contact details and <10 req/s. Fetches
run 6 threads wide behind shared `RateLimiter` instances — per-call sleeps would
not bound the aggregate rate once requests are concurrent. A full 63-name run
takes roughly 2 minutes.

### XBRL traps that produced real, wrong answers here

Each of these shipped a bad number before being fixed. Do not "simplify" them away.

1. **Cash is not liquidity.** Biotechs hold most of their money in marketable
   securities. Counting only `CashAndCashEquivalentsAtCarryingValue` understated
   runway by 10x and fired false financing vetoes. Liquidity = cash + the largest
   short-term investment tag (they are alternative presentations, not additive —
   summing them double-counts).
2. **Companies silently abandon tags.** Viridian's cash tag last appeared in
   2019; naively taking the newest row for that tag returned a six-year-old
   $24.9M against a real $816.5M position. Tags are selected by recency across a
   family, anchored to a single balance-sheet date.
3. **Cash-flow values are year-to-date cumulative.** A 9-month figure read as
   quarterly overstates burn by 3x. Burn is normalised to a per-day rate.
4. **XBRL lags the filing.** A company announces a quarter by 8-K item 2.02 weeks
   before the 10-Q, so the freshest balance sheet often exists only in an earnings
   release. When a later periodic report or results 8-K exists, the runway is
   marked `superseded_by` and may not gate ACT.
5. **Stale is not distressed.** A balance sheet older than 200 days is marked
   `stale` and may *not* fire the runway veto — it becomes a soft flag. Absence of
   data must never be reported as bad news.
6. **The tag list will never be complete — so it self-heals.** EWTX reported
   $388M under `MarketableSecurities`, absent from the curated list, and was
   read as holding only $72M of cash with its runway one step from a financing
   veto. `discover_investments()` therefore sweeps a company's full
   `companyfacts` for any investment-shaped tag whenever the targeted lookup
   finds none. Two rules make it safe: only **instant** facts qualify (duration
   facts carry a `start`, which excludes cash-flow items like
   `PaymentsToAcquireMarketableSecurities` that match by name), and a
   **combined** tag such as `CashCashEquivalentsAndShortTermInvestments`
   already contains the cash, so it replaces the total rather than adding to it
   — adding it double-counted SION by $63M. The sweep runs only for suspicious
   names (companyfacts is multi-megabyte) and costs ~2 minutes per full run.
   When it finds nothing, `cash_only_verified` is set and a short-runway veto
   on that name is trustworthy *because* it was checked.
7. **Foreign filers have no us-gaap XBRL.** Australian/Swiss/French issuers
   (RADX, ABVX, LEGN) return no runway at all. Report that as unknown, never as
   healthy.

## The veto layer

Mechanical cheapness is easy to compute and dangerous alone. Most large
drawdowns in this sector are correctly pricing a broken thesis. `signals.py`
blocks the SETUP/ACT tiers when any of these are live:

- `424B*` prospectus supplement within 10 days — **read it**: a priced takedown is
  a hard stop, an at-the-market programme is only registered capacity. The form
  type alone cannot distinguish them, so it vetoes until the report refutes it.
- 8-K item **3.01** (listing deficiency) within 45 days
- 8-K item **3.02** (unregistered equity sale) within 10 days
- 8-K item **4.01 / 4.02** (auditor change / non-reliance)
- `NT 10-Q` / `NT 10-K` late filing within 45 days
- any single-session move ≤ **−25%** in the last 10 sessions, with the likely
  causal filing attached
- liquidity below **1.5 quarters** of burn, when not stale

Soft flags (shelf registrations, officer departures, charter amendments, new
debt, stale or superseded financials) annotate but do not block.

**A veto may only be overridden in the report with specific cited evidence.**
This has already worked in practice: a COGT 424B5 was correctly refuted as a
$400M ATM programme, and a PRAX item 4.01 as a clean auditor change.

## Tiers

- `WATCH` — RSI(14) < 40 **and** %B < 0.25 (approaching oversold)
- `SETUP` — RSI(14) < 35 **and** %B < 0.15 **and** no hard veto
- `ACT` — SETUP **and** price ≤ `entry_high` **and** (runway ≥ 3 quarters, not
  stale and not superseded, **or** a catalyst within 90 days)

### Thresholds are measured, not chosen

`scripts/backtest.py` replays the stored history (29,077 ticker-days, no
lookahead) and scores each rule's forward return against an all-days baseline.
**Re-run it before changing any threshold.** What it found:

| Rule | Fires | Median edge @20d | @60d |
|---|---|---|---|
| RSI<30 & %B<0.05 (the old SETUP) | 1.81% | **+3.63pp** | +0.26pp |
| RSI<35 & %B<0.15 (current SETUP) | 6.59% | +2.90pp | −0.49pp |
| bottom decile of 1y range (the old WATCH) | 19.25% | +0.49pp | **−2.68pp** |
| RSI<25 | 1.46% | +2.38pp | **−9.59pp** |
| RSI<35 & %B<0.15 & volume>1.5× | 1.92% | +2.63pp | **+1.72pp** |

Three consequences, all encoded above:

1. **Price cheapness alone is noise.** Bottom-quartile-of-range used to set
   WATCH and predicts nothing. It is now reported as context only.
2. **More oversold is not better.** RSI<25 underperforms badly at 60 sessions.
   Below `RSI_TRAP` (22) the reasons carry an explicit distress warning.
3. **The edge is a ~20-session bounce, not a hold.** It decays to nothing by 60
   sessions. Only volume confirmation (`capitulation_volume`) survives that far.
   Any technical-only idea in the report must state an exit horizon.

Caveats on the numbers: survivorship bias (acquired and delisted names are
absent), a strongly bullish sample period (+13% mean 60-day baseline), and no
transaction costs — micro-cap spreads are wide. Treat it as a smoke test of the
thresholds, not proof of edge.

### The finding that matters most

`scripts/score_alerts.py` grades alerts against **XBI**, not against an
all-days baseline. That distinction changed the conclusion:

| | +20d vs XBI | +60d vs XBI |
|---|---|---|
| All oversold alerts | −0.28% med | −2.28% med, 46.2% win |
| **With** capitulation volume | −1.25% med / +3.97% mean | **+0.25% med / +7.08% mean** |
| **Without** capitulation volume | −0.28% med | **−2.68% med, 44.0% win** |

**The oversold trigger on its own does not beat simply owning XBI.** The earlier
backtest's "edge" was measured against the average ticker-day in this universe,
which is not something anyone can buy. Against the investable alternative it
disappears, and without volume confirmation it is negative.

Consequences, encoded in the code:

- `ACT` **requires** `capitulation_volume`. An unconfirmed oversold reading is
  surfaced as SETUP for review but never called actionable.
- The report must state whether a technical signal clears that bar.
- The desk's real value is the veto layer, the catalyst clock and the balance
  sheet work — **not** the RSI trigger. Treat the trigger as timing for names
  already wanted on fundamentals, never as a reason to buy by itself.

## Relative strength

Every name is scored against **XBI** (equal-weighted small/mid-cap biotech — the
honest comparison; IBB is cap-weighted and behaves like large-cap pharma) at
5/20/60 sessions. Without it, one sector drawdown flags all 60+ names at once and
reads as 60 signals when it is really one.

- `idiosyncratic: true` — 10pp+ behind XBI over 20d while the sector held up.
  Company-specific; find the cause before doing anything.
- `sector_wide: true` — XBI itself is down 5%+. Most of the move is beta.

Benchmarks are fetched with `assetclass=etf`; passing `stocks` returns a 400.

## Catalysts

`catalysts.toml` holds dated binaries (PDUFA, AdCom, readouts) with a
`confidence` of confirmed / expected / rumored. `signals.py` computes
`days_until` and warns hard inside 21 days. Per the user's choice it **warns but
never blocks** — an approaching binary changes sizing, not permission.

The daily run appends catalysts it establishes from filings, with sources. An
invented date would silently distort every later run, so unsourced dates are
forbidden.

## Float

`dei:EntityPublicFloat` (10-K cover, annual) divided by the price on that date
gives float shares. Two corrections proved essential:

1. **Carry float forward as a fraction of shares outstanding, not an absolute.**
   XFOR went 5.8M → 99.1M shares in a year; a year-old absolute float against
   today's short interest read as 93% of float short.
2. **Reject inconsistent inputs.** A derived float below 5% or above 100% of
   shares outstanding means filer error or an unadjusted split — PTCT reports a
   $3.35M float on 83M shares, which produced 15,401% of float short. 14 of 63
   names are rejected this way; they report `unusable` with the reason rather
   than a wrong number.

## Exits

`exit_signals()` covers the half the desk previously ignored. Flags, by severity:

- **high** `invalidation_breached` — close at or below `invalidation_price` in
  `watchlist.toml` (set mechanically by `propose_zones.py` at 5% below the
  anchor window's own low; override by hand freely).
- **high** `catalyst_resolved` — a dated catalyst passed in the last 21 days.
  Without this the desk would keep carrying a thesis whose binary already
  resolved.
- **medium** `veto_active` — hard vetoes are live; if the name is held, the
  reason for holding has changed.
- **low** `horizon_elapsed` — the last live alert is older than the
  20-session horizon where the edge was measured. A technical thesis that old
  has expired.

High-severity flags push to ntfy, deduplicated on their own `exit_key` so a
standing veto does not buzz nightly. Exits lead the notification ahead of new
setups: a thesis breaking outranks an idea appearing.

## Exposure

`signals.json.exposure` sums what every actionable name would request at its
bucket cap against the investable ceiling (100% less the configured cash
reserve). Several full-size names firing at once can exceed it easily, and
`scale_factor_needed` says by how much. Nothing else computes this, and these
names trigger together in a drawdown.

## Heartbeat

`pharma-heartbeat.timer` (Mon-Fri) alerts if no report appears for two weekdays.
**Silence is the desk's normal output**, so a broken run and a quiet market look
identical. Separate unit on purpose: if the main run dies before reaching
notify.py, run_daily.sh's own failure handler dies with it.

## Market regime

XBI versus its 200-day average, in `signals.json` under `regime`. In a
`downtrend` the ACT tier additionally requires a `strong` conviction score —
signals stay visible, the bar to act rises. Currently: **uptrend**, +19.6% vs
200DMA.

## Insider transactions (Form 4)

Parsed from raw EDGAR ownership XML. **Only transaction code `P` counts.**

Codes `A` (grant), `M` (option exercise) and `F` (tax withholding) all report as
"acquired" and are not purchases. ARDX's CEO filed an `M` at $0.99 immediately
followed by an `S` at $5.06 — naively that reads as insider buying and is the
exact opposite. Counting A/M is the standard way to manufacture a fake signal.

- `cluster_buy` — 2+ insiders, ≥$50k in 120 days. **The strongest available
  evidence for refuting a veto**: people with the fullest picture committing
  their own money while the tape says broken.
- `net_selling` — deliberately set at ≥$25M and 10× buys. At a $250k bar it
  fired on 34 of 63 names, which is wallpaper. Selling is far less informative
  than buying (10b5-1 plans, RSU vesting, tax, diversification).

## Short interest

Two sources: Nasdaq's bi-monthly settled short interest (days-to-cover) and
FINRA Reg SHO daily short volume — one file covers every symbol, so it is a few
requests for the whole watchlist rather than one per name.

`crowded_short` is set at **10** days-to-cover and `extreme_short` at **15**. At
the obvious 5 it flagged 47 of 63 names: above 5 is simply normal for small-cap
biotech and discriminates nothing. Thresholds have to sit where the distribution
actually thins out.

## Email delivery

`render_email.py` → `multipart/mixed` → `multipart/alternative` (text + HTML)
plus the `.md` attached. Constraints, each learned the hard way:

- Styles **inlined on every element** — Gmail/Outlook strip `<style>` blocks.
- Tables in an `overflow-x` container, else the page scrolls sideways on a phone.
- **Size is measured, not estimated.** Gmail clips past ~102KB; a wide markdown
  table expands ~11× under inline styles while prose expands ~3×, so budgeting
  by character count fails. `build_email_html` renders, measures, and re-renders
  smaller until it fits, falling back to a body-less summary if it cannot.
- No external CSS, fonts or images — most clients block them.

## Chart links

`signals.json` carries `links` and a ready-made `links_md` per name (Finviz
6M/3Y/10Y via `p=d|w|m`, TradingView, financials, EDGAR). The report puts
`links_md` under every name it discusses.

Notifications fire on tier *changes* into SETUP/ACT, not on tier states — an
oversold name should not buzz the phone every evening for three weeks.

## Entry zones

Set by `scripts/propose_zones.py` from each name's own trading range, so they can
be recomputed as prices move. Method:

- **Anchor window** — if the name had a ≤−25% single-session collapse in the last
  120 sessions, only bars *after* the break are used. Pre-break prices describe a
  company that no longer exists; averaging them in would place a buy zone far
  above where the stock trades, which is how arithmetic catches a falling knife.
  Otherwise the trailing year is used.
- **Too soon** — under 25 sessions in the window means no zone is written and ACT
  stays disabled for that name.
- `entry_high` = 25th percentile of the window; `entry_low` = 8th percentile,
  clamped to a band no narrower than 8% and no wider than 45%.

These are mechanical starting points, not valuations. Override any of them by
hand in `watchlist.toml`; re-run with `--apply` to refresh the rest.

**Zones go stale, and stale zones fail closed.** A zone describes the regime it
was built from; once price is `ZONE_STALE_DRIFT_PCT` (25%) away, `zone_stale` is
set and a soft flag raised. This matters because the failure is otherwise
invisible: ACT simply never fires, which looks identical to "no opportunity".
Refresh with `propose_zones.py --apply`.

## Buckets and sizing

`tier` in `watchlist.toml` (`A` / `B` / `lottery` / `legacy`) selects a
position-size ceiling and nothing else — it does not affect signal logic. Every
name gets identical veto treatment regardless of bucket.

The ceilings themselves are configured in `watchlist.toml [settings]`
(`max_position_pct`, `max_position_pct_lottery`, `max_bucket_pct_lottery`,
`min_cash_reserve_pct`) and reach the report as `max_position_pct` on each
signal. **Never restate those numbers in code or documentation** — read them
from the data, or the two drift apart.

What the buckets *mean*, and the reasoning behind the ceilings, is in
`STRATEGY.local.md`.

## Conventions

- Stdlib only in `scripts/` — no pip dependencies to rot in a cron job.
- Config in TOML, parsed with stdlib `tomllib`.
- Secrets in `~/.config/pharma/notify.env`, never in the repo.
- `data/history.sqlite` accumulates bars and filings so day-over-day deltas and
  "new since last run" work even when a provider has an outage.
- Screening a candidate list must pass `--state` to `signals.py` so it does not
  consume the live watchlist's alert state.
- Adding a ticker means one `[[ticker]]` block; CIK and trial sponsor resolve
  automatically from SEC's company list.

## Schedule

`pharma-desk.timer` Mon–Fri 23:18 local — after the US close in every DST
alignment, so the daily bar is settled. `Persistent=true` catches up a miss.

```bash
systemctl --user list-timers 'pharma-*'
journalctl --user -u pharma-desk.service -n 50
./run_daily.sh --no-llm            # data + signals only, fast and free
```

## Tickers change underneath you

Companies get acquired and renamed. When a symbol stops resolving, `fetch.py`
reports "no CIK in SEC ticker map" and Nasdaq returns "Symbol not exists" — that
is usually a takeout, not a bug. Confirmed during this project: TERN acquired by
Merck, NUVL by GSK, PSTV renamed to Cerenome (CNSY). Re-check unresolved symbols
before assuming the pipeline is broken.
