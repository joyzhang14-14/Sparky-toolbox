from owa_research.readers.challenge import detect_challenge


def test_cloudflare_js_by_http_layer():
    body = '<html><title>Just a moment...</title><form id="challenge-form">'
    assert detect_challenge(503, {"Server": "cloudflare"}, body) == "cloudflare-js"


def test_cloudflare_managed_by_trace():
    body = '<img src="/cdn-cgi/images/trace/managed/js/transparent.gif">'
    assert detect_challenge(403, {"server": "cloudflare"}, body) == "cloudflare-managed"


def test_cloudflare_firewall_header():
    assert detect_challenge(403, {"server": "cloudflare", "cf-error-code": "1020"}, "") == "cloudflare-firewall"


def test_dom_selectors_without_server_header():
    assert detect_challenge(200, {}, '<div id="turnstile-wrapper"></div>') == "cloudflare-js"
    assert detect_challenge(200, {}, '<input name="cf-turnstile-response">') == "cloudflare-js"


def test_script_hint_ctype():
    body = "<script>window._cf_chl_opt={cType: 'managed'}</script><title>Just a moment...</title>"
    assert detect_challenge(200, {}, body) == "cloudflare-managed"


def test_other_vendors():
    assert detect_challenge(403, {"x-datadome": "protected"}, "") == "datadome"
    assert detect_challenge(403, {"x-kpsdk-ct": "abc"}, "") == "kasada"
    assert detect_challenge(403, {"set-cookie": "_pxhd=1"}, "") == "perimeterx"


def test_empty_shell_and_normal_page():
    assert detect_challenge(403, {}, "<html><body></body></html>") == "blocked-empty-page"
    assert detect_challenge(200, {"server": "cloudflare"}, "<html><title>NVIDIA</title><body>" + "text " * 500) is None
    assert detect_challenge(404, {}, "<html><title>Not found</title><body>gone</body></html>") is None
