# Eightfold Multi-Source Candidate Data Transformer

## What this does
Reads candidate data from multiple source types, builds one canonical
profile per candidate (deduplicated, normalized, with provenance and
confidence), and can reshape the output to a runtime-supplied config
without any code changes.

## Sources implemented
- **Structured:** Recruiter CSV export (`.csv`), ATS JSON blob (`.json`)
- **Unstructured:** Resume / recruiter notes (`.txt`, `.pdf`)

GitHub and LinkedIn sources were **deliberately descoped** given the time
box — they require external API calls / auth, which adds integration risk
without changing the core merge/confidence/config logic this assignment
is actually testing.

From resumes, we extract: email, phone, skills (fixed vocabulary),
**education** and **experience** (both via section-header detection —
find a line like "EDUCATION" or "INTERNSHIP EXPERIENCE", grab lines until
the next recognized section header, then heuristically parse institution/
degree/end_year for education, and title/summary for experience).
Full name and location are **not** extracted from resumes — see
limitations below.

## Pipeline (4 stages, not the example's 7 — see design doc for why)
```
detect_and_extract  ->  normalize  ->  merge_with_confidence  ->  project_and_validate
```
1. **detect_and_extract**: identifies each file by extension + light content
   sniffing, pulls raw fields into a canonical `CandidateRecord` per source.
2. **normalize**: phones -> E.164, skills -> lowercase canonical names,
   emails -> lowercase. Done *before* merge so conflict-detection compares
   like-for-like.
3. **merge_with_confidence**: dedups by email (fallback: normalized name),
   resolves conflicts by source precedence (structured > unstructured),
   and assigns confidence as a byproduct of the merge decision (agreement
   -> 1.0, conflict -> 0.5, single source -> its extraction confidence).
4. **project_and_validate**: applies an optional runtime config to reshape
   output (field selection, renaming, on_missing policy, confidence/
   provenance toggles), or falls back to the default schema. Validates
   required fields before returning.

## How to run

```bash
pip install pdfplumber pypdf python-docx --break-system-packages

# Default schema
python main.py --input sample_inputs --out output_default.json

# Custom config (the "required twist")
python main.py --input sample_inputs --config config_example.json --out output_custom.json
```

Sample inputs are in `sample_inputs/`:
- `recruiter.csv` — 3 candidates, one missing an email (edge case)
- `ats_export.json` — 2 candidates, one (Ravi Kumar) overlaps with the CSV
  with a *conflicting title* (edge case)
- `resume_akshaya.txt` — same person as a CSV row, matched by email,
  contributes skills
- `resume_garbled.txt` — empty file, simulates failed extraction (edge case)

## Tests
```bash
python -m unittest tests.test_pipeline -v
```
Covers: cross-source dedup by email, conflict resolution + confidence
drop, missing/empty input directory, malformed file not crashing the run,
and both `on_missing: error` and `on_missing: omit` config behaviors.

## Edge cases handled (see design doc for full reasoning)
1. **Conflicting values across sources** (e.g. job title) — resolved by
   source precedence, confidence dropped to 0.5 to flag the disagreement
   rather than reporting false certainty.
2. **Same candidate, no shared email** — falls back to normalized full-name
   matching, but this is a known-weaker signal (no fuzzy matching implemented).
3. **Garbled/empty unstructured source** — produces an empty record rather
   than guessing; never invents a field.
4. **Missing required field entirely** (no email) — handled per `on_missing`
   config: null (default), omit, or error.
5. **Malformed/unreadable source file** — caught, logged as a warning, run
   continues with remaining valid sources.
6. **Malformed individual value (e.g. a typo'd phone number)** — a phone
   that doesn't normalize to a valid 10-15 digit number is dropped during
   normalization (with a warning logged), rather than being kept alongside
   a correct number from another source as if it were a second real
   contact number. Caught via real testing during development (see git
   history / README limitations) - this is also why normalize runs
   *before* merge, so bad values never reach the conflict/union logic.

## Known limitations / deliberately descoped
- No GitHub/LinkedIn API integration.
- Full name and location are **not** extracted from resume text (only
  from CSV/ATS). Name extraction from a resume header is doable but was
  deprioritized; location rarely appears as an explicit field on a
  resume (often only implied via a college/employer city, which we
  decided not to guess from).
- Education/experience extraction is section-header-heuristic, not real
  NLP: it depends on the resume having a recognizable header line
  ("EDUCATION", "EXPERIENCE", etc.) and on PDF text extraction preserving
  line breaks cleanly. Company name and exact start/end dates within an
  experience entry are not reliably separable from the title via regex
  alone, so those sub-fields stay `null` even when the entry itself is found.
- Name-only fallback matching is exact-normalized, not fuzzy (no
  Levenshtein/typo tolerance).
- `project_and_validate`'s path resolver supports the specific path
  patterns from the brief's example config, not a general JSONPath engine.
- The custom-config projection path does not yet aggregate confidence for
  list-type fields like `skills[].name` into a single `skills_confidence`
  number (the default schema does show per-skill confidence individually).
- `overall_confidence` reflects average confidence of present fields, not
  source-diversity/completeness - a single-source candidate can score
  similarly to a multi-source corroborated one if individual field
  confidences happen to be similar.
- Resume/notes detection is extension-based for `.pdf`; for `.txt` files,
  detection can't distinguish "resume" from "recruiter notes" by
  extension alone, so both are treated identically (same extraction logic,
  tagged source="recruiter_notes").
