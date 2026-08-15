# Biotech desk

An unattended daily research desk for small-cap pharma. Every weekday evening it
pulls SEC filings, prices, insider trades and short interest for a watchlist,
computes signals, writes a report, and pushes a phone notification **only when
something is genuinely worth acting on** — which most days it isn't.

Research support for your own decisions. Not financial advice, and it places no
orders.

## What makes it different from a screener

**It refuses to call a falling knife a dip.** Mechanical cheapness is trivial to
compute and dangerous alone: in this sector most large drawdowns are correctly
pricing a broken thesis. A veto layer blocks any buy signal sitting on top of a
priced offering, a delisting notice, an auditor change, a recent collapse, or a
balance sheet with under 1.5 quarters of cash — until that veto is specifically
refuted with evidence.

**Its thresholds are measured, not chosen.** `backtest.py` replays the stored
history and `score_alerts.py` grades real alerts, both scored against **XBI**
rather than an unbuyable all-days average. That distinction changed the design:
the oversold trigger *does not beat simply owning the ETF* unless confirmed by
capitulation volume, so the top tier now requires it.

**Facts and judgement are separate.** `fetch.py` and `signals.py` are
deterministic and never estimate. The reasoning pass runs on their output and
can be argued with. A price the model half-remembers never reaches the report.

## Requirements

- Python **3.11+** (stdlib only — no pip install, nothing to rot in a cron job)
- Linux with systemd user timers, or any scheduler that can run one shell script
- API keys: **none**. Every data source is free and keyless.

## Setup

### 1. Clone and configure

```bash
git clone https://github.com/svedbg/pharma.git ~/projects/pharma
cd ~/projects/pharma
mkdir -p ~/.config/pharma
cp pharma.env.example ~/.config/pharma/pharma.env
chmod 600 ~/.config/pharma/pharma.env
$EDITOR ~/.config/pharma/pharma.env
```

**`SEC_CONTACT_EMAIL` is required** — SEC's fair-access policy demands a real
contact address in the User-Agent and throttles requests without one. Everything
else is optional; leave a section blank to disable that channel.

The config lives outside the repository on purpose, so no credential can be
committed by accident.

### 2. Build your watchlist

```bash
cp watchlist.example.toml watchlist.toml
```

`watchlist.toml` is gitignored — it is your trading plan, not shared code. Only
`symbol` is required — the SEC CIK and the
ClinicalTrials.gov sponsor resolve automatically from the company name.

```toml
[[ticker]]
symbol = "XYZ"
tier = "A"            # A | B | lottery — sets the position-size ceiling
thesis = ""           # leave blank until researched; the daily run proposes these
entry_low = 0         # set by propose_zones.py
entry_high = 0
invalidation_price = 0
invalidation = "what would prove this wrong"
```

`watchlist.example.toml` carries three names so the repo runs out of the box.
Replace them with your own.

### 3. Describe your strategy

```bash
cp STRATEGY.example.md STRATEGY.local.md
$EDITOR STRATEGY.local.md
```

Also gitignored. It holds the part that is yours rather than shared code:
objective, risk posture, what each bucket means, brokers, constraints. The
analysis pass reads it before making any sizing or routing suggestion.

Numeric ceilings live in `watchlist.toml [settings]`, not here, so code and
documentation cannot drift apart.

### 4. First run

```bash
./run_daily.sh --no-llm     # data + signals only: fast, free, no LLM calls
```

That populates `data/` and prints the signal table — about four minutes for 60
names. Then set entry zones, derived from each name's own trading range:

```bash
python3 scripts/propose_zones.py            # dry run — review the proposals
python3 scripts/propose_zones.py --apply    # write them into watchlist.toml
```

### 5. The analysis pass (optional)

