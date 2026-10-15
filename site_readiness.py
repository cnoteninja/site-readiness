#!/usr/bin/env python3
"""
site-readiness: one-command network site readiness check.

Plug into the wall jack, run it, hand the client a professional HTML report.
The report opens with a plain-English executive summary (verdict, voice
capacity, internet quality in words, one line per check area), and every
issue carries a "How to fix" remediation line.

Checks:
  * Local IP, gateway, DNS servers, DHCP info
  * Gateway ping + DNS resolution timing
  * Common service ports on the gateway (80/443/22/3389/9100/515/631)
  * VoIP readiness (jitter / packet loss / estimated MOS, TCP approximation)
  * Wi-Fi survey summary (visible SSIDs, strongest APs, congestion)
  * Internet download speed

Usage:
    python site_readiness.py [--site "Client HQ - Floor 2"] [--out report.html]
    python site_readiness.py --demo   # sample report, synthetic data, no network

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


# ------------------------------------------- verdict & remediation --
# Centralized check -> fix mapping. Each finding key below maps to one
# concrete next step. To cover a new check, add its key here and emit the
# key from findings(); the report renders the "How to fix" line itself.
REMEDIATION = {
    "no_local_ip":
        "Reseat the patch cable at both ends and confirm the jack's link "
        "light is on, then release/renew the address (Windows: "
        "`ipconfig /release` then `ipconfig /renew`; anything else: unplug "
        "and replug the cable) and re-run. Still no IP? The switch port for "
        "this drop may be disabled - escalate to the site's IT or building "
        "facilities.",
    "gateway_unreachable":
        "Confirm the gateway address looks sane (usually ends in .1 or "
        ".254) and that no local firewall is blocking ping. If the address "
        "is right but silent, the router may be down or this drop may be on "
        "the wrong VLAN - check with the site's IT.",
    "gateway_loss":
        "Packet loss on the LAN side usually means a bad cable, a duplex "
        "mismatch, or an overloaded switch. Try a different patch cable and "
        "a different jack; if the loss follows the drop, the cable run "
        "itself needs replacing.",
    "dns_failure":
        "Set a known-good DNS server (8.8.8.8) temporarily and re-run. If "
        "that fixes it, the site's configured DNS server is the problem - "
        "report it to the site's IT.",
    "dns_all_failed":
        "No name resolution at all. Check the configured DNS servers "
        "(`ipconfig /all` or /etc/resolv.conf) and try 8.8.8.8 / 1.1.1.1 "
        "manually. If public DNS also fails, outbound port 53 may be "
        "blocked by the firewall.",
    "voip_unreachable":
        "The probe target didn't answer. Confirm the gateway address and "
        "that at least one TCP port on it is reachable. If the firewall "
        "blocks the gateway entirely, point the probe at an internal "
        "PBX/SBC instead.",
    "voip_failed":
        "Voice quality is below target (jitter < 30 ms, loss < 1%, MOS >= "
        "3.6). Enable QoS / DSCP EF prioritization for voice on the site "
        "router, make sure nothing was hogging bandwidth during the test, "
        "and re-run at a quiet time. If it still fails, the ISP link may "
        "need an upgrade.",
    "slow_speed":
        "Re-run with nothing else using the link (pause backups, cloud "
        "sync, streaming). If it's still slow, test from a second device on "
        "the same drop to rule out the laptop, then report the measured "
        "speed to the ISP with this report attached.",
    "speed_failed":
        "The download test couldn't complete. Check that HTTPS to the "
        "internet works at all, and watch for captive portals or corporate "
        "proxies - authenticate through the portal and re-run.",
}


def remediation_for(check_key):
    """Fix guidance for a finding key; generic fallback for unknown keys."""
    return REMEDIATION.get(check_key, "Investigate and re-run the check.")


def findings(data):
    """All issues as (check_key, message) tuples.

    Every key must exist in REMEDIATION so the report can attach a fix line.
    verdict() keeps the old plain-message behavior for the CLI.
    """
    found = []
    if not data["local_ip"]:
        found.append(("no_local_ip",
                      "No local IP - drop may be dead or DHCP failing."))
    g = data["gateway_ping"]
    if g is None:
        found.append(("gateway_unreachable", "Gateway unreachable."))
    elif (g.get("loss_pct") or 0) > 0:
        found.append(("gateway_loss",
                      "Packet loss to gateway (%s%%)." % g["loss_pct"]))
    dns = data.get("dns") or []
    bad = [d for d in dns if not d["ok"]]
    if bad and len(bad) == len(dns):
        found.append(("dns_all_failed",
                      "DNS failed for every test domain."))
    else:
        for d in bad:
            found.append(("dns_failure",
                          "DNS failed for %s." % d["domain"]))
    v = data.get("voip")
    if v and not v.get("ok"):
        if v.get("latency_ms") is None:
            found.append(("voip_unreachable",
                          "VoIP probe target unreachable (%s)."
                          % v.get("target", "unknown")))
        else:
            found.append(("voip_failed",
                          "VoIP check failed (jitter %s ms, loss %s%%, "
                          "MOS %s)." % (v.get("jitter_ms"),
                                        v.get("loss_pct"), v.get("mos"))))
    spd = data.get("speed_mbps")
    if spd is None:
        found.append(("speed_failed", "Speed test could not complete."))
    elif spd < 10:
        found.append(("slow_speed",
                      "Download speed only %s Mbps." % spd))
    return found


def verdict(data):
    return [msg for _, msg in findings(data)]


# ------------------------------------------------ executive summary --
CRITICAL_FINDINGS = {"no_local_ip", "gateway_unreachable",
                     "voip_unreachable", "dns_all_failed"}


def overall_verdict(data):
    """(verdict_text, css_class, plain_english_note)."""
    keys = {k for k, _ in findings(data)}
    if not keys:
        return ("Ready", "ok",
                "Every check passed. This site is ready for normal office use.")
    if keys & CRITICAL_FINDINGS:
        return ("Not ready", "bad",
                "One or more critical checks failed - fix the issues below "
                "before signing the site off.")
    return ("Ready with caveats", "warn",
            "Usable, but the issues below should be fixed or noted before "
            "handoff.")


def voip_capacity_line(v):
    """Plain-English '~N VoIP users' line derived from the VoIP probe."""
    if not v or v.get("latency_ms") is None:
        return ("Voice calls can't be recommended yet - the voice probe "
                "couldn't reach its target.")
    if not v.get("ok"):
        return ("Not recommended for voice calls until the issues below are "
                "fixed (estimated call quality MOS %s, target is 3.6+)."
                % v.get("mos"))
    mos = v["mos"]
    users = "40+" if mos >= 4.2 else ("~20" if mos >= 4.0 else "~8")
    return ("Ready for about %s simultaneous voice calls (estimated call "
            "quality MOS %s out of 5)." % (users, mos))


def internet_quality_line(mbps):
    """Internet quality described in words from the measured speed."""
    if mbps is None:
        return ("Speed test couldn't complete - internet quality is "
                "unverified.")
    if mbps >= 100:
        return ("Fast (%s Mbps) - comfortable for video calls, cloud apps, "
                "and voice at the same time." % mbps)
    if mbps >= 25:
        return ("Good (%s Mbps) - comfortable for typical office work."
                % mbps)
    if mbps >= 10:
        return ("Adequate (%s Mbps) - fine for email and browsing, tight if "
                "many people stream or call at once." % mbps)
    return ("Slow (%s Mbps) - expect complaints on busy days." % mbps)


def area_lines(data):
    """One plain-English line per check area: (status, label, text).

    status is one of pass / warn / fail / info; all text is generated from
    the results data structure, never hardcoded per-run.
    """
    lines = []
    if data["local_ip"]:
        lines.append(("pass", "Wall jack",
                      "The wall jack is live - this machine got IP %s."
                      % data["local_ip"]))
    else:
        lines.append(("fail", "Wall jack",
                      "The wall jack looks dead - no IP address was assigned."))
    g = data["gateway_ping"]
    if g is None:
        lines.append(("fail", "Router",
                      "The local router didn't answer, so anything past it "
                      "couldn't be tested reliably."))
    elif (g.get("loss_pct") or 0) > 0:
        lines.append(("warn", "Router",
                      "The router answers but is dropping %s%% of packets - "
                      "expect hiccups." % g["loss_pct"]))
    else:
        lines.append(("pass", "Router",
                      "The local router answers quickly (%s ms average, no "
                      "packet loss)." % g["avg_ms"]))
    dns = data.get("dns") or []
    bad = [d for d in dns if not d["ok"]]
    good = [d for d in dns if d["ok"] and d.get("ms") is not None]
    if not dns:
        lines.append(("warn", "Name lookups", "No name-lookup checks ran."))
    elif not bad:
        ms = min(d["ms"] for d in good) if good else None
        lines.append(("pass", "Name lookups",
                      "Name lookups work%s." %
                      (" - sites resolve in about %s ms" % ms if ms else "")))
    elif len(bad) == len(dns):
        lines.append(("fail", "Name lookups",
                      "Name lookups are completely broken - the internet "
                      "will feel 'down' even though the link is up."))
    else:
        lines.append(("warn", "Name lookups",
                      "Name lookups mostly work, but %s failed - some sites "
                      "may not load." % ", ".join(d["domain"] for d in bad)))
    ports = data.get("ports") or []
    opened = [p["port"] for p in ports if p["open"]]
    if ports:
        lines.append(("info", "Port scan",
                      "%d of %d common ports on the router answered%s." % (
                          len(opened), len(ports),
                          " (%s)" % ", ".join(map(str, opened)) if opened
                          else "")))
    v = data.get("voip")
    if not v or v.get("latency_ms") is None:
        lines.append(("fail", "Voice calls",
                      "Voice quality couldn't be tested - the probe target "
                      "didn't answer."))
    elif v["ok"]:
        lines.append(("pass", "Voice calls",
                      "Voice quality looks good (estimated score %s out of "
                      "5)." % v["mos"]))
    else:
        lines.append(("fail", "Voice calls",
                      "Voice calls will likely sound choppy or drop "
                      "(estimated score %s out of 5; target is 3.6+)."
                      % v["mos"]))
    wifi = data.get("wifi") or []
    if wifi:
        top = max(wifi, key=lambda n: n["signal"])
        lines.append(("info", "Wi-Fi nearby",
                      "%d wireless network(s) visible; strongest is '%s'."
                      % (len(wifi), top["ssid"])))
    else:
        lines.append(("info", "Wi-Fi nearby",
                      "No wireless networks detected (adapter may be off or "
                      "unavailable)."))
    spd = data.get("speed_mbps")
    if spd is None:
        st = "warn"
    elif spd < 10:
        st = "fail"
    elif spd < 25:
        st = "warn"
    else:
        st = "pass"
    lines.append((st, "Internet speed", internet_quality_line(spd)))
    return lines


def render_exec_summary(data):
    e = html.escape
    verdict_text, vclass, vnote = overall_verdict(data)
    markers = {"pass": ("✓", "m-pass"), "warn": ("!", "m-warn"),
               "fail": ("✗", "m-fail"), "info": ("•", "m-info")}
    items = []
    for st, label, text in area_lines(data):
        mark, mclass = markers[st]
        items.append("<li><span class='%s'>%s</span> <b>%s:</b> %s</li>"
                     % (mclass, mark, e(label), e(text)))
    return """<section class="exec">
