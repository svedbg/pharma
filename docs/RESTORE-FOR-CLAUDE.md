# Restoring this desk — instructions for Claude Code

**You are reading this because someone asked you to restore the biotech desk on
a new machine, or after a loss.** Follow it in order. It is written for an agent
with shell access, and every step has a check you can actually run.

The desk is a personal end-of-day research system for a small-cap pharma
watchlist. It runs unattended on a timer and emails a report. Read `CLAUDE.md`
in the repo root before changing anything — this file covers only restoring.

## The one thing to understand first

The code is public and reclones in seconds. **The data does not exist anywhere
else.** These are the irreplaceable files:

| File | What is lost with it |
|---|---|
| `watchlist.toml` | the trading plan — names, buckets, entry zones, invalidation levels |
| `catalysts.toml` | the catalyst calendar the nightly run has accumulated, entry by sourced entry |
| `STRATEGY.local.md` | objective, risk posture, bucket meanings, broker preferences |
| `data/history.sqlite` | every bar, filing, alert, run and paper trade the desk has ever recorded |
| `state/alerts.json` | which alerts have already fired — without it the next run re-notifies everything |

So the governing rule for this whole procedure: **never overwrite one of these
with anything, and never delete one, without the user explicitly confirming
that specific file.** If a restore target already has them, stop and ask. A
wrong `cp` here is not recoverable.

## Preflight — do this before touching anything

```bash
uname -s                                    # Linux or Darwin: the scheduler differs
python3 -c 'import tomllib, pyexpat; print("interpreter ok")'
git --version
ls ~/projects/pharma 2>/dev/null && echo "CHECKOUT ALREADY EXISTS -- see below"
```

`pyexpat` is not optional. Without it the Form 4 insider layer silently
disappears from every report — no error, just a missing section. If the import
fails, find another interpreter and set `PHARMA_PYTHON=/path/to/python3`; do not
proceed with one that fails this probe.

**If a checkout already exists**, this may be a repair rather than a fresh
restore. Inventory before acting, and report what you find to the user rather
than merging blind:

```bash
cd ~/projects/pharma
for f in watchlist.toml catalysts.toml STRATEGY.local.md data/history.sqlite state/alerts.json; do
  printf '%-26s %s\n' "$f" "$(test -e "$f" && echo PRESENT || echo absent)"
done
```

## Scenario A — restoring from the migration bundle

The bundle is `pharma-desk-migration-<date>.zip`. It contains `RESTORE.md`,
`restore.sh`, `MANIFEST.txt`, `config/` and `repo/`.

```bash
# 1. the code
mkdir -p ~/projects
git clone https://github.com/svedbg/pharma.git ~/projects/pharma

# 2. the private layer
unzip -q ~/pharma-desk-migration-<date>.zip -d ~/restore
cd ~/restore/pharma-desk-migration
./restore.sh ~/projects/pharma
```

`restore.sh` refuses to overwrite anything that already exists and prints
`skip (exists)` for each — that refusal is a feature, not a failure. Do **not**
reach for `--force` to silence it. If it skipped something the user wanted
replaced, ask which files, and confirm each one.

The units expand `%h` and assume the project sits at `~/projects/pharma`. If it
must live elsewhere, the systemd unit files need editing before installation —
say so rather than installing units that point at nothing.

## Scenario B — restoring from a `make backup` archive

Archives are `~/pharma-backups/pharma-backup-<stamp>.tar.gz`, newest last.

```bash
ls -lt ~/pharma-backups/ | head
mkdir -p ~/restore && tar xzf ~/pharma-backups/pharma-backup-<stamp>.tar.gz -C ~/restore
```

Verify the database **before** copying anything over a live checkout:

```bash
python3 -c "
import sqlite3
c = sqlite3.connect('$HOME/restore/pharma-backup-<stamp>/repo/data/history.sqlite')
print(c.execute('pragma integrity_check').fetchone()[0])
print({t: c.execute(f'select count(*) from {t}').fetchone()[0]
       for t in ('bars','filings','alerts','runs')})"
```

