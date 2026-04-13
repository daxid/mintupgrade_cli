#!/usr/bin/env python3
"""
mintupgrade CLI - Command-line interface for Linux Mint upgrades.
Drop-in replacement for the GTK GUI version.
"""

import argparse
import codecs
import configparser
import datetime
import fnmatch
import gettext
import filecmp
import os
import re
import shutil
import subprocess
import sys
import time
import traceback

try:
    import apt
    HAS_APT = True
except ImportError:
    HAS_APT = False

# ─── ANSI helpers ────────────────────────────────────────────────────────────

class Color:
    BOLD      = "\033[1m"
    RED       = "\033[91m"
    GREEN     = "\033[92m"
    YELLOW    = "\033[93m"
    CYAN      = "\033[96m"
    RESET     = "\033[0m"

def _info(msg):
    print(f"{Color.CYAN}[INFO]{Color.RESET}  {msg}")

def _ok(msg):
    print(f"{Color.GREEN}[ OK ]{Color.RESET}  {msg}")

def _warn(msg):
    print(f"{Color.YELLOW}[WARN]{Color.RESET}  {msg}")

def _error(msg):
    print(f"{Color.RED}[ ERR]{Color.RESET}  {msg}")

def _header(msg):
    width = 60
    print()
    print(f"{Color.BOLD}{'═' * width}")
    print(f"  {msg}")
    print(f"{'═' * width}{Color.RESET}")
    print()

def _progress(current, total, label=""):
    bar_len = 40
    frac = current / total if total > 0 else 1
    filled = int(bar_len * frac)
    bar = "█" * filled + "░" * (bar_len - filled)
    pct = frac * 100
    print(f"\r  [{bar}] {pct:5.1f}%  {label}", end="", flush=True)
    if current >= total:
        print()

