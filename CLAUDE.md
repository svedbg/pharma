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
                         -> data/summaries/*.json one small record per day, for the archive index
scripts/propose_zones.py -> watchlist.toml        entry zones from each name's own range
scripts/detail.py                                 per-ticker drill-down for the analysis pass
prompts/daily.md         -> reports/YYYY-MM-DD.md the analysis (judgement lives here)
scripts/notify.py                                 ntfy push + email
scripts/render_email.py                           markdown -> mobile-readable HTML (used by notify and publish)
scripts/publish.py       -> site/                 local browsable archive of past reports (`make site`)
run_daily.sh                                      orchestration, invoked by the scheduler (systemd timer / launchd job)
```

Measurement and validation, none of it on the daily path except the scorecard:

```
scripts/backtest.py                               replay the stored bars through the rules
scripts/score_alerts.py  -> data/scorecard.txt    grade the alerts actually raised, against XBI
scripts/paper.py                                  paper-trade log, scored against XBI
scripts/screen.py        -> candidates TOML       monthly look outside the watchlist (`make screen`)
scripts/heartbeat.py                              alert when reports stop appearing
```

An LLM must never be the source of a price, a share count or a cash balance.
Those come from `data/latest.json` or they do not appear in the report.

**Never read `data/latest.json` wholesale during analysis** — at 60+ names it is
several megabytes. Read `data/signals.json` (compact, all names) and then use
`python3 scripts/detail.py TICKER` for the few names that warrant a close look.
The triage rules in `prompts/daily.md` cap deep-dives at 12 per run.

`detail.py` is the only view of a name the analysis pass gets, so it has to
carry what a decision uses: the entry zone and where price sits in it, the
`invalidation_price` an exit is measured against, the conviction checklist on
both sides, and the exit flags. It printed none of those — `fetch.py` carries
`invalidation_price` into the snapshot with a comment saying it was added *so
detail.py could show it*, and detail.py still did not. A drill-down missing the
two numbers ACT and the exit flags turn on is a buy-side-only view of a name
that may already have broken its own thesis. Every stage of that chain looked
fine on its own while the field never arrived, which is why the test drives
`signals.main()` and then `detail.main()` end to end rather than checking either
in isolation.

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
not bound the aggregate rate once requests are concurrent. A full run over ~60
names takes two to four minutes: roughly two for the fetch itself, plus up to
two more when several names trigger the multi-megabyte `companyfacts` sweep
described in trap 6.

**Every response is decoded through `get_json()`, never `json.loads(http_get())`.**
Every caller in `fetch.py` guards `FetchError` and nothing else, so a bare
decode had a hole straight through all of it: a provider that answers HTTP 200
with an HTML body — a captive portal, a Cloudflare interstitial, a maintenance
page — raises `JSONDecodeError`, which is not a `FetchError` and was caught by
none of them. The blast radius was out of all proportion to the cause. In
`build_record` the only thing left to catch it is the blanket `except` around
`fut.result()`, so one malformed trials or short-interest response **discarded
the whole ticker, bars included** — and `fetch_bars` could not fail over, so a
Nasdaq error page killed the record instead of falling through to Yahoo, which
is the entire reason two providers are wired up. A body that is not JSON is
what "this source did not answer" looks like, so it is reported as that, with
the first 120 bytes attached to name the impostor.

**A stale CIK map beats no run.** `load_cik_map` is the first request the run
makes, and its failure ended the run before a single price was fetched. CIKs
move at the pace of corporate actions, not of trading days: a map a fortnight
old resolves every name except one renamed since, and a rename already presents
as `no CIK in SEC ticker map`, which the record carries as an error and which
this file already tells you to re-check by hand. So a fetch failure now falls
back to the cached copy at any age, saying how old it is. With no cached copy
it still fails loudly — the fallback is for a stale map, not for no map, and an
empty one would resolve nothing for every name while looking like a quiet day.

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
   on that name is trustworthy *because* it was checked. `discover_investments`
   therefore returns `(ran, best)` rather than a bare result: "the sweep found
   no securities" and "the sweep never completed" are opposite facts, and only
   the first may set `cash_only_verified` — which is printed inside the veto as
   "confirmed by a full XBRL tag sweep". A timed-out request must not buy that
   sentence.
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
  **`424B7` is excluded**: it registers shares existing holders already own, so
  it is a resale, not an issuance, and it carries only the soft flag of that
  name. It matched the `424B` prefix as well as its own soft rule, so every
  resale registration took a priced-takedown veto sitting next to a soft flag
  explaining it was only a resale — advice nobody could act on, because the veto
  beside it had already blocked the name. Whatever dilution preceded it arrives
  on its own hard veto as item 3.02.
- 8-K item **3.01** (listing deficiency) within 45 days
- 8-K item **3.02** (unregistered equity sale) within 10 days
- 8-K item **4.01 / 4.02** (auditor change / non-reliance)
- `NT 10-Q` / `NT 10-K` late filing within 45 days
- any single-session move ≤ **−25%** in the last 10 sessions, with the likely
  causal filing attached
- liquidity below `runway_veto_quarters` (default **1.5**) of burn, when the
  balance sheet is neither stale nor superseded

Soft flags (shelf registrations, officer departures, charter amendments, new
debt, stale or superseded financials) annotate but do not block.

**Both runway thresholds live in `watchlist.toml [settings]`, and they have to
stay ordered.** The veto bar was a bare `1.5` inside `financial_vetoes()` while
its counterpart `min_runway_quarters_for_act` — the bar for *clearing* a name —
was configurable: the looser rule was the one you could see and tune, and the
one that blocks a name outright was neither. Set the veto above the ACT bar and
the band between them inverts, so a name in it is simultaneously proof of
distress and proof of a sound balance sheet — the same contradiction the
superseded-runway fix below exists to remove. `main()` warns when they are the
wrong way round.

**The runway veto and `runway_ok` must agree about which balance sheets they
trust.** They did not: ACT already refused a `superseded_by` runway as
unreliable, while the veto read the same figure as proof of distress — too old
to clear a name, fresh enough to condemn it. OTLK carried a 0.59-quarter hard
veto off a balance sheet its own later 8-K had replaced. A short runway on
superseded data is now a soft flag (`short runway, superseded`) carrying the
number and the superseding filing, and `conviction()` still counts it against
the name. It simply no longer blocks on its own. Trap 5's rule generalises:
absence of *current* data is ignorance, not distress.

**Every window above is measured from the snapshot's own `local_date`, never
from the wall clock.** `signals.py` derives one `asof` date in `main()` and
threads it through `_days_ago`, `evaluate_filings`, `financial_vetoes`,
`load_catalysts`, `resolved_catalysts`, `next_catalyst` and `analyse`; those
functions require it rather than defaulting, because a default is silent when
wrong and nothing in the output records which clock produced a `days_ago`.

`analyse` was the exception, and it is the function every signal flows through.
It defaulted to today "so an ad-hoc caller need not construct one", with its own
docstring conceding that the default was what made the function's determinism
conditional — which is a plain statement that the convenience cost the property.
An ad-hoc caller did not get a slightly inconvenient answer; it got a different
one on Tuesday than on Monday, with nothing in the output to say so. It now
requires `asof` like the rest, and the tests name their session rather than
inheriting the day they happen to run on.

The two clocks diverge whenever `signals.py` runs on a different day from the
fetch — re-running it to pick up a watchlist edit (which this file recommends,
since the overlay exists so hand edits need not wait for a fetch), pointing
`--snapshot` at an archived file, or a run that straddles midnight. Re-running
yesterday's snapshot this morning aged OTLK's two 424B5 vetoes from 1 and 3 days
to 2 and 4, HUMA's listing deficiency from 15 to 16, and PRAX's auditor change
from 44 to 45. None crossed a threshold that day; a filing sitting on one would
have.

**Measuring and recording are separate decisions.** `main()` holds two values:
`session`, the validated date the snapshot describes, and `asof`, what ages are
measured from. They differ only when the snapshot cannot say. Measuring from
today is then a reasonable degraded mode — the run still produces a report and
the warning says what it did — but *recording* on that basis is not, so a run
with no session writes neither an alert row nor an archive summary. Both are
keyed by session date, and a key nobody can read back is worse than no row:
`score_alerts.py` raises on a NULL and silently matches no bar for a malformed
string, which loses the alert from the only number that says whether the desk
works. `session_date` in the output is always the parsed value, so `2026-8-15`
is normalised to `2026-08-15` rather than naming a summary file the archive
looks for under the canonical spelling and never finds.

**Recording is only deferred, never skipped, so a session-less run leaves the
alert state alone.** `state/alerts.json` is what makes an alert fire *once*, so
writing it commits to having recorded the transition. It used to be written
unconditionally: a run with no session logged no alert row — correctly — and
then advanced every tier anyway, so the next run saw no change and never logged
it either, while the notification had already gone out. Refusing to write the
unreadable key and consuming the transition regardless turned an ungradeable row
into no row at all. The state write is now gated on the same `session`, which
defers the whole thing to the next run that can name its day: one duplicate
buzz, against a permanently missing row.

**The session is the newest bar, not `date.today()`.** `fetch.session_date()`
derives `local_date` from the bars themselves — the date most sources agree is
their newest, so one stale or bogus series cannot redate the run — and only
`generated_at` reads the clock. Everything above measures from a value that was
itself a clock reading, which is the session only when the day's bar already
exists, and it routinely does not: the scheduled run fires 18 minutes after the
US close, any hand run before then is hours early, and a market holiday has no
bar at all. When they diverged the alert was keyed a day ahead of the bar its
own numbers came from, and `score_alerts.py` — which resolves an alert to the
first bar at or after its session date — then graded the entry from the *next*
session, at a price that had already moved. The one live ARDX alert was recorded
at 3.91 and graded from 4.06. Backfilled alerts are keyed to their own bar, so
the two halves of the scorecard were not measured the same way, which is the one
comparison it exists to make. **The report is named for the session too**
(`run_daily.sh` reads it back out of `signals.json`), because `publish.py` pairs
`reports/<d>.md` with `data/summaries/<d>.json` by that name.

**A veto may only be overridden in the report with specific cited evidence.**
This has already worked in practice: a COGT 424B5 was correctly refuted as a
$400M ATM programme, and a PRAX item 4.01 as a clean auditor change.

## Tiers

- `WATCH` — RSI(14) < 40 **and** %B < 0.25 (approaching oversold)
- `SETUP` — RSI(14) < 35 **and** %B < 0.15 **and** no hard veto
- `ACT` — SETUP **and** price ≤ `entry_high` **and** a financing backdrop
  (runway ≥ 3 quarters or cash-flow positive, in both cases neither stale nor
  superseded, **or** a catalyst within 90 days) **and** `capitulation_volume`
  **and**, in a `downtrend` regime, a `strong` conviction score.

That is the whole of it. The last two conditions used to live only in the
sections that derived them — "The finding that matters most" and "Market regime"
— so this list, which is where a reader looks for the definition, described a
looser tier than the code has fired since. They are now **pinned by tests as
well as by prose**: a documented gate with no test is a gate that can go quiet
without anything failing, and the regime half in particular had neither a test
of its own nor one of `market_regime()`, on the condition that decides whether
the desk's only actionable tier fires at all. Both directions are asserted — a
downtrend holds a moderate name at SETUP, and a strong one still reaches ACT —
because a gate that merely blocked would make the tier unreachable for months
at a time and look identical to a quiet market. **The financing half accepts a
cash-generative company.** `runway()` returns quarters of liquidity at the
current burn, and a company that funds itself from operations has no such
number; folding that into `None` made `runway_ok` read the strongest balance
sheet on the list as ignorance, so it needed a catalyst inside 90 days to reach
ACT while a company with three quarters of cash did not. It now returns
`cash_flow_positive: True` with `quarters: None`, and every consumer must branch
on `is None` rather than on falsiness.

The catalyst half of that test reads **both** clocks: the next active trial's
primary-completion date from ClinicalTrials.gov, and any dated entry in
`catalysts.toml`. It used to consult only the first, so a sourced PDUFA three
weeks out did not satisfy a test that a soft sponsor estimate did — the
trustworthy calendar was the one that could not open the gate. This is the one
place `catalysts.toml` participates in a tier; everywhere else it warns only.

### Thresholds are measured, not chosen

`scripts/backtest.py` replays the stored history (~29,000 ticker-days and
growing, no lookahead) and scores each rule two ways: against an all-days
baseline, and against **XBI over the same calendar dates**.
**Re-run it before changing any threshold.** What it found (run of 2026-08-16;
the history grows daily, so re-run rather than quoting these):

| Rule | Fires | edge @20d | @60d | vs XBI @20d | vs XBI @60d |
|---|---|---|---|---|---|
| RSI<30 & %B<0.05 (superseded SETUP) | 1.82% | +3.61pp | +0.24pp | +1.05% | −3.56% |
| RSI<35 & %B<0.15 (live SETUP) | 6.61% | +3.04pp | −0.50pp | +0.90% | −2.58% |
| bottom decile of 1y range (the old WATCH) | 19.23% | +0.48pp | **−2.59pp** | −0.21% | −1.76% |
| RSI<25 | 1.46% | +2.36pp | **−9.62pp** | +1.22% | **−11.95%** |
| live SETUP + volume>1.5× (the ACT bar) | 1.92% | +2.71pp | **+1.76pp** | +0.18% | −1.55% |

**Read the last two columns first.** The all-days baseline is the average
ticker-day in this universe, which is not something anyone can buy; XBI is. The
two disagree, and where they do the ETF settles it — every rule above is weaker
against XBI than against the baseline, and the baseline itself runs −1.23% at
20d and −3.03% at 60d against XBI. That gap *is* the finding in the next section,
now visible in the same table rather than only in the scorecard.

`backtest.py` builds the live rule from `SETUP_RSI` / `SETUP_PCTB` /
`CAPITULATION_VOL` imported from `signals.py`, never from numbers retyped into
it. It once hardcoded the old pair and labelled them "live SETUP" while calling
the real rule "(looser)" — the instrument that justifies the thresholds was
scoring a threshold the desk had already abandoned. The retired pair is kept as
"superseded SETUP" for comparison. The zone rules below reconstruct the zone
through `propose_zones.zone_from_closes()` for the same reason: one
implementation, so the instrument cannot score a paraphrase of the rule.

The benchmarks are **excluded from the universe** (`signals.NOT_CANDIDATES`).
XBI and IBB sit in the same `bars` table as the watchlist, and scoring them
diluted the baseline every edge here is measured against while letting the rules
fire on the very thing they are supposed to beat. `score_alerts.backfill` always
excluded them; this file did not.

That list was then written three times, spelled three ways: `NOT_CANDIDATES` in
`backtest.py`, an inline `(BENCHMARK, "IBB")` in `score_alerts.py`, and
`BENCHMARKS` in `fetch.py`. Adding a third benchmark would have updated one of
them and silently left the ETF in the universe of the others — scored as a
candidate by the very instrument that measures whether the rules beat it. There
is one definition now, in `signals.py`, and `paper.py` imports `BENCHMARK`
instead of retyping `"XBI"`. Stage 2 deliberately does not import stage 1, so a
test pins `signals.NOT_CANDIDATES` to `fetch.BENCHMARKS` instead: **adding a
benchmark to the fetch fails the suite until it is added to both.**

#### Does a stale zone deserve to open ACT?

`zone_stale` fires at 25% drift in either direction but only *gates* the upward
one: above the zone `in_zone` is false and ACT cannot fire, while below it ACT
fires normally — and below is the direction this desk hunts. Whether that
permission is earned is a measurable question, so it was measured rather than
argued. Splitting the ACT bar by it (zones reconstructed as of each day from
prior bars only):

| | Fires | vs XBI @20d | vs XBI @60d |
|---|---|---|---|
| ACT bar, in zone, **zone fresh** | 0.96% (n=280) | +1.85% med / +4.06% mean | −1.11% med |
| ACT bar, in zone, **stale 25%+ down** | 0.36% (n=104) | **+2.38% med / +16.07% mean** | −1.77% med |

**So the gate stays off.** The stale-to-the-downside subset is not worse than the
fresh one at the 20-session horizon where the edge lives — it is slightly better
on the median and far better on the mean, which is what a population of deeply
beaten-down names that occasionally re-rate looks like. Blocking it would remove
signals that perform at least as well as the ones it keeps. n=104 is small and
both medians are negative by 60 sessions, so this is a reason not to add a gate
rather than evidence that these are good trades; deciding a cheap name is broken
remains the veto layer's job, not the zone's.

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
all-days baseline. That distinction changed the conclusion (run of 2026-08-14;
`data/scorecard.txt` holds the current one, refreshed every run).

Both ends of that comparison are the **ticker's own bar dates**, resolved
through `bench_at()`. It used to advance the benchmark `h` positions through the
ETF's bar list while the alert advanced `h` positions through the name's, so any
session the name missed and the ETF did not compared two different calendar
windows — the misalignment `relative_strength()` in `signals.py` already exists
to prevent, absent from the module that produces the number below. No figure
here moves today (the 15 affected names are only missing the most recent bar, so
no scored window reaches the gap); the first mid-window halt would have shifted
them silently.

| | +20d vs XBI | +60d vs XBI |
|---|---|---|
| All oversold alerts | −0.28% med | −2.28% med, 46.2% win |
| **With** capitulation volume | −1.25% med / +3.97% mean | **+0.25% med / +7.08% mean** |
| **Without** capitulation volume | −0.28% med | **−2.68% med, 44.0% win** |

These move as history accrues — the without-volume 60-day figure has already
drifted to −2.79% — so read the table for its *direction*, which has been stable,
and quote `data/scorecard.txt` for a number.

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

**Write every date as a quoted ISO string — `date = "2026-09-15"`.** This is the
one hand-edited file the pipeline reads, and `_catalyst_date()` now normalises
it because both ways of getting it slightly wrong reached a report:

- *Unquoted* (`date = 2026-09-15`) is a TOML date literal, so `tomllib` returns
  a `datetime.date` and the string comparisons in `load_catalysts` raised
  `TypeError`. One missing pair of quotes killed the whole run.
- *Unpadded* (`"2026-8-01"`) parses fine but sorts wrong — every filter here is
  a string comparison and `'8' > '0'`, so a date that had already passed
  compared as later than today. It was never filtered out as history, never
  picked up by `resolved_catalysts`, and printed forever as `CATALYST IN -15
  DAYS … size for the outcome, not the chart` — the strongest sizing
  instruction the report emits, on a binary that already resolved.

Unparseable dates are dropped with a warning rather than guessed at. Two
catalysts may share a date (a PDUFA and its AdCom); the archive summary sorts on
`(days_until, symbol)` explicitly, because comparing whole tuples fell through
to comparing the catalyst dicts, which raised out of `main()` and cost the day's
report.

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

**Every field a human edits in `watchlist.toml` must be listed in
`signals.WATCHLIST_FIELDS`**, which is the overlay `main()` applies on top of the
snapshot so a hand edit takes effect without waiting for the next fetch.
`invalidation_price` was missing from it, and from the record `fetch.py` builds,
so `invalidation_breached` was unreachable for every name on the list — 57 of 59
had a stop set and none of them could fire. Nothing looked broken: the tier
logic was untouched, the flag simply never appeared, and the unit test on
`exit_signals()` passed throughout because it built its record by hand. That is
why `test_invalidation_price_reaches_analyse_through_the_watchlist_overlay`
drives `main()` end to end instead. Adding a field to the watchlist means adding
it there too.

## Exposure

`signals.json.exposure` sums what every actionable name would request at its
bucket cap against the investable ceiling (100% less the configured cash
reserve). Several full-size names firing at once can exceed it easily, and
`scale_factor_needed` says by how much. Nothing else computes this, and these
names trigger together in a drawdown.

It counts **SETUP alongside ACT**, which `tiers_counted` publishes so the report
cannot mistake it for a committed figure. That is deliberate: this is the worst
case, not a buy list. SETUP means "research this", and the concentration
question is exactly what happens if the research says yes to all of them at
once.

"Nothing else computes this" was also true of its test coverage until recently.
Both branches are now driven through `main()`: over-committed, where
`scale_factor_needed` is the fraction to multiply every size by, and within the
ceiling, where it is `1.0` rather than absent — the report multiplies by it
unconditionally, so an omitted value would size every position at zero.

## Heartbeat

`pharma-heartbeat.timer` / `com.pharma.heartbeat` (Mon-Fri) alerts if no report
appears for three weekdays — two of genuine slack, plus one for the
gap between a session and the day it is checked on, since reports are named
for the session and a run that loses the race with the provider dates its
report a day back.
**Silence is the desk's normal output**, so a broken run and a quiet market look
identical. Separate unit on purpose: if the main run dies before reaching
notify.py, run_daily.sh's own failure handler dies with it.

**It also checks that the last run could actually deliver.** A report on disk is
not evidence that anything was sent, and watching reports alone meant a wrong
SMTP password read as a perfectly healthy desk: the run wrote its file, the
heartbeat passed, and the phone stayed silent indefinitely. `notify.py` records
each channel's outcome in `data/last_delivery.json` and
`heartbeat.delivery_fault()` is the only place that answer exists.

Two distinctions the record has to keep, or it cries wolf:

- **`channels`** holds the send result, `None` meaning *not attempted*.
  `send_ntfy` and `send_email` both return `False` when they are merely
  unconfigured, which is indistinguishable from failure at the call site, so
  the result is only recorded for a channel `configured_channels()` says is set
  up. Otherwise anyone deliberately running email only gets a broken-ntfy alarm
  every night.
- **`configured`** is what the machine is set up to do, as opposed to what this
  run had occasion to do. Sending nothing is not a fault — a quiet day with
  `EMAIL_ALWAYS=0` correctly sends nothing, and silence is this desk's normal
  output. Having nowhere to send is.

A missing file is not a fault either — it means notify.py has not run since this
was added. The alarm still goes out over the same channels it is reporting on,
which is why the heartbeat also exits non-zero: when the fault *is* delivery,
the scheduler's journal may be the only record that survives.

**Every path out of `notify.main()` writes the record, including the two that
send nothing worth celebrating.** Because a missing file is deliberately not a
fault, any path that returns without writing one is a path whose outcome cannot
be observed — and both of them were exactly the paths where something had
already gone wrong:

- `--failure`, the notice sent when the run itself died, recorded nothing. It is
  the one send with nothing downstream of it to notice that it never arrived.
- An empty config returned before writing anything, which made the "nowhere to
  send" branch above unreachable in the case it most obviously describes: a
  machine with no configuration at all. The fault has to be written down to be
  noticed.

## Market regime

XBI versus its 200-day average, in `signals.json` under `regime`. In a
`downtrend` the ACT tier additionally requires a `strong` conviction score —
signals stay visible, the bar to act rises. Read the current label and
`pct_vs_sma200` from `signals.json`; a figure pinned in this file is stale the
next evening.

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
  This is the `inline_styles=True` default of `md_to_html`; the local archive
  passes `False` and is themed by a real stylesheet instead.
- Tables in an `overflow-x` container, else the page scrolls sideways on a phone.
- **Size is measured, not estimated.** Gmail clips past ~102KB; a wide markdown
  table expands ~11× under inline styles while prose expands ~3×, so budgeting
  by character count fails. `build_email_html` renders, measures, and re-renders
  smaller until it fits, falling back to a body-less summary if it cannot.
- No external CSS, fonts or images — most clients block them.
- **One summary, stamped once per channel.** `build_alert_text` returns what
  happened and nothing else — no date, no product name — and `ntfy_title` and
  `email_subject` each add their own. It previously returned a string that had
  to serve as both a standalone push title and an email subject's suffix, so the
  subject's own prefix duplicated it and a quiet day went out as
  `[biotech desk] 2026-08-15 - Biotech desk 2026-08-15`.

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
- `entry_high` = the **higher** of the window's 25th percentile and a realistic
  pullback level, the latter being the average of the window's last 50 sessions
  capped 5% below spot. A pure distribution percentile is useless for a name in
  a strong uptrend: it returns the price before the re-rating, which the stock
  never revisits if the thesis is working, and SLS at $12.36 was handed a "buy
  zone" of $1.91. Taking the higher of the two means the zone tracks the current
  regime without ever amounting to chasing.

  **Both legs read the anchor window and nothing outside it.** The pullback leg
  averaged `closes[-50:]`, which reaches back through the break for any name
  whose post-collapse window is shorter than 50 sessions — so the prices the
  window exists to exclude came back in through the other half of the same
  `max()`, and the zone rose on exactly the freshest collapses. The 5%-below-spot
  cap hid most of it, which is why it survived: `min()` only lets the average
  through when the name is already trading above it, and when the contamination
  is large the cap fires and the zone silently becomes "5% under wherever this
  happens to trade" — the one outcome the cap exists to prevent. The window is
  never shorter than 25 sessions, so the average always has something to
  describe. Re-running the backtest afterwards moved nothing: the two zone rows
  below are unchanged to the last decimal, because the trailing-1y branch makes
  `window[-50:]` and `closes[-50:]` the same list and only a 25-to-49-session
  post-collapse window can differ.
- `entry_low` = 22% under `entry_high`, a **scale-in reference and not a gate**.
  A price below it is cheaper, not disqualifying — gating on a floor would
  reject a name making new lows while oversold and unvetoed, which is precisely
  the setup this system exists to find. Deciding whether a name is broken is the
  veto layer's job.
- `invalidation_price` = 5% below the anchor window's own low. If the name trades
  under the worst level of the regime the zone was built from, the setup that
  justified the zone no longer exists.

These are mechanical starting points, not valuations. Override any of them by
hand in `watchlist.toml`; re-run with `--apply` to refresh the rest. A name whose
window is too short keeps whatever zone it already has rather than having it
zeroed.

`--apply` **inserts a key the block does not have**, rather than only rewriting
ones already present. It used to do the latter, so a ticker added by hand
without an `invalidation_price` line never got one and `--apply` reported
success anyway — the same shape as the bug that left `invalidation_price`
unreachable for every name on the list: nothing looked broken, the field simply
was not there. Insertions go at the end of that ticker's own block, deepest
first so earlier ones do not shift later line numbers.

The zone arithmetic lives in `zone_from_closes()`, which both `propose()` and
`backtest.py` call. One implementation, so the instrument that justifies the
rule cannot end up scoring a paraphrase of it.

**Zones go stale, and which way they drifted decides what that costs.** A zone
describes the regime it was built from; once price is `ZONE_STALE_DRIFT_PCT`
(25%) away, `zone_stale` is set and a soft flag raised. Refresh with
`propose_zones.py --apply`.

*Above* the zone the failure is silent and total: `in_zone` is false, so ACT
simply never fires, which looks identical to "no opportunity". *Below* it,
`in_zone` is true and **ACT fires normally** — nothing in the tier logic
consults `zone_stale`. The soft flag used to assert "ACT cannot fire for this
name" in both cases, so a name could reach ACT carrying a flag that flatly
denied it, and downward is the direction this desk actually hunts. The text is
now direction-aware. The behaviour is deliberately unchanged, and that is a
**measured** decision, not a default — see "Does a stale zone deserve to open
ACT?" above: the stale-downward subset of the ACT bar performs at least as well
as the fresh one at 20 sessions, so gating it would discard signals no worse
than the ones kept. A price below `entry_low` is cheaper, not disqualifying, and
deciding whether a name that cheap is broken is the veto layer's job.

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

## Monthly screen

`scripts/screen.py` widens the aperture beyond the watchlist. Two stages because
the universe is too large to fetch fully: a cheap prices-only pass over every
biotech-shaped SEC registrant (~590 today, matched by company-name fragments
since the company list carries no sector field), then the normal pipeline on the
shortlist only. Skips anything under $0.50 or below $500k median daily dollar
volume.

**`--limit` defaults to off, and when set it samples rather than truncates.**
`build_universe` sorts for determinism and then took `out[:limit]`, so the cap
was decided by the alphabet: at the old default of 500 against a real universe
of 591, every name from `SPTX` onward was invisible — and invisible *every*
month, since the same 91 were cut on every run. The documented `--limit 400`
hid 191. A blind spot fixed by spelling is the last thing that belongs in the
one tool whose whole job is looking outside the watchlist. A cap now takes an
evenly spaced stride across the full list and prints how many names it skipped,
because a cap nobody is told about reads as "we looked at everything, and this
is what there was".

## Paper trading

`scripts/paper.py` records intended trades against `data/history.sqlite` and
grades them **over the identical holding period against XBI** — a +9% trade
while the sector rose 12% is a losing decision, and absolute P&L says the
opposite. Nothing here is validated forward, so this is what eventually answers
whether the desk beats owning the ETF, at the cost of patience rather than
capital. `run_daily.sh` writes `paper.py status` to `data/paper_status.txt`,
which the daily prompt covers *before* new ideas.

`paper.py report` states that verdict in words, and below
`MIN_TRADES_FOR_A_VERDICT` (20) it **withholds the sentence rather than
qualifying it**. It used to print "NOT beating XBI … buying the ETF would have
been better" off a single closed trade and append "too few to conclude anything"
underneath — which is a conclusion with a disclaimer, not a refusal to conclude,
since the reader has already been handed the verdict by the time the caveat
arrives. The numbers still print; only the sentence that acts on them waits.

Dates typed at this CLI go through `iso_date()`. Every date in `paper_trades` is
compared as a string — against bar dates and against each other — so `2026-1-1`
sorts above `2026-01-14` and picks the wrong bar; it is normalised, and anything
that is not a date at all is refused loudly. A close dated before its own open
is rejected too: the holding period inverts, and `bench_return()` then runs the
benchmark backwards over the same pair with nothing in the output to show it.
This is the third file where a hand-typed date is compared lexically, after the
session date and `catalysts.toml`.

## The local archive

`scripts/publish.py` (`make site`) renders `reports/*.md` into `site/` with an
index, prev/next navigation and per-day stats read from `data/summaries/*.json`.
It reuses `md_to_html()` from `render_email.py` rather than carrying a second
markdown converter, calling it with `inline_styles=False`.

**That flag is not decoration, it is the difference between two documents.** A
mail client strips `<style>` blocks, so the email target puts CSS on every
element. A browser does not — and an inline style beats a stylesheet on every
element, so bolting the site's stylesheet onto email markup meant the archive
rendered near-black `#1a1d21` body text on its own `#0f1216` dark background.
The palette was fully written, fully correct and fully overridden. The site
target therefore emits semantic classes (`.scroll`, `td.pos/.neg/.act/.setup`)
and no inline CSS at all, with `_cell_kind()` shared so both targets agree about
which cells are significant and differ only in how they say so.

**Deliberately local only.** The reports carry entry zones, invalidation levels
and broker routing — the same material `watchlist.toml` is gitignored to protect
— so `site/` is gitignored and nothing is uploaded. One consequence: because the
converter is shared, anything escaping into HTML lands in a real browser here,
not just in a sandboxed mail client. That is why link URLs are quote-escaped —
**and why the scheme is allowlisted too**. Quote-escaping closed the
attribute-breakout hole and left the scheme alone, so `[x](javascript:alert%281%29)`
still rendered as a live handler: no quote required, nothing to escape. Only
`http`, `https`, `mailto` and relative targets become an `<a href>`; anything
else is rendered as inert text saying so. The report is written from WebFetch
and WebSearch content, so this is untrusted input by construction.

`data/summaries/<date>.json` is written by `signals.py` **only on a live run**,
which is the same `--state` test that gates the alert log. A screening pass
borrows the whole module, and before that gate it silently overwrote the real
day's summary and corrupted the archive index.

## Briefing documents

`docs/` holds two hand-written HTML briefings and the PDF built from one of them
(`make brief` → the committed `docs/the-91-percent-question.pdf`; `make briefing`
→ a gitignored internal PDF). Both need headless Chrome. They describe the
project rather than driving it — nothing in the pipeline reads them.

## Conventions

- Stdlib only in `scripts/` — no pip dependencies to rot in a cron job.
- Config in TOML, parsed with stdlib `tomllib`.
- Stage 2 reads no clock. Ages and windows come from the snapshot's session
  date, passed in; `generated_at` is the one wall-clock value, because it
  records when the run happened rather than what it analysed. Stage 1 sets that
  session date from its own bars (`fetch.session_date`), not from
  `date.today()` — see the veto layer for what the difference cost.
- Secrets in `~/.config/pharma/pharma.env`, never in the repo. The older
  `notify.env` is still read so an existing install keeps working, but
  `pharma.env` wins and is the one to write to.
- `data/history.sqlite` accumulates bars and filings so day-over-day deltas and
  "new since last run" work even when a provider has an outage.
- Screening a candidate list must pass **`--screening`** to `signals.py`, and a
  `--state` path of its own. Either marks a run as *not* live, and liveness
  gates every shared artefact: the alert log `score_alerts.py` grades, and the
  per-day summary the archive indexes. It used to be inferred from the state
  file's basename alone, so a screening pass that forgot `--state` was silently
  a live run. Both are kept deliberately — forgetting one is then the same
  mistake as forgetting the other, rather than two independent ways to lose.
- Adding a ticker means one `[[ticker]]` block; CIK and trial sponsor resolve
  automatically from SEC's company list.

## Schedule

`pharma-desk.timer` (Linux) / `com.pharma.desk` (macOS) Mon–Fri 23:18 local —
after the US close in every DST alignment, so the daily bar is settled. On
Linux `Persistent=true` catches up a miss; launchd only coalesces across
sleep, not shutdown — the heartbeat covers the gap.

23:18 Europe/Sofia is 18 minutes after the 16:00 ET close in summer, which is a
race against the provider publishing the daily bar at all. That is fine and
needs no adjustment: the run analyses whatever the newest bar turns out to be
and names itself after that session, so losing the race costs a day of latency
rather than a day of wrong dates.

```bash
# Linux
systemctl --user list-timers 'pharma-*'
journalctl --user -u pharma-desk.service -n 50
# macOS
launchctl print gui/$(id -u)/com.pharma.desk | head -20
tail -50 ~/Library/Logs/pharma-desk.log
# both
./run_daily.sh --no-llm            # data + signals only, fast and free
```

`PHARMA_PYTHON=/path/to/python3` is tried first, then `python3`, then
`~/.local/bin/python3`. It is a preference, not an override: every candidate
must pass the same `tomllib` + `pyexpat` probe, and one that fails warns and
falls through rather than being used.

Before the fetch the run waits for `captive.apple.com` to answer, up to ~2
minutes, then proceeds anyway. It lives here rather than in the scheduler so
that a cron or a hand run gets it too; the launchd desk job therefore does not
wait separately, though the heartbeat — which never comes through this script —
still does.

Two traps in the units themselves, both now pinned by tests:

- **`TimeoutStartSec` has to outlive the run it bounds.** `run_daily.sh` bounds
  every stage itself, and those sum to ~107 minutes worst case. At the old
  `TimeoutStartSec=3600` systemd's SIGTERM arrived 47 minutes early — and it
  arrives *past* the failure handler, so a slow run died with no report and no
  notification, looking exactly like the silence the desk emits on a quiet day.
  Now 7200. The inner bounds do the work; this is the last resort behind them,
  and a last resort that fires first is not one.
- **A bare `ExecStart=` name ignores the unit's own `Environment=PATH`.**
  systemd resolves it against its *compiled-in* search path instead, so
  `ExecStart=python3` silently pins to `/usr/bin/python3` and skips the pyenv
  shim the unit puts first — and a name present only on `Environment=PATH` does
  not launch at all (verified on systemd 255). The heartbeat goes through
  `/usr/bin/env python3`, which is absolute and then does the lookup with the
  inherited PATH. It used to hardcode `%h/.pyenv/shims/python3`: a dead
  `ExecStart` the moment pyenv is removed or has its version unset, in the one
  unit whose whole job is to notice when things have gone quiet. Safe to fall
  back because `heartbeat.py` is stdlib-only and needs neither `tomllib` nor
  `pyexpat`.

### Installed units drift from the repo, silently

Both schedulers copy: systemd units into `~/.config/systemd/user/`, and launchd
bakes the whole job command into the plist at install time. Editing either one
here changes nothing until it is installed again, and the old one keeps working
a release behind — which presents as a bug in the code rather than in the
install. `make check-units` diffs what is installed against what this checkout
would write, for whichever scheduler is present, and exits non-zero on drift.

## Tickers change underneath you

Companies get acquired and renamed. When a symbol stops resolving, `fetch.py`
reports "no CIK in SEC ticker map" and Nasdaq returns "Symbol not exists" — that
is usually a takeout, not a bug. Confirmed during this project: TERN acquired by
Merck, NUVL by GSK, PSTV renamed to Cerenome (CNSY). Re-check unresolved symbols
before assuming the pipeline is broken.
