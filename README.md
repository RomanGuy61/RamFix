# ramfix

A small cross-platform command-line utility for inspecting memory usage and
asking the operating system to release cached / stale memory. On Linux it can
also inspect and change swap settings and swap files.

> **An honest note first:** modern operating systems are already very good at
> managing RAM. "Memory cleaners" rarely make a system permanently faster, and
> dropping caches can *temporarily slow things down* because the data has to be
> read from disk again. Use this tool deliberately for troubleshooting, not as
> a background tuner. It never kills processes or changes persistent settings
> unless you explicitly ask it to.

## Install as a `RamFix` command (anywhere)

```bash
python install.py            # system-wide if root, otherwise per-user
python install.py --user     # force per-user install
python install.py --prefix /opt/ramfix
python install.py --uninstall
```

The installer:

- copies the `ramfix` package into a stable library directory;
- creates a launcher named `RamFix` (`RamFix.cmd` on Windows) that runs the
  installed copy, so moving this source folder won't break it;
- also installs `ramfix` and `RAMFIX` aliases (symlinks on case-sensitive
  filesystems). On Windows/macOS with case-insensitive filesystems a single
  launcher already answers to every spelling;
- adds the launcher directory to your PATH (a marked block in your shell config
  on Linux/macOS, or the user PATH in the Windows registry). Use `--no-path` to
  skip this;
- checks for `psutil` and installs it if missing (`--no-deps` to skip).

After a per-user install, open a new terminal or `source ~/.bashrc` (or your
shell's config) before running the command.

```bash
RamFix --help          # also works as: ramfix --help, RAMFIX --help
RamFix status
```

To run directly from this folder without installing:

```bash
pip install -r requirements.txt
python main.py --help          # or: python -m ramfix --help
```

`psutil` is the only dependency. Python 3.9+ is supported (developed on 3.15).

## Commands

```bash
RamFix status                  # memory usage report (RAM + swap)
RamFix top -n 15               # processes using the most RAM
RamFix clean                   # drop caches / trim working sets (default)
RamFix clean --swap            # cycle swap (Linux, needs root)
RamFix clean --gc              # run Python garbage collection
RamFix clean --all -y          # every action, no prompts
RamFix clean --dry-run         # preview without changing anything
RamFix monitor                 # live summary, Ctrl-C to stop
RamFix doctor                  # full check-up + safe cleanup
```

### Change swap (Linux only)

```bash
RamFix swap status                    # swap devices and current swappiness
RamFix swap swappiness                # read vm.swappiness
RamFix swap swappiness 60             # set it (0-200, needs root)
RamFix swap on                        # enable all swap (swapon -a)
RamFix swap off                       # disable all swap (swapoff -a)
RamFix swap off /swapfile             # disable one device/file
RamFix swap create 2G /swapfile       # create, format and enable a swap file
RamFix swap resize 4G /swapfile       # resize an existing swap file
RamFix swap resize 4G --persist       # ...and update /etc/fstab for the new UUID
```

Notes on swap changes:

- All mutating swap operations need root (or passwordless `sudo -n`). The tool
  prints the exact command to run manually if privileges are missing.
- `swap on/off`, `swap create` and `swap resize` ask for confirmation; pass
  `-y` to skip it.
- `swap resize` disables swap briefly, resizes the file, runs `mkswap` and
  re-enables it. If free RAM cannot hold the swapped-out pages, `swapoff` fails
  safely and nothing changes.
- Resizing changes the swap UUID. By default the tool warns you; with
  `--persist` it updates `/etc/fstab` automatically after making a timestamped
  backup (`/etc/fstab.ramfix-<timestamp>.bak`).
- `swap create --persist` appends a matching swap line to `/etc/fstab` (also
  with a backup).

## What each cleanup action does

| Platform | `clean` (cache) | `clean --swap` |
|---|---|---|
| Linux | `sync` then writes `3` to `/proc/sys/vm/drop_caches` (pagecache, dentries, inodes) | `swapoff -a && swapon -a` |
| Windows | Calls `EmptyWorkingSet` on every accessible process via `ctypes` | not supported |
| macOS | Runs the built-in `purge` command | not supported |

`clean --gc` runs Python's garbage collector for **this app's process only** —
it is included for completeness rather than as a meaningful system-wide action.

On Linux/macOS, privileged actions are attempted with non-interactive
`sudo -n`. On Windows, run the terminal as Administrator to trim other
processes' working sets; the app also tries to enable `SeDebugPrivilege`.

## Project layout

```
ramfix/
  __init__.py    package metadata
  __main__.py    python -m ramfix entry point
  cli.py         argparse commands and output formatting
  stats.py       psutil-based memory/process statistics
  cleaner.py     platform-specific cleanup actions
  swap.py        Linux swap inspection and configuration
  utils.py       platform detection, formatting, privileges, colour
main.py          in-place launcher
install.py       installs/updates/removes the global RamFix command
tests/           unit tests (no privileges required)
```

## Safety

- No process is ever terminated.
- No system configuration files are modified unless `--persist` is given, and
  then only after a backup is created.
- Swap cycling/resizing asks for confirmation and fails safely if free RAM is
  too low.
- `--dry-run` previews cache cleanup actions without touching the system.

## Tests

```bash
python -m unittest discover -s tests -v
```
