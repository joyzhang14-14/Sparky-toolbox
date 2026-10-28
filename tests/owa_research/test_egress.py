import ipaddress

import pytest

from owa_research.core.errors import ErrorCode, ToolError
from owa_research.isolation.egress import DomainRule, EgressPolicy, is_blocked_hostname, is_public_ip


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
    "::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "64:ff9b::7f00:1", "64:ff9b::a00:1", "2002:7f00:1::", "2002:0a00:0001::",
    "fe80::1", "fc00::1", "fd12::1", "2001:0::1", "224.0.0.1", "ff02::1",
])
def test_non_public_addresses_blocked(ip):
    assert not is_public_ip(ipaddress.ip_address(ip))


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "2606:4700::1111", "::ffff:8.8.8.8", "64:ff9b::808:808"])
def test_public_addresses_allowed(ip):
    assert is_public_ip(ipaddress.ip_address(ip))


@pytest.mark.parametrize("host", [
    "localhost", "LOCALHOST", "foo.localhost", "metadata.google.internal", "host.docker.internal", "searxng",
    "x.internal", "printer.local", "[::ffff:127.0.0.1]", "127.1", "2130706433", "0x7f000001", "0177.0.0.1",
])
def test_blocked_hostnames(host):
    assert is_blocked_hostname(host)


@pytest.mark.parametrize("host", ["example.com", "sec.gov", "a1.b2.example.org", "8.8.8.8"])
def test_allowed_hostnames(host):
    assert not is_blocked_hostname(host)


def test_domain_rule_path_prefix_is_segment_based():
    r = DomainRule.parse("example.com/a")
    assert r.matches("https://example.com/a") and r.matches("https://example.com/a/b") and r.matches("https://sub.example.com/a")
    assert not r.matches("https://example.com/a-evil") and not r.matches("https://example.com/ab")
    assert DomainRule.parse("https://x.org/docs/").matches("https://x.org/docs/1")


def test_validate_url_rules():
    p = EgressPolicy.from_lists(None, ["bad.com"])
    assert p.validate_url("http://example.com/x") == "https://example.com/x"
    for bad, code in [
        ("https://user:pw@example.com/", ErrorCode.SSRF_BLOCKED),
        ("ftp://example.com/", ErrorCode.SSRF_BLOCKED),
        ("https://127.0.0.1/", ErrorCode.SSRF_BLOCKED),
        ("https://[::ffff:127.0.0.1]/", ErrorCode.SSRF_BLOCKED),
        ("https://host.docker.internal:8000/v1", ErrorCode.SSRF_BLOCKED),
        ("https://example.com:8080/", ErrorCode.SSRF_BLOCKED),
        ("https://evil.bad.com/", ErrorCode.SSRF_BLOCKED),
        ("https://example.com/" + "a" * 2000, ErrorCode.INVALID_ARGUMENT),
    ]:
        with pytest.raises(ToolError) as ei:
            p.validate_url(bad)
        assert ei.value.code == code, bad


def test_signed_urls_long_but_under_limit_pass():
    p = EgressPolicy()
    assert p.validate_url("https://s3.example.com/f.pdf?X-Amz-Signature=" + "a" * 1500)


def test_peer_check():
    p = EgressPolicy()
    p.check_peer("93.184.216.34", host="example.com")
    with pytest.raises(ToolError) as ei:
        p.check_peer("10.0.0.5", host="rebind.example.com")
    assert ei.value.code == ErrorCode.SSRF_BLOCKED
