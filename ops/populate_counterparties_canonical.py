"""FR-CR-05-192 — Populate `counterparties` table from operator-curated
canonical list in `ops/data/counterparties_canonical.tsv`.

Background: the existing 16 rows in `counterparties` were grouped by
**Type** (Financial/VC, Government, ...) rather than by Company. The
canonical company directory thus contained 16 categories instead of
~200 actual counterparties — `summary_canonicalize` org-resolution
returned zero matches because the canonical names («CDIB Capital»,
«Affinity Partners», «Bosch», ...) simply weren't in the table.

This op:
  1. Reads the TSV (Type / Company / Contact Info columns; '#' lines
     are comments)
  2. INSERT-ON-CONFLICT-DO-NOTHING into `counterparties` keyed by
     `name_normalised` (so re-runs are idempotent)
  3. Adds / updates a `CounterpartyAttribute` row per company with
     `source='canonical_seed'` carrying the Type label (kept for
     analytics)
  4. Skips blank Company cells and pure-whitespace rows

After this runs, `summary_canonicalize.resolve_organizations_to_counterparties`
has a real directory of ~200 names to resolve org-mentions against.

Usage:
    docker exec manager-bot-1 python -m ops.populate_counterparties_canonical \\
        [--dry-run] [--tsv-path /custom/path.tsv]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from app.db import session_scope
from app.models.counterparty import Counterparty, CounterpartyAttribute


def _normalise_name(name: str) -> str:
    """Lowercase + collapse whitespace + strip — same shape used by
    FR-CR-05-132 to dedupe Counterparty rows.
    """
    return " ".join(name.lower().split())


def parse_tsv(path: Path) -> list[tuple[str, str]]:
    """Yield (Type, Company) tuples from the TSV. Skips comment lines
    (`# ...`), header line, and rows where Company is blank.
    """
    out: list[tuple[str, str]] = []
    with path.open("r", encoding="utf-8") as f:
        seen_header = False
        for raw in f:
            line = raw.rstrip("\n").rstrip("\r")
            if not line.strip():
                continue
            if line.lstrip().startswith("#"):
                continue
            cells = line.split("\t")
            if len(cells) < 2:
                continue
            type_ = cells[0].strip()
            company = cells[1].strip()
            if not seen_header and (
                type_.lower() == "type" and company.lower() == "company"
            ):
                seen_header = True
                continue
            if not company:
                continue
            out.append((type_, company))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--tsv-path",
        default=str(
            Path(__file__).parent / "data" / "counterparties_canonical.tsv"
        ),
        help="Path to the canonical TSV (default ops/data/...).",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tsv_path = Path(args.tsv_path)
    if not tsv_path.exists():
        print(f"ERROR: TSV not found at {tsv_path}", file=sys.stderr)
        return 2

    rows = parse_tsv(tsv_path)
    print(f"Parsed {len(rows)} (Type, Company) entries from {tsv_path}")

    inserted = 0
    updated_attrs = 0
    skipped_existing = 0

    with session_scope() as s:
        existing_by_norm: dict[str, Counterparty] = {
            cp.name_normalised: cp
            for cp in s.query(Counterparty).all()
        }
        for type_, company in rows:
            norm = _normalise_name(company)
            if not norm:
                continue
            cp = existing_by_norm.get(norm)
            if cp is None:
                cp = Counterparty(name=company, name_normalised=norm)
                if not args.dry_run:
                    s.add(cp)
                    try:
                        s.flush()
                    except IntegrityError:
                        s.rollback()
                        cp = (
                            s.query(Counterparty)
                            .filter(Counterparty.name_normalised == norm)
                            .first()
                        )
                        if cp is None:
                            continue
                existing_by_norm[norm] = cp
                inserted += 1
            else:
                skipped_existing += 1
            # Attach / refresh the `canonical_seed` satellite row with Type
            if not args.dry_run and cp.id is not None:
                attr = (
                    s.query(CounterpartyAttribute)
                    .filter(
                        CounterpartyAttribute.counterparty_id == cp.id,
                        CounterpartyAttribute.source == "canonical_seed",
                    )
                    .first()
                )
                payload = {"Type": type_, "Company": company}
                if attr is None:
                    s.add(CounterpartyAttribute(
                        counterparty_id=cp.id,
                        source="canonical_seed",
                        attributes=payload,
                    ))
                    updated_attrs += 1
                else:
                    if attr.attributes != payload:
                        attr.attributes = payload
                        updated_attrs += 1
        if not args.dry_run:
            s.commit()

    print()
    print(f"Inserted new Counterparty rows: {inserted}")
    print(f"Existed already (skipped insert): {skipped_existing}")
    print(f"CounterpartyAttribute (canonical_seed) rows touched: {updated_attrs}")
    if args.dry_run:
        print("--dry-run — nothing committed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
