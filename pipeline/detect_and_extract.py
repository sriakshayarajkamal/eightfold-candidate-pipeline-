"""
Stage 1: detect_and_extract

Looks at each input file, figures out what kind of source it is
(by extension + light content sniffing), and extracts raw values into
a CandidateRecord. We merge detect+extract into one stage because the
detection logic is trivial (a few lines) and doesn't deserve its own
module - splitting it further would just be extra files to navigate.

Design choice: if a file can't be read or doesn't look like any known
source, we SKIP it and log a warning. We never crash the whole run
because one file is bad (this is the "Robust" constraint).
"""

import csv
import json
import re
import os

from .record import CandidateRecord, FieldValue

SKILL_VOCAB = [
    "python", "java", "javascript", "js", "sql", "react", "node", "nodejs",
    "c++", "c", "aws", "docker", "kubernetes", "django", "flask", "git",
    "machine learning", "ml", "data analysis", "html", "css", "typescript",
    "mongodb", "postgresql", "mysql", "linux", "go", "golang", "rest api",
]

SKILL_CANON = {
    "js": "javascript",
    "ml": "machine learning",
    "golang": "go",
    "nodejs": "node",
}

# Section header aliases we look for in resume text. Matching is
# case-insensitive against a whole line that's short and looks like a
# header (we don't try to detect headers by font/boldness - we don't
# have layout info from plain extracted text, so this is line-pattern
# based: short line, mostly uppercase or title-case, no trailing period).
EDUCATION_HEADERS = ["education", "academic background", "qualification"]
EXPERIENCE_HEADERS = [
    "experience", "internship experience", "work experience",
    "professional experience", "employment history",
]
# Headers that signal "the section we care about has ended"
SECTION_STOP_HEADERS = [
    "education", "experience", "internship experience", "work experience",
    "professional experience", "employment history", "skills", "skills summary",
    "projects", "certifications", "courses and certification", "achievements",
    "objective", "summary", "tools", "publications", "awards",
]