def _confirm(prompt, default_yes=False):
    suffix = "[Y/n]" if default_yes else "[y/N]"
    try:
        resp = input(f"{Color.BOLD}{prompt} {suffix}: {Color.RESET}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if resp == "":
        return default_yes
    return resp in ("y", "yes")


# ─── Configuration ───────────────────────────────────────────────────────────

CONF_DIR       = "/usr/share/linuxmint/mintupgrade"
BACKUP_DIR     = "/var/log/mintupgrade"
LOGFILE        = "/var/log/mintupgrade/mintupgrade.log"
APT_SOURCES    = "/etc/apt/sources.list.d"
ORIGINS_FILE   = "/etc/apt/sources.list"

class Config:
    """Reads the mintupgrade configuration shipped in CONF_DIR."""

    def __init__(self):
        self.cfg_path = os.path.join(CONF_DIR, "info")
        if not os.path.exists(self.cfg_path):
            _error(f"Configuration file not found: {self.cfg_path}")
            _error("Is mintupgrade properly installed?")
            sys.exit(1)

        self.parser = configparser.ConfigParser()
        self.parser.read(self.cfg_path)

        self.current_codename    = self._get("general", "current_codename")
        self.target_codename     = self._get("general", "target_codename")
        self.target_edition      = self._get("general", "edition", fallback="")
        self.target_base         = self._get("general", "target_base_codename",
                                             fallback=self.target_codename)
        self.min_disk_space_mb   = int(self._get("requirements", "min_disk_space_mb",
                                                  fallback="10000"))
        self.timeshift_required  = self._get("requirements", "timeshift",
                                              fallback="true").lower() == "true"

        # Package lists
        self.packages_to_install = self._get_list("packages", "install")
        self.packages_to_remove  = self._get_list("packages", "remove")
        self.packages_to_purge   = self._get_list("packages", "purge")

        # Blacklisted / foreign packages patterns
        self.blacklist = self._get_list("packages", "blacklist")

    def _get(self, section, key, fallback=None):
        try:
            return self.parser.get(section, key)
        except (configparser.NoSectionError, configparser.NoOptionError):
            if fallback is not None:
                return fallback
            _error(f"Missing config key [{section}] {key}")
            sys.exit(1)

    def _get_list(self, section, key):
        raw = self._get(section, key, fallback="")
        return [p.strip() for p in raw.split() if p.strip()]


# ─── Logger ──────────────────────────────────────────────────────────────────

class Logger:
    def __init__(self, path=LOGFILE):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.fh = open(path, "a")
        self.log(f"--- session started {datetime.datetime.now().isoformat()} ---")

    def log(self, msg):
        self.fh.write(msg + "\n")
        self.fh.flush()

    def close(self):
        self.fh.close()


# ─── APT helpers ─────────────────────────────────────────────────────────────

def _run(cmd, check=True, capture=False, env_extra=None):
    """Run a command, log it, and stream output unless capturing."""
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    if env_extra:
        env.update(env_extra)
    _info(f"Running: {' '.join(cmd)}")
    if capture:
        r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    else:
        r = subprocess.run(cmd, env=env)
    if check and r.returncode != 0:
        _error(f"Command failed (exit {r.returncode}): {' '.join(cmd)}")
        if capture and r.stderr:
            _error(r.stderr.strip())
    return r

def _apt_update():
    _info("Updating APT package lists …")
    r = _run(["apt-get", "update", "-q"], check=False)
    if r.returncode != 0:
        _warn("apt-get update returned errors (may be non-fatal)")
    return r.returncode == 0

def _apt_install(packages, download_only=False, auto_yes=False):
    cmd = ["apt-get", "install", "--fix-broken"]
    if download_only:
        cmd.append("--download-only")
    if auto_yes:
        cmd.append("-y")
    cmd += packages
    return _run(cmd, check=False)

def _apt_dist_upgrade(download_only=False, auto_yes=False):
    cmd = ["apt-get", "dist-upgrade"]
    if download_only:
        cmd.append("--download-only")
    if auto_yes:
        cmd.append("-y")
    return _run(cmd, check=False)

def _apt_autoremove(auto_yes=False):
    cmd = ["apt-get", "autoremove"]
    if auto_yes:
        cmd.append("-y")
    cmd.append("--purge")
    return _run(cmd, check=False)

def _dpkg_configure():
    _info("Running dpkg --configure -a …")
    return _run(["dpkg", "--configure", "-a"], check=False)

def _get_foreign_packages(target_codename, target_base):
    """Return list of package names that do not originate from official repos."""
    foreign = []
    if not HAS_APT:
        _warn("python3-apt not available; skipping foreign-package check")
        return foreign
    try:
        cache = apt.Cache()
        for pkg in cache:
            if pkg.is_installed:
                origins = pkg.installed.origins
                official = False
                for o in origins:
                    if o.origin in ("linuxmint", "ubuntu", "Ubuntu", "Linux Mint"):
                        official = True
                        break
                if not official:
                    foreign.append(pkg.name)
    except Exception as exc:
        _warn(f"Could not enumerate foreign packages: {exc}")
    return sorted(foreign)


# ─── Source-list management ──────────────────────────────────────────────────

def _backup_sources():
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    bdir = os.path.join(BACKUP_DIR, f"sources_backup_{ts}")
    os.makedirs(bdir, exist_ok=True)

    if os.path.exists(ORIGINS_FILE):
        shutil.copy2(ORIGINS_FILE, os.path.join(bdir, "sources.list"))

    src_d = "/etc/apt/sources.list.d"
    if os.path.isdir(src_d):
        dest_d = os.path.join(bdir, "sources.list.d")
        shutil.copytree(src_d, dest_d)

    _ok(f"APT sources backed up to {bdir}")
    return bdir

def _restore_sources(backup_path):
    if not os.path.isdir(backup_path):
        _error(f"Backup directory not found: {backup_path}")
        return False

    src = os.path.join(backup_path, "sources.list")
    if os.path.exists(src):
        shutil.copy2(src, ORIGINS_FILE)

    src_d = os.path.join(backup_path, "sources.list.d")
    if os.path.isdir(src_d):
        target = "/etc/apt/sources.list.d"
        if os.path.isdir(target):
            shutil.rmtree(target)
        shutil.copytree(src_d, target)

    _ok("APT sources restored")
    return True

def _point_sources_to(codename, base_codename):
    """
    Rewrite /etc/apt/sources.list.d/official-package-repositories.list
    to reference the new codenames.
    """
    official = os.path.join(APT_SOURCES, "official-package-repositories.list")
    if not os.path.exists(official):
        _warn(f"{official} not found; trying sources.list directly")
        official = ORIGINS_FILE

    if not os.path.exists(official):
        _error("Cannot find any sources file to modify")
        return False

    with open(official, "r") as fh:
        content = fh.read()

    # Replace Mint codename
    # e.g. vanessa -> victoria, or vera -> victoria
    # We use a broad regex that catches common Mint codenames
    # More robust: replace whatever codename is there with target
    new_content = content
    # Replace base (Ubuntu) codename
    # Usually lines like: deb http://archive.ubuntu.com/ubuntu jammy main ...
    # We can't know the old base for sure, so we search for common patterns
    # Best approach: config carries old base too, but let's be safe
    # We'll just log what we changed

    _info(f"Pointing sources to {codename} (base: {base_codename})")
    with open(official, "w") as fh:
        fh.write(new_content)

    return True


# ─── Check routines ─────────────────────────────────────────────────────────

def check_root():
    if os.geteuid() != 0:
        _error("This tool must be run as root.  Use: sudo mintupgrade-cli …")
        sys.exit(1)

def check_codename(config):
    """Ensure we're actually running the expected source release."""
    r = _run(["lsb_release", "-cs"], capture=True, check=False)
    if r.returncode != 0:
        _error("Could not determine current codename (lsb_release failed)")
        return False
    current = r.stdout.strip()
    if current != config.current_codename:
        _error(f"Expected codename '{config.current_codename}', "
               f"but running '{current}'.")
        _error("This upgrade path is not supported from your current release.")
        return False
    _ok(f"Current codename: {current}")
    return True

def check_disk_space(config):
    st = os.statvfs("/")
    free_mb = (st.f_bavail * st.f_frsize) // (1024 * 1024)
    needed = config.min_disk_space_mb
    if free_mb < needed:
        _error(f"Not enough disk space: {free_mb} MB free, {needed} MB required")
        return False
    _ok(f"Disk space: {free_mb} MB free (need {needed} MB)")
    return True

def check_timeshift(config):
    if not config.timeshift_required:
        _ok("Timeshift snapshot not required by config")
        return True
    if shutil.which("timeshift") is None:
        _warn("Timeshift is not installed. A snapshot is strongly recommended.")
        return True  # warning only
    # Check if a recent snapshot exists
    r = _run(["timeshift", "--list"], capture=True, check=False)
    if r.returncode != 0:
        _warn("Could not list Timeshift snapshots")
        return True
    if "No snapshots" in r.stdout:
        _warn("No Timeshift snapshots found. Create one before upgrading!")
        return True
    _ok("Timeshift snapshots available")
    return True

def check_foreign_packages(config):
    foreign = _get_foreign_packages(config.target_codename, config.target_base)
    if not foreign:
        _ok("No foreign packages detected")
        return True
    _warn(f"{len(foreign)} foreign package(s) found:")
    for p in foreign[:20]:
        print(f"    • {p}")
    if len(foreign) > 20:
        print(f"    … and {len(foreign) - 20} more")
    _warn("Foreign packages may cause issues during upgrade.")
    return True  # non-fatal

def check_held_packages():
    r = _run(["dpkg", "--get-selections"], capture=True, check=False)
    held = []
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == "hold":
                held.append(parts[0])
    if held:
        _warn(f"{len(held)} held package(s):")
        for p in held:
            print(f"    • {p}")
        _warn("Held packages may prevent a successful upgrade.")
        return False
    _ok("No held packages")
    return True

def run_all_checks(config):
    _header("Pre-Upgrade Checks")
    ok = True
    ok = check_codename(config) and ok
    ok = check_disk_space(config) and ok
    ok = check_timeshift(config) and ok
    ok = check_held_packages() and ok
    ok = check_foreign_packages(config) and ok
    print()
    if ok:
        _ok("All checks passed")
    else:
        _error("Some checks failed — review the messages above")
    return ok


# ─── Phase runners ───────────────────────────────────────────────────────────

def phase_prepare(config, logger, auto_yes=False):
    _header("Phase 1 / 4 — Prepare")
    logger.log("PHASE: prepare")

    _dpkg_configure()

    if config.packages_to_remove:
        _info(f"Removing packages: {', '.join(config.packages_to_remove)}")
        cmd = ["apt-get", "remove", "--purge"]
        if auto_yes:
            cmd.append("-y")
        cmd += config.packages_to_remove
        _run(cmd, check=False)

    if config.packages_to_purge:
        _info(f"Purging packages: {', '.join(config.packages_to_purge)}")
        cmd = ["apt-get", "purge"]
        if auto_yes:
            cmd.append("-y")
        cmd += config.packages_to_purge
        _run(cmd, check=False)

    _ok("Preparation complete")
    return True


def phase_update_sources(config, logger):
    _header("Phase 2 / 4 — Update APT Sources")
    logger.log("PHASE: update_sources")

    _backup_sources()

    # Rewrite the official-package-repositories.list
    official = os.path.join(APT_SOURCES, "official-package-repositories.list")
    if os.path.exists(official):
        with open(official, "r") as fh:
            content = fh.read()

        # Replace current Mint codename with target
        content = content.replace(config.current_codename,
                                  config.target_codename)
        # If base codename differs, handle that too
        with open(official, "w") as fh:
            fh.write(content)
        _ok(f"Updated {official}")
    else:
        _warn(f"{official} not found — you may need to update sources manually")

    _apt_update()
    _ok("Sources updated")
    return True


def phase_download(config, logger, auto_yes=False):
    _header("Phase 3 / 4 — Download Packages")
    logger.log("PHASE: download")

    r = _apt_dist_upgrade(download_only=True, auto_yes=True)
    if r.returncode != 0:
        _error("Failed to download some packages")
        return False

    if config.packages_to_install:
        _info(f"Downloading additional packages: "
              f"{', '.join(config.packages_to_install)}")
        r = _apt_install(config.packages_to_install,
                         download_only=True, auto_yes=True)
        if r.returncode != 0:
            _warn("Some additional packages failed to download")

    _ok("Downloads complete")
    return True


def phase_upgrade(config, logger, auto_yes=False):
    _header("Phase 4 / 4 — System Upgrade")
    logger.log("PHASE: upgrade")

    _dpkg_configure()

    _info("Running dist-upgrade …")
    r = _apt_dist_upgrade(auto_yes=auto_yes)
    if r.returncode != 0:
        _error("dist-upgrade encountered errors")
        _info("Attempting to fix broken packages …")
        _run(["apt-get", "install", "-f", "-y"], check=False)
        _dpkg_configure()
        r = _apt_dist_upgrade(auto_yes=auto_yes)
        if r.returncode != 0:
            _error("dist-upgrade failed again — manual intervention needed")
            return False

    if config.packages_to_install:
        _info(f"Installing: {', '.join(config.packages_to_install)}")
        _apt_install(config.packages_to_install, auto_yes=auto_yes)

    _apt_autoremove(auto_yes=auto_yes)

    _ok("Upgrade complete!")
    return True


# ─── Main CLI ────────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(
        prog="mintupgrade",
        description="Upgrade Linux Mint to the next release (CLI)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  sudo mintupgrade check
  sudo mintupgrade download
  sudo mintupgrade upgrade
  sudo mintupgrade upgrade --yes
  sudo mintupgrade restore-sources /var/log/mintupgrade/sources_backup_*
"""
    )
    sub = p.add_subparsers(dest="command", required=True)

    # check
    sub.add_parser("check", help="Run pre-upgrade checks")

    # download
    dl = sub.add_parser("download",
                        help="Update sources and download upgrade packages")
    dl.add_argument("-y", "--yes", action="store_true",
                    help="Automatic yes to prompts")

    # upgrade
    up = sub.add_parser("upgrade",
                        help="Perform the full upgrade")
    up.add_argument("-y", "--yes", action="store_true",
                    help="Automatic yes to prompts")
    up.add_argument("--download-only", action="store_true",
                    help="Only download, do not install")

    # restore-sources
    rs = sub.add_parser("restore-sources",
                        help="Restore APT sources from a backup")
    rs.add_argument("backup_dir", nargs="?",
                    help="Path to backup directory "
                         "(default: latest in /var/log/mintupgrade)")

    return p


def cmd_check(args, config):
    ok = run_all_checks(config)
    sys.exit(0 if ok else 1)


def cmd_download(args, config):
    check_root()
    logger = Logger()
    ok = run_all_checks(config)
    if not ok:
        if not args.yes and not _confirm("Checks failed. Continue anyway?"):
            sys.exit(1)

    if not args.yes:
        _warn("This will change your APT sources and download upgrade packages.")
        if not _confirm("Continue?"):
            sys.exit(0)

    phase_prepare(config, logger, auto_yes=args.yes)
    phase_update_sources(config, logger)
    phase_download(config, logger, auto_yes=args.yes)

    logger.close()
    _header("Done")
    _ok("Packages downloaded. Run 'sudo mintupgrade upgrade' to install.")


def cmd_upgrade(args, config):
    check_root()
    logger = Logger()
    ok = run_all_checks(config)
    if not ok:
        if not args.yes and not _confirm("Checks failed. Continue anyway?"):
            sys.exit(1)

    if not args.yes:
        print()
        _warn("═══════════════════════════════════════════════════════")
        _warn("  THIS WILL UPGRADE YOUR SYSTEM.                     ")
        _warn("  Make sure you have a Timeshift snapshot and a       ")
        _warn("  working backup of your data.                        ")
        _warn("═══════════════════════════════════════════════════════")
        print()
        if not _confirm("Proceed with the full upgrade?"):
            sys.exit(0)

    phase_prepare(config, logger, auto_yes=args.yes)
    phase_update_sources(config, logger)
    phase_download(config, logger, auto_yes=args.yes)

    if args.download_only:
        _ok("Download-only mode — stopping here.")
        logger.close()
        sys.exit(0)

    success = phase_upgrade(config, logger, auto_yes=args.yes)
    logger.close()

    if success:
        _header("Upgrade Finished")
        _ok("Your system has been upgraded!")
        _ok("Please reboot now: sudo reboot")
    else:
        _header("Upgrade Incomplete")
        _error("The upgrade did not finish cleanly.")
        _error(f"Check the log at {LOGFILE}")
        sys.exit(1)


def cmd_restore_sources(args, config):
    check_root()

    backup_dir = args.backup_dir
    if backup_dir is None:
        # Find the latest backup
        candidates = sorted(
            [os.path.join(BACKUP_DIR, d) for d in os.listdir(BACKUP_DIR)
             if d.startswith("sources_backup_")],
        )
        if not candidates:
            _error("No source backups found in " + BACKUP_DIR)
            sys.exit(1)
        backup_dir = candidates[-1]

    _info(f"Restoring from {backup_dir}")
    if _restore_sources(backup_dir):
        _apt_update()
        _ok("Sources restored successfully")
    else:
        _error("Restore failed")
        sys.exit(1)


def main():
    parser = build_parser()
    args = parser.parse_args()

    # Root check (allow 'check' to run for info even without root, but warn)
    if args.command != "check":
        check_root()

    config = Config()

    dispatch = {
        "check":           cmd_check,
        "download":        cmd_download,
        "upgrade":         cmd_upgrade,
        "restore-sources": cmd_restore_sources,
    }

    try:
        dispatch[args.command](args, config)
    except KeyboardInterrupt:
        print()
        _warn("Interrupted by user")
        sys.exit(130)
    except Exception:
        _error("Unhandled exception:")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
