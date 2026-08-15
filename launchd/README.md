# Timers (macOS)

The launchd equivalent of `systemd/`. Same two jobs, same schedule.

```bash
launchd/install-launchd.sh              # install or reinstall; idempotent
launchd/install-launchd.sh --uninstall  # remove both jobs
```

- **com.pharma.desk** — Mon–Fri 23:18 local. The US close (16:00 ET) lands at
  22:00–23:00 Europe/Sofia in every daylight-saving alignment, so the daily bar
  is settled.
- **com.pharma.heartbeat** — Mon–Fri 10:23 local. Alerts if no report has
  appeared for two weekdays. A separate job on purpose: if the desk run dies
  before reaching `notify.py`, its own failure handler dies with it.

```bash
launchctl kickstart -k gui/$(id -u)/com.pharma.desk   # run now
launchctl print gui/$(id -u)/com.pharma.desk | head   # state, runs, last exit
tail -f ~/Library/Logs/pharma-desk.log
```

Note that a kickstarted job still waits out its startup jitter (the equivalent of
the systemd units' `RandomizedDelaySec`) — up to 4 minutes for the desk, 10 for
the heartbeat — before it does anything.

## Why the installer resolves paths instead of the plist

launchd starts jobs from a clean environment, and `zsh -lc` is a login but
non-interactive shell: it sources `.zprofile` but **not** `.zshrc`. Anything a
version manager puts on `PATH` interactively is therefore invisible to the job.
`install-launchd.sh` resolves `claude` and a usable `python3` at install time and
bakes their directories into the installed plist. Re-run it if either moves.

"Usable" means `import tomllib, pyexpat` succeeds. `tomllib` is the 3.11+ floor;
`pyexpat` parses the Form 4 ownership XML. An interpreter without it does not
fail — it drops the insider layer, marks the snapshot `degraded`, and produces a
report that looks entirely normal, so both the installer and `run_daily.sh`
check for it explicitly.

## Differences from the systemd units

| systemd | launchd |
|---|---|
| `OnCalendar=Mon-Fri 23:18` | `StartCalendarInterval` array, `Weekday` 1–5 |
| `RandomizedDelaySec=` | `sleep $((RANDOM % n))` inside the job |
| `Persistent=true` | implicit: a missed calendar job runs at the next wake |
| `Environment=PATH=…` | `__PATH_EXTRA__`, baked in at install time |
| `journalctl --user -u …` | `~/Library/Logs/pharma-*.log` |

There is no launchd equivalent of `loginctl enable-linger`: user agents run
whenever the user is logged in, and a job missed while the machine was asleep or
shut down fires once on the next wake.
