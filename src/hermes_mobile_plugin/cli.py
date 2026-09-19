"""CLI commands for Hermes Mobile Plugin."""

# Windows legacy codepages (cp1252 & co.) cannot encode the emoji this CLI
# prints; piped/redirected stdout is exactly where the active console encoding
# bites. Reconfigure stdio to UTF-8 before anything prints — Python 3.7+.
import io
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):
    if (_stream is not None and hasattr(_stream, "reconfigure")
            and getattr(_stream, "encoding", "utf-8").lower().replace("-", "") not in
            ("utf8", "utf8mb4")):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):
            pass
del io, _sys, _stream

import argparse
import logging
import os
import shutil
import sys
import time
from pathlib import Path

from .config import load_hermes_config, validate_config
from .constants import GATEWAY_PORT, PLUGIN_DIR, PLUGIN_NAME, PLUGIN_VERSION
from .qr_generator import generate_qr, print_connection_details
from .supervisor import (
    SUPERVISOR_PID_FILE,
    ensure_running,
    is_supervisor_running,
    read_supervisor_pid,
    start_daemon,
    terminate_pid,
)

logger = logging.getLogger(__name__)


def cmd_install(args: argparse.Namespace) -> int:
    """Execute full installation: plugin files + QR + supervisor.

    Args:
        args: CLI arguments.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    print("=" * 60)
    print(f"📱 Hermes Mobile Plugin v{PLUGIN_VERSION} - Complete Setup")
    print("=" * 60)
    print()

    # Step 1: Validate config
    print("📦 Step 1/4: Validating configuration...")
    config = load_hermes_config()
    warnings = validate_config(config)
    if warnings:
        for warning in warnings:
            print(f"   ⚠️  {warning}")
    else:
        print("   ✅ Configuration valid")
    print()

    # Step 2: Deploy plugin files into Hermes' plugins directory (the same
    # deployment the .bat/.sh installers and hermes-mobile-install use).
    print("📦 Step 2/4: Deploying plugin files...")
    try:
        copied = deploy_plugin_files(PLUGIN_DIR)
        for path in copied:
            print(f"   ✅ {path}")
    except InstallError as e:
        print(f"   ⚠️  Deployment failed: {e}")
        print("   Continuing anyway...")
    print()

    # Step 3: Generate QR code
    print("📷 Step 3/4: Generating QR code...")
    try:
        import asyncio
        output_path = asyncio.run(generate_qr(config, open_browser=False, save_file=True))
        print(f"   ✅ QR code generated: {output_path}")
    except Exception as e:
        print(f"   ⚠️  QR generation failed: {e}")
        print("   Continuing anyway...")
    print()

    # Step 4: Start supervisor
    print("🛡️  Step 4/4: Starting 24x7 gateway supervisor...")

    # Spawn the DETACHED watchdog (its own session, survives this CLI
    # exiting). The old in-process daemon thread printed '24x7 monitoring'
    # and then died the moment cmd_install returned — a lie. ensure_running
    # is idempotent via the PID file + flock guard in the child.
    if is_supervisor_running():
        pid = read_supervisor_pid()
        print(f"   ⚠️  Supervisor already running (PID: {pid if pid and pid > 0 else 'locked'})")
    else:
        sup_pid = ensure_running()
        if sup_pid > 0:
            print(f"   ✅ Supervisor started (PID: {sup_pid}, 24x7 monitoring enabled)")
        else:
            print("   ⚠️  Supervisor failed to start")
    print()

    # Summary
    print("=" * 60)
    print("🎉 Setup Complete!")
    print("=" * 60)
    print()
    print("📱 Next steps:")
    print("   1. Start Hermes Agent: hermes agent")
    print("   2. Install Hermes Mobile APK on your phone")
    print("   3. Open app → Settings → Scan QR Code")
    print("   4. Scan the QR from your browser")
    print()
    print("🔗 Connection details:")
    print_connection_details(config)
    print()
    print("📊 Management commands:")
    print("   hermes-mobile-plugin status      # Check supervisor status")
    print("   hermes-mobile-plugin supervisor  # Start supervisor manually")
    print("   hermes-mobile-plugin supervisor --stop  # Stop supervisor")
    print("   Get-Content ~/.hermes/logs/gateway_supervisor.log -Wait  # (PowerShell; on macOS/Linux: tail -f)")
    print()

    return 0


def cmd_qr(args: argparse.Namespace) -> int:
    """Generate and display QR code.

    Args:
        args: CLI arguments with --no-browser and --output options.

    Returns:
        Exit code.
    """
    try:
        import asyncio
        from .qr_generator import generate_qr
        from .constants import PLUGINS_DIR

        config = load_hermes_config()

        if args.output:
            output_path = Path(args.output).absolute()
            output_dir = output_path.parent
        else:
            output_dir = PLUGINS_DIR / PLUGIN_NAME
            output_path = output_dir / "qr_code.html"

        output_dir.mkdir(parents=True, exist_ok=True)

        asyncio.run(generate_qr(
            config,
            output_dir=output_dir,
            open_browser=not args.no_browser,
            save_file=True,
            ip_override=args.ip,
        ))

        print_connection_details(config)
        return 0

    except Exception as e:
        print(f"❌ Failed to generate QR: {e}", file=sys.stderr)
        return 1


def cmd_status(args: argparse.Namespace) -> int:
    """Check supervisor and gateway status.

    Returns:
        Exit code (0 if healthy, 1 if not).
    """
    # Check supervisor (shared helper — os.kill-based probes signal the
    # process on Windows, which is exactly what we must not do here)
    if is_supervisor_running():
        pid = read_supervisor_pid()
        print(f"✅ Supervisor running (PID: {pid if pid and pid > 0 else 'unknown (PID file locked)'})")
    else:
        print("❌ Supervisor not running")

    # Check gateway (TCP liveness + HTTP /health — the same contract the
    # supervisor uses; a wedged event loop passes TCP but fails HTTP)
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{GATEWAY_PORT}/health", timeout=3) as resp:
            healthy = resp.status == 200
    except Exception:
        healthy = False
    if healthy:
        print(f"✅ Gateway is healthy (port {GATEWAY_PORT})")
        return 0
    print(f"❌ Gateway is down (port {GATEWAY_PORT})")
    return 1


def cmd_supervisor(args: argparse.Namespace) -> int:
    """Start or stop the gateway supervisor.

    Args:
        args: CLI arguments with --stop option.

    Returns:
        Exit code.
    """
    if args.stop:
        # Stop supervisor (cross-platform terminate). read_supervisor_pid
        # returns -1 when a legacy supervisor's mandatory lock makes the PID
        # file unreadable — still running, PID unknown — so fall back to
        # process enumeration instead of crashing on read_text().
        from .supervisor import (
            find_supervisor_pids,
            read_supervisor_pid,
            terminate_pid,
        )

        pid = read_supervisor_pid()
        if pid == 0:
            print("⚠️  Supervisor not running (no PID file)")
            return 0
        if pid == -1:
            pids = find_supervisor_pids()
            pids = [p for p in pids if p != os.getpid()]
            if not pids:
                print("⚠️  Supervisor not running (stale locked PID file)")
                try:
                    SUPERVISOR_PID_FILE.unlink(missing_ok=True)
                except OSError as e:
                    print(f"⚠️  Could not remove stale PID file: {e}")
                return 0
            for p in pids:
                terminate_pid(p)
            print(f"✅ Supervisor stopped (PID(s): {', '.join(map(str, pids))})")
        else:
            if not terminate_pid(pid):
                print("⚠️  Supervisor not running")
            else:
                print("✅ Supervisor stopped")
        try:
            SUPERVISOR_PID_FILE.unlink(missing_ok=True)
        except OSError as e:
            print(f"⚠️  Could not remove PID file (supervisor may still be exiting): {e}")
        return 0
    else:
        # Start supervisor
        print("🔄 Starting Gateway Supervisor (24x7 daemon)...")
        print("   Monitoring port 8642 every 10 seconds")
        print("   Will restart gateway after 3 consecutive failures")
        print("   Logs: ~/.hermes/logs/gateway_supervisor.log")
        print()

        daemon_pid = start_daemon()
        if daemon_pid > 0:
            print(f"✅ Supervisor daemon started (PID: {daemon_pid})")
            return 0
        else:
            print("❌ Failed to start supervisor")
            return 1


class InstallError(Exception):
    """Raised when plugin deployment into Hermes' plugins dir fails."""
    pass


