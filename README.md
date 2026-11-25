# Ventus

Fan curve and CPU power-limit control for the HP OMEN 16-ae0xxx (board 8CC0) on Linux.

## Why

The patched `hp-wmi` driver gives three fan modes. In **auto**, HP's controller (the EC) falls
back to a state that holds the CPU at about 45 W. In **manual**, the driver keeps the EC in its
"user-defined" state, so the CPU gets its full limit, but the fans then sit at one fixed speed.
Ventus runs the fans in manual mode from a temperature curve, which gives full power with
sensible fan speeds.

Measured on 2026-11-25:
- Manual fans at 3300 RPM: 55 W at the HP default limit, 74 W at a 75 W limit.
- HP auto: 44–45 W, whatever the limit is set to.

## Parts

- **`ventusd.py`** is a root service (`ventusd.service`). Every second it:
  - reads the CPU and GPU temperatures, both fans, package power and clocks;
  - sets the fan speed from the curve;
  - keeps the firmware (MMIO) sustained CPU power limit at your chosen value;
  - puts the CPU's own (MSR) limit back after a power cut. On battery HP's firmware lowers it to
    24/30 W, and it doesn't raise it again when the charger comes back.

  GPU temperature comes from `nvidia-smi`, but only while the GPU is already awake, so it never
  wakes an idle GPU.
- **`ventus_gui.py`** is the GTK 4 / libadwaita app. It shows live readings, the fan mode
  (HP Auto / Curve / Max), a draggable fan-curve editor, the power limit and the power profile.
- **`common.py`** holds the shared protocol, config defaults and curve maths.

## Safety

- If the service exits for any reason (stop, crash, watchdog), the fans go back to HP auto.
  The daemon does this itself, and systemd's `ExecStopPost` does it too. HP's original power limit
  is restored.
- If the CPU temperature can't be read in curve mode, the fans go to full speed.
- If the fans read 0 RPM while the CPU is at 80 °C or more, in any mode, the fans run at full speed
  for 60 s.
- If the temperature stays at 95 °C or more for 3 s, the fans go to 100% regardless of the curve.
- Ventus writes the CPU's MSR limits in one case only: on AC (for at least 10 s), when they're
  below what the firmware itself sets for the current profile (Performance 130/130 W, Balanced and
  Low-power 55/130 W). It writes back exactly that value, never more. If the firmware keeps
  lowering it again, Ventus stops after 3 tries and shows a banner. On battery it never touches
  them.
- Anyone can read the status. Only root and the users in `allowed_uids` in
  `/etc/ventus/config.json` can change settings (checked with `SO_PEERCRED`).

## Benchmark

    ventus-bench "curve 75W"     # about 2-3 min; adds a row to the comparison table
    ventus-bench --list          # show the table again

It runs `openssl speed` SHA-512 (plain integer work): a 10 s single-thread run, then all cores for
9 × 10 s. "Sustained" is the all-core speed after 40 s, which is what the power limit controls.
Watts, temperatures and fan speeds come from ventusd. Stop other heavy programs first, or the
"busy" column shows they were running.

## Install / remove

    sudo bash install-ventus.sh               # install or update
    sudo bash install-ventus.sh --uninstall

Logs: `journalctl -u ventusd`

## Tests

The tests need no root and no hardware; the daemon runs against a fake sysfs tree:

    /usr/bin/python3 -sE -m unittest discover -s tests -v

## License

MIT, see [LICENSE](LICENSE).
