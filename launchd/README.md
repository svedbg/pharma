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

Note that a kickstarted job still waits out its startup jitter (the equivalent
of the systemd units' `RandomizedDelaySec`) — up to 4 minutes for the desk, 10
for the heartbeat — before it does anything.

## Why the plists are generated, not shipped

`install-launchd.sh` writes both plists with `plistlib` at install time. Two
reasons:

1. **Paths cannot be trusted in a template.** A project directory containing
   `&`, `|`, `<`, `>` or a quote breaks a sed substitution three different ways
   (sed syntax, XML validity, shell quoting inside the command string).
   Generation shell-quotes and XML-escapes each value in its proper context.
2. **launchd starts jobs from a clean environment.** `zsh -lc` sources
   `.zprofile` but not `.zshrc`, so version-managed binaries are invisible. The
   installer resolves `claude` and a vetted `python3` and bakes the python in
   as an **absolute path** via `EnvironmentVariables/PHARMA_PYTHON` — which
   `run_daily.sh` tries first. Prepending the interpreter's *directory* to PATH
   is not enough: on a stock Mac `/usr/local/bin/python3` is Apple's 3.9 and
   would shadow it.

"Vetted" means `import tomllib, pyexpat` succeeds. `tomllib` is the 3.11+
floor; `pyexpat` parses the Form 4 ownership XML. An interpreter without it
does not fail — it drops the insider layer, marks the snapshot `degraded`, and
produces a report that looks entirely normal, so the installer, the job command
and `run_daily.sh` all check it explicitly. Re-run the installer if either
binary moves.

## Differences from the systemd units

| systemd | launchd |
|---|---|
| `OnCalendar=Mon-Fri 23:18` | `StartCalendarInterval` array, `Weekday` 1–5 |
| `RandomizedDelaySec=` | `sleep $((RANDOM % n))` inside the job |
| `TimeoutStartSec=3600` | no equivalent key — every stage is bounded inside `run_daily.sh` (`run_with_timeout`) |
| `Persistent=true` | **partial**: see below |
| `Environment=PATH=…` | `EnvironmentVariables` dict, baked at install time |
| `journalctl --user -u …` | `~/Library/Logs/pharma-*.log` (stdout and stderr share one file, as journald interleaved them) |

**The `Persistent=true` parity is only partial.** launchd coalesces calendar
events missed during *sleep* into one run at the next wake. A machine powered
**off** across the trigger time skips that day entirely — there is no catch-up
on boot, unlike `Persistent=true`. For a laptop that is shut down overnight,
the heartbeat job is what surfaces the gap: two missed weekdays raise the
stale alert.

There is no launchd equivalent of `loginctl enable-linger`: user agents run
whenever the user is logged in.
