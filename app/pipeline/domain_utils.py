"""Registrable-domain extraction.

``companies.domain`` is the dedupe key (spec: "the real key"), so every stage
that touches a URL needs the same normalization: lowercase, no ``www``, and
collapsed to the *registrable* domain (``foo.co.uk``, not
``blog.foo.co.uk``) rather than a naive "last two labels" split, which breaks
on multi-part public suffixes. ``tldextract`` carries the Public Suffix List
for this rather than us maintaining one.
"""
from __future__ import annotations

import tldextract

# Cache the PSL on disk once rather than re-fetching/re-parsing per call.
_extract = tldextract.TLDExtract(cache_dir=None)

# Hosting/platform domains that are never themselves a company's "own site"
# even when a candidate resolves there — gate layer 3 needs this list too.
PLATFORM_DOMAINS = {
    "github.io", "github.com", "gitlab.io", "vercel.app", "netlify.app",
    "linktr.ee", "notion.site", "substack.com", "medium.com", "carrd.co",
    "bio.link", "twitter.com", "x.com", "facebook.com", "instagram.com",
    "linkedin.com", "youtube.com", "producthunt.com", "ycombinator.com",
    "sec.gov", "blogspot.com", "wordpress.com", "wixsite.com", "webflow.io",
    "framer.website", "framer.app", "pages.dev", "surge.sh", "glitch.me",
    "repl.co", "herokuapp.com", "typeform.com", "google.com", "apple.com",
}


def registrable_domain(url: str | None) -> str | None:
    """Lowercased registrable domain, or ``None`` if the URL has none
    (missing, malformed, bare IP, etc.)."""
    if not url:
        return None
    ext = _extract(url)
    if not ext.domain or not ext.suffix:
        return None
    return f"{ext.domain}.{ext.suffix}".lower()


def is_platform_domain(domain: str | None) -> bool:
    return bool(domain) and domain in PLATFORM_DOMAINS
