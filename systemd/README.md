# Timers

Copy into `~/.config/systemd/user/`, then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now pharma-desk.timer pharma-premarket.timer pharma-heartbeat.timer
systemctl --user list-timers 'pharma-*'
```

`%h` expands to the user's home directory, so no paths need editing as long as
the project lives at `~/projects/pharma`.

- **pharma-desk** — **Tue–Sat 01:30** local, analysing the session that closed
  the previous evening. The US close lands at 22:00–23:00 Europe/Sofia in every
  daylight-saving alignment, so this is 2½–3½ hours after the closing print and
  the price provider has actually published the daily bar. At the old Mon–Fri
  23:18 it had not, on every single scheduled run, and every report was named
  for the previous session. The day spec has to move with the hour: at 01:30 the
  run covers the previous calendar day, so Mon–Fri would skip Monday's session
  and analyse Friday's twice.
- **pharma-premarket** — Mon–Fri 14:30 local (07:30 ET), about two hours before
  the US open. The overnight news and filings pass; see "The pre-market pass" in
  `CLAUDE.md`. Optional — the desk works without it.
- **pharma-heartbeat** — Mon–Fri 10:23 local. Alerts if no report has appeared
  for three weekdays. Deliberately a separate unit: if the main run dies before
  reaching `notify.py`, its own failure handler dies with it.

`Persistent=true` on the desk and heartbeat catches up a run missed while the
machine was off. The pre-market timer sets `Persistent=false` on purpose: a
missed pre-market pass is worthless, and catching it up at 17:00 would email a
"pre-market" note about a session already hours into trading — worse than
silence, because it reads as current.

Requires `loginctl enable-linger $USER` so user timers fire without a login session.

## After pulling

The units are *copied*, so an edit here does nothing until it is copied again
and `daemon-reload` has run — quietly, with the old unit still working one
release behind.

```bash
./systemd/check-units.sh   # do the installed units still match this checkout?
make check-units           # the same question for both schedulers
```
