"""Offline unit tests for netvitals (no network access needed)."""
import json
from argparse import Namespace

import pytest

import netvitals as nv


# --------------------------------------------------------------------------
# ping parsing
# --------------------------------------------------------------------------

LINUX_PING_OUT = """PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.
64 bytes from 8.8.8.8: icmp_seq=1 ttl=119 time=1.23 ms
64 bytes from 8.8.8.8: icmp_seq=2 ttl=119 time=2.10 ms
64 bytes from 8.8.8.8: icmp_seq=3 ttl=119 time=0.95 ms
64 bytes from 8.8.8.8: icmp_seq=4 ttl=119 time=1.87 ms

--- 8.8.8.8 ping statistics ---
4 packets transmitted, 4 received, 0% packet loss, time 3004ms
rtt min/avg/max/mdev = 0.950/1.537/2.100/0.471 ms
"""

WINDOWS_PING_OUT = """Pinging 8.8.8.8 with 32 bytes of data:
Reply from 8.8.8.8: bytes=32 time=12ms TTL=119
Reply from 8.8.8.8: bytes=32 time=9ms TTL=119
Reply from 8.8.8.8: bytes=32 time=14ms TTL=119
"""


def test_parse_ping_times_linux():
    assert nv.parse_ping_times(LINUX_PING_OUT) == [1.23, 2.10, 0.95, 1.87]


def test_parse_ping_times_windows():
    assert nv.parse_ping_times(WINDOWS_PING_OUT) == [12.0, 9.0, 14.0]


def test_parse_ping_times_empty():
    assert nv.parse_ping_times("") == []
    assert nv.parse_ping_times(None) == []


@pytest.mark.parametrize("times,count,expected_status", [
    ([1.0, 2.0, 3.0, 4.0], 4, nv.OK),
    ([1.0], 4, nv.WARN),          # partial loss
    ([], 4, nv.FAIL),             # total loss
    ([300.0] * 4, 4, nv.WARN),    # latency above warn threshold
    ([50.0] * 4, 4, nv.OK),
])
def test_ping_stats(times, count, expected_status):
    c = nv._ping_stats(times, count, latency_warn=200.0)
    assert c.status == expected_status


def test_ping_stats_extra_values():
    c = nv._ping_stats([1.0, 2.0, 3.0, 4.0], 4, 200.0)
    assert c.extra["loss_pct"] == 0
    assert c.extra["avg_ms"] == 2.5
    assert c.extra["min_ms"] == 1.0
    assert c.extra["max_ms"] == 4.0
    assert c.ms == 2.5


def test_ping_stats_loss_percentage():
    c = nv._ping_stats([1.0], 4, 200.0)
    assert c.extra["loss_pct"] == 75.0


# --------------------------------------------------------------------------
# target parsing
# --------------------------------------------------------------------------

def test_split_hostport_plain():
    assert nv._split_hostport("10.0.0.1:8080") == ("10.0.0.1", 8080)


def test_split_hostport_no_port():
    assert nv._split_hostport("example.com") == ("example.com", None)


def test_split_hostport_ipv6_brackets():
    assert nv._split_hostport("[2001:db8::1]:8443") == ("2001:db8::1", 8443)
    assert nv._split_hostport("[2001:db8::1]") == ("2001:db8::1", None)


def test_parse_bare_host():
    t = nv.parse_target("10.0.0.1")
    assert (t.kind, t.host) == ("host", "10.0.0.1")


def test_parse_host_port_becomes_tcp():
    t = nv.parse_target("10.0.0.1:8080")
    assert (t.kind, t.host, t.port) == ("tcp", "10.0.0.1", 8080)


def test_parse_https_url_with_port():
    t = nv.parse_target("https://example.com:8443/x?y=1")
    assert (t.kind, t.host, t.port) == ("http", "example.com", 8443)
    assert t.url == "https://example.com:8443/x?y=1"


def test_parse_http_url_default_port():
    t = nv.parse_target("http://example.com/path")
    assert (t.kind, t.host, t.port) == ("http", "example.com", None)


def test_parse_scheme_targets():
    assert nv.parse_target("tcp:example.com:53").kind == "tcp"
    assert nv.parse_target("tcp:example.com:53").port == 53
    assert nv.parse_target("dns:example.com").kind == "dns"
    assert nv.parse_target("icmp:example.com").kind == "icmp"
    assert nv.parse_target("icmp:example.com").host == "example.com"


def test_parse_target_custom_name():
    t = nv.parse_target("8.8.8.8", "DNS-Root")
    assert t.name == "DNS-Root"


def test_parse_target_empty_raises():
    with pytest.raises(ValueError):
        nv.parse_target("   ")


def test_parse_target_bad_port_in_url():
    with pytest.raises(ValueError):
        nv.parse_target("https://example.com:99999/")


def test_parse_tcp_requires_port():
    with pytest.raises(ValueError):
        nv.parse_target("tcp:example.com")


# --------------------------------------------------------------------------
# status aggregation
# --------------------------------------------------------------------------

def _mk(status):
    return nv.TargetResult(nv.parse_target("x"), [nv.Check("a", status)])


def test_aggregate_status_worst_wins():
    r = nv.TargetResult(
        nv.parse_target("x"),
        [nv.Check("a", nv.OK), nv.Check("b", nv.WARN), nv.Check("c", nv.FAIL)],
    )
    assert r.status == nv.FAIL


def test_aggregate_status_all_ok():
    assert _mk(nv.OK).status == nv.OK


def test_aggregate_status_empty_is_skip():
    r = nv.TargetResult(nv.parse_target("x"), [])
    assert r.status == nv.SKIP


