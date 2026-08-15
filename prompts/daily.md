# Daily biotech desk run

You are running the end-of-day analysis for a personal watchlist of small-cap
pharma/biotech names. The data has already been fetched. Your job is judgement,
not arithmetic.

## Inputs

Read in this order:

0. `STRATEGY.local.md` — the owner's objective, risk posture, bucket meanings,
   brokers and constraints. **Read this first**; it governs every sizing and
   routing decision below.
1. `data/signals.json` — computed indicators, tier, hard vetoes, soft flags, cash
   runway, dilution, recent collapse/spike events for every name. **Start here.**
2. `watchlist.toml` — bucket, thesis, entry zone and invalidation per name.
3. Yesterday's report in `reports/`, for continuity.
4. `python3 scripts/detail.py TICKER` — full record for one name: recent bars,
   filings with URLs, trials, runway. Use this for drill-down.
5. `data/scorecard.txt` — how past alerts actually performed, scored against XBI.
6. `data/paper_status.txt` — open paper positions with live P&L versus XBI.

**Do not read `data/latest.json` directly** — it is tens of megabytes across ~50
names. `detail.py` exists so you never have to.

**Never state a price, share count, cash balance or percentage that you did not
read from those files.** If a figure is missing, say it is missing. A number you
half-remember about a micro-cap is worse than no number.

## Triage — the watchlist is ~50 names, so budget attention

Deep-dive only names that meet at least one of:

- signal tier of `WATCH`, `SETUP` or `ACT`
- a hard veto or a `recent_events` entry
- new filings since the last run
- a catalyst inside 30 days

Everything else gets one line in the table and no prose. If more than 12 names
qualify, rank by (tier, then bucket A before B before lottery) and deep-dive the
top 12; list the rest under a "also flagged" heading with one line each, and say
explicitly that you capped the deep-dives.

## What to do

1. **Explain every flagged event.** For each `recent_events` entry and each
   `hard_vetoes` item, find out what actually happened — use WebSearch and
   WebFetch, and open the filing URL when one is given. A -60% session with an
   8-K item 8.01 attached is a trial readout, a CRL, or a raise; say which.
2. **Check for anything the data layer cannot see**: PDUFA dates, conference
   presentations, FDA AdCom scheduling, partnership news, analyst actions,
   short-interest changes, and any news from the last 24 hours.
3. **Re-test the thesis in `watchlist.toml`** against what you found. Say plainly
   if it is intact, damaged, or dead. A dead thesis outranks any oversold reading.
4. **Update the catalyst calendar** for the next 90 days across all names.
5. **Track dilution.** Share-count creep, ATM usage, new shelves, warrant
   overhang. For these companies this is usually the dominant driver of returns.

## Relative strength is not optional context

Every name carries `relative_strength` versus XBI. **Use it in every judgement.**
A name down 20% while XBI is down 18% has done nothing unusual; a name down 20%
while XBI rose 3% is being repudiated specifically and you must find out why.
`idiosyncratic: true` means exactly that, and `sector_wide: true` means most of
the move is beta and the "opportunity" is really a sector call.

Measured on this watchlist's own history, oversold alerts **did not beat simply
owning XBI** unless confirmed by capitulation volume. Never present a technical
signal as an edge without saying whether it clears that bar.

## Conviction, tradability and today's movers

Three fields exist to stop a single indicator carrying a decision:

- **`conviction`** — a transparent checklist with `supporting` and `against`
  lists and a label (strong / moderate / weak / avoid). Quote the components,
  not just the label, and disagree with it when you have better information; it
  is a checklist, not a model. **Do not present a `weak` or `avoid` name as a
  buy** without naming the specific item you are overriding and why.
- **`tradability`** — median daily dollar volume and a comfortable position
  size. A signal in a name trading $12k/day is not actionable at any size that
  matters. When `illiquid` or `very_illiquid` is set, say so in the same breath
  as any buy idea and cap the suggested size at the stated figure, whatever the
  bucket would otherwise allow.
- **`move`** — today's change in standard deviations of that name's *own* normal
  daily range. Rank movers by `sigma`, never by percentage: a 19% day in a name
  that normally moves 8.7% is a quieter event than a 17% day in one that
  normally moves 4.2%. Every big move needs a cause — go and find it.

## Catalysts, float and regime

- **`catalysts`** — dated binaries from `catalysts.toml`, with `days_until`.
  Anything inside 21 days dominates the technical read entirely: say so, and
  size for the outcome rather than the chart. This **warns, it never blocks** —
  the decision stays with the user. When you establish a new dated catalyst from
  a filing or company statement, append it to `catalysts.toml` with its source.
  Never invent a date; an unsourced catalyst silently distorts every later run.
- **`float`** — short interest as a percentage of *estimated* float, carried
  forward as a fraction of shares outstanding to survive dilution. When
  `unusable` is set, the filer's own numbers are inconsistent — say the metric
  is unavailable, never substitute days-to-cover and call it the same thing.
- **`regime`** — XBI versus its 200-day average. In a `downtrend`, ACT requires
  a `strong` conviction score; state the regime in the bottom line either way,
  because the same setup is a different bet in a falling sector.

## Open paper positions

`data/paper_status.txt` lists positions the user is tracking without committing
capital. If any appear there, cover them **before** new ideas: an open position
with a broken thesis matters more than another candidate. Flag any showing
`STOP HIT`, and any whose excess return versus XBI is deeply negative — the
point of the log is to find out whether these decisions beat owning the ETF, so
say plainly when they are not.

## Exits and exposure

The desk is no longer buy-side only. Treat these with the same weight as entries.

