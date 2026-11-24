# ramfix 2.0

A cross-platform utility for inspecting memory usage and asking the operating
system to release cached / stale memory. Version 2.0 adds **RAM compression
controls**, **per-app RAM limits**, and a **background guard** that cleans
automatically on a RAM threshold or when selected apps start. It ships with
both a CLI and a Tkinter GUI.

> **An honest note first:** modern operating systems are already very good at
> managing RAM. "Memory cleaners" rarely make a system permanently faster, and
> dropping caches can *temporarily slow things down* because the data has to be
> read from disk again. Use these tools deliberately, not as a background tuner.
> Nothing here kills processes unless the kernel OOM-kills an app you set a
> limit on.

## Install as a `RamFix` command (anywhere)

```bash
python install.py            # system-wide if root, otherwise per-user
python install.py --user     # force per-user install
python install.py --prefix /opt/ramfix
python install.py --uninstall
```

The installer copies the package to a stable library directory, creates a
`RamFix` launcher (`RamFix.cmd` on Windows), installs `ramfix` / `RAMFIX`
aliases on case-sensitive filesystems, checks for `psutil`, and adds the
launcher directory to PATH (`--no-path` / `--no-deps` to skip those steps).

After a per-user install, open a new terminal or source your shell config.

```bash
RamFix --help
RamFix status
```

To run without installing: `pip install -r requirements.txt`, then
`python main.py --help` or `python -m ramfix --help`.

Python 3.9+, `psutil` required. Tkinter is only needed for the GUI.

## CLI commands

### Core

```bash
RamFix status                  # memory usage report
RamFix top -n 15               # processes using the most RAM
RamFix clean                   # drop caches / trim working sets
RamFix clean --swap            # cycle swap (Linux)
RamFix clean --gc              # Python garbage collection
RamFix clean --all -y
RamFix clean --dry-run
RamFix monitor                 # live CLI summary, Ctrl-C to stop
RamFix doctor                  # check-up + safe cleanup
RamFix gui                     # desktop GUI (alias: RamFix ui)
```

### Swap (Linux)

```bash
RamFix swap status
RamFix swap swappiness [0-200]       # omit value to read
RamFix swap on [DEVICE]              # default: all
RamFix swap off [DEVICE]
RamFix swap create 2G /swapfile
RamFix swap resize 4G /swapfile
RamFix swap resize 4G --persist      # update /etc/fstab for the new UUID
```

### Compression (Linux)

See the kernel's zram/zswap compression state, and change the compressor or
zram disk size:

```bash
RamFix compression status
RamFix compression algorithm zstd -d zram0
RamFix compression size 4G -d zram0
RamFix compression zswap on
RamFix compression zswap off
RamFix compression compressor zstd
```

`compression status` shows each zram device's algorithm, size, compression
ratio and compressed/orig sizes, plus global zswap parameters. The reported
ratio comes from `/sys/block/zram*/mm_stat` (`orig_data_size / compr_data_size`).

Changing an algorithm or size briefly disables swap on that device and
reformats it — requires root. Available algorithms are read from
`/sys/block/zram*/comp_algorithm`, so only values your kernel supports are
accepted. zswap changes write to `/sys/module/zswap/parameters/` and need a
kernel with zswap support.

### Per-app RAM limits (Linux, cgroup v2)

```bash
RamFix limit list
RamFix limit set 1234 512M --name firefox
RamFix limit remove firefox
RamFix limit clear
```

Limits are implemented with cgroup v2 `memory.max`, in cgroups named
`ramfix-<name>` so they are easy to manage. `limit set` moves the given PID
into its own cgroup and caps its RAM. `limit remove` moves its processes back
to the root cgroup and deletes the cgroup. All of these require root.

> **Warning:** if the limit is too low, the kernel can OOM-kill the app. Pick a
> value comfortably above its normal working set.

### Automation / guard

```bash
RamFix guard show                          # current settings
RamFix guard threshold 85                  # clean when RAM reaches 85%
RamFix guard cooldown 60                   # at most one clean per 60s
RamFix guard watch add firefox
RamFix guard watch remove firefox
RamFix guard watch list
RamFix guard flag clean_on_threshold on
RamFix guard flag clean_on_app_start off
RamFix guard run                           # start the daemon, Ctrl-C to stop
```

Settings are stored in `~/.config/ramfix/config.json` (override with
`XDG_CONFIG_HOME`). `guard run` polls every 2 seconds (change with
`-i SECONDS`) and:

- **On RAM threshold** — when usage reaches the configured percentage, it runs
  the cache clean once, then waits for the cooldown.
- **On app start** — when a watched process name appears that was not present
  on the previous poll, it runs the cache clean once.

Both triggers can be toggled independently with `guard flag`. The daemon never
kills anything. Because cache cleaning needs root, run it as
`sudo RamFix guard run` or configure passwordless `sudo`; otherwise it will
report that cleaning failed.

## Desktop GUI

```bash
RamFix gui        # or RamFix ui, python -m ramfix gui
```

Six tabs:

| Tab | Contents |
|---|---|
| Overview | RAM/swap usage bars, cache figures, swappiness, clean/cycle/doctor buttons |
| Processes | Top 15 resident-memory processes, live |
| Swap | Swappiness slider, swap devices, create/resize swap file |
| Compression | zram/zswap report, algorithm and size controls, zswap toggles |
| App limits | Active cgroup limits, add/remove controls |
| Automation | Threshold, cooldown, trigger flags, watched-app list, start/stop guard |

There is an auto-refresh toggle for the Overview and an activity log. Privileged
and slow actions run in worker threads so the window stays responsive; all
destructive actions ask for confirmation.

On Linux, Tkinter may need a separate package (e.g. `python3-tk` on
Fedora/Debian). The CLI does not need it.

## What each cleanup action does

| Platform | `clean` (cache) | `clean --swap` |
|---|---|---|
| Linux | `sync` + writes `3` to `/proc/sys/vm/drop_caches` | `swapoff -a && swapon -a` |
| Windows | `EmptyWorkingSet` on accessible processes | not supported |
| macOS | built-in `purge` | not supported |

Compression, per-app limits and automation are Linux-only; other platforms get
a clear "not supported" message.

## Project layout

```
ramfix/
  __init__.py     version
  __main__.py     python -m ramfix
  cli.py          CLI commands
  stats.py        psutil memory/process stats
  cleaner.py      cache cleanup actions
  swap.py         swap configuration
  compression.py  zram / zswap controls
  limits.py       cgroup v2 per-app RAM limits
  automation.py   guard config + threshold/app-start daemon
  gui.py          Tkinter interface
  utils.py        platform, formatting, privileges
main.py           in-place launcher
install.py        global RamFix command installer
tests/            unit tests
```

## Tests

```bash
python -m unittest discover -s tests -v
```
