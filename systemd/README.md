# Timers

Copy into `~/.config/systemd/user/`, then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now pharma-desk.timer pharma-premarket.timer pharma-heartbeat.timer
systemctl --user list-timers 'pharma-*'
```

`%h` expands to the user's home directory, so no paths need editing as long as
the project lives at `~/projects/pharma`.

- **pharma-desk** — **Tue–Sat 09:00** local, analysing the session that closed
  the previous evening. The hour is measured, not derived from the close: the
  `runs` table shows Nasdaq had not published the day's bar at any evening hour
  ever observed (16:25–18:40 ET) and had published it by 01:52 ET the following
  morning, which is 08:52 Sofia. Mon–Fri 23:18 and then Tue–Sat 01:30 were both
  argued from the close, and both lost on every single scheduled run — every
  report was named for the session *before* the one that had just closed. The
  day spec has to move with the hour: at 09:00 the run still covers the previous
  calendar day, so Mon–Fri would skip Monday's session and analyse Friday's
  twice.
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
