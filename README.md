# Multi-Source Candidate Data Transformer
### Eightfold Engineering Intern Assignment — Jul–Dec 2026

---

## What Problem Does This Solve?

In real hiring, candidate information arrives from many different places at once — a recruiter's spreadsheet, an ATS system, a resume PDF, a GitHub profile. Each source uses different field names, different formats, and sometimes conflicting values.

This pipeline takes all of those messy, inconsistent inputs and produces **one clean, trustworthy candidate profile** per person — deduplicated, normalized, with a full record of where every value came from and how confident we are in it.

> **Core principle:** *Wrong-but-confident is worse than honestly empty.* A bad value that looks correct silently pollutes hiring decisions. This pipeline never invents data — if it can't extract a field with confidence, it leaves it null and says so.

---

## How to Run

### 1. Install dependencies
```bash
pip install pdfplumber pypdf python-docx phonenumbers
```

### 2. Run with default output schema
```bash
python main.py --input sample_inputs --out output_default.json
```

### 3. Run with a custom output config
```bash
python main.py --input sample_inputs --config config_example.json --out output_custom.json
```

### 4. Run tests
```bash
python -m unittest tests.test_pipeline -v
```

That's it. Output JSON files appear in the project root.

---

## Input Sources

The pipeline handles two categories of sources. You need at least one from each.

### Structured Sources
These have a predictable format with clear field names.

| Source | File | What it gives us |
|---|---|---|
| Recruiter CSV | `recruiter.csv` | name, email, phone, company, title |
| ATS JSON | `ats_export.json` | same fields but with completely different key names (e.g. `applicant_name` instead of `name`) |

### Unstructured Sources
These are free-form — the pipeline has to extract fields using pattern matching and heuristics.

| Source | File | What it gives us |
|---|---|---|
| Resume PDF | `Sri_Akshaya_Resume.pdf` | name, email, phone, skills, education, experience, location, headline, years of experience |
| Resume / Notes (TXT) | `resume_akshaya.txt`, `resume_garbled.txt` | same as above |
| **GitHub Profile** ⭐ | `github_users.txt` | name, bio/headline, location, GitHub URL, skills inferred from repo languages |

### GitHub Enrichment (No API Key Needed)
Add usernames to `sample_inputs/github_users.txt`, one per line:
```
# Format: username  OR  username:email@hint.com
# The email hint tells the pipeline which candidate to merge this profile into
sriakshayarajkamal:akshayarsri@gmail.com
```
The pipeline calls GitHub's **free public REST API** — no token, no login required. If the network is down or the username doesn't exist, it logs a warning and continues. The rest of the pipeline is unaffected.

---

## Pipeline — 4 Stages

```
┌─────────────────────┐
│  Your input folder  │
│  (CSV, JSON, PDF,   │
│   TXT, GitHub)      │
└────────┬────────────┘
         │
         ▼
┌─────────────────────────────────────────────┐
│  STAGE 1 — detect_and_extract.py            │
│                                             │
│  • Looks at each file, figures out what     │
│    type it is (by extension + sniffing)     │
│  • Extracts raw fields into a              │
│    CandidateRecord per source               │
│  • Fetches GitHub profiles via public API   │
│  • Bad/unreadable files are SKIPPED,        │
│    never crash the whole run                │
└────────┬────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────┐
│  STAGE 2 — normalize.py                     │
│                                             │
│  • Phones → E.164 format                    │
│    (uses Google's phonenumbers library,     │
│     fully offline, handles 200+ countries)  │
│  • Skills → lowercase canonical names      │
│  • Emails → lowercase                       │
│  • Invalid phones are DROPPED with warning  │
│  ⚡ Runs BEFORE merge so "+91-9876543210"   │
│     and "9876543210" aren't treated as      │
│     two different numbers                   │
└────────┬────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────┐
│  STAGE 3 — merge_with_confidence.py         │
│                                             │
│  • Groups records belonging to the same     │
│    candidate (match by email → then name)   │
│  • Resolves conflicts: structured sources   │
│    (CSV/ATS) beat unstructured (resume)     │
│  • Confidence scoring:                      │
│    1.0 = multiple sources agree             │
│    0.5 = sources conflict (flagged!)        │
│    else = single-source extraction score    │
│  • Skills, emails, phones: union across     │
│    all sources (additive, not conflicting)  │
└────────┬────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────┐
│  STAGE 4 — project_and_validate.py          │
│                                             │
│  • Applies optional runtime config to       │
│    reshape output (rename fields, pick      │
│    subset, toggle provenance on/off)        │
│  • Falls back to full default schema        │
│    if no config given                       │
│  • Validates required fields before         │
│    returning — raises error if missing      │
└────────┬────────────────────────────────────┘
         │
         ▼
┌─────────────────────┐
│  output.json        │
│  One clean profile  │
│  per candidate      │
└─────────────────────┘
```