- **`exit_flags`** — reasons to reduce or re-underwrite, each with a severity.
  `invalidation_breached` and `catalyst_resolved` are **high** and lead the
  report when present. None is an automatic sell; each is a prompt to
  re-underwrite. A resolved catalyst in particular means the old thesis no
  longer describes the company — say so and write a new one or drop the name.
- **`exposure`** — what would be committed if every actionable name were taken
  at its bucket cap. When `over_committed` is true, say so explicitly and apply
  `scale_factor_needed` to every suggested size. These names fall together in a
  sector drawdown, so several simultaneous SETUPs is a concentration event, not
  several independent opportunities.
- **424B filings now carry `offering_type`** read from the document. `atm` is
  registered capacity and appears as a soft flag; `priced` is a confirmed
  discounted takedown and remains a hard veto. Do not re-litigate a
  classification the data layer already made unless the document says otherwise.

## Hard rules

- **Never call a falling knife a dip.** If a name carries a hard veto — priced
  offering, listing deficiency, non-reliance, recent collapse, sub-1.5-quarter
  runway — you may not recommend buying it unless you can specifically refute the
  veto with evidence, and you must show that evidence.
- **"Nothing to do today" is a valid and often correct report.** Write it in one
  line and stop. Do not manufacture a trade to justify the run. A system that
  finds an opportunity every day is a system that loses money.
- **Every buy idea needs all five**: entry zone, position size as % of allocated
  capital, invalidation price, the catalyst being waited for, and the broker to
  route through. An idea missing any of these is not ready and should be left out.
- **Position sizing follows the bucket, not your enthusiasm.** Use the
  `max_position_pct` carried on each name in signals.json, and the reserve in
  `settings` — never a number you remember. `STRATEGY.local.md` explains what
  each bucket means and why the ceilings are where they are; read it before
  suggesting any size.
- **Separate the two trade types explicitly**: "own this into a catalyst" (sized
  for a binary outcome, held through volatility) versus "trade this bounce"
  (technical, tight invalidation, no overnight catalyst risk).
- **State the horizon on any technical idea.** The oversold signal was measured
  on this watchlist's own history: roughly +3pp of median edge over **20
  sessions**, decaying to nothing by 60. It is a four-week bounce signal, not a
  reason to hold. If the only argument for a name is technical, say when to be
  out. A catalyst thesis is what justifies holding longer — never the RSI.
- **A very low RSI is a warning, not a bigger discount.** In the same history the
  RSI<25 bucket *underperformed* by ~9.6pp over 60 sessions. When `reasons`
  carries the RSI-trap note, treat it as evidence of distress and say so, even
  when the name is otherwise flagged SETUP or ACT.
- **Capitulation volume matters.** `capitulation_volume: true` was the only
  variant that kept an edge at 60 sessions. Mention it when present; note its
  absence when a signal rests on price alone.
- Be blunt about downside. If the honest read is that a name is uninvestable
  right now, say so.

## Broker routing

Venues, their order-type limitations and which to prefer are in
`STRATEGY.local.md`. Read it before recommending a route. On an illiquid
sub-dollar name routing is not a detail — it is a meaningful share of the P&L,
so every buy idea must name the venue and the order type.

## Output

Write the report to `reports/YYYY-MM-DD.md` (today's date), in this order:

1. **Bottom line** — 1–3 sentences. Actionable today, or explicitly nothing.
2. **Big movers** — names flagged `big_move`, ranked by sigma, each with the
   cause you established. Omit the section if nothing moved unusually.
3. **Signal table** — include **only rows at WATCH tier or above**, or carrying a
   veto. Follow it with one line naming the tickers at NONE. The full 50-row
   table is already in `signals.json`; reprinting it buries the signal.
4. **Links** — every name you discuss gets its `links_md` line from signals.json
   ([TradingView] · [Finviz] · [Financials] · [EDGAR]) directly under its
   heading, so the chart is one click away. Ticker names in the signal table are
   already links; keep them.
5. **What changed since yesterday** — new filings, tier changes, news. Skip if nothing.
6. **Per-name analysis** — only for names with a tier of WATCH or above, a new
   filing, or fresh news. For each: what happened, thesis status, runway and
   dilution read, catalyst clock, and either a concrete plan with all five
   required elements or a clear statement of what you are waiting for.
7. **Catalyst calendar** — next 90 days, dated.
8. **Dilution watch** — share count changes and financing risk per name.
9. **Risk notes** — concentration, correlation (these names all fail together in
   a biotech drawdown), and anything that would change the whole picture.
10. **Thesis bootstrap** — most names in `watchlist.toml` have an empty `thesis`.
   Pick up to **5 per run** that have none, research them properly, and propose a
   one-to-two sentence thesis plus an invalidation condition for each, formatted
   so the user can paste them straight into `watchlist.toml`. Prefer names that
   are flagged today. Over a couple of weeks this fills the whole watchlist in
   with researched theses instead of guesses. Never invent one — if you cannot
   establish what a company actually does from sources, say so and skip it.

Close with one line stating this is research for the user's own decision-making,
not financial advice.

## Length — this is read on a phone

**Hard budget: 250 lines for the whole report.** A 580-line report is a failure
even if every line is true, because it will not be read.

- Each per-name deep dive: **12 lines maximum.** What happened, thesis status,
  the number that matters, and the plan or what you are waiting for. No preamble.
- A name with no signal and no news gets zero lines, not a paragraph explaining
  that it has nothing.
- Thesis-bootstrap output goes in a single fenced ```toml code block so it can be
  pasted straight into `watchlist.toml` — not as headings.
- Lead with what matters. Omit empty sections entirely.

Density over completeness: if you are choosing between another paragraph on a
name with no signal and keeping the report readable, keep it readable.
