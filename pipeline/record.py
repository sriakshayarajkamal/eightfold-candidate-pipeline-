"""
Canonical candidate record.

This is the single internal shape every source gets converted into before
merging. Every field also carries a "provenance" entry so we always know
which source + method produced a value, and how confident we are in it.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FieldValue:
    """A single value plus where it came from and how sure we are."""
    value: object
    source: str          # e.g. "recruiter_csv", "resume_pdf"
    method: str          # e.g. "direct_field", "regex_extract"
    confidence: float    # 0.0 - 1.0


@dataclass
class CandidateRecord:
    """
    Internal working record for one candidate, built up across sources
    before being merged and projected into the final output schema.
    """
    full_name: Optional[FieldValue] = None
    emails: list = field(default_factory=list)      # list[FieldValue], value=str
    phones: list = field(default_factory=list)       # list[FieldValue], value=str
    location: Optional[FieldValue] = None             # value={city,region,country}
    links: dict = field(default_factory=dict)         # e.g. {"linkedin": FieldValue, ...}
    headline: Optional[FieldValue] = None
    years_experience: Optional[FieldValue] = None
    skills: list = field(default_factory=list)        # list[FieldValue], value=str skill name
    experience: list = field(default_factory=list)    # list[FieldValue], value=dict
    education: list = field(default_factory=list)     # list[FieldValue], value=dict
    current_company: Optional[FieldValue] = None
    current_title: Optional[FieldValue] = None
