from taisce_cuan.utils import is_https_url, is_valid_url


def test_is_valid_url_accepts_well_formed_urls():
    assert is_valid_url("https://example.com")
    assert is_valid_url("http://example.com/path?q=1")
    assert is_valid_url("https://pypi.org/pypi/requests/json")
    assert is_valid_url("ftp://files.example.com/pub")


def test_is_valid_url_rejects_malformed_urls():
    assert not is_valid_url("example.com")  # no scheme
    assert not is_valid_url("https://")  # no host
    assert not is_valid_url("not a url")
    assert not is_valid_url("")
    assert not is_valid_url("   ")
    assert not is_valid_url(None)  # type: ignore[arg-type]


def test_is_https_url():
    assert is_https_url("https://example.com")
    assert is_https_url("https://pypi.org/pypi/requests/json")
    assert not is_https_url("http://example.com")
    assert not is_https_url("ftp://example.com")
    assert not is_https_url("example.com")
    assert not is_https_url("")
