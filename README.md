# Hermes Mobile Plugin

Generates QR codes for Hermes Mobile App to connect directly to Hermes Agent Desktop gateway (port 8642).

## Installation

### From Local Source
```bash
# Clone the repository
git clone https://github.com/<your-username>/hermes-mobile-plugin.git
cd hermes-mobile-plugin
pip install -e .

# Then run the one-command setup
hermes-mobile-plugin install
```

### Directly from GitHub (without cloning)
```bash
# Install the plugin directly from GitHub
pip install git+https://github.com/<your-username>/hermes-mobile-plugin@v0.0.1

# Then run the one-command setup
hermes-mobile-plugin install
```

> Replace `<your-username>` with your GitHub username or the repository owner.

## Usage

### Full Setup (Recommended)
```bash
# One-command complete setup
hermes-mobile-plugin install
```

### Individual Commands
```bash
# Generate QR code only
hermes-mobile-plugin qr

# Check supervisor status
hermes-mobile-plugin status

# Start/stop supervisor manually
hermes-mobile-plugin supervisor      # Start
hermes-mobile-plugin supervisor --stop  # Stop
```

## How it Works

1. Reads Hermes Agent config (`~/.hermes/config.yaml`)
2. Detects Tailscale IP (or local IP) using multiple fallback methods
3. Extracts API key and gateway port
4. Generates QR code with connection credentials
5. Starts 24x7 gateway supervisor to keep gateway online

## QR Code Contains
```json
{
  "url": "http://100.89.25.56:8642",
  "api_key": "hermes-mobile-d5a8f8ad9d7e45b1e7000e5f9ec424f41180a54791ea5b1ea06013f40b0a3f27",
  "context_compression": true,
  "tailscale_ip": "100.89.25.56",
  "version": "0.0.1"
}
```

## Mobile App Setup
1. Install Hermes Mobile APK (from GitHub releases)
2. Open app → Settings → Scan QR Code
3. Scan QR from your browser
4. App auto-configures and connects to Hermes Agent Desktop gateway

## Gateway Supervisor (24x7 Monitoring)
The supervisor ensures your Hermes Gateway stays online:
- Checks health every 10 seconds
- Restarts gateway after 3 consecutive failures
- Logs to `~/.hermes/logs/gateway_supervisor.log`
- PID file at `~/.hermes/logs/gateway_supervisor.pid`

### Supervisor Management
```bash
# Check status
hermes-mobile-plugin status

# View logs
tail -f ~/.hermes/logs/gateway_supervisor.log

# Stop supervisor
hermes-mobile-plugin supervisor --stop

# Restart supervisor
hermes-mobile-plugin supervisor
```

## Requirements
- Python 3.10+
- Hermes Agent 0.20.0+ (installed separately from https://hermes-agent.nousresearch.com)
- Tailscale (optional, for cross-network connections)
- Hermes Mobile APK (separate download)

## Development
```bash
# Install from source
pip install -e .

# Run tests
pytest

# Format code
black src/
ruff check src/
```