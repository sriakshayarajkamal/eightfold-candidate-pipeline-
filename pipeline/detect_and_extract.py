"""
Stage 1: detect_and_extract

Looks at each input file, figures out what kind of source it is
(by extension + light content sniffing), and extracts raw values into
a CandidateRecord.

Sources handled:
  Structured:   recruiter_csv, ats_json
  Unstructured: resume (.pdf/.docx/.txt), recruiter_notes (.txt),
                github_profile (fetched live via public REST API — no key needed)

Design choice: if a file can't be read or doesn't look like any known
source, we SKIP it and log a warning. We never crash the whole run
because one file is bad (this is the "Robust" constraint).

GitHub API: public endpoints only, unauthenticated (60 req/hr limit).
If the network is unavailable or the username doesn't exist, we log a
warning and continue — the rest of the pipeline is unaffected.
This satisfies the deterministic constraint: we do NOT use any LLM/
generative API. Same inputs -> same outputs; GitHub profile data is
a point-in-time read, not generated.
"""

import csv
import json
import re
import os
import urllib.request
import urllib.error

from .record import CandidateRecord, FieldValue

# ---------------------------------------------------------------------------
# Skill vocabulary — expanded to cover common real-world resumes
# ---------------------------------------------------------------------------
SKILL_VOCAB = [
    # Languages
    "python", "java", "javascript", "js", "typescript", "c++", "c#",
    "go", "golang", "rust", "kotlin", "swift", "scala", "matlab",
    "php", "ruby", "perl", "bash", "shell",
    # Web / Frontend
    "react", "angular", "vue", "html", "css", "sass", "webpack", "nextjs",
    "next.js", "tailwind",
    # Backend / Frameworks
    "node", "nodejs", "django", "flask", "fastapi", "spring", "spring boot",
    "express", "rails",
    # Data / ML
    "sql", "machine learning", "ml", "deep learning", "nlp",
    "data analysis", "pandas", "numpy", "scikit-learn", "tensorflow",
    "pytorch", "keras", "spark", "hadoop", "tableau", "power bi",
    # Databases
    "mongodb", "postgresql", "mysql", "sqlite", "redis", "elasticsearch",
    "cassandra", "dynamodb",
    # Cloud / DevOps
    "aws", "azure", "gcp", "docker", "kubernetes", "terraform", "ansible",
    "jenkins", "github actions", "ci/cd", "linux",
    # Tools
    "git", "rest api", "graphql", "grpc", "kafka", "rabbitmq",
]

SKILL_CANON = {
    "js": "javascript",
    "ml": "machine learning",
    "golang": "go",
    "nodejs": "node",
    "next.js": "nextjs",
    "scikit-learn": "scikit-learn",
}

# Section header aliases we look for in resume text. Matching is
# case-insensitive against a whole line that's short and looks like a
# header (we don't try to detect headers by font/boldness - we don't
# have layout info from plain extracted text, so this is line-pattern
# based: short line, mostly uppercase or title-case, no trailing period).
EDUCATION_HEADERS = ["education", "academic background", "qualification", "academics"]
EXPERIENCE_HEADERS = [
    "experience", "internship experience", "work experience",
    "professional experience", "employment history", "internships",
]
SUMMARY_HEADERS = ["summary", "objective", "profile", "about me", "career objective"]

# Headers that signal "the section we care about has ended"
SECTION_STOP_HEADERS = [
    "education", "experience", "internship experience", "work experience",
    "professional experience", "employment history", "skills", "skills summary",
    "projects", "certifications", "courses and certification", "achievements",
    "objective", "summary", "tools", "publications", "awards", "internships",
    "languages", "hobbies", "interests", "references",
]

