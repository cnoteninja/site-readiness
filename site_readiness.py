#!/usr/bin/env python3
"""
site-readiness: one-command network site readiness check.

Plug into the wall jack, run it, hand the client a professional HTML report.

Checks:
  * Local IP, gateway, DNS servers, DHCP info
  * Gateway ping + DNS resolution timing
  * Common service ports on the gateway (80/443/22/3389/9100/515/631)
  * VoIP readiness (jitter / packet loss / estimated MOS, TCP approximation)
  * Wi-Fi survey summary (visible SSIDs, strongest APs, congestion)
  * Internet download speed

Usage:
    python site_readiness.py [--site "Client HQ - Floor 2"] [--out report.html]

Zero third-party dependencies. Windows / Linux / macOS.
"""

import argparse
import html
import json
import math
import os
import platform
import re
import shutil
import socket
import subprocess
import time
import urllib.request
from datetime import datetime, timezone

COMMON_PORTS = [80, 443, 22, 3389, 9100, 515, 631, 161]
SPEED_URL = "https://speed.cloudflare.com/__down?bytes=25000000"  # 25 MB
SPEED_FALLBACK = "http://cachefly.cachefly.net/10mb.test"


def run(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:
        return ""


# ------------------------------------------------------------ local network --
def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


def gateway_and_dns():
    gw, dns = "", []
    sys = platform.system()
    if sys == "Windows":
        out = run(["ipconfig", "/all"])
        m = re.search(r"Default Gateway.*?:\s*([0-9.]+)", out)
        if m:
            gw = m.group(1)
        dns = re.findall(r"DNS Servers.*?:\s*([0-9.]+)", out)
    else:
        out = run(["ip", "route", "show", "default"]) or run(["netstat", "-rn"])
        m = re.search(r"default via ([0-9.]+)|default\s+([0-9.]+)", out)
        if m:
            gw = m.group(1) or m.group(2)
        try:
            with open("/etc/resolv.conf") as fh:
                dns = re.findall(r"nameserver\s+([0-9.]+)", fh.read())
        except Exception:
            pass
    return gw, dns


def ping(host, count=4):
    if not host:
        return None
    sys = platform.system()
    cmd = (["ping", "-n", str(count), host] if sys == "Windows"
           else ["ping", "-c", str(count), "-W", "2", host])
    out = run(cmd, timeout=20)
    m = re.search(r"Average = (\d+)ms", out) or \
        re.search(r"avg\w*\s*=\s*[\d.]+/([\d.]+)", out)
    loss = re.search(r"Lost = \d+ \((\d+)%", out) or \
        re.search(r"(\d+)% packet loss", out)
    if not m and not loss:
        return None
    return {"avg_ms": float(m.group(1)) if m else None,
            "loss_pct": int(loss.group(1)) if loss else None}


def dns_check():
    results = []
    for domain in ("google.com", "cloudflare.com"):
        t = time.time()
        try:
            ip = socket.gethostbyname(domain)
            results.append({"domain": domain, "ok": True, "ip": ip,
                            "ms": round((time.time() - t) * 1000, 1)})
        except Exception as exc:
            results.append({"domain": domain, "ok": False, "error": str(exc)[:60]})
    return results


def scan_ports(host, ports):
    out = []
    for p in ports:
        t = time.time()
        try:
            s = socket.create_connection((host, p), timeout=2)
            s.close()
            out.append({"port": p, "open": True,
                        "ms": round((time.time() - t) * 1000, 1)})
        except Exception:
            out.append({"port": p, "open": False})
    return out


# ------------------------------------------------------------------ voip --
def voip_probe(host, port, samples=20, timeout=3):
    """TCP-based VoIP readiness approximation, stdlib only.

    Real RTP voice is UDP, and UDP jitter cannot be measured without a
    cooperating echo server. As a documented approximation we take
    repeated small-payload TCP round-trips (the SYN/SYN-ACK handshake
    time on a TCP port) and estimate:
      * latency  = mean RTT (ms)
      * jitter   = mean absolute difference of consecutive RTTs (ms),
                   the same mean-deviation idea as RFC 3550 jitter
      * loss     = share of samples that failed or timed out (%)

    Results feed estimate_mos() below and the VoIP report section.
    """
    rtts = []
    lost = 0
    for _ in range(samples):
        t = time.time()
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
            rtts.append((time.time() - t) * 1000)
        except Exception:
            lost += 1
    loss_pct = lost / samples * 100
    if not rtts:
        return {"target": "%s:%d" % (host, port), "latency_ms": None,
                "jitter_ms": None, "loss_pct": round(loss_pct, 1),
                "mos": None, "ok": False}
    latency = sum(rtts) / len(rtts)
    jitter = (sum(abs(rtts[i] - rtts[i - 1]) for i in range(1, len(rtts)))
              / (len(rtts) - 1)) if len(rtts) > 1 else 0.0
    mos = estimate_mos(latency + jitter, loss_pct / 100.0)
    return {"target": "%s:%d" % (host, port),
            "latency_ms": round(latency, 1),
            "jitter_ms": round(jitter, 1),
            "loss_pct": round(loss_pct, 1),
            "mos": mos,
            "ok": bool(jitter < 30 and loss_pct < 1 and mos >= 3.6)}


def estimate_mos(delay_ms, loss):
    """Estimate VoIP MOS from delay and loss using a simplified E-model
    (ITU-T G.107) rating, then map R to MOS.

    Formula (exact):
        H = 1 if delay_ms > 177.3 else 0
        R = 93.2 - 0.024*delay_ms - 0.11*(delay_ms - 177.3)*H
            - 30*ln(1 + 15*loss)
        MOS = 1 + 0.035*R + 7e-6*R*(R - 60)*(100 - R)

    delay_ms is the effective one-way delay: we pass latency + jitter,
    because a jitter buffer adds roughly the jitter amount to the
    effective delay. loss is the loss probability 0..1.
    MOS is clamped to the [1.0, 4.5] scale and rounded to 2 decimals.
    """
    H = 1.0 if delay_ms > 177.3 else 0.0
    R = (93.2 - 0.024 * delay_ms - 0.11 * (delay_ms - 177.3) * H
         - 30 * math.log(1 + 15 * loss))
    mos = 1 + 0.035 * R + 7e-6 * R * (R - 60) * (100 - R)
    return round(max(1.0, min(4.5, mos)), 2)


# ------------------------------------------------------------------- wifi --
def wifi_scan():
    """Lightweight visible-network scan (summary only)."""
    sys = platform.system()
    nets = []
    if sys == "Windows" and shutil.which("netsh"):
        out = run(["netsh", "wlan", "show", "networks", "mode=bssid"])
        ssid = None
        for line in out.splitlines():
            m = re.match(r"SSID \d+ : (.*)", line.strip())
            if m:
                ssid = m.group(1).strip()
            m = re.match(r"Signal\s+: (\d+)%", line.strip())
            if m and ssid:
                nets.append({"ssid": ssid, "signal": int(m.group(1))})
    elif sys == "Linux" and shutil.which("nmcli"):
        out = run(["nmcli", "-t", "-f", "SSID,SIGNAL", "dev", "wifi",
                   "list", "--rescan", "yes"], timeout=45)
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[0]:
                try:
                    nets.append({"ssid": parts[0], "signal": int(parts[1])})
                except ValueError:
                    pass
    return nets


# ------------------------------------------------------------------ speed --
def speed_test(url=SPEED_URL, timeout=60):
    """Download-based speed test, stdlib only. Returns Mbps or None."""
    start = time.time()
    total = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "site-readiness/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if time.time() - start > timeout:
                    break
    except Exception:
        if url != SPEED_FALLBACK:
            return speed_test(SPEED_FALLBACK, timeout)
        return None
    elapsed = time.time() - start
    if elapsed <= 0 or total == 0:
        return None
    return round(total * 8 / elapsed / 1e6, 1)


# ----------------------------------------------------------------- report --
def verdict(data):
    issues = []
    if not data["local_ip"]:
        issues.append("No local IP — drop may be dead or DHCP failing.")
    g = data["gateway_ping"]
    if g is None:
        issues.append("Gateway unreachable.")
    elif (g.get("loss_pct") or 0) > 0:
        issues.append("Packet loss to gateway (%s%%)." % g["loss_pct"])
    for d in data["dns"]:
        if not d["ok"]:
            issues.append("DNS failed for %s." % d["domain"])
    v = data.get("voip")
    if v and not v.get("ok"):
        issues.append("VoIP check failed (jitter %s ms, loss %s%%, MOS %s)." % (
            v.get("jitter_ms"), v.get("loss_pct"), v.get("mos")))
    if data["speed_mbps"] is not None and data["speed_mbps"] < 10:
        issues.append("Download speed only %s Mbps." % data["speed_mbps"])
    return issues


def build_html(data, site):
    e = html.escape
    issues = verdict(data)
    if not issues:
        banner = "<div class='ok'><b>READY</b> — no issues found.</div>"
    else:
        items = "".join("<li>%s</li>" % e(i) for i in issues)
        banner = ("<div class='bad'><b>%d ISSUE(S)</b><ul>%s</ul></div>"
                  % (len(issues), items))

    ports = "".join(
        "<tr><td>%d</td><td>%s</td><td>%s</td></tr>" % (
            p["port"], "OPEN" if p["open"] else "closed",
            ("%s ms" % p["ms"]) if p.get("ms") else "-")
        for p in data["ports"])
    dns = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            e(d["domain"]), "OK" if d["ok"] else "FAIL",
            ("%s ms" % d["ms"]) if d.get("ms") else e(d.get("error", "")))
        for d in data["dns"])
    wifi = "".join(
        "<tr><td>%s</td><td>%d%%</td></tr>" % (e(n["ssid"]), n["signal"])
        for n in sorted(data["wifi"], key=lambda n: n["signal"], reverse=True)[:15])

    v = data.get("voip")
    if v and v.get("latency_ms") is not None:
        vstat = "<div class='%s'><b>VoIP: %s</b></div>" % (
            "ok" if v["ok"] else "bad", "PASS" if v["ok"] else "FAIL")
        voip = """%s
<table><tr><th>Metric</th><th>Measured</th><th>Target</th></tr>
<tr><td>Target</td><td>%s</td><td>-</td></tr>
<tr><td>Latency (avg RTT)</td><td>%s ms</td><td>-</td></tr>
<tr><td>Jitter</td><td>%s ms</td><td>&lt; 30 ms</td></tr>
<tr><td>Packet loss</td><td>%s%%</td><td>&lt; 1%%</td></tr>
<tr><td>Estimated MOS</td><td>%s</td><td>&ge; 3.6</td></tr></table>
<p><small>Method: jitter/loss approximated from repeated small-payload TCP
round-trips (stdlib only — no UDP echo). MOS via simplified E-model; see README.</small></p>""" % (
            vstat, e(v["target"]), v["latency_ms"], v["jitter_ms"],
            v["loss_pct"], v["mos"])
    else:
        voip = ("<div class='bad'><b>VoIP: FAIL</b></div>"
                "<p>Target unreachable (%s).</p>" %
                e(v["target"] if v and v.get("target") else "no route"))

    ts = e(data["timestamp"])
    return """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Site Readiness — {site}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:900px;margin:2em auto;padding:0 1em}}
.ok{{background:#d4edda;padding:1em;border-radius:8px}}.bad{{background:#f8d7da;padding:1em;border-radius:8px}}
table{{border-collapse:collapse;width:100%;margin:.5em 0}}th,td{{border:1px solid #ddd;padding:.4em .6em;text-align:left}}
th{{background:#f5f5f5}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:1em}}</style>
</head><body>
<h1>Site Readiness Report</h1><p><b>Site:</b> {site} &nbsp; <b>When:</b> {ts} &nbsp;
<b>Tech host:</b> {host}</p>
{banner}
<h2>Network</h2>
<table><tr><th>Item</th><th>Value</th></tr>
<tr><td>Local IP</td><td>{ip}</td></tr>
<tr><td>Gateway</td><td>{gw}</td></tr>
<tr><td>Gateway ping</td><td>{gping}</td></tr>
<tr><td>DNS servers</td><td>{dns_s}</td></tr>
<tr><td>Download speed</td><td>{spd}</td></tr></table>
<h2>DNS resolution</h2><table><tr><th>Domain</th><th>Status</th><th>Detail</th></tr>{dns}</table>
<h2>Gateway ports</h2><table><tr><th>Port</th><th>Status</th><th>RTT</th></tr>{ports}</table>
<h2>VoIP readiness</h2>{voip}
<h2>Visible Wi-Fi (top 15)</h2><table><tr><th>SSID</th><th>Signal</th></tr>{wifi}</table>
<p><small>Generated by <a href="https://github.com/cnoteninja/site-readiness">site-readiness</a></small></p>
</body></html>""".format(
        site=e(site), ts=ts, host=e(data["hostname"]), banner=banner,
        ip=e(data["local_ip"] or "none"), gw=e(data["gateway"] or "none"),
        gping=e(data["gateway_ping"] and
                "%s ms avg, %s%% loss" % (data["gateway_ping"]["avg_ms"],
                                         data["gateway_ping"]["loss_pct"]) or "unreachable"),
        dns_s=e(", ".join(data["dns_servers"]) or "none"),
        spd="%s Mbps" % data["speed_mbps"] if data["speed_mbps"] else "test failed",
        dns=dns, ports=ports, voip=voip, wifi=wifi or "<tr><td colspan=2>no data</td></tr>")


