# Strategy — template

Copy to `STRATEGY.local.md` and fill in. That file is gitignored: it holds the
part of the system that is *yours* rather than shared code — how much you are
willing to lose on one name, where you trade, and what you are actually trying
to do. The engineering lives in `CLAUDE.md` and is public-safe.

Keep this file short. It is read at the start of any sizing, routing or
"should I buy this" question, so density matters more than completeness.

---

## Objective

One or two sentences. What is this capital for, and over what horizon? Be
specific enough that a suggestion can be judged against it.

> Example: Speculative satellite capital, 2–3 year horizon, hunting a small
> number of multi-baggers. Drawdowns are acceptable; permanent loss of the whole
> allocation is not.

## Risk posture

The numeric ceilings live in `watchlist.toml` under `[settings]` so that code
and documentation cannot drift apart. State the *reasoning* here, not the
numbers:

- Why that per-name ceiling, and what a total loss at that size would mean
- Whether you scale in, and on what evidence
- What you will never do (e.g. average down into a damaged thesis)
- Cash reserve and what it is reserved *for*

## Buckets

What each `tier` in `watchlist.toml` means to you, in your words. The code only
uses these to set a sizing ceiling; the meaning is yours.

| Bucket | What it means | How you size it |
|---|---|---|
| `A` | | |
| `B` | | |
| `lottery` | | |

## Brokers and execution

Which venues you actually use and what each is good for. This matters more than
it sounds on illiquid names, where routing is a real share of the P&L.

- Broker 1 — order types available, when to use it
- Broker 2 — limitations, when *not* to use it
- Your default order type, and when you would ever use a market order

## Personal constraints

Anything that should shape a recommendation: tax treatment, currency, position
limits imposed elsewhere, times you cannot trade, instruments you will not touch.

## What I want from the daily report

What is genuinely useful to you versus noise. Be blunt — this directly shapes
what gets written.