def _looks_like_header(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 40:
        return False
    if stripped.endswith("."):
        return False
    return True


def _extract_section(text: str, header_aliases: list) -> list:
    """
    Find a section whose header line matches one of header_aliases
    (case-insensitive), and return the list of non-empty lines belonging
    to that section, stopping at the next recognized section header or
    end of text. Returns [] if no matching header is found - we never
    invent section content.
    """
    lines = text.split("\n")
    start_idx = None
    for i, line in enumerate(lines):
        clean = line.strip().lower()
        if _looks_like_header(line) and any(clean == alias or clean.startswith(alias) for alias in header_aliases):
            start_idx = i + 1
            break

    if start_idx is None:
        return []

    section_lines = []
    for line in lines[start_idx:]:
        clean = line.strip().lower()
        if _looks_like_header(line) and any(clean == alias or clean.startswith(alias) for alias in SECTION_STOP_HEADERS):
            break
        if line.strip():
            section_lines.append(line.strip())

    return section_lines


DEGREE_KEYWORDS = [
    "b.tech", "btech", "b.e", "be ", "m.tech", "mtech", "m.e", "bsc", "b.sc",
    "msc", "m.sc", "mba", "bca", "mca", "phd", "ph.d", "diploma",
    "hsc", "ssc", "sslc", "higher secondary", "secondary school",
]


def _parse_education_lines(lines: list) -> list:
    """
    Best-effort: groups education section lines into entries. Each entry
    captures whatever's findable - institution (line itself), degree
    (if a known degree keyword appears anywhere in the entry), and an
    end_year (if a 4-digit year appears). Fields we can't find stay None
    - never guessed.
    """
    entries = []
    current_institution = None
    current_text_block = []

    def flush():
        if current_institution is None and not current_text_block:
            return
        block_text = " ".join(current_text_block).lower()
        degree = next((kw for kw in DEGREE_KEYWORDS if kw in block_text), None)
        year_matches = re.findall(r"(20\d{2}|19\d{2})(?!\d)", " ".join(current_text_block))
        end_year = max(int(y) for y in year_matches) if year_matches else None
        entries.append({
            "institution": current_institution,
            "degree": degree,
            "field": None,  # not reliably extractable without real NLP
            "end_year": end_year,
        })

    for line in lines:
        # Heuristic: a line containing a year range or "Institute"/"University"/
        # "School"/"College" is treated as the start of a new entry.
        if re.search(r"\b(institute|university|school|college)\b", line.lower()) or \
           re.search(r"\d{4}\s*[-–]\s*\d{4}", line):
            if current_institution is not None or current_text_block:
                flush()
                current_text_block = []
            current_institution = line
            current_text_block = [line]
        else:
            current_text_block.append(line)

    flush()
    return entries


def _parse_experience_lines(lines: list) -> list:
    """
    Best-effort: groups experience section lines into entries. Each entry
    captures company/title (from the header-ish first line of the entry)
    and a short summary (the bullet lines under it). Start/end dates are
    left None unless an explicit YYYY-MM or Month YYYY pattern is found -
    we do not attempt to infer date ranges from vague text.
    """
    entries = []
    current_header = None
    current_bullets = []

    def flush():
        if current_header is None:
            return
        entries.append({
            "company": None,   # not reliably separable from title via regex alone
            "title": current_header,
            "start": None,
            "end": None,
            "summary": " ".join(current_bullets) if current_bullets else None,
        })

    for line in lines:
        # A line is treated as a new entry header if it does NOT start with
        # a bullet character and is reasonably short (title/company/date line).
        if not line.startswith(("-", "•", "*")) and len(line) < 100:
            if current_header is not None:
                flush()
                current_bullets = []
            current_header = line
        else:
            current_bullets.append(line.lstrip("-•* ").strip())

    flush()
    return entries


def detect_source_type(filepath: str) -> str:
    """Return a string tag for the kind of source this file is, or
    'unknown' if we can't tell."""
    ext = os.path.splitext(filepath)[1].lower()

    if ext == ".csv":
        return "recruiter_csv"

    if ext == ".json":
        # Sniff content: ATS blobs use field names that don't match ours
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and any(
                k in data for k in ("candidate_name", "applicant", "contact_info")
            ):
                return "ats_json"
            return "ats_json"  # any other json we treat as an ATS-style blob
        except Exception:
            return "unknown"

    if ext in (".txt",):
        return "recruiter_notes"

    if ext in (".pdf", ".docx"):
        return "resume"

    return "unknown"


def extract_from_csv(filepath: str) -> list:
    """Recruiter CSV export -> list of raw dicts, one per row."""
    rows = []
    try:
        with open(filepath, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
    except Exception as e:
        print(f"[warn] could not read CSV {filepath}: {e}")
    return rows


def extract_from_ats_json(filepath: str) -> list:
    """
    ATS JSON blob -> list of raw dicts. ATS field names don't match ours,
    so we map known alternate key names to our internal keys here.
    Unknown/missing keys are simply absent (never invented).
    """
    KEY_MAP = {
        "candidate_name": "name", "applicant_name": "name", "full_name": "name",
        "email_address": "email", "contact_email": "email", "email": "email",
        "phone_number": "phone", "mobile": "phone", "phone": "phone",
        "employer": "current_company", "company": "current_company",
        "current_company": "current_company",
        "job_title": "title", "position": "title", "title": "title",
    }
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[warn] could not read ATS JSON {filepath}: {e}")
        return []

    records = data if isinstance(data, list) else [data]
    normalized_rows = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        row = {}
        for k, v in rec.items():
            mapped_key = KEY_MAP.get(k, None)
            if mapped_key:
                row[mapped_key] = v
        normalized_rows.append(row)
    return normalized_rows


def extract_from_resume(filepath: str) -> dict:
    """
    Resume (PDF/DOCX/.txt) -> best-effort regex extraction.
    This is heuristic, not real NLP - intentionally descoped given time.
    We extract: email, phone, skills (from a fixed vocabulary), and raw text
    is kept so we can report low confidence if extraction looks weak.
    """
    text = ""
    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext == ".pdf":
            try:
                import pdfplumber
                with pdfplumber.open(filepath) as pdf:
                    text = "\n".join((page.extract_text() or "") for page in pdf.pages)
            except ImportError:
                from pypdf import PdfReader
                reader = PdfReader(filepath)
                text = "\n".join((page.extract_text() or "") for page in reader.pages)
        elif ext == ".docx":
            import docx
            doc = docx.Document(filepath)
            text = "\n".join(p.text for p in doc.paragraphs)
        else:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
    except Exception as e:
        print(f"[warn] could not read resume {filepath}: {e}")
        return {"raw_text": "", "extraction_failed": True}

    email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    phone_match = re.search(r"(\+?\d[\d\-\s()]{8,}\d)", text)

    text_lower = text.lower()
    found_skills = set()
    for skill in SKILL_VOCAB:
        if re.search(r"\b" + re.escape(skill) + r"\b", text_lower):
            found_skills.add(SKILL_CANON.get(skill, skill))

    education_lines = _extract_section(text, EDUCATION_HEADERS)
    experience_lines = _extract_section(text, EXPERIENCE_HEADERS)

    return {
        "raw_text": text,
        "email": email_match.group(0) if email_match else None,
        "phone": phone_match.group(0) if phone_match else None,
        "skills": sorted(found_skills),
        "education": _parse_education_lines(education_lines),
        "experience": _parse_experience_lines(experience_lines),
        "extraction_failed": len(text.strip()) == 0,
    }


def extract_from_notes(filepath: str) -> dict:
    """
    Free-text recruiter notes -> same heuristic extraction as resumes.
    NOTE: we deliberately run the SAME email/phone/skill extraction as
    extract_from_resume here. Reasoning: both resumes and recruiter notes
    can be plain .txt files, and detection by extension alone can't tell
    them apart (both are unstructured prose). Rather than guessing which
    one a .txt file "really" is, we extract the same signal from either,
    and tag provenance with source="recruiter_notes" so downstream
    precedence rules (which rank resume/notes equally low vs CSV/ATS)
    still apply correctly. This is a known/declared edge case - see README.
    """
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except Exception as e:
        print(f"[warn] could not read notes {filepath}: {e}")
        return {"raw_text": "", "extraction_failed": True}

    if len(text.strip()) == 0:
        return {"raw_text": "", "extraction_failed": True}

    email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    phone_match = re.search(r"(\+?\d[\d\-\s()]{8,}\d)", text)

    text_lower = text.lower()
    found_skills = set()
    for skill in SKILL_VOCAB:
        if re.search(r"\b" + re.escape(skill) + r"\b", text_lower):
            found_skills.add(SKILL_CANON.get(skill, skill))

    return {
        "raw_text": text,
        "email": email_match.group(0) if email_match else None,
        "phone": phone_match.group(0) if phone_match else None,
        "skills": sorted(found_skills),
        "extraction_failed": False,
    }


def build_records_from_csv_rows(rows: list) -> list:
    """Each CSV row -> one CandidateRecord (one source: 'recruiter_csv')."""
    records = []
    for row in rows:
        rec = CandidateRecord()
        name = row.get("name") or row.get("full_name")
        if name:
            rec.full_name = FieldValue(name.strip(), "recruiter_csv", "direct_field", 0.9)
        email = row.get("email")
        if email:
            rec.emails.append(FieldValue(email.strip().lower(), "recruiter_csv", "direct_field", 0.9))
        phone = row.get("phone")
        if phone:
            rec.phones.append(FieldValue(phone.strip(), "recruiter_csv", "direct_field", 0.9))
        company = row.get("current_company")
        if company:
            rec.current_company = FieldValue(company.strip(), "recruiter_csv", "direct_field", 0.9)
        title = row.get("title")
        if title:
            rec.current_title = FieldValue(title.strip(), "recruiter_csv", "direct_field", 0.9)
        records.append(rec)
    return records


def build_records_from_ats_rows(rows: list) -> list:
    """Each ATS JSON record -> one CandidateRecord (source: 'ats_json')."""
    records = []
    for row in rows:
        rec = CandidateRecord()
        if row.get("name"):
            rec.full_name = FieldValue(row["name"].strip(), "ats_json", "key_mapped", 0.85)
        if row.get("email"):
            rec.emails.append(FieldValue(row["email"].strip().lower(), "ats_json", "key_mapped", 0.85))
        if row.get("phone"):
            rec.phones.append(FieldValue(str(row["phone"]).strip(), "ats_json", "key_mapped", 0.85))
        if row.get("current_company"):
            rec.current_company = FieldValue(row["current_company"].strip(), "ats_json", "key_mapped", 0.85)
        if row.get("title"):
            rec.current_title = FieldValue(row["title"].strip(), "ats_json", "key_mapped", 0.85)
        records.append(rec)
    return records


def build_record_from_resume(data: dict) -> CandidateRecord:
    """One resume file -> one CandidateRecord (source: 'resume')."""
    rec = CandidateRecord()
    # Edge case: extraction totally failed (garbled/empty PDF text) ->
    # we deliberately produce NO fields rather than guessing. An empty
    # record here is correct behaviour, not a bug.
    if data.get("extraction_failed"):
        return rec

    if data.get("email"):
        rec.emails.append(FieldValue(data["email"].strip().lower(), "resume", "regex_extract", 0.5))
    if data.get("phone"):
        rec.phones.append(FieldValue(data["phone"].strip(), "resume", "regex_extract", 0.5))
    for skill in data.get("skills", []):
        rec.skills.append(FieldValue(skill, "resume", "keyword_match", 0.6))

    # Education/experience are section-heuristic extractions: lower
    # confidence (0.4) than skills/email regex, since section boundary
    # detection on raw extracted text is fragile (depends on PDF text
    # extraction preserving line breaks cleanly). If no section header
    # was found at all, these lists are simply empty - never invented.
    for edu_entry in data.get("education", []):
        rec.education.append(FieldValue(edu_entry, "resume", "section_heuristic", 0.4))
    for exp_entry in data.get("experience", []):
        rec.experience.append(FieldValue(exp_entry, "resume", "section_heuristic", 0.4))

    return rec


def build_record_from_notes(data: dict) -> CandidateRecord:
    """Recruiter free-text notes / .txt resume -> one CandidateRecord
    (source: 'recruiter_notes')."""
    rec = CandidateRecord()
    if data.get("extraction_failed"):
        return rec
    if data.get("email"):
        rec.emails.append(FieldValue(data["email"].strip().lower(), "recruiter_notes", "regex_extract", 0.5))
    if data.get("phone"):
        rec.phones.append(FieldValue(data["phone"].strip(), "recruiter_notes", "regex_extract", 0.5))
    for skill in data.get("skills", []):
        rec.skills.append(FieldValue(skill, "recruiter_notes", "keyword_match", 0.5))
    return rec


def detect_and_extract(input_dir: str) -> list:
    """
    Main entrypoint for stage 1. Walks input_dir, detects each file,
    extracts it, and returns a list of CandidateRecord - ONE PER SOURCE
    PER ROW (not merged yet; merging is stage 3's job).
    """
    if not os.path.isdir(input_dir):
        print(f"[warn] input dir does not exist: {input_dir}")
        return []

    records = []
    for fname in sorted(os.listdir(input_dir)):
        fpath = os.path.join(input_dir, fname)
        if not os.path.isfile(fpath):
            continue

        source_type = detect_source_type(fpath)

        try:
            if source_type == "recruiter_csv":
                rows = extract_from_csv(fpath)
                records.extend(build_records_from_csv_rows(rows))
            elif source_type == "ats_json":
                rows = extract_from_ats_json(fpath)
                records.extend(build_records_from_ats_rows(rows))
            elif source_type == "resume":
                data = extract_from_resume(fpath)
                records.append(build_record_from_resume(data))
            elif source_type == "recruiter_notes":
                data = extract_from_notes(fpath)
                records.append(build_record_from_notes(data))
            else:
                print(f"[warn] skipping unrecognized file: {fname}")
        except Exception as e:
            # Robustness constraint: one bad file must not kill the run.
            print(f"[warn] failed to process {fname}: {e}")
            continue

    return records
