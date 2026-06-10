"""OSINTChanAdapter — Tier 4 4chan thread/post query (Section 11.34).

4chan threads are ephemeral, so this key-less reimplementation queries the
4plebs archive's FoolFuuka search API (no key required) for posts mentioning the
seed, falling back to the shared keyless web search scoped to 4chan archives.
The old adapter shelled out to an ``osintchan.py`` script that was never cloned,
so it always returned empty.
"""
from __future__ import annotations

from urllib.parse import quote

from worker_python.adapters._net import ddg_search, http_get
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_FOOLFUUKA_SEARCH = (
    "https://archive.4plebs.org/_/api/chan/search/?text={query}&result_type=op"
)


class OSINTChanAdapter(ToolAdapter):
    """Keyless 4chan-archive search via 4plebs + web-search fallback."""

    def name(self) -> str:
        return "osintchan"

    def version(self) -> str:
        return "4plebs-api"

    def get_tool_tier(self) -> int:
        return 4

    def health_check(self) -> bool:
        return True

    def run(self, seed: str) -> list[dict]:
        query = (seed or "").strip()
        if not query:
            return []
        results: list[dict] = []

        resp = http_get(_FOOLFUUKA_SEARCH.format(query=quote(query)), timeout=12)
        if resp is not None and resp.status_code == 200:
            try:
                payload = resp.json()
            except Exception:  # noqa: BLE001
                payload = {}
            posts = (payload.get("0", {}) or {}).get("posts", []) if isinstance(payload, dict) else []
            for post in posts[:15]:
                if not isinstance(post, dict):
                    continue
                board = post.get("board", {}).get("shortname", "") if isinstance(post.get("board"), dict) else ""
                thread = post.get("thread_num")
                num = post.get("num")
                url = (
                    f"https://archive.4plebs.org/{board}/thread/{thread}/#{num}"
                    if board and thread
                    else ""
                )
                results.append(
                    {
                        "board": board,
                        "thread": thread,
                        "num": num,
                        "url": url,
                        "comment": (post.get("comment") or "")[:300],
                        "timestamp": post.get("fourchan_date") or post.get("timestamp"),
                    }
                )

        # Fallback: keyless web search over public 4chan archives.
        if not results:
            for hit in ddg_search(
                f'"{query}" site:archive.4plebs.org OR site:archived.moe', max_results=8
            ):
                results.append(
                    {"url": hit.get("url", ""), "comment": hit.get("title", ""), "board": ""}
                )
        return results

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for data in raw:
            url = data.get("url") or "4chan"
            units.append(
                self.make_evidence(
                    source_platform="4chan",
                    source_tier=2,
                    seed_value="",
                    result_type="account_found",
                    result_value=url,
                    platform_enrichment=data,
                )
            )
        return units