<h2>Executive summary</h2>
<div class="%s"><b>Verdict: %s</b><br>%s</div>
<p><b>Voice:</b> %s</p>
<p><b>Internet:</b> %s</p>
<h3>Check by check, in plain English</h3>
<ul class="areas">%s</ul>
<p class="fwd"><i>Safe to forward to a client or manager as-is - technical
detail follows below.</i></p>
</section>""" % (vclass, e(verdict_text), e(vnote),
                e(voip_capacity_line(data.get("voip"))),
                e(internet_quality_line(data.get("speed_mbps"))),
                "".join(items))


# ------------------------------------------------------------------ demo --
def demo_data():
    """Bundled synthetic results: a deliberate mix of passes and failures
    (healthy drop, gateway, and speed; one DNS hiccup; a failing VoIP probe)
    so the executive summary and remediation lines render with zero network
    access. Used by --demo."""
    return {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "site": "Demo Site - Floor 2 (SYNTHETIC DATA)",
        "hostname": "demo-laptop",
        "local_ip": "10.1.5.23",
        "gateway": "10.1.5.1",
        "dns_servers": ["10.1.5.1", "8.8.8.8"],
        "platform": "Demo",
        "gateway_ping": {"avg_ms": 1.8, "loss_pct": 0},
        "dns": [
            {"domain": "google.com", "ok": True, "ip": "142.250.72.14",
             "ms": 12.4},
            {"domain": "cloudflare.com", "ok": False, "error": "timed out"},
        ],
        "ports": [
            {"port": 80, "open": False},
            {"port": 443, "open": True, "ms": 2.1},
            {"port": 22, "open": False},
            {"port": 3389, "open": False},
            {"port": 9100, "open": True, "ms": 1.9},
            {"port": 515, "open": False},
            {"port": 631, "open": False},
            {"port": 161, "open": False},
        ],
        "voip": {"target": "10.1.5.1:443", "latency_ms": 28.6,
                 "jitter_ms": 42.3, "loss_pct": 2.5, "mos": 3.12,
                 "ok": False},
        "wifi": [
            {"ssid": "Client-Guest", "signal": 82},
            {"ssid": "Client-Corp", "signal": 74},
            {"ssid": "xfinitywifi", "signal": 41},
        ],
        "speed_mbps": 45.2,
    }


# ----------------------------------------------------------------- report --
def build_html(data, site):
    e = html.escape
    found = findings(data)
    if not found:
        banner = "<div class='ok'><b>READY</b> — no issues found.</div>"
    else:
        items = "".join(
            "<li>%s<br><span class='fix'><b>How to fix:</b> %s</span></li>"
            % (e(msg), e(remediation_for(key)))
            for key, msg in found)
        banner = ("<div class='bad'><b>%d ISSUE(S)</b><ul>%s</ul></div>"
                  % (len(found), items))

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
.warn{{background:#fff3cd;padding:1em;border-radius:8px}}
.exec{{background:#eef4ff;border:1px solid #c8d9f5;padding:1em 1.2em;border-radius:8px;margin:1.5em 0}}
.exec h2{{margin-top:.2em}}.areas{{list-style:none;padding-left:0}}.areas li{{margin:.4em 0}}
.m-pass{{color:#155724;font-weight:bold}}.m-warn{{color:#856404;font-weight:bold}}
.m-fail{{color:#721c24;font-weight:bold}}.m-info{{color:#0c5460;font-weight:bold}}
.fix{{font-size:.92em;color:#5a1a1a}}.fwd{{color:#444}}
table{{border-collapse:collapse;width:100%;margin:.5em 0}}th,td{{border:1px solid #ddd;padding:.4em .6em;text-align:left}}
th{{background:#f5f5f5}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:1em}}</style>
</head><body>
<h1>Site Readiness Report</h1><p><b>Site:</b> {site} &nbsp; <b>When:</b> {ts} &nbsp;
<b>Tech host:</b> {host}</p>
{execsum}
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
        site=e(site), ts=ts, host=e(data["hostname"]),
        execsum=render_exec_summary(data), banner=banner,
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
    ap.add_argument("--demo", action="store_true",
                    help="build a sample report from synthetic data "
                         "(no network probes)")
    args = ap.parse_args()

    if args.demo:
        print("demo mode: synthetic data, no network probes")
        data = demo_data()
        if args.site != "Unlabeled site":
            data["site"] = args.site
    else:
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
        fh.write(build_html(data, data["site"]))
    issues = verdict(data)
    print("done -> %s" % args.out)
    print("verdict: %s" % ("READY" if not issues else "%d issue(s)" % len(issues)))
    for i in issues:
        print("  !", i)


if __name__ == "__main__":
    main()
