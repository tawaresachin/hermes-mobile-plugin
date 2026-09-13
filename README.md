# Hermes Mobile QR & Audio Plugin ☤

[![License: MIT](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](https://github.com/tawaresachin/hermes-mobile-plugin/blob/main/LICENSE)
[![Version](https://img.shields.io/github/v/tag/tawaresachin/hermes-mobile-plugin?label=version&color=blue&style=for-the-badge)](https://github.com/tawaresachin/hermes-mobile-plugin/releases)
[![Docs](https://img.shields.io/badge/Docs-Hermes--Agent-FFD700?style=for-the-badge)](https://hermes-agent.nousresearch.com/docs/)
[![CI](https://img.shields.io/github/actions/workflow/status/tawaresachin/hermes-mobile-plugin/ci.yml?branch=main&style=for-the-badge)](https://github.com/tawaresachin/hermes-mobile-plugin/actions)

**What it does** – Generates a QR code for the Hermes Mobile app, exposes local STT/TTS routes and keeps the gateway running 24/7.

| Feature | Description |
|---|---|
| QR Pairing | One‑click QR that configures the mobile app with IP, API key and port. |
| Voice support | Local Whisper STT and Edge TTS routes on the gateway. |
| 24/7 supervisor | Auto‑restarts the gateway if it crashes. |
| Attachment support | Upload / download files between phone and agent workspace. |

## Install (one command)

```bash
pip install hermes-mobile-plugin
hermes-mobile-plugin install
```

*Direct from GitHub*  

```bash
pip install git+https://github.com/tawaresachin/hermes-mobile-plugin@v0.0.6
hermes-mobile-plugin install
```

## How to connect the phone

1. Install the Hermes Mobile APK from the releases page.  
2. Run `hermes-mobile-plugin qr` – a QR image appears.  
3. In the app open Settings → **Scan QR Code** and scan the image.  
4. The app auto‑configures and connects.

## Commands you may need

- `hermes-mobile-plugin status` – show gateway health.  
- `hermes-mobile-plugin supervisor --stop` – stop monitor.  
- `hermes-mobile-plugin supervisor --start` – start / restart monitor.

## Development (optional)

```bash
git clone https://github.com/tawaresachin/hermes-mobile-plugin.git
cd hermes-mobile-plugin
pip install -e .
pytest
```

## License

MIT – see the LICENSE file.
