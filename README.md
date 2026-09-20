<div align="center">

# netvitals

**One-file network vitals probe** — ping, TCP, DNS, HTTP and TLS checks in parallel,
for network engineers and ops people who want a fast answer, not a dashboard to configure.

[![tests](https://github.com/sasoun1366/netvitals/actions/workflows/test.yml/badge.svg)](https://github.com/sasoun1366/netvitals/actions/workflows/test.yml)
[![python](https://img.shields.io/badge/python-3.9%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![size](https://img.shields.io/badge/size-1%20file-ff69b4)](netvitals.py)

</div>

---

```
1.1.1.1             tcp     ✓ OK      1 ms   1.1.1.1:443 open, 1 ms
                    dns     ✓ OK      0 ms   1 record(s), 0.0 ms [1.1.1.1]
https://github.com  http    ✓ OK     64 ms   HTTP 200, 64 ms
                    tls     ✓ OK     25 ms   expires in 71 d, issuer: Sectigo Limited, CN=github.com
tcp:1.1.1.1:53      tcp     ✓ OK      2 ms   1.1.1.1:53 open, 2 ms
dns:github.com      dns     ✓ OK     12 ms   1 record(s), 12.2 ms [140.82.116.3]
4 ok — 4 target(s) in 0.1 s
```

## Why one file?

* **No setup.** Copy `netvitals.py` to any box, `pip install rich requests`, done.
* **Readable.** The whole tool fits in a single ~700-line file — auditable, hackable, no framework.
* **Portable.** Works on Linux, macOS and Windows (uses the system `ping`).
* **Scriptable.** JSON output, meaningful exit codes, watch mode — drops straight into cron, CI and other tools.

## Checks

| check | what it does | warn when |
|-------|--------------|-----------|
| `icmp` | latency via system ping: min/avg/max/jitter + packet loss | loss > 0, or avg latency above `--latency-warn` |
| `tcp`  | port reachability + connect time | — (fail on timeout/refused) |
| `dns`  | name resolution, record count, time | fail on NXDOMAIN |
| `http` | status code, response time, redirects, expected body text | status ≥ 400, latency above `--latency-warn`, missing expected text |
| `tls`  | certificate expiry, issuer, CN, SANs | expires within `--cert-warn-days`, expired, verification failed |

A target's status is the **worst** status among its checks: `ok`, `warn`, `fail`, `error` (tool problem, e.g. missing `ping` permission).

## Install

```bash
# option 1: use the file directly (works anywhere with Python 3.9+)
pip install rich requests
python netvitals.py 8.8.8.8

# option 2: install as a package
pip install .
netvitals 8.8.8.8
```

## Quick start

```bash
# full pulse on a host (dns + icmp + tcp on ports 443,80)
netvitals 192.168.1.1

# website: http status + TLS cert in one go
netvitals https://api.example.com

# a mixed batch — everything runs in parallel
netvitals 1.1.1.1 --checks dns,tcp https://github.com tcp:1.1.1.1:53 dns:github.com
```

### Target syntax

| target | meaning |
|--------|---------|
| `host` | full pulse: `dns` + `icmp` + `tcp` (default ports `443,80`) |
| `host:port` | TCP check on that port |
| `http://url` / `https://url` | HTTP check (TLS check included for `https`) |
| `tcp:host:port` | TCP check |
| `icmp:host` | ping only |
| `dns:domain` | DNS only |

Bare-host pulse ports are configurable: `netvitals 10.0.0.5 --default-ports 22,8080`.
The default pulse itself is overridable: `netvitals 10.0.0.5 --checks icmp,tcp`.
IPv6 works too: `netvitals tcp:[2001:db8::1]:443`.

### Use a target file

`examples/targets.txt` shows the format — one target per line, optional `name target`:

```
# name        target
DNS-Root      8.8.8.8
GitHub        https://github.com
Mail          tcp:mail.example.com:25
```

```bash
netvitals -f prod.txt -f backup.txt          # files are repeatable
netvitals -f prod.txt --watch --interval 60  # keep watching
```

## Watch mode

```bash
netvitals -f prod.txt --watch --interval 30 --rounds 10
```

Re-probes every 30 s, prints a timestamped event **only when a target changes state**, and re-renders the table each round:

```
[2026-09-20 14:03:12] 1.1.1.1: first check → ok
[2026-09-20 14:05:12] mail.example.com: ok → fail
```

With `--json`, each round emits one JSON document per line (JSONL) — pipe it into your log pipeline.

## JSON output

```bash
netvitals https://example.com --json
```

```json
{
  "tool": "netvitals",
  "version": "0.1.0",
  "round": 1,
  "timestamp": "2026-09-20T06:20:08+00:00",
  "targets": [
    {
      "target": "https://example.com",
      "name": "https://example.com",
      "status": "ok",
      "total_ms": 90.4,
      "checks": [
        {
          "check": "http",
          "status": "ok",
          "detail": "HTTP 200, 55 ms",
          "ms": 55.3,
          "extra": { "status_code": 200, "final_url": "https://example.com/" }
        },
        {
          "check": "tls",
          "status": "ok",
          "detail": "expires in 38 d, issuer: SSL Corporation, CN=example.com",
          "ms": 23.6,
          "extra": {
            "expires_in_days": 37.7,
            "not_after": "Oct 27 22:17:21 2026 GMT",
            "issuer": "SSL Corporation",
            "common_name": "example.com",
            "sans": ["example.com", "*.example.com"]
          }
        }
      ]
    }
  ]
}
```

## Useful flags

| flag | meaning |
|------|---------|
| `-t SEC` | per-check timeout (default 5 s) |
| `-n N` | ICMP probes per host (default 4) |
| `-w N` | parallel workers (default 8) |
| `--expect-status CODE` | require exactly this HTTP status |
| `--expect-text TEXT` | require this text in the body (case-insensitive) |
| `--latency-warn MS` | warn above this latency (default 200, `0` disables) |
| `--cert-warn-days N` | warn when a cert expires within N days (default 30) |
| `-k` | skip TLS verification (self-signed labs); cert info still reported |
| `-q` | only show warn/fail/error targets |
| `-v` | extra per-check data: resolved IPs, cert SANs, ping stats, … |
| `-j` | JSON output |

## Exit codes (for cron / CI)

| code | meaning |
|------|---------|
| `0` | all targets ok |
| `1` | at least one warn/fail/error, but not all |
| `2` | all targets failed (or bad usage) |

Cron + mail example:

```cron
*/5 * * * * /opt/netvitals/netvitals -f /opt/netvitals/prod.txt -q >> /var/log/netvitals.log 2>&1
```

CI step example (GitHub Actions):

```yaml
- name: Check production endpoints
  run: |
    pip install rich requests
    python netvitals.py -f targets-prod.txt --expect-status 200 --latency-warn 500
```

## Roadmap

- [x] icmp / tcp / dns / http / tls checks, parallel, one file
- [x] target files, watch mode, JSON output, exit codes
- [x] GitHub Actions CI on Python 3.9–3.13 + auto releases
- [ ] UDP checks
- [ ] SNMP: interface counters, CPU, memory for Cisco & friends
- [ ] Config diff between two devices
- [ ] PyPI: `pip install netvitals`
- [ ] Plugin checks: load extra checks from a directory

Want one of these? Open an issue or send a PR — see [CONTRIBUTING](CONTRIBUTING.md).

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q          # offline unit tests, no network needed
```

CI runs the suite on Python 3.9–3.13 (`.github/workflows/test.yml`), and pushing a `v*` tag
publishes a GitHub release automatically (`.github/workflows/release.yml`).

## Notes & limitations

* `icmp` uses the **system `ping`** binary. In unprivileged containers it may be blocked; netvitals reports that clearly (`no permission for ICMP (try sudo / CAP_NET_RAW)`) instead of failing the run.
* DNS timing uses the system resolver; timeout is best-effort (the OS controls resolver timeouts).
* HTTP checks follow redirects and report the final URL; `--expect-status` applies to the final response.

## License

[MIT](LICENSE)
