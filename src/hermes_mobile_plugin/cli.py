"""CLI commands for Hermes Mobile Plugin."""

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

from .config import load_hermes_config, validate_config
from .constants import PLUGIN_NAME, PLUGIN_VERSION
from .qr_generator import generate_qr, print_connection_details
from .supervisor import GatewaySupervisor, start_daemon

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
    print("📦 Step 1/3: Validating configuration...")
    config = load_hermes_config()
    warnings = validate_config(config)
    if warnings:
        for warning in warnings:
            print(f"   ⚠️  {warning}")
    else:
        print("   ✅ Configuration valid")
    print()

    # Step 2: Generate QR code
    print("📷 Step 2/3: Generating QR code...")
    try:
        import asyncio
        output_path = asyncio.run(generate_qr(config, open_browser=False, save_file=True))
        print(f"   ✅ QR code generated: {output_path}")
    except Exception as e:
        print(f"   ⚠️  QR generation failed: {e}")
        print("   Continuing anyway...")
    print()

    # Step 3: Start supervisor
    print("🛡️  Step 3/3: Starting 24x7 gateway supervisor...")

    # Spawn the DETACHED watchdog (its own session, survives this CLI
    # exiting). The old in-process daemon thread printed '24x7 monitoring'
    # and then died the moment cmd_install returned — a lie. ensure_running
    # is idempotent via the PID file + flock guard in the child.
    from .supervisor import SUPERVISOR_PID_FILE, ensure_running, is_supervisor_running

    if is_supervisor_running():
        pid = int(SUPERVISOR_PID_FILE.read_text().strip()) if SUPERVISOR_PID_FILE.exists() else 0
        print(f"   ⚠️  Supervisor already running (PID: {pid})")
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
    print("   tail -f ~/.hermes/logs/gateway_supervisor.log  # View logs")
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
    from .supervisor import SUPERVISOR_PID_FILE

    # Check supervisor
    if SUPERVISOR_PID_FILE.exists():
        try:
            pid = int(SUPERVISOR_PID_FILE.read_text().strip())
            import os
            os.kill(pid, 0)
            print(f"✅ Supervisor running (PID: {pid})")
        except (ProcessLookupError, ValueError):
            print("❌ Supervisor not running (stale PID file)")
            SUPERVISOR_PID_FILE.unlink(missing_ok=True)
    else:
        print("❌ Supervisor not running")

    # Check gateway
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 8642), timeout=2):
            print("✅ Gateway is healthy (port 8642)")
    except (socket.timeout, ConnectionRefusedError, OSError):
        print("❌ Gateway is down (port 8642)")
        return 1

    return 0


def cmd_supervisor(args: argparse.Namespace) -> int:
    """Start or stop the gateway supervisor.

    Args:
        args: CLI arguments with --stop option.

    Returns:
        Exit code.
    """
    from .supervisor import SUPERVISOR_PID_FILE

    if args.stop:
        # Stop supervisor
        if SUPERVISOR_PID_FILE.exists():
            try:
                pid = int(SUPERVISOR_PID_FILE.read_text().strip())
                import os
                os.kill(pid, 15)
                SUPERVISOR_PID_FILE.unlink()
                print("✅ Supervisor stopped")
                return 0
            except ProcessLookupError:
                print("⚠️  Supervisor not running")
                SUPERVISOR_PID_FILE.unlink(missing_ok=True)
                return 0
            except ValueError:
                print("❌ Invalid PID file")
                return 1
        else:
            print("⚠️  Supervisor not running (no PID file)")
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