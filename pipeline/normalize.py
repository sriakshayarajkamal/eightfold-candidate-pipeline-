"""
Stage 2: normalize

Takes the raw CandidateRecord list from stage 1 and standardizes formats
IN PLACE: phones -> E.164 via the `phonenumbers` library (deterministic,
offline, handles country codes properly), skills -> canonical lowercase,
emails -> lowercase.

Design choice: normalize BEFORE merging so conflict detection compares
like-for-like ("+919876543210" vs "9876543210" should not look like a
conflict).

We use the `phonenumbers` library instead of hand-rolled regex because:
  - It handles 200+ country formats correctly and deterministically
  - It's fully offline (no API, no network)
  - It distinguishes "invalid number" from "valid but odd format"
  - Satisfies the deterministic constraint from the brief
"""

import re

try:
    import phonenumbers
    from phonenumbers import NumberParseException
    _PHONENUMBERS_AVAILABLE = True
except ImportError:
    _PHONENUMBERS_AVAILABLE = False

DEFAULT_REGION = "IN"   # assume India if no country code present


def normalize_phone(raw: str) -> str | None:
    """
    Normalize to E.164 using the phonenumbers library.
    Returns None if the number is unparseable or invalid — callers
    should drop None results rather than keeping a bad value.
    Falls back to digit-strip heuristic if library not installed.
    """
    if not raw or not raw.strip():
        return None

    if _PHONENUMBERS_AVAILABLE:
        try:
            parsed = phonenumbers.parse(raw, DEFAULT_REGION)
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(
                    parsed, phonenumbers.PhoneNumberFormat.E164
                )
            return None   # syntactically parsed but not a valid number
        except NumberParseException:
            return None
    else:
        # Fallback: manual heuristic (original behaviour)
        digits = re.sub(r"\D", "", raw)
        if raw.strip().startswith("+"):
            candidate = "+" + digits
        elif len(digits) == 10:
            candidate = "+91" + digits
        elif len(digits) > 10:
            candidate = "+" + digits
        else:
            return None
        digs = candidate.lstrip("+")
        return candidate if (digs.isdigit() and 10 <= len(digs) <= 15) else None


def normalize_skill(raw: str) -> str:
    """Lowercase + strip."""
    return raw.strip().lower()


def normalize_records(records: list) -> list:
    for rec in records:
        normalized_phones = []
        for fv in rec.phones:
            result = normalize_phone(fv.value)
            if result is None:
                print(f"[warn] dropping invalid phone '{fv.value}' "
                      f"from source '{fv.source}'")
                continue
            normalized_phones.append(
                fv.__class__(result, fv.source, "e164_normalize", fv.confidence)
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
