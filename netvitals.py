#!/usr/bin/env python3
"""netvitals — one-file network vitals probe.

Checks the "vitals" of one or more targets in parallel:

  icmp  latency via the system ping (min/avg/max/jitter, packet loss)
  tcp   port reachability + connect time
  dns   name resolution + record count + time
  http  status code, response time, redirects, expected body text
  tls   certificate expiry, issuer, subject, SANs

Target syntax (any mix on the command line or via --file):

  host                       full pulse: dns + icmp + tcp (default ports 443,80)
  host:port                  tcp check only
  http://url  https://url    http check (tls check included for https)
  tcp:host:port              tcp check only
  icmp:host                  ping only
  dns:domain                 dns only

Output: rich table by default, --json for machines, --quiet for problems only.
Watch mode: --watch [--interval N] [--rounds N] re-probes and logs state changes.

Exit codes: 0 = all ok, 1 = at least one warn/fail, 2 = all failed or bad usage.

Requires: Python 3.9+, rich, requests  (pip install rich requests)
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import platform
import re
import shutil
import socket
import ssl
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

VERSION = "0.1.0"
USER_AGENT = "netvitals/%s" % VERSION

OK, WARN, FAIL, ERROR, SKIP = "ok", "warn", "fail", "error", "skip"
_RANK = {SKIP: 0, OK: 1, WARN: 2, FAIL: 3, ERROR: 4}
CHECKS_ALL = ("icmp", "tcp", "dns", "http", "tls")
DEFAULT_PULSE = ("dns", "icmp", "tcp")

try:
    import requests  # type: ignore
except ImportError:  # pragma: no cover
    requests = None

try:
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    _RICH = True
except ImportError:  # pragma: no cover
    _RICH = False


# --------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------

@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    ms: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Target:
    spec: str
    name: str
    kind: str  # host | tcp | http | dns | icmp
    host: str = ""
    port: Optional[int] = None
    url: Optional[str] = None


@dataclass
class TargetResult:
    target: Target
    checks: List[Check]
    ms: float = 0.0

    @property
    def status(self) -> str:
        if not self.checks:
            return SKIP
        return max((c.status for c in self.checks), key=lambda s: _RANK[s])


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _short(value: Any, limit: int = 100) -> str:
    """Collapse whitespace and truncate long error messages."""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _split_hostport(spec: str) -> Tuple[str, Optional[int]]:
    """Split 'host:port'. Understands [ipv6]:port. Returns (host, port|None)."""
    spec = spec.strip()
    if spec.startswith("["):
        m = re.match(r"^\[(?P<host>[^\]]+)\]?(?::(?P<port>\d+))?$", spec)
        if m:
            port = int(m.group("port")) if m.group("port") else None
            return m.group("host"), port
    if spec.count(":") >= 2:  # bare IPv6 literal, no port
        return spec, None
    host, _, port_s = spec.rpartition(":")
    if host and port_s.isdigit():
        port = int(port_s)
        if 1 <= port <= 65535:
            return host, port
    return spec, None


def parse_target(spec: str, name: Optional[str] = None) -> Target:
    """Turn a raw target string into a Target. Raises ValueError on bad input."""
    spec = spec.strip()
    if not spec:
        raise ValueError("empty target")
    name = name or spec

    if spec.startswith(("http://", "https://")):
        parts = urlsplit(spec)
        host = parts.hostname  # host only, port stripped, no credentials
        if not host:
            raise ValueError("no host in URL: %s" % spec)
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("port out of range in URL: %s" % spec)
        return Target(spec, name, "http", host=host, port=port, url=spec)

    if spec.startswith("tcp:"):
        if not spec[4:]:
            raise ValueError("tcp: needs host:port")
        host, port = _split_hostport(spec[4:])
        if port is None:
            raise ValueError("tcp: needs a port, e.g. tcp:example.com:443")
        return Target(spec, name, "tcp", host=host, port=port)

    if spec.startswith("icmp:"):
        if not spec[5:]:
            raise ValueError("icmp: needs a host")
        return Target(spec, name, "icmp", host=spec[5:])

    if spec.startswith("dns:"):
        if not spec[4:]:
            raise ValueError("dns: needs a domain")
        return Target(spec, name, "dns", host=spec[4:])

    if ":" in spec:
        host, port = _split_hostport(spec)
        if port is not None:
            return Target(spec, name, "tcp", host=host, port=port)

    return Target(spec, name, "host", host=spec)


def plan_checks(t: Target, args: argparse.Namespace) -> List[str]:
    """Which checks run for a target. --checks overrides the default pulse."""
    if t.kind == "http":
        return ["http"]
    if t.kind == "tcp":
        return ["tcp"]
    if t.kind == "icmp":
        return ["icmp"]
    if t.kind == "dns":
        return ["dns"]
    if args.checks:
        wanted = [c.strip() for c in args.checks.split(",") if c.strip()]
        return [c for c in CHECKS_ALL if c in wanted]
    return list(DEFAULT_PULSE)


def _parse_ports(spec: str) -> List[int]:
    out: List[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or not 1 <= int(part) <= 65535:
            raise ValueError("invalid port in %r: %s" % (spec, part))
        p = int(part)
        if p not in out:
            out.append(p)
    if not out:
        raise ValueError("no valid ports in %r" % spec)
    return out


def load_targets_file(path: str) -> List[Tuple[Optional[str], str]]:
    """Parse a target file: blank lines and #/; comments are skipped;
    each remaining line is either 'target' or 'name target'."""
    out: List[Tuple[Optional[str], str]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith(("#", ";")):
                continue
            parts = re.split(r"[,\s]+", line, maxsplit=1)
            if len(parts) == 2:
                out.append((parts[0].strip(), parts[1].strip()))
            else:
                out.append((None, line))
    return out


def summarize(results: Sequence[TargetResult]) -> Dict[str, int]:
    counts = {s: 0 for s in (OK, WARN, FAIL, ERROR, SKIP)}
    for r in results:
        counts[r.status] += 1
    return counts


def _exit_code(results: Sequence[TargetResult]) -> int:
    if not results:
        return 2
    statuses = [r.status for r in results]
    if all(s == OK for s in statuses):
        return 0
    if all(s in (FAIL, ERROR) for s in statuses):
        return 2
    return 1


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

_PING_TIME_RE = re.compile(r"time[=:] ?(\d+(?:\.\d+)?)\s*ms")


def parse_ping_times(text: str) -> List[float]:
    """Extract all 'time=N ms' values from ping output (Linux/macOS/Windows)."""
    return [float(m) for m in _PING_TIME_RE.findall(text or "")]


def _ping_stats(times: Sequence[float], count: int, latency_warn: float) -> Check:
    """Turn raw reply times into a Check (pure function, easy to test)."""
    if not times:
        return Check("icmp", FAIL, "no replies")
    loss = (count - len(times)) / count * 100.0
    avg = sum(times) / len(times)
    jitter = statistics.pstdev(times) if len(times) > 1 else 0.0
    extra = {
        "min_ms": round(min(times), 2),
        "avg_ms": round(avg, 2),
        "max_ms": round(max(times), 2),
        "jitter_ms": round(jitter, 2),
        "loss_pct": round(loss, 1),
        "replies": len(times),
    }
    detail = "avg %.1f ms, loss %.0f%%" % (avg, loss)
    if loss >= 100:
        status = FAIL
    elif loss > 0:
        status = WARN
    else:
        status = OK
        if latency_warn and avg > latency_warn:
            status = WARN
            detail += " (warn: >%.0f ms)" % latency_warn
    return Check("icmp", status, detail, ms=round(avg, 2), extra=extra)


def check_icmp(host: str, count: int, timeout: float, latency_warn: float) -> Check:
    exe = shutil.which("ping")
    if exe is None:
        return Check("icmp", ERROR, "ping binary not found")
    wait_s = max(1, int(timeout))
    if platform.system() == "Windows":
        cmd = [exe, "-n", str(count), "-w", str(int(timeout * 1000)), host]
    else:
        cmd = [exe, "-c", str(count), "-W", str(wait_s), host]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=count * wait_s + 10,
        )
    except subprocess.TimeoutExpired:
        return Check("icmp", FAIL, "ping timed out after %d probes" % count)

    times = parse_ping_times(proc.stdout)
    check = _ping_stats(times, count, latency_warn)
    if check.status == FAIL and check.detail == "no replies":
        blob = ((proc.stdout or "") + (proc.stderr or "")).lower()
        if "operation not permitted" in blob:
            check.detail = "no permission for ICMP (try sudo / CAP_NET_RAW)"
        elif proc.returncode not in (0, None):
            check.detail = "no replies (ping exit %s)" % proc.returncode
    return check


def check_tcp(host: str, port: int, timeout: float) -> Check:
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout):
            pass
    except (socket.timeout, TimeoutError):
        return Check("tcp", FAIL, "timeout connecting to %s:%s" % (host, port))
    except ConnectionRefusedError:
        return Check("tcp", FAIL, "connection refused %s:%s" % (host, port))
    except socket.gaierror as e:
        return Check("tcp", ERROR, "cannot resolve %s: %s" % (host, _short(e, 60)))
    except OSError as e:
        return Check("tcp", FAIL, "%s:%s: %s" % (host, port, _short(e, 60)))
    ms = (time.perf_counter() - t0) * 1000
    return Check("tcp", OK, "%s:%s open, %.0f ms" % (host, port, ms),
                 ms=round(ms, 1), extra={"port": port})


_EAI_NONAME = getattr(socket, "EAI_NONAME", -2)


def check_dns(name: str, timeout: float) -> Check:
    t0 = time.perf_counter()
    try:
        infos = socket.getaddrinfo(name, None)
    except socket.gaierror as e:
        if e.errno == _EAI_NONAME:
            return Check("dns", FAIL, "%s: no such name" % name)
        return Check("dns", ERROR, "%s: %s" % (name, _short(e, 80)))
    except OSError as e:
        return Check("dns", ERROR, "%s: %s" % (name, _short(e, 80)))
    ms = (time.perf_counter() - t0) * 1000
    addrs = sorted({info[4][0] for info in infos})
    shown = ", ".join(addrs[:3]) + (" …" if len(addrs) > 3 else "")
    detail = "%d record(s), %.1f ms [%s]" % (len(addrs), ms, shown) if addrs else \
        "0 records, %.1f ms" % ms
    return Check("dns", OK, detail, ms=round(ms, 1), extra={"addresses": addrs})


def check_http(url: str, args: argparse.Namespace) -> Check:
    if requests is None:
        return Check("http", ERROR, "requests is not installed (pip install requests)")
    insecure = bool(args.insecure)
    if insecure:
        try:
            import urllib3
            urllib3.disable_warnings()
        except Exception:
            pass
    t0 = time.perf_counter()
    try:
        resp = requests.get(
            url,
            timeout=args.timeout,
            headers={"User-Agent": USER_AGENT},
            verify=not insecure,
            allow_redirects=True,
        )
    except requests.exceptions.SSLError as e:
        return Check("http", FAIL, "TLS error: %s" % _short(e))
    except requests.exceptions.ConnectTimeout:
        return Check("http", FAIL, "timeout connecting to %s" % url)
    except requests.exceptions.ReadTimeout:
        return Check("http", FAIL, "timeout reading response from %s" % url)
    except requests.exceptions.ConnectionError as e:
        return Check("http", FAIL, "connection error: %s" % _short(e))
    except requests.exceptions.RequestException as e:
        return Check("http", ERROR, _short(e))
    ms = (time.perf_counter() - t0) * 1000
    code = resp.status_code
    detail = "HTTP %d, %.0f ms" % (code, ms)
    if resp.history:
        detail += " (via %d redirect%s → %s)" % (
            len(resp.history), "s" if len(resp.history) > 1 else "", resp.url)
    if args.expect_status is not None and code != args.expect_status:
        status = FAIL
        detail += " (expected %d)" % args.expect_status
    elif code >= 400:
        status = FAIL
    else:
        status = OK
        if args.latency_warn and ms > args.latency_warn:
            status = WARN
            detail += " (warn: >%.0f ms)" % args.latency_warn
    if args.expect_text:
        if args.expect_text.lower() in resp.text.lower():
            detail += ", expected text found"
        else:
            status = FAIL
            detail += ", expected text MISSING"
    return Check("http", status, detail, ms=round(ms, 1),
                 extra={"status_code": code, "final_url": str(resp.url)})


def _cert_field(rdn: Any, key: str) -> str:
    for groups in rdn or ():
        for k, v in groups:
            if k == key:
                return v
    return ""


def _days_until(not_after: str) -> float:
    return (ssl.cert_time_to_seconds(not_after) - time.time()) / 86400.0


def check_tls(host: str, port: int, timeout: float, insecure: bool,
              cert_warn_days: int) -> Check:
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
                ms = (time.perf_counter() - t0) * 1000
    except ssl.SSLCertVerificationError as e:
        return Check("tls", FAIL, "certificate verification failed: %s" % _short(e))
    except ssl.SSLError as e:
        return Check("tls", FAIL, "TLS handshake error: %s" % _short(e))
    except (socket.timeout, TimeoutError):
        return Check("tls", FAIL, "timeout connecting to %s:%s" % (host, port))
    except socket.gaierror as e:
        return Check("tls", ERROR, "cannot resolve %s: %s" % (host, _short(e, 60)))
    except OSError as e:
        return Check("tls", FAIL, "%s:%s: %s" % (host, port, _short(e, 60)))
    if not cert:
        return Check("tls", WARN, "no certificate presented (%.0f ms)" % ms,
                     ms=round(ms, 1))
    days = _days_until(cert["notAfter"])
    issuer = (_cert_field(cert.get("issuer", ()), "organizationName")
              or _cert_field(cert.get("issuer", ()), "commonName"))
    cn = _cert_field(cert.get("subject", ()), "commonName")
    detail = "expires in %.0f d, issuer: %s" % (days, issuer or "?")
    if cn:
        detail += ", CN=%s" % cn
    if days < 0:
        status = FAIL
        detail = "certificate EXPIRED %.0f d ago" % -days
    elif cert_warn_days and 0 <= days < cert_warn_days:
        status = WARN
        detail += " (warn: <%.0f d)" % cert_warn_days
    else:
        status = OK
    return Check("tls", status, detail, ms=round(ms, 1), extra={
        "expires_in_days": round(days, 1),
        "not_after": cert.get("notAfter"),
        "issuer": issuer,
        "common_name": cn,
        "sans": [v for t, v in cert.get("subjectAltName", ()) if t == "DNS"],
    })


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

def run_target(t: Target, args: argparse.Namespace) -> TargetResult:
    t0 = time.perf_counter()
    checks: List[Check] = []
    try:
        if t.kind == "http":
            checks.append(check_http(t.url, args))
            if t.url.startswith("https://"):
                checks.append(check_tls(t.host, t.port or 443, args.timeout,
                                        args.insecure, args.cert_warn_days))
        elif t.kind == "icmp":
            checks.append(check_icmp(t.host, args.ping_count, args.timeout,
                                     args.latency_warn))
        elif t.kind == "tcp":
            checks.append(check_tcp(t.host, t.port, args.timeout))
        elif t.kind == "dns":
            checks.append(check_dns(t.host, args.timeout))
        elif t.kind == "host":
            ports = _parse_ports(args.default_ports)
            for name in plan_checks(t, args):
                if name == "dns":
                    checks.append(check_dns(t.host, args.timeout))
                elif name == "icmp":
                    checks.append(check_icmp(t.host, args.ping_count, args.timeout,
                                             args.latency_warn))
                elif name == "tcp":
                    per_port = [check_tcp(t.host, p, args.timeout) for p in ports]
                    open_check = next((c for c in per_port if c.status == OK), None)
                    if open_check:
                        checks.append(open_check)
                    else:
                        checks.append(Check(
                            "tcp", FAIL,
                            "no open ports (%s)" % ", ".join(str(p) for p in ports)))
                elif name == "tls":
                    p = 443 if 443 in ports else ports[0]
                    checks.append(check_tls(t.host, p, args.timeout,
                                            args.insecure, args.cert_warn_days))
                elif name == "http":
                    checks.append(check_http("http://%s" % t.host, args))
    except Exception as e:  # a target must never crash the whole run
        checks.append(Check("probe", ERROR, "unexpected error: %s" % _short(e)))
    return TargetResult(t, checks, ms=round((time.perf_counter() - t0) * 1000, 1))


def run_round(targets: Sequence[Target], args: argparse.Namespace) -> List[TargetResult]:
    workers = max(1, min(args.workers, len(targets)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda t: run_target(t, args), targets))


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

_STYLE = {
    OK: ("green", "OK"),
    WARN: ("yellow", "WARN"),
    FAIL: ("red", "FAIL"),
    ERROR: ("magenta", "ERR"),
    SKIP: ("dim", "SKIP"),
}
_ICON = {OK: "✓", WARN: "⚠", FAIL: "✗", ERROR: "!", SKIP: "–"}


def render(results: Sequence[TargetResult], args: argparse.Namespace,
           round_no: Optional[int] = None, elapsed: float = 0.0) -> None:
    console = Console()
    if round_no is not None:
        when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        console.print("[bold]Round %d[/bold] · %s · %d target(s)"
                      % (round_no, when, len(results)))
    table = Table(box=box.SIMPLE_HEAD, header_style="bold", pad_edge=False)
    table.add_column("Target", style="bold cyan", no_wrap=True)
    table.add_column("Check", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Time", justify="right", no_wrap=True)
    table.add_column("Detail", max_width=64)

    rows: List[Tuple[str, str, Text, str, str]] = []
    for r in results:
        kept = [c for c in r.checks if not (args.quiet and c.status == OK)]
        if not kept:
            continue
        for i, c in enumerate(kept):
            color, label = _STYLE[c.status]
            status = Text.assemble((_ICON[c.status] + " ", color), (label, color))
            rows.append((
                r.target.name if i == 0 else "",
                c.name,
                status,
                ("%.0f ms" % c.ms) if c.ms is not None else "–",
                c.detail,
            ))
    for row in rows:
        table.add_row(*row)
    if rows:
        console.print(table)

    counts = summarize(results)
    bits = []
    for s in (OK, WARN, FAIL, ERROR, SKIP):
        if counts.get(s):
            bits.append("[%s]%d %s[/]" % (_STYLE[s][0], counts[s], s))
    if args.quiet and not any(counts.get(s) for s in (WARN, FAIL, ERROR, SKIP)):
        console.print("[green]all %d target(s) ok[/]" % len(results))
    else:
        console.print(" · ".join(bits) + " — %d target(s) in %.1f s"
                      % (len(results), elapsed))

    if args.verbose:
        for r in results:
            lines: List[str] = []
            for c in r.checks:
                if c.extra:
                    lines.append("· %s: %s" % (c.name, c.detail))
                    for k, v in c.extra.items():
                        lines.append("    %s: %s" % (k, v))
            if lines:
                console.print(Panel(Text("\n".join(lines)), title=r.target.name,
                                    border_style="dim"))


def payload(results: Sequence[TargetResult], round_no: int) -> Dict[str, Any]:
    return {
        "tool": "netvitals",
        "version": VERSION,
        "round": round_no,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "targets": [
            {
                "target": r.target.spec,
                "name": r.target.name,
                "status": r.status,
                "total_ms": r.ms,
                "checks": [
                    {
                        "check": c.name,
                        "status": c.status,
                        "detail": c.detail,
                        "ms": c.ms,
                        **({"extra": c.extra} if c.extra else {}),
                    }
                    for c in r.checks
                ],
            }
            for r in results
        ],
    }


# --------------------------------------------------------------------------
# watch mode
# --------------------------------------------------------------------------

def run_watch(targets: Sequence[Target], args: argparse.Namespace) -> int:
    console = Console()
    last: Dict[str, str] = {}
    round_no = 0
    code = 1
    try:
        while True:
            round_no += 1
            if sys.stdout.isatty():
                console.clear()
            t0 = time.perf_counter()
            results = run_round(targets, args)
            elapsed = time.perf_counter() - t0
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for r in results:
                prev = last.get(r.target.spec)
                if prev is None:
                    console.print("[%s] %s: first check → [%s]%s[/]"
                                  % (now, r.target.name, _STYLE[r.status][0], r.status))
                elif prev != r.status:
                    console.print("[%s] %s: %s → [%s]%s[/]"
                                  % (now, r.target.name, prev, _STYLE[r.status][0], r.status))
            last = {r.target.spec: r.status for r in results}
            if args.json:
                print(json.dumps(payload(results, round_no)))
            else:
                render(results, args, round_no=round_no, elapsed=elapsed)
            code = _exit_code(results)
            if args.rounds and round_no >= args.rounds:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped after %d round(s)[/dim]" % round_no)
    return code


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

_EPILOG = """\
examples:
  netvitals 8.8.8.8 https://github.com
  netvitals tcp:10.0.0.1:22 dns:mail.example.com icmp:router.local
  netvitals -f targets.txt --watch --interval 60
  netvitals https://api.example.com --expect-status 200 --expect-text ok --json
  netvitals 10.0.0.5 --default-ports 22,8080 --checks icmp,tcp -v
  netvitals -f prod.txt --latency-warn 150 --cert-warn-days 14 -q

