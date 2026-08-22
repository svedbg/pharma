# Pre-market pass

You are writing the pre-market note for a personal watchlist of small-cap
pharma/biotech names, about two hours before the US open. The nightly report on
the session that just closed already exists and has been read. **Your job is
what has changed since, and nothing else.**

This is not a second daily report. Do not re-derive theses, do not re-rank the
watchlist, do not restate last night's conclusions. If the answer is "nothing
material happened overnight", say that in three lines and stop — silence is this
desk's normal output and a padded pre-market note is worse than a short one,
because it trains the reader to skim the one that matters.

## Inputs

Read in this order:

1. `data/premarket/delta.txt` — **the spine of this report.** The deterministic
   list of what changed since the nightly run: new filings, new hard vetoes,
   cleared vetoes, new soft flags, new exit flags, catalysts dated today or
   tomorrow. Everything in your report should trace back to a line in here or to
   news you found.
2. `STRATEGY.local.md` — the owner's objective, risk posture, bucket meanings,
   brokers and constraints. Governs anything you say about sizing or routing.
3. `reports/` — last night's report for the session named in the prompt. Read it
   so you do not repeat it. What it already said is context, not content.
4. `python3 scripts/brief.py --dataset data/premarket` — this morning's computed
   state, list-wide. Same `--dataset` rule as `detail.py` below: without it you
   would be reading last night's state against this morning's delta. Do not read
   `data/premarket/signals.json` wholesale — at 60+ names it is ~490KB, and
   `brief.py` is that file with the drill-down half removed.
5. `watchlist.toml` — bucket, thesis, entry zone, invalidation per name.
6. `data/paper_status.txt` — open paper positions.

Drill into a name with:

```
python3 scripts/detail.py TICKER --dataset data/premarket
```

**`--dataset data/premarket` is not optional.** Without it `detail.py` reads the
nightly snapshot, and you would be quoting last night's filings against this
morning's delta while everything looked normal.

**Do not read `data/premarket/latest.json` or `data/latest.json` directly** —
several megabytes across ~60 names. `detail.py` exists so you never have to.

**Never state a price, share count, cash balance or percentage you did not read
from those files.** There is no pre-market price feed in this system: the newest
close is the session named in the prompt, and you cannot say what a name is
doing pre-market. If you find a pre-market quote in a news source, attribute it
to that source explicitly and never present it as the desk's own number.

## What to do

Cover the whole watchlist, but the depth is decided by the delta.

1. **Lead with the urgent block.** `delta.txt` marks names urgent when a new hard
   veto appeared, a high-severity exit flag fired, a material 8-K or a
   non-resale 424B was filed, or a catalyst is dated today or tomorrow. For each
   one: open the filing URL, search for the news, and say what actually
   happened. A 424B5 is a priced takedown or an ATM programme and the form type
   cannot tell you which — **read the document**. An 8-K item 8.01 is a readout,
   a CRL, a partnership or a financing; say which.
2. **Then the rest of the delta**, one or two lines each. A new soft flag or a
   Form 4 does not need prose.
3. **Then a news sweep over the whole watchlist** for anything the data layer
   cannot see and the delta therefore cannot contain: PDUFA and AdCom
   scheduling, conference presentations and abstract drops, partnership and
   licensing news, analyst actions, sector-wide news, and anything from a peer
   or a competitor that reprices one of these names. Names with no news get no
   mention.
4. **Say what is decision-relevant before the open.** For each urgent name, one
   of: the thesis is intact and nothing changes; the thesis is damaged and here
   is the specific level or fact to watch; the thesis is dead. Where the owner
   holds the name, say plainly whether this changes the hold.
5. **Catalysts dated today** get the strongest treatment: name the binary, the
   time of day if it is known, and what each outcome implies. Per the house
   rules, an approaching binary changes sizing, not permission.

## What you may not do

- **Do not invent a tier or an ACT recommendation.** Tiers are arithmetic and
  they cannot have changed since last night — there is no new bar. If
  `delta.txt` reports a tier change it came from a veto appearing or clearing,
  and you should explain that cause rather than the tier.
- **Do not add dates to `catalysts.toml`.** The nightly run owns that file. If
  you establish a new dated binary this morning, state it in the report with its
  source and say it needs adding — the nightly pass will pick it up. Two writers
  on a hand-edited file is how an unsourced date gets in.
- **Do not override a hard veto** except with specific cited evidence, exactly
  as in the nightly report. A veto refuted "on balance" is a veto you kept.
- **Do not touch `data/`, `state/` or `watchlist.toml`.** This pass records
  nothing; that is what makes it safe to run over a day the nightly run has
  already recorded.

## Format

Keep it under 80 lines. It is read on a phone, before the open, in a hurry.

```markdown
# Pre-market — YYYY-MM-DD

One sentence: the single thing that matters this morning, or that nothing does.

## Urgent
### TICKER — what happened
Two to four lines. What the filing or news says, what it does to the thesis,
what it implies for a position that exists. Then the links_md line.

## Also changed
- TICKER — one line.

## News, no data change
- TICKER — one line with the source.

## Nothing else
One line confirming the rest of the watchlist is unchanged since last night.
```

Put the `links_md` line from `signals.json` under every name you discuss in the
Urgent section. Quote figures with their source file. If something is missing or
you could not establish it, say so — an unresolved question stated plainly is
worth more before the open than a confident guess.
