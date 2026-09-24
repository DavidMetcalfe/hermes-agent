"""Repo-wide bundled-skills self-scan CI gate (issue #111334, class 2 acceptance).

"Bundled skills/ Hub-scan: 0 self-blocks; CI gate prevents regression."

Every bundled skill under ``skills/<group>/<skill>/`` is scanned with the same
community-source policy the Hub applies at install time. Any skill that would
self-block (verdict == "dangerous") must appear in the KNOWN_BLOCKED allowlist
below; a NEW dangerous skill fails CI, and each allowlisted entry is a debt
item with a named fix PR that must eventually burn down to zero.

Design notes:
- Subset semantics (actual ⊆ allowlist): an allowlisted skill that gets fixed
  and turns CLEAN must NOT fail this test — that keeps the gate merge-order-safe
  against the outstanding fix PRs landing in any order. Only *new* detections
  block the merge.
- The vacuum guard is load-bearing: a walk that silently finds zero skills
  (layout change, path typo) would make the subset assertion pass on an empty
  set. It must fail loudly instead. (Competitor PR #98489's test has exactly
  that vacuous-pass bug; we do not repeat it.)
"""

from pathlib import Path

from tools.skills_guard import scan_skill

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPO_ROOT / "skills"

# Skills that currently scan "dangerous" under the community policy and are
# therefore known self-blocks. Every entry cites the open PR(s) fixing it;
# when a fix merges, delete the entry. This list is a burn-down, not a home.
KNOWN_BLOCKED: dict[str, str] = {
    # Fix vehicles: #98489, #98490 — both currently hardcode the pre-move
    # skills/research/ path and need a retarget to skills/web/ before merge.
    "web/blocked-page-recovery": (
        "critical env_exfil_curl; fix vehicles #98489/#98490 (retarget "
        "needed: they still point at the pre-move skills/research/ path; "
        "#111334)"
    ),
    # Fix vehicles: #102476, #105220.
    "software-development/github": (
        "critical curl_pipe_python + env_exfil_curl; fix vehicles "
        "#102476/#105220 (#111334)"
    ),
}

# Lower bound on how many skills the walk must find (58 on current main). A
# layout change that makes the walk scan almost nothing would otherwise let
# the subset assertion pass vacuously. If the bundled corpus legitimately
# shrinks (e.g. skills moved to optional-skills), update this floor in the
# same PR.
MIN_SCANNED_SKILLS = 50


def _iter_bundled_skills() -> list[Path]:
    """Two-level walk: ``skills/<group>/<skill>/`` directories. Skips
    DESCRIPTION.md and any non-directory entries. A group-level directory
    that itself holds a SKILL.md is scanned as a skill, so the walk cannot
    silently miss a skill that sits one level up from the canonical layout."""
    assert SKILLS_ROOT.is_dir(), f"skills root missing: {SKILLS_ROOT}"
    skills: list[Path] = []
    for group in sorted(p for p in SKILLS_ROOT.iterdir() if p.is_dir()):
        if (group / "SKILL.md").is_file():
            skills.append(group)
            continue
        for skill in sorted(p for p in group.iterdir() if p.is_dir()):
            skills.append(skill)
    return skills


def _scan_corpus() -> tuple[dict[str, list[str]], int]:
    """Scan every bundled skill at community trust (what the Hub applies on
    install). Returns ``(dangerous_map, scanned_count)`` where dangerous_map
    maps ``<group>/<skill>`` -> sorted critical pattern ids."""
    dangerous: dict[str, list[str]] = {}
    skills = _iter_bundled_skills()
    for skill in skills:
        result = scan_skill(skill, source="community")
        if result.verdict == "dangerous":
            key = (
                skill.name
                if skill.parent == SKILLS_ROOT
                else f"{skill.parent.name}/{skill.name}"
            )
            dangerous[key] = sorted(
                {f.pattern_id for f in result.findings if f.severity == "critical"}
            )
    return dangerous, len(skills)


def test_corpus_walk_finds_plausible_number_of_skills():
    """Vacuum guard: the walk must actually see the corpus, or the subset
    assertion below proves nothing (PR #98489's vacuous-pass bug)."""
    _, count = _scan_corpus()
    assert count >= MIN_SCANNED_SKILLS, (
        f"Only scanned {count} bundled skills (< {MIN_SCANNED_SKILLS}). "
        "The skills/ layout likely changed and this gate would pass "
        "vacuously — update the walk, do not lower this floor blindly. If "
        "the bundled corpus legitimately shrank (e.g. skills moved to "
        "optional-skills), update this floor in the same PR."
    )


def test_bundled_skills_corpus_gate():
    """0 unexpected self-blocks: actual dangerous set ⊆ KNOWN_BLOCKED.

    Subset semantics are deliberate: an allowlisted skill that gets fixed and
    turns CLEAN does NOT fail the test (merge-order-safe against the cited fix
    PRs); only a NEW dangerous skill does."""
    dangerous, count = _scan_corpus()
    regressions = sorted(set(dangerous) - set(KNOWN_BLOCKED))
    assert not regressions, (
        f"{len(regressions)} bundled skill(s) would self-block a community "
        f"install ({count} skills scanned): "
        + "; ".join(f"{name} criticals={dangerous[name]}" for name in regressions)
        + ". Reword the trigger content, or land a fix PR, before adding an "
        "allowlist entry (entries must cite their fix PRs — #111334)."
    )