The full run adds a reasoning step that researches flagged names, confirms or
refutes vetoes against the source filings, and writes the report. It shells out
to [Claude Code](https://claude.com/claude-code):

```bash
./run_daily.sh              # full run: fetch, signals, analysis, notify
```

Without it you still get every signal, veto and alert — just no written
analysis. Swap the `claude -p` line in `run_daily.sh` for any other CLI that
accepts a prompt and can write files.

### 6. Schedule it

```bash
cp systemd/pharma-*.service systemd/pharma-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pharma-desk.timer pharma-heartbeat.timer
loginctl enable-linger "$USER"     # so timers fire without a login session
```

Two timers, both weekday-only:

- **pharma-desk** at 23:18 — after the US close, so the daily bar is settled.
  Adjust `OnCalendar` for your timezone.
- **pharma-heartbeat** at 10:23 — alerts if no report appeared for two weekdays.
  Separate on purpose: **silence is this system's normal output**, so a broken
  run and a quiet market look identical without it.

## Reading the output

| Tier | Meaning |
|---|---|
| `NONE` | nothing |
| `WATCH` | approaching oversold: RSI<40 and %B<0.25 |
| `SETUP` | oversold (RSI<35, %B<0.15) and no hard veto. **A shortlist to research, not a buy** |
| `ACT` | SETUP, at or below `entry_high`, confirmed by capitulation volume, financing and catalyst backdrop acceptable |

The gap between SETUP and ACT is the whole point: oversold *without* volume
confirmation underperformed XBI by 2.7pp median over 60 sessions. SETUP means
"the filter passed and nothing is obviously broken" — not "buy this".

Notifications fire on tier *changes* into SETUP/ACT and on new high-severity
exit signals, so a name oversold for three weeks doesn't buzz nightly. Exits
lead: a thesis breaking outranks an idea appearing.

## Commands

```bash
./run_daily.sh --no-llm                      # data + signals, no analysis
python3 scripts/detail.py CAPR               # everything known about one name
python3 scripts/propose_zones.py             # entry zones from each name's range
python3 scripts/backtest.py                  # score the rules against a baseline
python3 scripts/score_alerts.py              # grade real alerts against XBI
python3 scripts/heartbeat.py --status        # is the desk still running?
python3 scripts/notify.py --failure "test"   # test the alert channels
```

### Monthly screen

The daily run only ever looks at names already on the watchlist, so the best
opportunity in the sector can pass by unseen. Once a month, widen the aperture:

```bash
make screen        # ~500 biotech registrants outside your list, prices only
```

It ranks by the same thresholds the desk uses and writes a TOML candidate file.
A shortlist to research, not a buy list.

### Paper trading

Nothing here is validated forward. Record intended trades and grade them against
the ETF before committing capital:

```bash
python3 scripts/paper.py open XYZ --size 15 --stop 12.06 --horizon catalyst
python3 scripts/paper.py status         # open positions, live P&L vs XBI
python3 scripts/paper.py close XYZ --note "catalyst passed"
python3 scripts/paper.py report         # did it actually beat the ETF?
```

`report` says plainly whether the trades beat XBI, and refuses to let you
over-read a small sample.

## Development

```bash
make setup     # dev virtualenv: ruff + pytest (the runtime needs neither)
make check     # lint + tests, exactly what CI runs
```

The test suite is regression protection, not decoration: every case maps to a
bug that actually shipped here — an RSI seeded from the wrong end of the series,
liquidity understated tenfold by a missing XBRL tag, a float ratio of 15,401%,
an inline `#` comment silently disabling email. `test_runtime_has_no_third_party_imports`
fails the build if anything under `scripts/` grows a dependency.

CI runs on Python 3.11 and 3.12 — both, because a 3.12-only f-string once slipped
past a 3.11 floor.

## Data sources

| What | Source |
|---|---|
| Daily OHLCV, short interest | Nasdaq API (Yahoo fallback) |
| Filings, 8-K item codes, Form 4 insider trades, XBRL financials | SEC EDGAR |
| Daily short volume | FINRA Reg SHO |
| Trials and completion dates | ClinicalTrials.gov v2 |
| News, PDUFA dates | web search during the analysis pass |

## Layout

```
watchlist.toml     your names, buckets, entry zones, invalidation levels (gitignored)
STRATEGY.local.md  your objective, risk posture, brokers (gitignored)
catalysts.toml     dated binaries (PDUFA, AdCom, readouts) with sources
scripts/           fetch, signals, helpers, notification, scoring
prompts/daily.md   the standing instruction for the analysis pass
systemd/           timer units
data/ logs/ reports/ state/    generated and personal — all gitignored
```

`CLAUDE.md` documents the architecture, the veto rules, the measured thresholds,
and a catalogue of XBRL traps that each produced a wrong number before being
fixed. Read it before changing anything in `signals.py`.

## What it deliberately will not do

- Recommend a name carrying an unrefuted hard veto
- Manufacture a trade to justify the run — "nothing to do today" is a valid and
  frequent output
- Size a lottery-bucket name like a conviction position
- Report a number it did not read from a primary source

## Caveats

The backtests carry survivorship bias (acquired and delisted names are absent),
cover a strongly bullish sample, and model no transaction costs — micro-cap
spreads are wide. Treat them as a smoke test of the thresholds, not proof of
edge. The honest answer to "does this work" arrives only once live alerts have
aged, which is what `score_alerts.py` exists to measure.

## Data source terms

Worth knowing before you rely on this:

- **SEC EDGAR and FINRA** publish this data openly and expect a descriptive
  User-Agent with contact details, which is why `SEC_CONTACT_EMAIL` is required.
  Stay under 10 requests/second; the code rate-limits itself.
- **Nasdaq and Yahoo endpoints are undocumented.** They are not published APIs,
  carry no stability guarantee, and may change or block without notice — Yahoo
  already rate-limits by IP aggressively enough that Nasdaq is the primary
  source here. Treat both as best-effort, and check their terms if you intend
  anything beyond personal research.

The design assumes sources will break: prices try two providers, every fetch
failure is recorded rather than silently defaulted, and a run that loses all
price data aborts instead of reporting on nothing.

## Licence

MIT. No warranty; you are responsible for your own trades.
