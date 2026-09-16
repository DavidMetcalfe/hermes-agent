"""GitHub PR/issue COMMENT deep-link resolution for ``@url:`` expansion.

Pasting a comment deep link (``…#issuecomment-123`` / ``…#discussion_r123``) used
to scrape the whole PR page: the plain PR URL and the same URL with the fragment
return byte-identical content (measured: byte-identical 11,390 chars; a
review-comment link returned 56,479 chars with NO diff hunk) because the fragment
is never sent to the server. The model got a big irrelevant page and could not
tell which comment was meant. This module resolves the anchored comment itself
through the GitHub REST API; every failure path returns ``None`` so the caller
falls back to the generic scrape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent.model_metadata import estimate_tokens_rough

# Anchored so ONLY the two comment-anchor shapes match (anything else — `#issue-1`,
# `#diff-…`, other hosts, `/pulls/2/reviews/3` — falls through to the scrape with
# no API call). The optional middle segment mirrors the deleted desktop regex's
# `(?:\/[^#\s]*)?` so `…/pull/1/files#issuecomment-2` still matches; `/issues/`
# joins `/pull/` because both hit the same issues-comments endpoint.
_COMMENT_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)"
    r"/(?P<kind>pull|issues)/(?P<number>\d+)(?:/[^#\s]*)?"
    r"#(?:(?P<issue>issuecomment-(?P<issue_id>\d+))|(?P<review>discussion_r(?P<review_id>\d+)))$"
)

_TIMEOUT_SECONDS = 10.0

@dataclass(frozen=True)
class GitHubCommentLink:
    """One recognised comment deep link, pre-solved to its REST path."""

    raw_url: str
    owner: str
    repo: str
    number: int
    is_review_comment: bool
    api_path: str


def parse_comment_url(url: str) -> GitHubCommentLink | None:
    """Return the link for an anchored GitHub comment URL, else ``None``.

    Only ``https://github.com/<owner>/<repo>/(pull|issues)/<n>[/…]#(issuecomment-\\d+|
    discussion_r\\d+)`` matches — the old desktop renderer's regex, widened to
    ``/issues/`` (same REST endpoint). Any other fragment (``#issue-1``, ``#diff-…``),
    other host, or bare PR URL returns ``None`` so the caller keeps the generic scrape.
    """
    match = _COMMENT_URL_PATTERN.match(url or "")
    if match is None:
        return None
    is_review = match.group("review") is not None
    comment_id = match.group("review_id") if is_review else match.group("issue_id")
    collection = "pulls/comments" if is_review else "issues/comments"
    return GitHubCommentLink(
        raw_url=url,
        owner=match.group("owner"),
        repo=match.group("repo"),
        number=int(match.group("number")),
        is_review_comment=is_review,
        api_path=f"repos/{match.group('owner')}/{match.group('repo')}/{collection}/{comment_id}",
    )


def _github_get_json(api_path: str) -> dict | None:
    """GET one GitHub REST path; return the parsed JSON object or ``None`` for
    ANY failure (exception, timeout, non-200 — rate limit / anonymous 403
    included — or unparseable JSON). ``None`` means "fall back to the scrape"."""
    import httpx

    from tools.skills_hub_github import GitHubAuth  # token ladder: PAT → gh CLI → App → anonymous

    try:
        resp = httpx.get(
            f"https://api.github.com/{api_path}",
            headers=GitHubAuth().get_headers(),
            timeout=_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            return None
        payload = resp.json()
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def build_comment_block(link: GitHubCommentLink) -> str | None:
    """Resolve *link* to the attached-context block, or ``None`` to fall back.

    Shape (``🔗``, distinct from the generic ``🌐`` scrape header, same
    ``(N tokens)`` convention)::

        🔗 review-comment path/to/file.ts:88 (42 tokens)
        @author on <comment url>

        <body>
        --- diff hunk ---
        <hunk>
    """
    payload = _github_get_json(link.api_path)
    if payload is None or not any(key in payload for key in ("body", "path", "diff_hunk")):
        return None
    author = str(((payload.get("user") or {}) if isinstance(payload.get("user"), dict)
                  else {}).get("login") or "unknown")
    url = str(payload.get("html_url") or link.raw_url)
    body = str(payload.get("body") or "").strip() or "(empty comment)"
    if link.is_review_comment:
        path = str(payload.get("path") or "")
        line = payload.get("line") or payload.get("original_line")
        location = f"{path}:{line}" if path and line is not None else (path or "unknown location")
        header = f"🔗 review-comment {location}"
    else:
        header = f"🔗 issue-comment {link.owner}/{link.repo}#{link.number}"
    lines = [f"{author} on {url}", "", body]
    diff_hunk = str(payload.get("diff_hunk") or "").strip()
    if diff_hunk:
        lines += ["--- diff hunk ---", diff_hunk]
    content = "\n".join(lines)
    return f"{header} ({estimate_tokens_rough(content)} tokens)\n{content}"
