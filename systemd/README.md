# Timers

Copy into `~/.config/systemd/user/`, then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now pharma-desk.timer pharma-heartbeat.timer
systemctl --user list-timers 'pharma-*'
```

`%h` expands to the user's home directory, so no paths need editing as long as
the project lives at `~/projects/pharma`.

- **pharma-desk** — Mon–Fri 23:18 local. The US close lands at 22:00–23:00
  Europe/Sofia in every daylight-saving alignment, so the daily bar is settled.
- **pharma-heartbeat** — Mon–Fri 10:23 local. Alerts if no report has appeared
  for two weekdays. Deliberately a separate unit: if the main run dies before
  reaching `notify.py`, its own failure handler dies with it.

`Persistent=true` on both catches up a run missed while the machine was off.
Requires `loginctl enable-linger $USER` so user timers fire without a login session.

## After pulling

The units are *copied*, so an edit here does nothing until it is copied again
and `daemon-reload` has run — quietly, with the old unit still working one
release behind.

```bash
./systemd/check-units.sh   # do the installed units still match this checkout?
make check-units           # the same question for both schedulers
```
