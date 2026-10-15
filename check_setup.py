#!/usr/bin/env python3
"""
check_setup.py: prerequisite checker for site-readiness.

Prints plain-English PASS / FAIL / SKIP lines (never a traceback) for
everything site_readiness.py needs before a run: a new-enough Python, the
probe targets it touches by default (short timeouts, graceful failures),
and confirmation that no third-party packages or config files are needed.

Usage:
    python check_setup.py

Exit code 0 when nothing fails, 1 otherwise.
"""

import os
import platform
import re
import shutil
import socket
import subprocess
import sys

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


def tcp_ok(host, port, timeout):
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:
        return ""


def default_gateway():
    if platform.system() == "Windows":
        m = re.search(r"Default Gateway.*?:\s*([0-9.]+)", run(["ipconfig"]))
        return m.group(1) if m else ""
    out = run(["ip", "route", "show", "default"]) or run(["netstat", "-rn"])
    m = re.search(r"default via ([0-9.]+)|default\s+([0-9.]+)", out)
    return (m.group(1) or m.group(2)) if m else ""


def configured_dns():
    try:
        with open("/etc/resolv.conf") as fh:
            return re.findall(r"nameserver\s+([0-9.]+)", fh.read())
    except Exception:
        return []


def main():
    checks = []  # (label, status, detail)

    # 1. Python version
    ok = sys.version_info >= (3, 8)
    checks.append(("Python %s (3.8+ required)" % platform.python_version(),
                   PASS if ok else FAIL, ""))

    # 2. site_readiness.py imports cleanly (stdlib only, no pip packages)
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import site_readiness  # noqa: F401
        checks.append(("site_readiness.py imports cleanly (stdlib only)",
                       PASS, ""))
    except Exception as exc:
        checks.append(("site_readiness.py imports cleanly (stdlib only)",
                       FAIL, str(exc)[:70]))

    # 3. ping binary (used for the gateway check)
    checks.append(("ping command available",
                   PASS if shutil.which("ping") else FAIL,
                   "" if shutil.which("ping")
                   else "the gateway ping check will be skipped"))

    # 4. gateway reachability (the tool probes the gateway first)
    gw = default_gateway()
    if not gw:
        checks.append(("Default gateway", SKIP,
                       "none detected - the tool will probe 8.8.8.8 instead"))
    elif not shutil.which("ping"):
        checks.append(("Gateway %s reachable" % gw, SKIP,
                       "ping command missing"))
    else:
        cmd = (["ping", "-n", "2", gw] if platform.system() == "Windows"
               else ["ping", "-c", "2", "-W", "2", gw])
        out = run(cmd, timeout=15)
        if "TTL=" in out or "0% packet loss" in out:
            checks.append(("Gateway %s reachable" % gw, PASS,
                           "answers ping"))
        else:
            checks.append(("Gateway %s reachable" % gw, FAIL,
                           "no ping reply - the gateway port scan and the "
                           "gateway-targeted VoIP probe will fail"))

    # 5. DNS servers the tool's lookups depend on
    servers = configured_dns()
    label_note = "from system config" if servers else "public fallback"
    for srv in (servers or ["8.8.8.8", "1.1.1.1"])[:3]:
        if tcp_ok(srv, 53, 3):
            checks.append(("DNS server %s:53 reachable (%s)" % (srv, label_note),
                           PASS, ""))
        else:
            checks.append(("DNS server %s:53 reachable (%s)" % (srv, label_note),
                           FAIL, "unreachable - name lookups may fail; try a "
                           "different DNS server"))

    # 6. VoIP fallback target (used when no gateway is found)
    if tcp_ok("8.8.8.8", 53, 3):
        checks.append(("VoIP fallback target 8.8.8.8:53 reachable", PASS,
                       "the tool probes the gateway first when one is found"))
    else:
        checks.append(("VoIP fallback target 8.8.8.8:53 reachable", FAIL,
                       "unreachable - the VoIP probe will fail unless the "
                       "gateway answers"))

    # 7. speed-test hosts (short timeouts, graceful failure)
    for host, port, scheme in (("speed.cloudflare.com", 443, "https"),
                               ("cachefly.cachefly.net", 80, "http")):
        if tcp_ok(host, port, 5):
            checks.append(("Speed test host %s (%s)" % (host, scheme),
                           PASS, ""))
        else:
            checks.append(("Speed test host %s (%s)" % (host, scheme),
                           FAIL, "unreachable - the tool will try the other "
                           "host, then report the speed test as failed"))

    # 8. config file (the repo uses none)
    checks.append(("Config file", SKIP,
                   "none required - every option is a command-line flag"))

    width = max(len(label) for label, _, _ in checks) + 2
    for label, status, detail in checks:
        line = "%-*s  %s" % (width, label, status)
        if detail:
            line += "  - " + detail
        print(line)
    passed = sum(1 for _, s, _ in checks if s == PASS)
    failed = sum(1 for _, s, _ in checks if s == FAIL)
    skipped = sum(1 for _, s, _ in checks if s == SKIP)
    print("-" * 50)
    if failed:
        print("Result: %d passed, %d failed, %d skipped - "
              "fix the failures above before running the full check."
              % (passed, failed, skipped))
        return 1
    print("Result: %d passed, %d skipped - ready to run site_readiness.py."
          % (passed, skipped))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never a traceback
        print("Setup check could not complete: %s" % exc)
        sys.exit(2)
