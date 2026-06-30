"""
CLI entrypoint.

Usage:
    python main.py --input sample_inputs --out profile_output.json
    python main.py --input sample_inputs --config config_example.json --out profile_output.json

This wires the 4 stages together:
  detect_and_extract -> normalize -> merge_with_confidence -> project_and_validate
"""

import argparse
import json
import sys

from pipeline.detect_and_extract import detect_and_extract
from pipeline.normalize import normalize_records
from pipeline.merge_with_confidence import merge_with_confidence
from pipeline.project_and_validate import project_and_validate, ValidationError


def run_pipeline(input_dir: str, config_path: str = None) -> list:
    config = None
    if config_path:
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            print(f"[warn] could not load config '{config_path}': {e}. "
                  f"Falling back to default schema.")
            config = None

    raw_records = detect_and_extract(input_dir)
    if not raw_records:
        print("[warn] no usable records extracted from any source.")

    normalized = normalize_records(raw_records)
    merged = merge_with_confidence(normalized)

    try:
        output = project_and_validate(merged, config)
    except ValidationError as e:
        print(f"[error] output failed validation: {e}")
        sys.exit(1)

    return output


def main():
    parser = argparse.ArgumentParser(description="Eightfold multi-source candidate transformer")
    parser.add_argument("--input", required=True, help="Path to folder of input source files")
    parser.add_argument("--config", required=False, help="Path to optional runtime output config JSON")
    parser.add_argument("--out", required=False, default="output.json", help="Path to write output JSON")
    args = parser.parse_args()

    output = run_pipeline(args.input, args.config)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote {len(output)} candidate record(s) to {args.out}")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