exit codes:
  0  all targets ok
  1  at least one warn/fail/error, but not all
  2  all targets failed (or bad usage)

target file format (one per line, '#' and ';' start comments):
  8.8.8.8
  MyRouter  10.0.0.1:22
  API       https://api.example.com
"""


_VALUE_OPTS = {
    "-f", "--file", "-t", "--timeout", "-n", "--ping-count", "-w", "--workers",
    "--default-ports", "--checks", "--expect-status", "--expect-text",
    "--latency-warn", "--cert-warn-days", "--interval", "--rounds",
}


def _prepare_argv(argv: Sequence[str]) -> List[str]:
    """Collect all target tokens at the end of argv.

    argparse cannot interleave a ``nargs='*'`` positional with options that
    appear in the middle of the command line, so targets are gathered here
    first. Everything after a ``--`` separator is always a target.
    """
    rest: List[str] = []
    targets: List[str] = []
    skip_value = False
    after_ddash = False
    for tok in argv:
        if after_ddash:
            targets.append(tok)
            continue
        if skip_value:
            rest.append(tok)
            skip_value = False
            continue
        if tok == "--":
            after_ddash = True
            continue
        if tok in _VALUE_OPTS:
            rest.append(tok)
            skip_value = True
            continue
        if tok.startswith("--") and "=" in tok:
            rest.append(tok)
            continue
        if tok.startswith("-") and len(tok) > 1:
            rest.append(tok)
            continue
        targets.append(tok)
    return rest + targets


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="One-file network vitals probe: ping, TCP, DNS, HTTP and TLS checks in parallel.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("targets", nargs="*", metavar="TARGET",
                   help="host, host:port, http(s)://url, tcp:host:port, icmp:host or dns:name")
    p.add_argument("-f", "--file", action="append", default=[], metavar="FILE",
                   help="target file: one target per line, optional 'name target' (repeatable)")
    p.add_argument("-t", "--timeout", type=float, default=5.0, metavar="SEC",
                   help="per-check timeout in seconds (default: 5)")
    p.add_argument("-n", "--ping-count", type=int, default=4, metavar="N",
                   help="ICMP probes per host (default: 4)")
    p.add_argument("-w", "--workers", type=int, default=8, metavar="N",
                   help="parallel workers (default: 8)")
    p.add_argument("--default-ports", default="443,80", metavar="LIST",
                   help="ports to try for bare hosts (default: 443,80)")
    p.add_argument("--checks", default="", metavar="LIST",
                   help="override the default host pulse, e.g. 'icmp,tcp' (choices: %s)"
                        % ",".join(CHECKS_ALL))
    p.add_argument("--expect-status", type=int, default=None, metavar="CODE",
                   help="require exactly this HTTP status code")
    p.add_argument("--expect-text", default=None, metavar="TEXT",
                   help="require this text in the HTTP body (case-insensitive)")
    p.add_argument("--latency-warn", type=float, default=200.0, metavar="MS",
                   help="warn when icmp/http latency exceeds MS (default: 200, 0 disables)")
    p.add_argument("--cert-warn-days", type=int, default=30, metavar="N",
                   help="warn when a TLS cert expires within N days (default: 30, 0 disables)")
    p.add_argument("-k", "--insecure", action="store_true",
                   help="skip TLS verification (self-signed etc.), cert info is still reported")
    p.add_argument("-j", "--json", action="store_true",
                   help="print a JSON document instead of the table")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="only show targets with warn/fail/error")
    p.add_argument("--watch", action="store_true",
                   help="keep probing in a loop and log state changes")
    p.add_argument("--interval", type=float, default=30.0, metavar="SEC",
                   help="seconds between rounds in watch mode (default: 30)")
    p.add_argument("--rounds", type=int, default=0, metavar="N",
                   help="stop after N rounds in watch mode (default: forever)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="show extra per-check data (resolved IPs, cert SANs, …)")
    p.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    return p


def _collect_targets(args: argparse.Namespace) -> List[Target]:
    names: List[Optional[str]] = []
    specs: List[str] = list(args.targets)
    for path in args.file:
        try:
            entries = load_targets_file(path)
        except OSError as e:
            raise SystemExit("netvitals: cannot read target file: %s" % _short(e))
        for name, spec in entries:
            names.append(name)
            specs.append(spec)
    targets: List[Target] = []
    seen = set()
    for i, spec in enumerate(specs):
        name = names[i] if i < len(names) else None
        t = parse_target(spec, name)
        if t.spec in seen:
            continue
        seen.add(t.spec)
        targets.append(t)
    return targets


def main(argv: Optional[Sequence[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    parser = _build_parser()
    args = parser.parse_args(_prepare_argv(list(argv)))

    try:
        for c in (c.strip() for c in args.checks.split(",") if c.strip()):
            if c not in CHECKS_ALL:
                parser.error("unknown check %r (choose from %s)"
                             % (c, ", ".join(CHECKS_ALL)))
        _parse_ports(args.default_ports)
    except ValueError as e:
        parser.error(str(e))

    try:
        targets = _collect_targets(args)
    except ValueError as e:
        parser.error(str(e))
    if not targets:
        print("netvitals: no targets given. Try: netvitals 8.8.8.8 https://example.com",
              file=sys.stderr)
        return 2
    if not _RICH and not args.json:
        print("netvitals: the 'rich' package is required for table output. "
              "Run: pip install rich requests", file=sys.stderr)
        return 2

    if args.watch:
        return run_watch(targets, args)

    t0 = time.perf_counter()
    results = run_round(targets, args)
    elapsed = time.perf_counter() - t0
    if args.json:
        print(json.dumps(payload(results, 1), indent=2))
    else:
        render(results, args, elapsed=elapsed)
    return _exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
