# WORK-C English and semantic matching data

- `laws/en/<job_id>.json`: document English references and row-to-pair indexes.
- `documents/en/<en_law_id>.json`: shared full English law documents.
- `reviewed/en/KR_*.json`: reviewed Korean-English row references where available.
- `meaning/pairs/<prefix>.jsonl`: Korean-English pair records, semantic state, evidence, and H-language renderings.

Korean text remains authoritative. `FAMILY_LATEST_FALLBACK` is not an exact-version translation. Meaning records are matching evidence, not automatic approval.
