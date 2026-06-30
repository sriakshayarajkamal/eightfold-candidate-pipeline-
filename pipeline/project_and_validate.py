"""
Stage 4: project_and_validate

This is the "required twist". We keep the internal CandidateRecord
completely separate from the output shape. The config describes the
desired OUTPUT shape; this stage reads each requested field's "from"
path against the canonical record and builds the final JSON.

Supported config keys per field (per the example in the brief):
  - path: dotted output key, e.g. "primary_email"
  - from: canonical path to pull from, e.g. "emails[0]" or "skills[].name"
  - type: declared type (used for validation, not enforced conversion)
  - required: bool
  - normalize: optional re-normalization hint (kept simple - phones/skills
    are already normalized upstream, so this mostly just documents intent)

on_missing (top-level config key): "null" | "omit" | "error"
include_confidence / include_provenance (top-level, bool): toggles whether
  confidence/provenance get attached to each emitted field.

If no config is given at all, we fall back to the DEFAULT canonical
schema from the brief (full passthrough of CandidateRecord -> default
output document).
"""


class ValidationError(Exception):
    pass


def _fv_to_value(fv):
    return fv.value if fv is not None else None


def _get_from_canonical(record, from_path: str):
    """
    Resolve a small set of dotted/bracket path patterns against the
    canonical CandidateRecord. We only support exactly what the brief's
    example config needs - this isn't a general JSONPath engine, that
    would be overkill for the scope here.

    Supported patterns:
      "emails[0]"        -> first email value (or None)
      "phones[0]"        -> first phone value (or None)
      "full_name"        -> scalar value (or None)
      "current_company"  -> scalar
      "current_title"    -> scalar
      "skills[].name"    -> list of all skill values
    """
    if from_path == "emails[0]":
        return _fv_to_value(record.emails[0]) if record.emails else None
    if from_path == "phones[0]":
        return _fv_to_value(record.phones[0]) if record.phones else None
    if from_path == "skills[].name":
        return [fv.value for fv in record.skills]
    if from_path == "full_name":
        return _fv_to_value(record.full_name)
    if from_path == "current_company":
        return _fv_to_value(record.current_company)
    if from_path == "current_title":
        return _fv_to_value(record.current_title)

    # Unknown path -> we never invent a value, return None and let
    # on_missing policy decide what happens.
    return None


def _get_confidence_for_path(record, from_path: str):
    if from_path == "emails[0]" and record.emails:
        return record.emails[0].confidence
    if from_path == "phones[0]" and record.phones:
        return record.phones[0].confidence
    if from_path == "full_name" and record.full_name:
        return record.full_name.confidence
    if from_path == "current_company" and record.current_company:
        return record.current_company.confidence
    if from_path == "current_title" and record.current_title:
        return record.current_title.confidence
    return None


def _get_provenance_for_path(record, from_path: str):
    fv = None
    if from_path == "emails[0]" and record.emails:
        fv = record.emails[0]
    elif from_path == "phones[0]" and record.phones:
        fv = record.phones[0]
    elif from_path == "full_name":
        fv = record.full_name
    elif from_path == "current_company":
        fv = record.current_company
    elif from_path == "current_title":
        fv = record.current_title
    if fv is None:
        return None
    return {"source": fv.source, "method": fv.method}


def apply_config(record, config: dict) -> dict:
    """Project one CandidateRecord into the shape described by config."""
    on_missing = config.get("on_missing", "null")
    include_confidence = config.get("include_confidence", False)
    include_provenance = config.get("include_provenance", False)

    output = {}
    for field_spec in config.get("fields", []):
        out_path = field_spec["path"]
        from_path = field_spec.get("from", out_path)
        required = field_spec.get("required", False)

        value = _get_from_canonical(record, from_path)

        if value is None or value == []:
            if required and on_missing == "error":
                raise ValidationError(f"Required field '{out_path}' is missing")
            if on_missing == "omit":
                continue
            # default: null
            output[out_path] = None
            continue

        output[out_path] = value
        if include_confidence:
            conf = _get_confidence_for_path(record, from_path)
            output[f"{out_path}_confidence"] = conf
        if include_provenance:
            prov = _get_provenance_for_path(record, from_path)
            output[f"{out_path}_provenance"] = prov

    return output


def to_default_schema(record) -> dict:
    """Full default output schema from the brief, no config involved."""
    return {
        "candidate_id": (record.emails[0].value if record.emails
                          else (record.full_name.value if record.full_name else "unknown")),
        "full_name": _fv_to_value(record.full_name),
        "emails": [fv.value for fv in record.emails],
        "phones": [fv.value for fv in record.phones],
        "location": None,  # descoped: no location source in our 2 chosen inputs
        "links": {},        # descoped: no GitHub/LinkedIn source wired up
        "headline": None,   # descoped: not extracted from resume in this scope
        "years_experience": None,  # descoped: would need date-range parsing
        "skills": [
            {"name": fv.value, "confidence": fv.confidence, "sources": [fv.source]}
            for fv in record.skills
        ],
        "experience": [
            {**fv.value, "confidence": fv.confidence, "source": fv.source}
            for fv in record.experience
        ],
        "education": [
            {**fv.value, "confidence": fv.confidence, "source": fv.source}
            for fv in record.education
        ],
        "provenance": _build_provenance(record),
        "overall_confidence": _overall_confidence(record),
    }


def _build_provenance(record) -> list:
    prov = []
    if record.full_name:
        prov.append({"field": "full_name", "source": record.full_name.source,
                     "method": record.full_name.method})
    for fv in record.emails:
        prov.append({"field": "emails", "source": fv.source, "method": fv.method})
    for fv in record.phones:
        prov.append({"field": "phones", "source": fv.source, "method": fv.method})
    if record.current_company:
        prov.append({"field": "current_company", "source": record.current_company.source,
                     "method": record.current_company.method})
    if record.current_title:
        prov.append({"field": "current_title", "source": record.current_title.source,
                     "method": record.current_title.method})
    for fv in record.skills:
        prov.append({"field": "skills", "source": fv.source, "method": fv.method})
    return prov


def _overall_confidence(record) -> float:
    all_fvs = []
    if record.full_name:
        all_fvs.append(record.full_name)
    all_fvs.extend(record.emails)
    all_fvs.extend(record.phones)
    all_fvs.extend(record.skills)
    if not all_fvs:
        return 0.0
    return round(sum(fv.confidence for fv in all_fvs) / len(all_fvs), 2)


def validate_output(doc: dict, config: dict = None) -> None:
    """
    Minimal validation: if a config was given, every field marked
    required=True must be present and non-null (unless on_missing
    policy explicitly allowed null). If no config, just sanity-check
    the default schema has its required top-level keys.
    """
    if config:
        on_missing = config.get("on_missing", "null")
        for field_spec in config.get("fields", []):
            if field_spec.get("required") and on_missing == "error":
                if doc.get(field_spec["path"]) is None:
                    raise ValidationError(
                        f"Validation failed: required field '{field_spec['path']}' is null"
                    )
    else:
        required_keys = ["candidate_id", "full_name", "emails", "phones", "provenance"]
        for k in required_keys:
            if k not in doc:
                raise ValidationError(f"Validation failed: default schema missing key '{k}'")


def project_and_validate(records: list, config: dict = None) -> list:
    """Main entrypoint for stage 4. Returns a list of output dicts,
    one per candidate."""
    results = []
    for rec in records:
        if config:
            doc = apply_config(rec, config)
        else:
            doc = to_default_schema(rec)
        validate_output(doc, config)
        results.append(doc)
    return results