---

## Output Schema

Every candidate profile has these fields:

```json
{
  "candidate_id": "akshayarsri@gmail.com",
  "full_name": "SRI AKSHAYA",
  "emails": ["akshayarsri@gmail.com"],
  "phones": ["+918015122453"],
  "location": { "city": "Coimbatore", "region": null, "country": "IN" },
  "links": { "github": "https://github.com/sriakshayarajkamal" },
  "headline": "Results-oriented Associate Engineer...",
  "years_experience": 1.5,
  "skills": [
    { "name": "python", "confidence": 0.75, "sources": ["github"] },
    { "name": "react",  "confidence": 0.60, "sources": ["resume"] }
  ],
  "experience": [ { "company": "...", "title": "...", "start": "...", "end": "..." } ],
  "education":  [ { "institution": "...", "degree": "b.tech", "end_year": 2027 } ],
  "provenance": [ { "field": "full_name", "source": "resume", "method": "header_heuristic" } ],
  "overall_confidence": 0.65
}
```

`provenance` is the audit trail — every field tells you exactly which source it came from and how it was extracted.

---

## How Confidence Is Calculated

Every single value in the pipeline carries its own confidence score (0.0 – 1.0) based on two things: **where it came from** and **whether sources agreed**.

### Per-field confidence — assigned at extraction time

| Source & Method | Confidence | Why |
|---|---|---|
| CSV / ATS JSON — direct field | **0.90** | Structured, typed, human-verified |
| GitHub API — api_field | **0.80** | Live API, reliable but not verified |
| Resume — regex email/phone | **0.75** | Strong pattern, hard to get wrong |
| GitHub — repo language inference | **0.75** | Language detected from actual code |
| Resume — name header heuristic | **0.70** | First capitalized line, usually right |
| Resume — location / date calc | **0.60 – 0.70** | Heuristic, can misfire on odd layouts |
| Resume — keyword skill match | **0.60** | Vocab match, not semantic understanding |
| Resume/Notes — section heuristic | **0.40** | Most fragile — depends on clean PDF layout |

### Then merge adjusts confidence based on agreement

| Situation | Result |
|---|---|
| 2+ sources have the **same value** | Bumped to **1.0** — corroborated |
| Sources have **different values** | Dropped to **0.5** — conflict flagged |
| Only one source has the value | Kept at extraction-time score above |

### `overall_confidence` — the single number per candidate

This is the **weighted average** of confidence scores across the key identity fields only: `full_name`, `emails`, `phones`, and `skills`. Location, education, experience are excluded because they are heuristic extractions and would unfairly drag the score down for candidates who came from structured sources.

**Example — Ravi Kumar (CSV + ATS JSON, both agree):**
- full_name: 1.0 (both sources agree)
- email: 0.9 (CSV direct field)
- phone: 0.9 (CSV direct field)
- no skills
- overall = (1.0 + 0.9 + 0.9) / 3 = **0.93**