def main():
    ap = argparse.ArgumentParser(description="Network site readiness check")
    ap.add_argument("--site", default="Unlabeled site")
    ap.add_argument("--out", default="readiness.html")
    ap.add_argument("--json", default="readiness.json")
    args = ap.parse_args()

    print("gathering local network info...")
    gw, dns_servers = gateway_and_dns()
    data = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "site": args.site,
        "hostname": socket.gethostname(),
        "local_ip": local_ip(),
        "gateway": gw,
        "dns_servers": dns_servers,
        "platform": platform.system(),
    }
    print("pinging gateway...")
    data["gateway_ping"] = ping(gw)
    print("checking DNS...")
    data["dns"] = dns_check()
    if gw:
        print("scanning gateway ports...")
        data["ports"] = scan_ports(gw, COMMON_PORTS)
    else:
        data["ports"] = []
    print("checking VoIP readiness...")
    if gw:
        voip_host = gw
        open_ports = [p["port"] for p in data["ports"] if p["open"]]
        voip_port = 443 if 443 in open_ports else (
            open_ports[0] if open_ports else 443)
    else:
        voip_host, voip_port = "8.8.8.8", 53  # Google Public DNS, TCP fallback
    data["voip"] = voip_probe(voip_host, voip_port)
    print("scanning wifi...")
    data["wifi"] = wifi_scan()
    print("running speed test (~30s)...")
    data["speed_mbps"] = speed_test()

    outdir = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(outdir, args.json), "w") as fh:
        json.dump(data, fh, indent=1)
    with open(os.path.join(outdir, os.path.basename(args.out)), "w",
              encoding="utf-8") as fh:
        fh.write(build_html(data, args.site))
    issues = verdict(data)
    print("done -> %s" % args.out)
    print("verdict: %s" % ("READY" if not issues else "%d issue(s)" % len(issues)))
    for i in issues:
        print("  !", i)


if __name__ == "__main__":
    main()
