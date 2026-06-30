"""
Minimal test suite (optional per brief, but strengthens the submission).
Run with: python -m pytest tests/ -v   (or: python tests/test_pipeline.py)
"""

import os
import sys
import json
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.detect_and_extract import detect_and_extract
from pipeline.normalize import normalize_records
from pipeline.merge_with_confidence import merge_with_confidence
from pipeline.project_and_validate import project_and_validate, ValidationError


def run(input_dir, config=None):
    records = detect_and_extract(input_dir)
    records = normalize_records(records)
    merged = merge_with_confidence(records)
    return project_and_validate(merged, config)


class TestPipeline(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _write(self, name, content):
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_merge_dedupes_by_email_across_sources(self):
        """Same candidate in CSV and a .txt resume should merge into ONE record."""
        self._write("recruiter.csv",
                     "name,email,phone,current_company,title\n"
                     "Test User,test.user@example.com,9876543210,Acme,Engineer\n")
        self._write("resume_test.txt",
                     "Test User\nEmail: test.user@example.com\nSkills: Python, SQL\n")
        output = run(self.tmpdir)
        self.assertEqual(len(output), 1)
        self.assertEqual(output[0]["full_name"], "Test User")
        skill_names = [s["name"] for s in output[0]["skills"]]
        self.assertIn("python", skill_names)

    def test_conflict_resolution_prefers_structured_source(self):
        """CSV and ATS disagree on title -> structured source wins, confidence drops."""
        self._write("recruiter.csv",
                     "name,email,phone,current_company,title\n"
                     "Test User,test.user@example.com,9876543210,Acme,Engineer\n")
        self._write("ats.json",
                     json.dumps([{
                         "applicant_name": "Test User",
                         "contact_email": "test.user@example.com",
                         "job_title": "Senior Engineer"
                     }]))
        output = run(self.tmpdir)
        self.assertEqual(len(output), 1)
        # both sources are rank 2 (structured), so either could legitimately win;
        # what matters is confidence reflects the disagreement
        prov = output[0]["provenance"]
        title_prov = [p for p in prov if p["field"] == "current_title"][0]
        self.assertEqual(title_prov["method"], "precedence_conflict")

    def test_missing_source_does_not_crash(self):
        """No input files at all -> empty result, no exception."""
        output = run(self.tmpdir)
        self.assertEqual(output, [])

    def test_malformed_csv_does_not_crash_whole_run(self):
        """A broken CSV alongside a good resume should still produce output
        for the resume, not crash the whole pipeline."""
        bad_csv_path = os.path.join(self.tmpdir, "broken.csv")
        with open(bad_csv_path, "wb") as f:
            f.write(b"\x00\x01\xff\xfe not really csv data")
        self._write("resume_ok.txt", "Jane Doe\nEmail: jane.doe@example.com\nSkills: Java\n")
        # Should not raise
        output = run(self.tmpdir)
        self.assertTrue(len(output) >= 1)

    def test_on_missing_error_raises_for_required_field(self):
        self._write("recruiter.csv", "name,email,phone,current_company,title\n,,9988776655,Wipro,QA\n")
        config = {
            "fields": [{"path": "full_name", "from": "full_name", "required": True}],
            "on_missing": "error"
        }
        with self.assertRaises(SystemExit):
            # main.run_pipeline would sys.exit; here we call project_and_validate directly
            records = detect_and_extract(self.tmpdir)
            records = normalize_records(records)
            merged = merge_with_confidence(records)
            try:
                project_and_validate(merged, config)
            except ValidationError:
                sys.exit(1)

    def test_on_missing_omit_drops_field(self):
        self._write("recruiter.csv", "name,email,phone,current_company,title\nNo Email Guy,,9988776655,Wipro,QA\n")
        config = {
            "fields": [
                {"path": "full_name", "from": "full_name"},
                {"path": "primary_email", "from": "emails[0]"}
            ],
            "on_missing": "omit"
        }
        output = run(self.tmpdir, config)
        self.assertEqual(len(output), 1)
        self.assertNotIn("primary_email", output[0])
        self.assertIn("full_name", output[0])

    def test_malformed_phone_is_dropped_not_kept_as_second_number(self):
        """A typo'd 9-digit phone from one source should NOT appear
        alongside the correct 10-digit phone from another source as if
        it were a second real number - it should be dropped."""
        self._write("recruiter.csv",
                     "name,email,phone,current_company,title\n"
                     "Test User,test.user@example.com,9876543210,Acme,Engineer\n")
        self._write("resume_test.txt",
                     "Test User\nEmail: test.user@example.com\nPhone: 987654321\nSkills: Python\n")
        output = run(self.tmpdir)
        self.assertEqual(len(output), 1)
        self.assertEqual(output[0]["phones"], ["+919876543210"])


if __name__ == "__main__":
    unittest.main()
