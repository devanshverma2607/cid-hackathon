"""Keyless network helpers shared by the reimplemented OSINT adapters.

The original ``dorks_eye``/``dorksint``/``huntpastebin``/``webdiver``/
``finalrecon``/``theharvester`` adapters wrapped third-party CLIs that either do
not exist as installable packages (``finalrecon``, ``dorksint``,
``huntpastebin``, ``webdiver``), are interactive Google scrapers that cannot run
unattended (``dorks-eye``), or ship no usable console entry-point in this image
(``theHarvester``).  As a result those tools were reported "healthy" by their
shallow ``health_check`` yet always returned zero rows.

These helpers replace that dead weight with real, key-free data sources:

* **DuckDuckGo HTML** (``ddg_search``)  — search-engine dorking, no API key.
* **crt.sh certificate transparency** (``crtsh_subdomains``) — passive subdomains.
* **DNS / TLS** (``dns_a_records`` / ``ssl_cert_info``) — infrastructure recon.
* **Direct HTTP** (``http_get``) — website fingerprinting.

Every outbound request is SSRF-guarded (no loopback / private / metadata hosts)
and may be routed through the Tor SOCKS proxy named in ``TOR_PROXY`` — falling
back transparently to direct egress when Tor (or the ``socksio`` backend) is
unavailable, so the adapters keep working either way.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import ssl
import time
from html import unescape
from urllib.parse import parse_qs, unquote, urlparse

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_TIMEOUT = 25.0
_BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata"}
_TAG_RE = re.compile(r"<[^>]+>")
_HOSTNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$")
_DDG_URL = "https://html.duckduckgo.com/html/"
_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S
)
_SNIPPET_RE = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.I | re.S)
_MOJEEK_URL = "https://www.mojeek.com/search"
_MOJEEK_RE = re.compile(r'<a[^>]+class="ob"[^>]+href="(https?://[^"]+)"', re.I)
_MOJEEK_TITLE_RE = re.compile(
    r'<a[^>]+class="title"[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', re.I | re.S
)


# ---------------------------------------------------------------------------
# SSRF guard (mirrors api.services.photo_hash._is_safe_url)
# ---------------------------------------------------------------------------
def is_safe_host(host: str) -> bool:
    """Reject loopback / private / link-local / reserved / metadata hosts."""
    host = (host or "").strip().strip("[]").lower()
    if not host or host in _BLOCKED_HOSTS or host.endswith(".internal"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # hostname literal — egress is controlled
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def is_safe_url(url: str) -> bool:
    """Only http(s) URLs to non-private literal hosts are allowed."""
    if not isinstance(url, str) or "://" not in url:
        return False
    scheme, _, rest = url.partition("://")
    if scheme.lower() not in ("http", "https"):
        return False
    host = rest.split("/", 1)[0].split("@")[-1].split(":", 1)[0]
    return is_safe_host(host)


def clean_domain(seed: str) -> str:
    """Normalise an arbitrary seed to a bare registrable hostname (or '')."""
    seed = (seed or "").strip().lower()
    seed = re.sub(r"^[a-z]+://", "", seed)
    seed = seed.split("/", 1)[0].split("@")[-1].split("?", 1)[0]
    seed = seed.split(":", 1)[0].strip(".")
    return seed if _HOSTNAME_RE.match(seed) else ""


def is_hostname(name: str) -> bool:
    return bool(_HOSTNAME_RE.match((name or "").strip().lower()))


# ---------------------------------------------------------------------------
# HTTP egress (optional Tor, automatic direct fallback)
# ---------------------------------------------------------------------------
def _tor_proxy() -> str:
    return os.environ.get("TOR_PROXY", "").strip()


def _client(use_tor: bool):
    """Build an httpx.Client, via Tor when requested + available, else direct."""
    import httpx

    headers = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}
    proxy = _tor_proxy() if use_tor else ""
    if proxy:
        try:
            return httpx.Client(
                timeout=_TIMEOUT,
                follow_redirects=True,
                headers=headers,
                proxy=proxy,
            )
        except Exception:  # noqa: BLE001 — socksio missing / bad proxy → direct
            pass
    return httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=headers)


def _attempts(use_tor: bool) -> list[bool]:
    return [True, False] if use_tor else [False]


def http_get(url: str, use_tor: bool = False, timeout: float | None = None):
    """SSRF-guarded GET; tries Tor (if asked) then direct. Returns Response|None."""
    if not is_safe_url(url):
        return None
    for tor in _attempts(use_tor):
        client = _client(tor)
        try:
            return client.get(url, timeout=timeout or _TIMEOUT)
        except Exception:  # noqa: BLE001 — try next transport / give up
            continue
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
    return None


# ---------------------------------------------------------------------------
# DuckDuckGo HTML search (keyless dorking backend)
# ---------------------------------------------------------------------------
def _strip_html(fragment: str) -> str:
    return unescape(_TAG_RE.sub("", fragment or "")).strip()


def _decode_ddg_href(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query)
        if qs.get("uddg"):
            return unquote(qs["uddg"][0])
    return href


def _parse_ddg(html: str, max_results: int) -> list[dict]:
    snippets = _SNIPPET_RE.findall(html)
    results: list[dict] = []
    for idx, (href, title) in enumerate(_RESULT_RE.findall(html)):
        url = _decode_ddg_href(href)
        if not url.startswith("http"):
            continue
        results.append(
            {
                "url": url,
                "title": _strip_html(title),
                "snippet": _strip_html(snippets[idx]) if idx < len(snippets) else "",
            }
        )
        if len(results) >= max_results:
            break
    return results


def _mojeek_search(query: str, max_results: int, use_tor: bool) -> list[dict]:
    """Fallback keyless search via Mojeek (tolerant of automated queries)."""
    html = ""
    for tor in _attempts(use_tor):
        client = _client(tor)
        try:
            resp = client.get(_MOJEEK_URL, params={"q": query})
            text = resp.text or ""
            if resp.status_code == 200 and 'class="ob"' in text:
                html = text
                break
        except Exception:  # noqa: BLE001
            continue
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
    if not html:
        return []
    titles = {u: _strip_html(t) for u, t in _MOJEEK_TITLE_RE.findall(html)}
    results: list[dict] = []
    seen: set[str] = set()
    for url in _MOJEEK_RE.findall(html):
        if url in seen:
            continue
        seen.add(url)
        results.append({"url": url, "title": titles.get(url, ""), "snippet": ""})
        if len(results) >= max_results:
            break
    return results


_SEED_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SEED_PHONE_RE = re.compile(r"^\+?[0-9][0-9\s\-().]{6,}$")


def classify_seed(seed: str) -> str:
    """Best-effort seed-type guess for dork selection: ``email`` | ``phone`` | ``username``."""
    s = (seed or "").strip()
    if _SEED_EMAIL_RE.match(s):
        return "email"
    if _SEED_PHONE_RE.match(s):
        return "phone"
    return "username"


def select_dorks(
    seed: str,
    base: tuple[str, ...],
    by_type: dict[str, tuple[str, ...]],
    max_dorks: int = 6,
) -> tuple[str, ...]:
    """Pick a seed-type-aware dork set: generic ``base`` + type-specific extras.

    De-duplicates while preserving order and caps the total at ``max_dorks`` to
    keep Tor-routed search traffic bounded.
    """
    extra = by_type.get(classify_seed(seed), ())
    seen: set[str] = set()
    dorks: list[str] = []
    for template in (*base, *extra):
        if template not in seen:
            seen.add(template)
            dorks.append(template)
    return tuple(dorks[:max_dorks])


def ddg_search(query: str, max_results: int = 20, use_tor: bool = True) -> list[dict]:
    """Keyless web search → ``[{'url','title','snippet'}]``.

    Primary backend is DuckDuckGo HTML (Tor first, then direct). DuckDuckGo
    aggressively rate-limits datacenter IPs with an "anomaly" challenge, so when
    it yields nothing we fall back to Mojeek, which tolerates automated queries.
    """
    query = (query or "").strip()
    if not query:
        return []
    html = ""
    for tor in _attempts(use_tor):
        client = _client(tor)
        try:
            resp = client.post(_DDG_URL, data={"q": query, "kl": "us-en"})
            text = resp.text or ""
            if resp.status_code == 200 and "result__a" in text:
                html = text
                break
        except Exception:  # noqa: BLE001
            continue
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
    results = _parse_ddg(html, max_results) if html else []
    if not results:
        results = _mojeek_search(query, max_results, use_tor)
    return results


# ---------------------------------------------------------------------------
# Passive infrastructure recon (DNS / TLS / certificate transparency)
# ---------------------------------------------------------------------------
def crtsh_subdomains(domain: str, use_tor: bool = False, limit: int = 200) -> list[str]:
    """Subdomains from crt.sh certificate-transparency logs (keyless JSON).

    crt.sh is notoriously rate-limited and intermittently slow, so the lookup is
    retried a few times with a short backoff before giving up. Each attempt uses
    a generous timeout because a cold query can take 10-30s to materialise.
    """
    domain = clean_domain(domain)
    if not domain:
        return []
    url = f"https://crt.sh/?q=%25.{domain}&output=json"
    rows = None
    for attempt in range(3):
        resp = http_get(url, use_tor=use_tor, timeout=40)
        if resp is not None and resp.status_code == 200:
            try:
                rows = resp.json()
                break
            except Exception:  # noqa: BLE001 — partial/empty body, retry
                rows = None
        if attempt < 2:
            time.sleep(2.0 * (attempt + 1))
    if not isinstance(rows, list):
        return []
    subs: set[str] = set()
    for row in rows:
        for name in str(row.get("name_value", "")).splitlines():
            name = name.strip().lstrip("*.").lower()
            if name.endswith(domain) and is_hostname(name):
                subs.add(name)
    return sorted(subs)[:limit]


def dns_a_records(domain: str) -> list[str]:
    """Resolved A/AAAA addresses for a domain (stdlib, no extra deps)."""
    domain = clean_domain(domain)
    if not domain:
        return []
    addrs: set[str] = set()
    try:
        for info in socket.getaddrinfo(domain, None):
            addrs.add(info[4][0])
    except OSError:
        pass
    return sorted(addrs)


def ssl_cert_info(domain: str, port: int = 443, timeout: float = 10.0) -> dict:
    """TLS certificate facts (issuer, subject, validity, SANs) for a domain."""
    domain = clean_domain(domain)
    if not domain:
        return {}
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((domain, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as tls:
                cert = tls.getpeercert()
    except Exception:  # noqa: BLE001 — handshake/cert failures are non-fatal
        return {}

    def _flatten(seq) -> dict:
        out: dict = {}
        for item in seq or ():
            for key, value in item:
                out[key] = value
        return out

    subject = _flatten(cert.get("subject"))
    issuer = _flatten(cert.get("issuer"))
    sans = [v for (t, v) in cert.get("subjectAltName", ()) if t == "DNS"]
    return {
        "subject_cn": subject.get("commonName"),
        "issuer_cn": issuer.get("commonName"),
        "issuer_org": issuer.get("organizationName"),
        "not_before": cert.get("notBefore"),
        "not_after": cert.get("notAfter"),
        "san": sans[:50],
    }


# ---------------------------------------------------------------------------
# Keyless username → public-profile existence (curated, clean-404 platforms)
# ---------------------------------------------------------------------------
_USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,38})$")


def _json_truthy(resp, *keys) -> bool:
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return False
    for key in keys:
        if isinstance(data, dict):
            data = data.get(key)
        else:
            return False
    return bool(data)


# (platform, url_template, validator(resp) -> bool). Only platforms with clean
# existence semantics (status 200/404 or an unambiguous JSON/body marker) are
# included, to keep false positives near zero without an API key.
_PROFILE_CHECKS = (
    ("github", "https://api.github.com/users/{u}", lambda r: r.status_code == 200 and _json_truthy(r, "login")),
    ("gitlab", "https://gitlab.com/{u}", lambda r: r.status_code == 200),
    ("dockerhub", "https://hub.docker.com/v2/users/{u}/", lambda r: r.status_code == 200 and _json_truthy(r, "username")),
    ("keybase", "https://keybase.io/_/api/1.0/user/lookup.json?usernames={u}", lambda r: r.status_code == 200 and bool((r.json().get("them") or [None])[0]) if r.headers.get("content-type", "").startswith("application/json") else False),
    ("pypi", "https://pypi.org/user/{u}/", lambda r: r.status_code == 200),
    ("gitea", "https://gitea.com/{u}", lambda r: r.status_code == 200),
    ("codeberg", "https://codeberg.org/{u}", lambda r: r.status_code == 200),
    ("bitbucket", "https://bitbucket.org/{u}/", lambda r: r.status_code == 200),
    ("replit", "https://replit.com/@{u}", lambda r: r.status_code == 200),
    ("telegram", "https://t.me/{u}", lambda r: r.status_code == 200 and 'tgme_page_title' in (r.text or "")),
    ("hackernews", "https://hn.algolia.com/api/v1/users/{u}", lambda r: r.status_code == 200 and _json_truthy(r, "id")),
    ("wordpress", "https://{u}.wordpress.com", lambda r: r.status_code == 200),
)


def is_username(name: str) -> bool:
    return bool(_USERNAME_RE.match((name or "").strip()))


def username_profiles(username: str, use_tor: bool = True, limit: int = 0) -> list[dict]:
    """Check a curated set of platforms for a public profile of ``username``.

    Returns ``[{'platform', 'url'}]`` for every platform where the handle
    resolves to an existing public profile. Network/transport failures and
    ambiguous responses are treated as "not found" (never a false positive).
    """
    handle = (username or "").strip().lstrip("@")
    if not is_username(handle):
        return []
    checks = _PROFILE_CHECKS[:limit] if limit else _PROFILE_CHECKS
    found: list[dict] = []
    for platform, template, validator in checks:
        url = template.format(u=handle)
        resp = http_get(url, use_tor=use_tor, timeout=12)
        if resp is None:
            continue
        try:
            ok = validator(resp)
        except Exception:  # noqa: BLE001 — a flaky validator must not abort the sweep
            ok = False
        if ok:
            # Present the human-facing profile URL, not the API endpoint.
            display = url
            if platform == "github":
                display = f"https://github.com/{handle}"
            elif platform == "dockerhub":
                display = f"https://hub.docker.com/u/{handle}"
            elif platform == "keybase":
                display = f"https://keybase.io/{handle}"
            elif platform == "hackernews":
                display = f"https://news.ycombinator.com/user?id={handle}"
            found.append({"platform": platform, "url": display})
    return found