**Example — SRI AKSHAYA (PDF resume only):**
- full_name: 0.7 (header heuristic)
- email: 0.75 (regex)
- phone: 0.75 (regex)
- 11 skills at 0.6–0.75 each
- overall ≈ **0.65**

A score near 1.0 means multiple structured sources corroborated the values. A score near 0.5 means the data came from a single unstructured source or sources conflicted. This helps recruiters instantly see which profiles are trustworthy vs which need a manual check.

### Plain English — three scenarios

**Sources agree.**
CSV says email is `akshaya.sri@example.com`, resume also finds that exact same email. Confidence goes to **1.0** — multiple independent sources confirmed the same value. That's the strongest signal we have.

**Sources conflict.**
CSV says title is `"Data Analyst"`, ATS says `"Senior Data Analyst"`. We pick the structured source as the winner using precedence. But confidence drops to **0.5** — not because we're unsure which source to trust, but because the disagreement itself is a signal that something changed or one source is outdated. We don't hide that conflict behind a confident-looking number.

**Only one source has it.**
Meena Pillai's name only appears in the CSV. Confidence stays at whatever the extraction assigned — **0.9** in this case — because there's nothing to compare against.

### How deduplication works

For **scalar fields** (name, title, company) — we pick exactly one winner and discard the rest.

For **list fields** (skills, emails, phones) — we union across all sources and dedup by exact value. If both the CSV and the resume mention Python, it appears **once** in the output with the highest-confidence version kept, not twice.

The reason this works correctly: normalization runs **before** dedup. `"Python"`, `"PYTHON"`, and `"python"` all become `"python"` before they reach the merge logic — so they collapse into one entry automatically.

---

## Custom Output Config

Pass `--config config_example.json` to reshape the output without touching any code:

```json
{
  "fields": [
    { "path": "full_name",     "from": "full_name",   "type": "string",   "required": true },
    { "path": "primary_email", "from": "emails[0]",   "type": "string",   "required": true },
    { "path": "phone",         "from": "phones[0]",   "type": "string",   "normalize": "E164" },
    { "path": "skills",        "from": "skills[].name","type": "string[]", "normalize": "canonical" }
  ],
  "include_confidence": true,
  "include_provenance": true,
  "on_missing": "null"
}
```

The config can:
- **Select** a subset of fields to include
- **Rename** fields (`primary_email` instead of `emails[0]`)
- **Toggle** provenance and confidence on or off
- **Control** what happens when a value is missing: `null`, `omit`, or `error`

---

## Modules

| File | Role |
|---|---|
| `main.py` | CLI entrypoint — wires all 4 stages, handles `--input`, `--config`, `--out` |
| `pipeline/record.py` | Defines `CandidateRecord` and `FieldValue` — the internal data model every source gets converted into |
| `pipeline/detect_and_extract.py` | Stage 1 — file detection, PDF/TXT/CSV/JSON/GitHub extraction |
| `pipeline/normalize.py` | Stage 2 — E.164 phone normalization, skill canonicalization |
| `pipeline/merge_with_confidence.py` | Stage 3 — deduplication, conflict resolution, confidence scoring |
| `pipeline/project_and_validate.py` | Stage 4 — config-driven output projection and validation |
| `tests/test_pipeline.py` | 7 tests covering edge cases (see below) |

---

## Edge Cases — How Each One Is Handled

### 1. Same candidate appears in multiple sources with conflicting data
**Example:** Ravi Kumar is in both the CSV (title: "Data Analyst") and ATS JSON (title: "Senior Data Analyst").

**Handling:** Structured sources (CSV, ATS) outrank unstructured sources (resume, notes). When two same-rank sources conflict, the pipeline picks a winner but **drops confidence to 0.5** and records `method: "precedence_conflict"` in provenance. This explicitly flags "we made a choice but we're not certain" instead of reporting false confidence.

---