def test_exit_code_all_ok():
    assert nv._exit_code([_mk(nv.OK), _mk(nv.OK)]) == 0


def test_exit_code_mixed():
    assert nv._exit_code([_mk(nv.OK), _mk(nv.FAIL)]) == 1


def test_exit_code_all_failed():
    assert nv._exit_code([_mk(nv.FAIL), _mk(nv.ERROR)]) == 2


def test_exit_code_empty():
    assert nv._exit_code([]) == 2


def test_summarize_counts():
    counts = nv.summarize([_mk(nv.OK), _mk(nv.WARN), _mk(nv.FAIL), _mk(nv.OK)])
    assert counts == {nv.OK: 2, nv.WARN: 1, nv.FAIL: 1, nv.ERROR: 0, nv.SKIP: 0}


# --------------------------------------------------------------------------
# check planning
# --------------------------------------------------------------------------

def _args(**kw):
    base = dict(checks="", default_ports="443,80", ping_count=4, timeout=5.0,
                latency_warn=200.0, cert_warn_days=30, insecure=False,
                expect_status=None, expect_text=None)
    base.update(kw)
    return Namespace(**base)


def test_plan_bare_host_default_pulse():
    assert nv.plan_checks(nv.parse_target("example.com"), _args()) == ["dns", "icmp", "tcp"]


def test_plan_bare_host_override():
    assert nv.plan_checks(nv.parse_target("example.com"), _args(checks="icmp,tcp")) == ["icmp", "tcp"]


def test_plan_http_target_ignores_checks_flag():
    assert nv.plan_checks(nv.parse_target("https://x.com"), _args(checks="icmp")) == ["http"]


def test_plan_tcp_target():
    assert nv.plan_checks(nv.parse_target("tcp:x.com:22"), _args()) == ["tcp"]


def test_parse_ports():
    assert nv._parse_ports("443, 80,443") == [443, 80]


def test_parse_ports_invalid():
    with pytest.raises(ValueError):
        nv._parse_ports("443,abc")
    with pytest.raises(ValueError):
        nv._parse_ports("99999")
    with pytest.raises(ValueError):
        nv._parse_ports("")


# --------------------------------------------------------------------------
# target file
# --------------------------------------------------------------------------

def test_load_targets_file(tmp_path):
    f = tmp_path / "targets.txt"
    f.write_text(
        "# comment line\n"
        "; another comment\n"
        "example.com\n"
        "MyHost  10.0.0.5:80\n"
        "Web, https://example.com\n"
        "\n"
        "8.8.8.8\n",
        encoding="utf-8",
    )
    got = nv.load_targets_file(str(f))
    assert got == [
        (None, "example.com"),
        ("MyHost", "10.0.0.5:80"),
        ("Web", "https://example.com"),
        (None, "8.8.8.8"),
    ]


# --------------------------------------------------------------------------
# TLS cert math
# --------------------------------------------------------------------------

def test_days_until_far_future():
    assert nv._days_until("Jan 01 00:00:00 2200 GMT") > 36500


def test_days_until_expired():
    assert nv._days_until("Jan 01 00:00:00 2020 GMT") < 0


# --------------------------------------------------------------------------
# json payload
# --------------------------------------------------------------------------

def test_payload_structure():
    r = nv.TargetResult(
        nv.parse_target("8.8.8.8"),
        [nv.Check("icmp", nv.OK, "avg 1.2 ms", ms=1.2, extra={"avg_ms": 1.2, "loss_pct": 0})],
        ms=5.0,
    )
    p = nv.payload([r], 1)
    assert p["tool"] == "netvitals"
    assert p["round"] == 1
    assert p["targets"][0]["target"] == "8.8.8.8"
    assert p["targets"][0]["status"] == "ok"
    assert p["targets"][0]["checks"][0]["extra"]["avg_ms"] == 1.2
    assert p["targets"][0]["checks"][0]["ms"] == 1.2
    json.dumps(p)  # must be serializable


def test_payload_without_extra_has_no_key():
    r = nv.TargetResult(nv.parse_target("x"), [nv.Check("tcp", nv.FAIL, "nope")])
    p = nv.payload([r], 1)
    assert "extra" not in p["targets"][0]["checks"][0]


# --------------------------------------------------------------------------
# argv preparation (targets interleaved with flags)
# --------------------------------------------------------------------------

def test_prepare_argv_interleaved():
    got = nv._prepare_argv(["a", "--checks", "x,y", "b", "c", "-v"])
    assert got.index("--checks") < got.index("x,y")
    assert got[-3:] == ["a", "b", "c"]
    assert "x,y" not in got[-3:]


def test_prepare_argv_ddash():
    got = nv._prepare_argv(["-t", "2", "--", "-weird.target", "ok.target"])
    assert got[-2:] == ["-weird.target", "ok.target"]
    assert got[:2] == ["-t", "2"]


def test_prepare_argv_equals_form():
    got = nv._prepare_argv(["t1", "--checks=icmp,tcp", "t2"])
    assert "--checks=icmp,tcp" in got
    assert got[-2:] == ["t1", "t2"]


def test_prepare_argv_no_options():
    assert nv._prepare_argv(["a", "b", "c"]) == ["a", "b", "c"]


# --------------------------------------------------------------------------
# misc helpers
# --------------------------------------------------------------------------

def test_short_truncates():
    s = nv._short("a" * 500, 50)
    assert len(s) == 50
    assert s.endswith("…")


def test_short_collapses_whitespace():
    assert nv._short("line1\n\n  line2\t\tline3") == "line1 line2 line3"