# Known Indian cities + common location patterns for location extraction
INDIAN_CITIES = [
    "mumbai", "delhi", "bangalore", "bengaluru", "hyderabad", "chennai",
    "kolkata", "pune", "ahmedabad", "jaipur", "surat", "lucknow",
    "kanpur", "nagpur", "indore", "thane", "bhopal", "visakhapatnam",
    "pimpri", "patna", "vadodara", "ghaziabad", "ludhiana", "agra",
    "coimbatore", "madurai", "noida", "gurgaon", "gurugram", "kochi",
    "chandigarh", "mysore", "mysuru", "ranchi", "trichy", "tiruchirappalli",
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


def _extract_name_from_resume(text: str) -> str | None:
    """
    Heuristic: the candidate name is usually the first non-empty line of
    a resume that looks like a proper name (2-4 capitalized words, no
    digits, no email/phone/URL). We check the first 10 lines only.
    Returns None if we can't find a confident match — never guesses.
    """
    lines = text.strip().split("\n")
    email_re = re.compile(r"@|\d{5,}|http|www\.|linkedin|github", re.I)
    for line in lines[:10]:
        stripped = line.strip()
        if not stripped:
            continue
        if email_re.search(stripped):
            continue
        # Must be 2-4 words, each starting with a capital letter
        words = stripped.split()
        if 2 <= len(words) <= 4 and all(re.match(r"^[A-Z][a-zA-Z'\-\.]+$", w) for w in words):
            return stripped
    return None


def _extract_location_from_resume(text: str) -> dict | None:
    """
    Best-effort location extraction. Looks for:
    1. "City, State" or "City, Country" patterns in the first 15 lines
    2. Known Indian city names anywhere in the first 15 lines
    Returns {city, region, country} with None for fields not found.
    Never invents a value.
    """
    lines = text.strip().split("\n")[:15]
    TECH_SKIP = re.compile(
        r"\b(python|java|javascript|sql|react|node|aws|docker|html|css|git|"
        r"programming|development|developer|engineer|software|skills)\b", re.I
    )
    loc_re = re.compile(r"^([A-Za-z][A-Za-z\s]{1,30})[,\-]\s*([A-Za-z][A-Za-z\s]{1,30})$")
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if re.search(r"@|\d{5,}|http|linkedin|github", stripped, re.I):
            continue
        if TECH_SKIP.search(stripped):
            continue
        m = loc_re.match(stripped)
        if m:
            city = m.group(1).strip()
            region = m.group(2).strip()
            if len(region) == 2:
                country = region.upper()
                region = None
            else:
                country = "IN"
            return {"city": city, "region": region, "country": country}

    text_lower = " ".join(lines).lower()
    for city in INDIAN_CITIES:
        if re.search(r"\b" + re.escape(city) + r"\b", text_lower):
            return {"city": city.title(), "region": None, "country": "IN"}

    return None


def _extract_summary_from_resume(text: str) -> str | None:
    """Extract the summary/objective section as a single string."""
    lines = _extract_section(text, SUMMARY_HEADERS)
    if not lines:
        return None
    return " ".join(lines)[:500]   # cap at 500 chars


def _calc_years_experience(experience_entries: list) -> float | None:
    """
    Calculate total years of experience from parsed experience entries.
    Only counts entries where we found both start and end dates.
    Returns None if no datable entries exist (never invents a number).

    Date formats attempted (offline, no dateparser dependency):
      - YYYY-MM  e.g. "2020-06"
      - Mon YYYY e.g. "Jun 2020" / "June 2020"
      - YYYY      e.g. "2020"
      - "present" / "current" -> treated as 2026-06 (submission date)
    """
    MONTH_MAP = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10,
        "november": 11, "december": 12,
    }
    PRESENT_YEAR, PRESENT_MONTH = 2026, 6

    def parse_date(s: str):
        s = s.strip().lower()
        if s in ("present", "current", "now", "till date", "ongoing"):
            return (PRESENT_YEAR, PRESENT_MONTH)
        # YYYY-MM
        m = re.match(r"(\d{4})-(\d{2})", s)
        if m:
            return (int(m.group(1)), int(m.group(2)))
        # Mon YYYY
        m = re.match(r"([a-z]+)\s+(\d{4})", s)
        if m and m.group(1) in MONTH_MAP:
            return (int(m.group(2)), MONTH_MAP[m.group(1)])
        # YYYY only
        m = re.match(r"(\d{4})$", s)
        if m:
            return (int(m.group(1)), 1)
        return None

    total_months = 0
    counted = 0
    for entry in experience_entries:
        start = parse_date(entry.get("start") or "")
        end = parse_date(entry.get("end") or "")
        if start and end:
            months = (end[0] - start[0]) * 12 + (end[1] - start[1])
            if months > 0:
                total_months += months
                counted += 1

    if counted == 0:
        return None
    return round(total_months / 12, 1)


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
    Best-effort: groups experience section lines into entries.
    Captures company, title, start/end dates, and summary bullets.

    Date extraction: looks for patterns like:
      "Jun 2021 - Present", "2020-06 - 2022-08", "2019 - 2021"
    on the header line of each entry. Extracts company from patterns like
    "Title at Company" or "Company | Title" or "Title, Company".
    """
    DATE_PATTERN = re.compile(
        r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4}"
        r"|\d{4}-\d{2}|\d{4})"
        r"\s*[-–to]+\s*"
        r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4}"
        r"|\d{4}-\d{2}|\d{4}|present|current|now|ongoing|till date)",
        re.IGNORECASE,
    )

    entries = []
    current_header = None
    current_bullets = []
    current_start = None
    current_end = None
    current_company = None

    def flush():
        if current_header is None:
            return
        # Try "Title at Company" split
        company = current_company
        title = current_header
        at_match = re.match(r"^(.+?)\s+at\s+(.+)$", current_header, re.I)
        pipe_match = re.match(r"^(.+?)\s*[|–-]\s*(.+)$", current_header)
        comma_match = re.match(r"^(.+?),\s*(.+)$", current_header)
        if at_match:
            title, company = at_match.group(1).strip(), at_match.group(2).strip()
        elif pipe_match:
            company, title = pipe_match.group(1).strip(), pipe_match.group(2).strip()
        elif comma_match and company is None:
            company, title = comma_match.group(1).strip(), comma_match.group(2).strip()

        entries.append({
            "company": company,
            "title": title,
            "start": current_start,
            "end": current_end,
            "summary": " ".join(current_bullets) if current_bullets else None,
        })

    for line in lines:
        is_bullet = line.startswith(("-", "•", "*", "○"))
        if not is_bullet and len(line) < 120:
            if current_header is not None:
                flush()
                current_bullets = []
                current_start = None
                current_end = None
                current_company = None

            # Strip date range from header line
            dm = DATE_PATTERN.search(line)
            if dm:
                current_start = dm.group(1).strip()
                current_end = dm.group(2).strip()
                header_clean = (line[:dm.start()] + line[dm.end():]).strip(" -–|,")
            else:
                header_clean = line

            current_header = header_clean if header_clean else line
        else:
            current_bullets.append(line.lstrip("-•*○ ").strip())

    flush()
    return entries


def fetch_github_profile(username: str) -> dict:
    """
    Fetch a GitHub public profile via the unauthenticated REST API.
    No API key required. Rate-limited to 60 req/hr — sufficient for
    a candidate pipeline. Fully offline-graceful: any network error,
    404, or rate-limit returns an empty dict and logs a warning.

    Deterministic: same username -> same data (point-in-time read).
    We do NOT use any generative/LLM API here.

    Returns dict with keys: name, bio, location, email, blog,
    languages (list), repos (list of repo names), followers.
    """
    result = {}
    try:
        url = f"https://api.github.com/users/{username}"
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                    "User-Agent": "eightfold-pipeline/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            profile = json.loads(resp.read().decode())

        result["name"] = profile.get("name")
        result["bio"] = profile.get("bio")
        result["location"] = profile.get("location")
        result["email"] = profile.get("email")
        result["blog"] = profile.get("blog")
        result["followers"] = profile.get("followers", 0)
        result["github_url"] = profile.get("html_url")

        # Fetch top repos to infer languages (up to 10 repos)
        repos_url = f"https://api.github.com/users/{username}/repos?per_page=10&sort=updated"
        req2 = urllib.request.Request(repos_url, headers={"Accept": "application/vnd.github+json",
                                                           "User-Agent": "eightfold-pipeline/1.0"})
        with urllib.request.urlopen(req2, timeout=5) as resp2:
            repos_data = json.loads(resp2.read().decode())

        result["repos"] = [r["name"] for r in repos_data if isinstance(r, dict)]
        langs = {r.get("language") for r in repos_data if isinstance(r, dict) and r.get("language")}
        result["languages"] = sorted(langs)

    except urllib.error.HTTPError as e:
        print(f"[warn] GitHub API HTTP {e.code} for user '{username}': {e.reason}")
    except urllib.error.URLError as e:
        print(f"[warn] GitHub API unreachable for '{username}': {e.reason}")
    except Exception as e:
        print(f"[warn] GitHub fetch failed for '{username}': {e}")

    return result


def build_record_from_github(username: str, data: dict) -> CandidateRecord:
    """GitHub profile data -> CandidateRecord (source: 'github')."""
    rec = CandidateRecord()
    if not data:
        return rec

    if data.get("name"):
        rec.full_name = FieldValue(data["name"].strip(), "github", "api_field", 0.8)
    if data.get("email"):
        rec.emails.append(FieldValue(data["email"].strip().lower(), "github", "api_field", 0.8))
    if data.get("bio"):
        rec.headline = FieldValue(data["bio"].strip(), "github", "api_field", 0.8)

    # Parse location string from GitHub (free-text, best-effort)
    if data.get("location"):
        loc_str = data["location"].strip()
        parts = [p.strip() for p in re.split(r"[,/]", loc_str)]
        city = parts[0] if parts else None
        region = parts[1] if len(parts) > 1 else None
        country = parts[2] if len(parts) > 2 else None
        rec.location = FieldValue({"city": city, "region": region, "country": country},
                                   "github", "api_field", 0.7)

    # GitHub URL always goes in links
    if data.get("github_url"):
        rec.links["github"] = FieldValue(data["github_url"], "github", "api_field", 1.0)
    if data.get("blog"):
        blog = data["blog"].strip()
        if blog:
            rec.links["portfolio"] = FieldValue(blog, "github", "api_field", 0.9)

    # Infer skills from repo languages
    lang_to_skill = {
        "Python": "python", "Java": "java", "JavaScript": "javascript",
        "TypeScript": "typescript", "Go": "go", "Rust": "rust",
        "C++": "c++", "C#": "c#", "C": "c", "Ruby": "ruby",
        "Kotlin": "kotlin", "Swift": "swift", "Scala": "scala",
        "PHP": "php", "Shell": "bash", "HTML": "html", "CSS": "css",
        "R": "r", "MATLAB": "matlab",
    }
    for lang in data.get("languages", []):
        skill = lang_to_skill.get(lang)
        if skill:
            rec.skills.append(FieldValue(skill, "github", "repo_language", 0.75))

    return rec


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
    Resume (PDF/DOCX/.txt) -> structured extraction dict.
    Uses pdfplumber for PDFs (deterministic, offline), regex + heuristics
    for all field extraction. No LLM/generative API used.

    Extracts: name, email, phone, skills, education, experience,
              location, headline/summary, years_experience.
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

    if not text.strip():
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
    experience_entries = _parse_experience_lines(experience_lines)

    return {
        "raw_text": text,
        "name": _extract_name_from_resume(text),
        "email": email_match.group(0) if email_match else None,
        "phone": phone_match.group(0) if phone_match else None,
        "skills": sorted(found_skills),
        "education": _parse_education_lines(education_lines),
        "experience": experience_entries,
        "location": _extract_location_from_resume(text),
        "headline": _extract_summary_from_resume(text),
        "years_experience": _calc_years_experience(experience_entries),
        "extraction_failed": False,
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
    if data.get("extraction_failed"):
        return rec

    # Name from resume header — confidence 0.7 (heuristic, not a structured field)
    if data.get("name"):
        rec.full_name = FieldValue(data["name"].strip(), "resume", "header_heuristic", 0.7)
    if data.get("email"):
        rec.emails.append(FieldValue(data["email"].strip().lower(), "resume", "regex_extract", 0.75))
    if data.get("phone"):
        rec.phones.append(FieldValue(data["phone"].strip(), "resume", "regex_extract", 0.75))
    if data.get("headline"):
        rec.headline = FieldValue(data["headline"], "resume", "section_heuristic", 0.6)
    if data.get("location"):
        rec.location = FieldValue(data["location"], "resume", "regex_extract", 0.6)
    if data.get("years_experience") is not None:
        rec.years_experience = FieldValue(data["years_experience"], "resume", "date_calc", 0.7)

    for skill in data.get("skills", []):
        rec.skills.append(FieldValue(skill, "resume", "keyword_match", 0.6))
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

    Special file: github_users.txt — one GitHub username per line.
    Each username is fetched from the public GitHub API (no key needed).
    If the file is absent or the network is down, this source is simply
    skipped — the pipeline never crashes because of it.
    """
    if not os.path.isdir(input_dir):
        print(f"[warn] input dir does not exist: {input_dir}")
        return []

    records = []

    # --- GitHub source (optional file listing usernames) ---
    github_file = os.path.join(input_dir, "github_users.txt")
    if os.path.isfile(github_file):
        try:
            with open(github_file, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
            for line in lines:
                # support "username:email@hint.com" format
                if ":" in line:
                    username, email_hint = line.split(":", 1)
                    username = username.strip()
                    email_hint = email_hint.strip().lower()
                else:
                    username, email_hint = line.strip(), None

                print(f"[info] fetching GitHub profile: {username}")
                data = fetch_github_profile(username)
                rec = build_record_from_github(username, data)

                # If an email hint is provided, inject it so the merge
                # stage can match this record to the right candidate.
                # Confidence is 0.6 (user-supplied hint, not from API).
                if email_hint and not rec.emails:
                    rec.emails.append(
                        FieldValue(email_hint, "github", "user_hint", 0.6)
                    )
                records.append(rec)
        except Exception as e:
            print(f"[warn] failed to process github_users.txt: {e}")

    # --- File-based sources ---
    for fname in sorted(os.listdir(input_dir)):
        if fname == "github_users.txt":
            continue
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
            print(f"[warn] failed to process {fname}: {e}")
            continue

    return records