def deploy_plugin_files(target_dir: Path) -> list[Path]:
    """Copy the plugin into Hermes' plugins directory.

    THE single deployment implementation: install.bat, install.sh and the
    pip-installed `hermes-mobile-install` script all land here, so the three
    paths can never drift (drift is exactly what broke the old .bat: its
    inline copy nested the package one level too deep and a malformed
    copy destination). Works from a src/ checkout AND from an already-
    deployed plugin dir (re-install/upgrade in place).

    Layout produced (what the Hermes plugin loader expects):
        <target>/__init__.py                  forwards register(ctx)
        <target>/plugin.yaml                  loader manifest
        <target>/hermes_mobile_plugin/...     the actual package

    Returns the list of copied paths.

    Raises:
        InstallError: If the package or the manifest cannot be located.
    """
    here = Path(__file__).resolve()
    package_src = here.parent.parent / "src" / "hermes_mobile_plugin"
    if not (package_src / "__init__.py").is_file():
        # Installed/deployed context: this module IS the package.
        package_src = here.parent
        if package_src.name != "hermes_mobile_plugin":
            raise InstallError(
                "Cannot locate the hermes_mobile_plugin package "
                "(neither src/ layout nor installed package)"
            )

    manifest_src = here.parent.parent.parent / "plugin.yaml"
    if not manifest_src.is_file():
        for parent in package_src.parents:
            if (parent / "plugin.yaml").is_file():
                manifest_src = parent / "plugin.yaml"
                break
    if not manifest_src.is_file():
        # Wheel install: the manifest ships inside the package (package-data
        # in pyproject.toml), so the walk finds nothing and we use that copy.
        bundled = package_src / "plugin.yaml"
        if not bundled.is_file():
            raise InstallError(
                "plugin.yaml manifest not found (repo layout or installed package data)"
            )
        manifest_src = bundled

    target_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []

    pkg_dst = target_dir / "hermes_mobile_plugin"
    if pkg_dst.exists():
        shutil.rmtree(pkg_dst)
    shutil.copytree(
        package_src,
        pkg_dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "plugin.yaml"),
    )
    copied.append(pkg_dst)

    # Forwarder for the loader (register(ctx) + create_plugin) and the
    # manifest the loader and _read_plugin_version both trust.
    init_dst = target_dir / "__init__.py"
    repo_init = package_src.parent / "__init__.py"
    if repo_init.is_file():
        shutil.copyfile(repo_init, init_dst)
    else:
        # Wheel install: generate the forwarder. It MUST forward register(ctx) —
        # a stub without it makes the loader skip the plugin and every
        # /api/* route 404s (the exact bug the old install.bat shipped).
        init_dst.write_text(
            '"""Hermes Mobile plugin loader forwarder (generated by install)."""\n'
            "from hermes_mobile_plugin import register, create_plugin, PLUGIN_VERSION  # noqa: F401\n"
            '__all__ = ["register", "create_plugin", "PLUGIN_VERSION"]\n',
            encoding="utf-8",
        )
    copied.append(init_dst)

    manifest_dst = target_dir / "plugin.yaml"
    shutil.copyfile(manifest_src, manifest_dst)
    copied.append(manifest_dst)

    return copied