### 2. Garbled / corrupted resume file ⭐
**Example:** `resume_garbled.txt` — contains broken encoding (`��`), leet-speak substitutions (`R3act!!!`, `Sk!lz`), obfuscated emails (`akshaya[dot]sri[at]example[dot]com`), and invalid phone numbers (`987-654-32XX`).

**Handling:**
- `R3act!!!` does not match `react` in the skill vocabulary → **not extracted**
- `akshaya[dot]sri[at]...` does not match the email regex → **not extracted**
- `987-654-32XX` fails `phonenumbers` validation → **dropped with warning**
- Result: an empty record with `candidate_id: "unknown"` — no invented data

---

### 3. Missing email on a candidate
**Example:** Meena Pillai in `recruiter.csv` has no email column.

**Handling:** The merge stage falls back to name-based matching. The record is kept as a singleton. The `candidate_id` becomes the name since there's no email to use.

---

### 4. GitHub profile name doesn't match the CSV name
**Example:** GitHub returns `"sri akshaya"` but CSV has `"Akshaya Sri"` — these don't match after normalization.

**Handling:** The `github_users.txt` format supports an optional email hint (`username:email@example.com`). This injects the email into the GitHub record so the merge stage can find the right candidate by email instead of guessing on name. Without the hint, the GitHub profile stays as its own separate record — the pipeline never force-merges what it can't confirm.

---

### 5. Malformed / unreadable file
**Example:** A CSV file with raw binary bytes instead of text.

**Handling:** The exception is caught, a warning is logged, and the pipeline continues with all remaining files. One bad file never kills the whole run.

---

### 6. Invalid phone number (too short / malformed)
**Example:** A 9-digit phone from a garbled resume (`987-654-32`).

**Handling:** The `phonenumbers` library validates the number after normalization. Anything that doesn't pass E.164 validation is dropped with a warning. It does NOT appear alongside the correct number as if it were a second contact — that would be wrong-but-confident.

---

### 7. Required field missing under strict config
**Example:** Config says `full_name` is required with `on_missing: error`, but a CSV row has no name.

**Handling:** `project_and_validate` raises a `ValidationError`, the CLI exits with code 1 and prints a clear error message.

---

## Why No LLM / Generative API?

The brief requires **deterministic** output — same inputs must always produce the same outputs.

LLM APIs (Gemini, GPT, etc.) break this in two ways:
- Non-deterministic: same text in → different extraction out (even at temperature 0)
- Fragile: API key expires / rate limit hit / network down → whole pipeline crashes

Instead, this pipeline uses **only offline, deterministic tools:**
- `pdfplumber` — PDF text extraction (deterministic, no network)
- `phonenumbers` — Google's phone parsing library (offline, 200+ country formats)
- Regex + heuristics — for name, location, headline, skill extraction
- GitHub public REST API — read-only, no key needed, gracefully skips if offline

This is a deliberate design decision, not a limitation.

---

## Sample Input Files

| File | Purpose |
|---|---|
| `recruiter.csv` | 3 candidates — one missing email (edge case: no email merge key) |
| `ats_export.json` | 2 candidates — Ravi Kumar overlaps with CSV with conflicting title (edge case: conflict resolution) |
| `Sri_Akshaya_Resume.pdf` | Real PDF resume — exercises pdfplumber OCR extraction |
| `resume_akshaya.txt` | Plain text resume with dates — exercises years_experience calculation |
| `resume_garbled.txt` | Corrupted/garbled text — exercises robustness (produces empty record) |
| `github_users.txt` | GitHub username with email hint — exercises GitHub API + forced merge |

---

## Known Limitations

- Name extraction from resumes is heuristic (first 2–4 capitalized words) — unusual header layouts may fail
- Education `field` (area of study) is not extracted — not reliably separable without NLP
- LinkedIn source not implemented — requires authentication/scraping, out of scope
- Name-only fallback matching is exact, not fuzzy — `"Akshaya Sri"` and `"Sri Akshaya"` won't auto-merge
- `years_experience` requires date ranges in experience entries — roles listed without dates produce null