`integrity_check` must print `ok`. If it does not, **stop** — try an older
archive rather than restoring a corrupt database over a working one.

Then copy `repo/` contents into the checkout, honouring the never-overwrite rule
above, and `config/pharma.env` to `~/.config/pharma/pharma.env` with `chmod 600`.

## The credential

`SMTP_PASS` is stubbed in both the bundle and every backup — it reads
`PASTE_YOUR_GMAIL_APP_PASSWORD_HERE`. It is a Gmail **app password** (16 chars,
requires 2FA), not the account password.

**You cannot supply this. Do not guess, generate, or invent one.** Tell the user
to create one at <https://myaccount.google.com/apppasswords> and paste it in, or
to run `$EDITOR ~/.config/pharma/pharma.env` themselves. Everything else in that
file is already filled in, including `NTFY_TOPIC` — which must match exactly or
the user's phone stops receiving.

Email stays off until it is set; ntfy and the rest of the desk work regardless.
That is a fine state to leave things in — say so rather than blocking.

## Verify — run all of these, report the results

```bash
cd ~/projects/pharma
make setup && make check          # 295 tests as of 2026-08-31
```

`test_catalyst_file.py` has four tests that **skip** when `catalysts.toml` is
absent. Skips there mean the calendar did not arrive — check for them
explicitly with `-rs` rather than reading a green run as success:

```bash
.venv/bin/pytest tests/test_catalyst_file.py -q -rs
```

The desk is stdlib-only and the `sqlite3` CLI is often not installed. Count
through Python:

```bash
python3 -c "
import sqlite3; c = sqlite3.connect('data/history.sqlite')
print({t: c.execute(f'select count(*) from {t}').fetchone()[0]
       for t in ('bars','filings','alerts','runs')})"
```

Then a real run that fetches and computes but sends nothing and calls no LLM:

```bash
./run_daily.sh --no-llm           # 2-4 minutes over ~60 names
python3 scripts/brief.py | head -40
```

`brief.py` needs `data/signals.json`, so it only works **after** that run. If
you call it first it correctly refuses and tells you so — that is not a fault.

## Scheduling

```bash
# Linux
cp systemd/pharma-*.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pharma-desk.timer pharma-premarket.timer pharma-heartbeat.timer
loginctl enable-linger $USER
# macOS
./launchd/install-launchd.sh
# both
make check-units                  # must exit 0; units are copied and drift silently
```

**Check the timezone before trusting any of it.** The hours are local and were
measured for Europe/Sofia: desk Tue–Sat 09:00, pre-market Mon–Fri 14:30,
heartbeat Mon–Fri 10:23. The 14:30 pass is 07:30 ET from Sofia and **must stay
before the 09:30 ET open** — that is the one property it cannot lose. On a
machine in another zone, work out the offset and tell the user what the hours
need to become; do not silently install times that put the "pre-market" pass
after the open.

## Stop and ask the user

Do not decide these alone:

- Any irreplaceable file already exists at the target and differs from what you
  are about to write.
- `integrity_check` on a restored database returns anything but `ok`.
- The old machine may still be running its timers. **Both machines running is
  the one genuinely damaging outcome**: each keeps its own `state/alerts.json`
  and `runs` table, there is no merge, and both will fetch, report and notify.
  Confirm the old machine is disabled:
  `systemctl --user disable --now pharma-desk.timer pharma-premarket.timer pharma-heartbeat.timer`
- The checkout cannot live at `~/projects/pharma`.
- The machine is in a timezone where the measured hours no longer hold.

## When you are done

Report plainly: what was restored, the row counts, whether `make check` was
green, whether any test skipped, whether email is still waiting on a password,
and whether the timers are installed and verified. If something was left
undone, say which and why — a half-restored desk that looks finished is worse
than one that is obviously incomplete, because silence is this desk's normal
output and a broken run looks exactly like a quiet market.

Then set up backups, since the restore has just proved why they matter:

```bash
make backup
```
