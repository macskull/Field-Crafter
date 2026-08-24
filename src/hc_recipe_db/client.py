from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import requests


@dataclass(slots=True)
class PagePayload:
    title: str
    html: str
    revision_id: int | None
    timestamp: str | None
    categories: list[str]
    url: str
    raw_json_path: str | None = None


class MissingPageError(RuntimeError):
    """Raised when MediaWiki reports a non-existent page (not retryable)."""

    def __init__(self, title: str | None = None, detail: str | None = None) -> None:
        self.title = title
        super().__init__(detail or (f"MediaWiki page does not exist: {title}" if title else "MediaWiki page does not exist"))


class MediaWikiClient:
    """Small MediaWiki API client with caching, throttling, and retries."""

    def __init__(
        self,
        api_url: str = "https://homecoming.wiki/w/api.php",
        wiki_base: str = "https://homecoming.wiki/wiki/",
        cache_dir: str | Path = "cache",
        user_agent: str = "HomecomingRecipeDB/0.1 (personal utility; respectful wiki scraper)",
        delay_seconds: float = 0.25,
        timeout: float = 30.0,
        max_retries: int = 4,
    ) -> None:
        self.api_url = api_url
        self.wiki_base = wiki_base
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self.delay_seconds = delay_seconds
        self.timeout = timeout
        self.max_retries = max_retries
        self._last_request = 0.0

    def _cache_path(self, prefix: str, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{prefix}_{digest}.json"

    def _request_json(self, params: dict[str, Any], *, cache_key: str | None = None, refresh: bool = False) -> dict[str, Any]:
        params = dict(params)
        # MediaWiki's maxlag parameter asks the server to defer non-urgent clients
        # when the backend is under load.  It is harmless on wikis that ignore it.
        params.setdefault("maxlag", 5)
        cache_path = self._cache_path("api", cache_key) if cache_key else None
        if cache_path and cache_path.exists() and not refresh:
            return json.loads(cache_path.read_text(encoding="utf-8"))

        now = time.monotonic()
        sleep_for = self.delay_seconds - (now - self._last_request)
        if sleep_for > 0:
            time.sleep(sleep_for)

        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(self.api_url, params=params, timeout=self.timeout)
                self._last_request = time.monotonic()
                if resp.status_code in (429, 502, 503, 504):
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        wait = float(retry_after) if retry_after else min(8.0, 0.5 * (2**attempt))
                    except ValueError:
                        wait = min(8.0, 0.5 * (2**attempt))
                    time.sleep(min(30.0, max(0.0, wait)))
                    continue
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    err = data["error"]
                    if err.get("code") == "missingtitle":
                        raise MissingPageError(detail=f"MediaWiki API missingtitle: {err.get('info', '')}")
                    raise RuntimeError(f"MediaWiki API error: {err}")
                if cache_path:
                    cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                return data
            except MissingPageError:
                # Missing pages/redlinks are deterministic; retrying only wastes
                # time and makes an expected stale wiki reference look like a
                # network failure.
                raise
            except Exception as exc:  # network/retry boundary
                last_exc = exc
                time.sleep(min(8.0, 0.5 * (2**attempt)))
        raise RuntimeError(f"MediaWiki request failed after {self.max_retries} attempts: {last_exc}")

    def category_members(self, category: str, *, refresh: bool = False) -> Iterator[str]:
        cmcontinue: str | None = None
        page = 0
        while True:
            params: dict[str, Any] = {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "list": "categorymembers",
                "cmtitle": f"Category:{category}",
                "cmnamespace": 0,
                "cmlimit": "max",
            }
            if cmcontinue:
                params["cmcontinue"] = cmcontinue
            data = self._request_json(
                params,
                cache_key=f"category:{category}:{page}:{cmcontinue or ''}",
                refresh=refresh,
            )
            for item in data.get("query", {}).get("categorymembers", []):
                title = item.get("title")
                if title:
                    yield title
            cont = data.get("continue", {})
            cmcontinue = cont.get("cmcontinue")
            if not cmcontinue:
                break
            page += 1

    def revision_metadata(self, titles: list[str], *, refresh: bool = False, batch_size: int = 50) -> dict[str, tuple[int | None, str | None]]:
        """Fetch latest revision IDs/timestamps in batches to avoid one extra request per page."""
        out: dict[str, tuple[int | None, str | None]] = {}
        for start in range(0, len(titles), batch_size):
            batch = titles[start:start + batch_size]
            params = {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "titles": "|".join(batch),
                "prop": "revisions",
                "rvprop": "ids|timestamp",
                "redirects": 1,
            }
            # MediaWiki rejects rvlimit/rvstart/etc. when multiple pages are
            # supplied in one query.  For a one-page batch we may request the
            # single latest revision explicitly; for multi-page batches the
            # API returns the current revision for each page without rvlimit.
            if len(batch) == 1:
                params["rvlimit"] = 1
            key = "revbatch:" + "|".join(batch)
            data = self._request_json(params, cache_key=key, refresh=refresh)
            for page in data.get("query", {}).get("pages", []):
                title = page.get("title")
                if not title:
                    continue
                revs = page.get("revisions") or []
                rev = revs[0] if revs else {}
                out[title] = (rev.get("revid"), rev.get("timestamp"))
            # Preserve redirect aliases as lookups to their canonical targets.
            for redir in data.get("query", {}).get("redirects", []):
                src, dst = redir.get("from"), redir.get("to")
                if src and dst and dst in out:
                    out[src] = out[dst]
        return out

    def parse_page(
        self, title: str, *, refresh: bool = False, fetch_timestamp: bool = True,
        expected_revision_id: int | None = None,
    ) -> PagePayload:
        # During an update scan we first fetch current revision IDs in batches. If
        # the cached parse response already has that exact revision, reuse it even
        # when refresh=True. This turns subsequent scans into incremental updates
        # instead of re-downloading every unchanged recipe page.
        if refresh and expected_revision_id is not None:
            cache_path = self._cache_path("api", f"parse:{title}")
            if cache_path.exists():
                try:
                    cached = json.loads(cache_path.read_text(encoding="utf-8"))
                    cached_revid = (cached.get("parse") or {}).get("revid")
                    if cached_revid is not None and int(cached_revid) == int(expected_revision_id):
                        refresh = False
                except Exception:
                    pass

        params = {
            "action": "parse",
            "format": "json",
            "formatversion": "2",
            "page": title,
            "prop": "text|categories|revid",
            "redirects": 1,
        }
        data = self._request_json(params, cache_key=f"parse:{title}", refresh=refresh)
        parsed = data["parse"]
        cats = [c.get("category", "") for c in parsed.get("categories", []) if c.get("category")]
        revision_id = parsed.get("revid")

        timestamp: str | None = None
        if fetch_timestamp:
            meta = self.revision_metadata([parsed.get("title", title)], refresh=refresh)
            rev_id, timestamp = meta.get(parsed.get("title", title), (None, None))
            if revision_id is None:
                revision_id = rev_id

        from urllib.parse import quote
        canonical_title = parsed.get("title", title)
        url = self.wiki_base + quote(canonical_title.replace(" ", "_"), safe="/:()'+,%-")
        cache_path = self._cache_path("api", f"parse:{title}")
        return PagePayload(
            title=canonical_title,
            html=parsed.get("text", ""),
            revision_id=revision_id,
            timestamp=timestamp,
            categories=cats,
            url=url,
            raw_json_path=str(cache_path) if cache_path.exists() else None,
        )
