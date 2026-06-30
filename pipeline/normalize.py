"""
Stage 2: normalize

Takes the raw CandidateRecord list from stage 1 and standardizes formats
IN PLACE: phones -> E.164, skills -> canonical lowercase names.

Design choice: we normalize BEFORE merging (not after), because conflict
detection in the merge stage only works correctly if two phone numbers
are already in the same format - otherwise "9876543210" and
"+91-9876543210" would look like a conflict when they're the same number.
"""

import re

DEFAULT_COUNTRY_CODE = "91"  # assume India if no country code present


def normalize_phone(raw: str) -> str:
    """Best-effort E.164 normalization. If we can't produce something
    confident, we return the digits-only version rather than inventing
    a country code we're not sure about - this is intentionally
    conservative per the 'wrong is worse than empty' principle."""
    digits = re.sub(r"\D", "", raw)

    if raw.strip().startswith("+"):
        return "+" + digits

    if len(digits) == 10:
        return f"+{DEFAULT_COUNTRY_CODE}{digits}"

    if len(digits) > 10:
        return f"+{digits}"

    # Too short to be a real number - return as-is, let validation flag it
    return digits


def is_valid_phone(normalized: str) -> bool:
    """
    A normalized phone is considered valid only if, after stripping the
    leading '+', it has 10-15 digits (E.164 range) AND - for our assumed
    default-country case - exactly 10 national digits. Anything shorter
    (e.g. a typo dropping one digit) is NOT a second real phone number;
    it's a malformed value and should not be silently kept alongside a
    correct one. Per "wrong but confident is worse than honestly empty",
    we drop these rather than presenting them as a valid second contact
    number.
    """
    digits = normalized.lstrip("+")
    if not digits.isdigit():
        return False
    return 10 <= len(digits) <= 15


def normalize_skill(raw: str) -> str:
    """Lowercase + strip. Canonical synonym mapping already happened
    at extraction time (SKILL_CANON) for resume/notes; here we just make
    sure CSV/ATS-sourced skill-like strings (if any) follow the same rule."""
    return raw.strip().lower()


def normalize_records(records: list) -> list:
    for rec in records:
        normalized_phones = []
        for fv in rec.phones:
            normalized_value = normalize_phone(fv.value)
            if not is_valid_phone(normalized_value):
                print(f"[warn] dropping malformed phone '{fv.value}' from source "
                      f"'{fv.source}' (normalized to '{normalized_value}', invalid length)")
                continue
            normalized_phones.append(
                fv.__class__(normalized_value, fv.source, "e164_normalize", fv.confidence)
            )
        rec.phones = normalized_phones

        rec.skills = [
            fv.__class__(normalize_skill(fv.value), fv.source, fv.method, fv.confidence)
            for fv in rec.skills
        ]
        rec.emails = [
            fv.__class__(fv.value.strip().lower(), fv.source, fv.method, fv.confidence)
            for fv in rec.emails
        ]
    return records