def install_plugin(args: "argparse.Namespace | None" = None) -> int:
    """Entry point for the `hermes-mobile-install` console script.

    Deploys the plugin files into Hermes' plugins directory. The old
    pyproject.toml pointed this script at cli:install_plugin, which never
    existed — every scripted installer therefore died on its final step
    with ImportError.
    """
    parser = argparse.ArgumentParser(
        prog="hermes-mobile-install",
        description="Deploy the Hermes Mobile plugin into Hermes' plugins directory",
    )
    parser.add_argument(
        "--target",
        default=None,
        help="Override the target directory "
             "(default: ~/.hermes/plugins/hermes-mobile-qr)",
    )
    ns = parser.parse_args() if args is None else args

    target = Path(ns.target) if getattr(ns, "target", None) else PLUGIN_DIR
    try:
        copied = deploy_plugin_files(target)
    except InstallError as e:
        print(f"❌ Install failed: {e}", file=sys.stderr)
        return 1
    print("✅ Hermes Mobile plugin deployed:")
    for path in copied:
        print(f"   {path}")
    print()
    print("Next: restart Hermes Agent (or run 'hermes-mobile-plugin install')")
    print("so the gateway picks up the plugin and generates the QR code.")
    return 0


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Hermes Mobile Plugin - One-command setup for mobile connection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  hermes-mobile-plugin install              # Full setup
  hermes-mobile-plugin qr                   # Generate QR only
  hermes-mobile-plugin qr --no-browser      # Generate without browser
  hermes-mobile-plugin status               # Check health
  hermes-mobile-plugin supervisor           # Start supervisor
  hermes-mobile-plugin supervisor --stop    # Stop supervisor
        """,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {PLUGIN_VERSION}")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Install command
    install_parser = subparsers.add_parser(
        "install",
        help="Full setup: install, generate QR, start supervisor",
        description="Complete one-command installation with QR generation and 24x7 gateway monitoring",
    )

    # QR command
    qr_parser = subparsers.add_parser(
        "qr",
        help="Generate connection QR code",
    )
    qr_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't open browser automatically",
    )
    qr_parser.add_argument(
        "--output",
        help="Custom output path for HTML file",
    )
    qr_parser.add_argument(
        "--ip",
        help="Override IP address (e.g., Tailscale IP when Tailscale is not running)",
    )

    # Status command
    subparsers.add_parser(
        "status",
        help="Check supervisor and gateway status",
    )

    # Supervisor command
    sup_parser = subparsers.add_parser(
        "supervisor",
        help="Start/stop gateway supervisor",
    )
    sup_parser.add_argument(
        "--stop",
        action="store_true",
        help="Stop the supervisor",
    )

    args = parser.parse_args()

    # Route to command handler
    if not args.command:
        # Default to install
        sys.exit(cmd_install(args))
    elif args.command == "install":
        sys.exit(cmd_install(args))
    elif args.command == "qr":
        sys.exit(cmd_qr(args))
    elif args.command == "status":
        sys.exit(cmd_status(args))
    elif args.command == "supervisor":
        sys.exit(cmd_supervisor(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()