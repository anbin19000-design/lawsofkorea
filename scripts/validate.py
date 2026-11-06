"""Validate local law review records without network access or dependencies."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "laws"
TEMPLATE = ROOT / "templates" / "law-family.example.json"
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
LANG_PATTERN = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(f"non-standard JSON constant: {value}")


def load_record(path):
    return json.loads(
        Path(path).read_text(encoding="utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )


def validate_record(record, *, allow_example=False):
    """Return all actionable validation errors; never change the input."""
    errors = []

    def require(condition, where, message):
        if not condition:
            errors.append(f"{where}: {message}")

    def obj(value, where, keys):
        if not isinstance(value, dict):
            errors.append(f"{where}: expected an object")
            return {}
        require(set(value) == set(keys), where, "fields must be " + ", ".join(keys))
        return value

    def string(value, where, *, nonempty=True):
        valid = isinstance(value, str) and (not nonempty or bool(value.strip()))
        require(valid, where, "expected a nonempty string" if nonempty else "expected a string")
        return value if isinstance(value, str) else ""

    record = obj(record, "root", (
        "schema_version", "family_id", "title_ko", "source_language",
        "target_language", "is_example", "catalog", "documents", "review",
    ))
    require(record.get("schema_version") == "1.0", "schema_version", "must be 1.0")
    family_id = string(record.get("family_id"), "family_id")
    require(bool(ID_PATTERN.fullmatch(family_id)), "family_id", "invalid identifier")
    string(record.get("title_ko"), "title_ko")
    require(record.get("source_language") == "ko", "source_language", "must be ko")
    language = string(record.get("target_language"), "target_language")
    require(bool(LANG_PATTERN.fullmatch(language)) and language != "ko", "target_language", "invalid target language")
    require(type(record.get("is_example")) is bool, "is_example", "must be boolean")
    example = record.get("is_example") is True
    require(not example or allow_example, "is_example", "examples cannot be stored as real law data")

    catalog = obj(record.get("catalog"), "catalog", ("confirmed", "expected_document_ids"))
    require(type(catalog.get("confirmed")) is bool, "catalog.confirmed", "must be boolean")
    expected = catalog.get("expected_document_ids")
    if not isinstance(expected, list) or not expected:
        errors.append("catalog.expected_document_ids: expected a nonempty list")
        expected = []
    expected_ids = set()
    for index, item in enumerate(expected):
        ident = string(item, f"catalog.expected_document_ids[{index}]")
        require(ident not in expected_ids, "catalog.expected_document_ids", f"duplicate ID {ident}")
        expected_ids.add(ident)

    review = obj(record.get("review"), "review", ("status", "checkpoint_receipt", "finish_receipt"))
    require(review.get("status") in ("in_progress", "complete"), "review.status", "invalid status")
    checkpoint = string(review.get("checkpoint_receipt"), "review.checkpoint_receipt", nonempty=False)
    finish = string(review.get("finish_receipt"), "review.finish_receipt", nonempty=False)
    documents = record.get("documents")
    if not isinstance(documents, list) or not documents:
        errors.append("documents: expected a nonempty list")
        documents = []
    document_ids = set()
    reviewed_count = 0
    unit_count = 0
    for di, document in enumerate(documents):
        where = f"documents[{di}]"
        document = obj(document, where, (
            "document_id", "kind", "title_ko", "source_url", "source_version", "units",
        ))
        ident = string(document.get("document_id"), f"{where}.document_id")
        require(ident not in document_ids, where, f"duplicate document ID {ident}")
        document_ids.add(ident)
        require(document.get("kind") in ("act", "decree", "rule", "annex"), where, "invalid document kind")
        string(document.get("title_ko"), f"{where}.title_ko")
        string(document.get("source_version"), f"{where}.source_version")
        source_url = string(document.get("source_url"), f"{where}.source_url")
        try:
            parsed = urlsplit(source_url)
            host = (parsed.hostname or "").lower()
            valid_url = (
                parsed.scheme == "https" and bool(host) and
                parsed.username is None and parsed.password is None and
                not any(char.isspace() for char in source_url)
            )
            require(valid_url, f"{where}.source_url", "expected an HTTPS URL without credentials or whitespace")
            if not example:
                reserved = ("example.com", "example.org", "example.net", "invalid", "test", "localhost")
                placeholder = any(host == suffix or host.endswith("." + suffix) for suffix in reserved)
                require(not placeholder, f"{where}.source_url", "replace the example source URL")
        except ValueError:
            errors.append(f"{where}.source_url: invalid URL")

        units = document.get("units")
        if not isinstance(units, list) or not units:
            errors.append(f"{where}.units: expected a nonempty list")
            units = []
        unit_ids = set()
        for ui, unit in enumerate(units):
            uw = f"{where}.units[{ui}]"
            unit = obj(unit, uw, (
                "unit_id", "source_ko", "translation", "status",
                "reviewed_source_sha256", "reviewed_by", "reviewed_at", "note",
            ))
            unit_count += 1
            uid = string(unit.get("unit_id"), f"{uw}.unit_id")
            require(uid not in unit_ids, uw, f"duplicate unit ID {uid}")
            unit_ids.add(uid)
            source = string(unit.get("source_ko"), f"{uw}.source_ko")
            translation = string(unit.get("translation"), f"{uw}.translation", nonempty=False)
            status = unit.get("status")
            require(status in ("pending", "reviewed", "blocked"), f"{uw}.status", "invalid status")
            digest = string(unit.get("reviewed_source_sha256"), f"{uw}.reviewed_source_sha256", nonempty=False)
            reviewer = string(unit.get("reviewed_by"), f"{uw}.reviewed_by", nonempty=False)
            timestamp = string(unit.get("reviewed_at"), f"{uw}.reviewed_at", nonempty=False)
            note = string(unit.get("note"), f"{uw}.note", nonempty=False)
            if status == "reviewed":
                reviewed_count += 1
                require(bool(translation.strip()), uw, "reviewed translation must not be empty")
                require(bool(reviewer.strip()), uw, "reviewed_by is required")
                require(digest == hashlib.sha256(source.encode("utf-8")).hexdigest(), uw, "reviewed source hash mismatch")
                try:
                    parsed_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    require(parsed_time.tzinfo is not None and parsed_time.utcoffset() is not None, uw, "reviewed_at needs a timezone")
                except ValueError:
                    errors.append(f"{uw}: reviewed_at must be an ISO 8601 timestamp")
            else:
                require(digest == reviewer == timestamp == "", uw, "unreviewed units must not carry review metadata")
            if status == "blocked":
                require(bool(note.strip()), uw, "blocked units require a note")

    require(document_ids <= expected_ids, "catalog", "documents are missing from expected_document_ids")
    if catalog.get("confirmed") is True:
        require(document_ids == expected_ids, "catalog", "confirmed catalog does not match document IDs")
    if reviewed_count:
        require(bool(checkpoint.strip()), "review", "reviewed units require a checkpoint receipt")
    if review.get("status") == "complete":
        require(catalog.get("confirmed") is True, "review", "completion requires a confirmed catalog")
        require(unit_count > 0 and reviewed_count == unit_count, "review", "completion requires every unit reviewed")
        require(bool(checkpoint.strip()) and bool(finish.strip()), "review", "completion requires checkpoint and finish receipts")
    else:
        require(not finish, "review", "finish receipt requires complete status")
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="JSON files; default: template and all data/laws records")
    args = parser.parse_args(argv)
    paths = args.paths or [TEMPLATE, *sorted(DATA.rglob("*.json"))]
    failures = 0
    real_records = 0
    seen = set()
    for raw_path in paths:
        path = raw_path.resolve()
        try:
            record = load_record(path)
            errors = validate_record(record, allow_example=path == TEMPLATE)
            if isinstance(record, dict) and path != TEMPLATE:
                ident = record.get("family_id")
                lang = record.get("target_language")
                if isinstance(ident, str) and isinstance(lang, str):
                    key = (ident, lang)
                    if key in seen:
                        errors.append("duplicate family_id and target_language")
                    seen.add(key)
                    if path.is_relative_to(DATA):
                        expected_path = DATA / ident / f"{lang}.json"
                        if path != expected_path:
                            errors.append("data path must be data/laws/<family_id>/<target_language>.json")
                real_records += 1
        except (OSError, UnicodeError, ValueError) as exc:
            errors = [str(exc)]
        if errors:
            failures += 1
            print(f"FAIL {raw_path}")
            for error in errors:
                print(f"  {error}")
        else:
            print(f"OK {raw_path}")
    print(f"Checked {len(paths)} file(s), {real_records} non-template record(s), {failures} failure(s).")
    if not real_records and not failures:
        print("No real law data registered yet; only the example template was validated.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
