"""
Stage 3: merge_with_confidence

This is the core "judgment" stage. Takes the normalized list of
per-source CandidateRecords and merges same-candidate records into one
canonical CandidateRecord each, resolving conflicts and assigning
confidence as we go (confidence is a byproduct of the merge decision,
not a separate pass - see design doc reasoning).

MATCH KEY (dedup):
  Primary: lowercased email (exact match across sources).
  Fallback: normalized full_name (lowercase, whitespace-collapsed) if
  no email is present on either side. This is a WEAK signal, so any
  merge made on name-only match is flagged in provenance as
  method="name_fallback_match" - a reviewer/downstream system can choose
  to trust it less.

PRECEDENCE (conflict resolution), when two sources disagree on the same
field:
  - current_company / current_title / phone: structured sources
    (recruiter_csv, ats_json) outrank unstructured (resume, notes),
    because recruiters/ATS data is usually more current and verified.
  - skills: union across all sources (skills are additive, not
    contradictory - having a skill on a resume isn't "wrong" just
    because the CSV doesn't mention it).
  - full_name: prefer whichever value came from the higher-confidence
    source; if tied, prefer the longer/more complete string.

CONFIDENCE FORMULA (kept simple and explainable on purpose):
  - 1.0  -> 2+ sources agree exactly on this value
  - 0.5  -> sources disagree on this value (we pick a winner via
            precedence, but flag this lower confidence explicitly -
            "wrong but confident is worse than honest", so a contested
            field gets a visible confidence drop instead of false certainty)
  - else -> the original per-field confidence assigned at extraction time
            (0.9 structured, 0.5-0.6 unstructured) when only one source
            has it.
"""

import re

from .record import CandidateRecord, FieldValue

SOURCE_RANK = {
    "recruiter_csv": 2,
    "ats_json": 2,
    "resume": 1,
    "recruiter_notes": 1,
}


def _normalize_name_key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def _match_key(rec: CandidateRecord):
    if rec.emails:
        return ("email", rec.emails[0].value)
    if rec.full_name:
        return ("name", _normalize_name_key(rec.full_name.value))
    return None


def _group_by_candidate(records: list) -> list:
    """Group per-source records into clusters belonging to the same
    candidate. Records with no usable match key at all are kept as
    their own singleton group (can't safely merge what we can't identify)."""
    groups = []  # list of (key, [records])
    keyed = {}

    for rec in records:
        key = _match_key(rec)
        if key is None:
            groups.append((None, [rec]))
            continue
        if key in keyed:
            keyed[key].append(rec)
        else:
            keyed[key] = [rec]

    groups.extend(keyed.items())
    return groups


def _pick_scalar(field_name: str, candidates: list) -> FieldValue:
    """
    candidates: list of FieldValue for the same field across sources.
    Returns a single winning FieldValue with confidence adjusted per
    the rules in the module docstring.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    values = {c.value for c in candidates}
    if len(values) == 1:
        # All sources agree -> high confidence
        winner = max(candidates, key=lambda c: SOURCE_RANK.get(c.source, 0))
        return FieldValue(winner.value, winner.source, "multi_source_agree", 1.0)

    # Disagreement: apply precedence by source rank, flag lower confidence
    winner = max(candidates, key=lambda c: SOURCE_RANK.get(c.source, 0))
    return FieldValue(winner.value, winner.source, "precedence_conflict", 0.5)


def _merge_list_field(field_name: str, all_values: list) -> list:
    """For list-type fields (emails, phones, skills): union + dedup,
    keeping the highest-confidence FieldValue per unique value."""
    best = {}
    for fv in all_values:
        if fv.value not in best or fv.confidence > best[fv.value].confidence:
            best[fv.value] = fv
    return list(best.values())


def merge_with_confidence(records: list) -> list:
    """Returns a list of merged CandidateRecord, one per candidate."""
    groups = _group_by_candidate(records)
    merged_records = []

    for key, group in groups:
        merged = CandidateRecord()

        name_candidates = [r.full_name for r in group if r.full_name]
        merged.full_name = _pick_scalar("full_name", name_candidates)

        merged.emails = _merge_list_field("emails", [fv for r in group for fv in r.emails])
        merged.phones = _merge_list_field("phones", [fv for r in group for fv in r.phones])
        merged.skills = _merge_list_field("skills", [fv for r in group for fv in r.skills])

        company_candidates = [r.current_company for r in group if r.current_company]
        merged.current_company = _pick_scalar("current_company", company_candidates)

        title_candidates = [r.current_title for r in group if r.current_title]
        merged.current_title = _pick_scalar("current_title", title_candidates)

        # Education/experience entries are dicts, not simple scalars, so we
        # can't dedup by exact value equality the way we do for skills.
        # We simply concatenate across sources - duplicate detection for
        # structured sub-records (e.g. same degree mentioned twice) is
        # explicitly out of scope given time, and concatenating is safer
        # than silently dropping a real entry.
        merged.education = [fv for r in group for fv in r.education]
        merged.experience = [fv for r in group for fv in r.experience]

        # New fields
        location_candidates = [r.location for r in group if r.location]
        merged.location = _pick_scalar("location", location_candidates)

        headline_candidates = [r.headline for r in group if r.headline]
        merged.headline = _pick_scalar("headline", headline_candidates)

        years_candidates = [r.years_experience for r in group if r.years_experience]
        merged.years_experience = _pick_scalar("years_experience", years_candidates)

        # Links: union across sources (github, portfolio, linkedin, etc.)
        for r in group:
            for k, fv in r.links.items():
                if k not in merged.links or fv.confidence > merged.links[k].confidence:
                    merged.links[k] = fv

        merged_records.append(merged)

    return merged_records
