"""GitHubApiAdapter — Tier 4 GitHub identity enrichment via the REST API.

Replaces the git-hound binary path. tillson/git-hound requires a full GitHub
*account* login (username/password + optional 2FA via config.yml) and only runs
in the Go worker, so it could never fire from the Python platform-trigger path.
This adapter instead uses the GitHub REST API with the user-supplied personal
access token (GITHUB_TOKEN, the ``ghp_`` value in .env), which is reliable,
needs no account password, and runs natively where the trigger lives.

For a discovered GitHub handle it returns the public profile (name, company,
blog, location, bio, twitter handle, public email, follower counts, account
age) plus any author email exposed in the account's public commits — both
high-value pivots for identity correlation.
"""
from __future__ import annotations

import os

import httpx

from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

GITHUB_API = "https://api.github.com"


class GitHubApiAdapter(ToolAdapter):
    """Enriches a GitHub username via the REST API using a personal access token."""

    def name(self) -> str:
        return "github_api"

    def version(self) -> str:
        return "rest-v3"

    def get_tool_tier(self) -> int:
        return 4

    def get_proxy_tier(self) -> int:
        # Direct egress: the token authenticates us for 5000 req/hr and GitHub
        # commonly rate-limits/blocks Tor exit ranges.
        return 2

    def health_check(self) -> bool:
        return bool(os.environ.get("GITHUB_TOKEN"))

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN', '')}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "socmint-osint",
        }

    @staticmethod
    def _extract_username(seed: str) -> str:
        """Accept a bare handle or a github.com profile URL and return the login."""
        s = seed.strip().strip("/")
        if "github.com" in s:
            s = s.split("github.com", 1)[1].strip("/")
        if "/" in s:
            s = s.split("/", 1)[0]
        return s.lstrip("@")

    def run(self, seed: str) -> list[dict]:
        username = self._extract_username(seed)
        if not username:
            return []

        headers = self._headers()
        profile: dict = {}
        emails: list[str] = []
        with httpx.Client(timeout=30.0, headers=headers) as client:
            resp = client.get(f"{GITHUB_API}/users/{username}")
            if resp.status_code != 200:
                return []
            profile = resp.json()

            # Best-effort: surface any author email exposed in public commits.
            try:
                search = client.get(
                    f"{GITHUB_API}/search/commits",
                    params={"q": f"author:{username}", "per_page": 20},
                )
                if search.status_code == 200:
                    for item in search.json().get("items", []):
                        commit = item.get("commit", {})
                        for actor in (commit.get("author"), commit.get("committer")):
                            email = (actor or {}).get("email", "")
                            if (
                                email
                                and "@" in email
                                and not email.endswith("noreply.github.com")
                                and email not in emails
                            ):
                                emails.append(email)
            except httpx.HTTPError:
                pass

        return [{"profile": profile, "emails": emails, "username": username}]

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            profile = item.get("profile") or {}
            if not profile:
                continue
            login = profile.get("login", item.get("username", ""))
            enrichment = {
                "login": login,
                "user_id": profile.get("id"),
                "name": profile.get("name"),
                "company": profile.get("company"),
                "blog": profile.get("blog"),
                "location": profile.get("location"),
                "public_email": profile.get("email"),
                "bio": profile.get("bio"),
                "twitter_username": profile.get("twitter_username"),
                "public_repos": profile.get("public_repos"),
                "followers": profile.get("followers"),
                "following": profile.get("following"),
                "created_at": profile.get("created_at"),
                "html_url": profile.get("html_url"),
                "avatar_url": profile.get("avatar_url"),
                "discovered_emails": item.get("emails", []),
            }
            units.append(
                self.make_evidence(
                    source_platform="github",
                    source_tier=2,
                    seed_value="",
                    result_type="account_found",
                    result_value=profile.get("html_url") or login,
                    platform_enrichment=enrichment,
                    notes="github profile (REST API)",
                )
            )

            # Emit discovered emails as separate pivots for correlation.
            seen = set()
            candidate_emails = list(item.get("emails", []))
            if profile.get("email"):
                candidate_emails.insert(0, profile["email"])
            for email in candidate_emails:
                if email in seen:
                    continue
                seen.add(email)
                units.append(
                    self.make_evidence(
                        source_platform="github",
                        source_tier=2,
                        seed_value="",
                        result_type="email_registered",
                        result_value=email,
                        notes="email exposed via github profile/commits",
                    )
                )
        return units
