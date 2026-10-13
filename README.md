# site-readiness

Plug into the wall jack, run one command, hand the client a professional
**Site Readiness Report**.

Checks the local drop (IP, gateway, DNS), pings the gateway, verifies DNS
resolution, scans common service ports, summarizes visible Wi-Fi, and measures
real download speed — then renders it all as a shareable HTML report with a
clear READY / ISSUES verdict.

Built for field engineers doing site surveys, office buildouts, and
"the internet doesn't work" triage. Zero dependencies.

## Quick start

```bash
python site_readiness.py --site "Client HQ - Floor 2"
```

Open `readiness.html`. That's your handoff doc.

## What it checks

| Check | Why it matters |
|---|---|
| Local IP / gateway / DNS | Proves the drop is live and DHCP works |
| Gateway ping + loss | Catches bad cabling and overloaded links |
| DNS resolution timing | Slow DNS feels like slow internet |
| Ports 80/443/22/3389/9100/515/631/161 | Shows what the site actually exposes |
| Visible Wi-Fi summary | Context for wireless complaints |
| Download speed test | A real number, not "it feels slow" |

## Files

| File | Purpose |
|---|---|
| `site_readiness.py` | The whole tool — checks + HTML/JSON report |
| `readiness.html` | Generated report (git-ignored) |
| `readiness.json` | Raw results for your records (git-ignored) |

## Requirements

- Python 3.8+, no pip packages
- Windows / Linux / macOS

## License

MIT — use it on client jobs.
