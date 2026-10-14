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
| VoIP readiness (jitter / loss / MOS) | Tells you if voice calls will sound good on this link |
| Visible Wi-Fi summary | Context for wireless complaints |
| Download speed test | A real number, not "it feels slow" |

## VoIP readiness check

VoIP calls fall apart on jitter and packet loss long before raw bandwidth
does, so the tool measures all three and estimates a MOS (Mean Opinion
Score) from 1.0 (bad) to 5.0 (great).

### Method — TCP approximation

Real voice traffic is UDP, and UDP jitter can't be measured from a laptop
without a cooperating echo server on the far end. This tool stays
dependency-free, so it uses a documented **TCP approximation** instead:

1. It opens 20 repeated small-payload TCP connections (SYN/SYN-ACK
   handshake time) to the gateway — or to 8.8.8.8:53 if no gateway is
   found. Port 443 is preferred when the port scan found it open.
2. **Latency** = mean round-trip time of the successful samples.
3. **Jitter** = mean absolute difference of consecutive RTTs (the same
   mean-deviation idea as RFC 3550 jitter).
4. **Loss** = share of samples that failed or timed out.

Treat this as a screening check, not a lab measurement: TCP handshakes
don't experience the same queuing as UDP voice packets, so real-world
voice jitter will typically be *equal or worse* than what this reports.

### MOS formula

A simplified E-model (ITU-T G.107) rating mapped to MOS. Exact formula:

```
H = 1 if delay > 177.3 ms else 0
R = 93.2 - 0.024*delay - 0.11*(delay - 177.3)*H - 30*ln(1 + 15*loss)
MOS = 1 + 0.035*R + 7e-6*R*(R - 60)*(100 - R)
```

- `delay` = latency + jitter (ms). The jitter buffer adds roughly the
  jitter amount to the effective one-way delay, so jitter feeds MOS
  through this term too.
- `loss` = packet loss probability (0–1).
- MOS is clamped to [1.0, 4.5] and rounded to 2 decimals.

### Thresholds

| Metric | Target |
|---|---|
| Jitter | < 30 ms |
| Packet loss | < 1% |
| Estimated MOS | ≥ 3.6 |

All three must hold for a **PASS**; any one failing (or the target being
unreachable) is a **FAIL**, and the failure shows up in the report's
verdict banner as well as the VoIP section.

### Example

```
Target          10.1.0.1:443
Latency         14.2 ms
Jitter           3.8 ms
Packet loss      0.0%
Estimated MOS    4.35   -> VoIP: PASS
```

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
