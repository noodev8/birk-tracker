"""Parse a Birkenstock invoice PDF and apply it to the birktracker DB.

Run from the project folder:
    python birk-tracker.py            # parse PDF, write JSON, then update DB
    python birk-tracker.py --dry-run  # do everything except COMMIT
    python birk-tracker.py --no-db    # parse PDF only, skip the DB step

The script picks up the first *.pdf it finds next to itself and writes the
result to invoice.json in the same folder. DB credentials are read from a
.env file alongside this script (see README).
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pdfplumber
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv


# --- Regex patterns ---------------------------------------------------------

# Header labels live in the right column of the invoice header. pdfplumber
# joins them with whatever sits to their left, so we anchor on the line ending.
RE_INVOICE_LABEL = re.compile(r"(^|\s)Number\s*$")
RE_ORDER_HEADER = re.compile(r"Order number/Date\s*$")
# "Date" alone at end of line — must be preceded by whitespace so we don't
# accidentally match "Order number/Date" or "Purchase order number/Date".
RE_DATE_HEADER = re.compile(r"\sDate\s*$")

# Item header: "5400 1015398 5 Barbados EVA Black 13 Pair"
RE_ITEM = re.compile(
    r"^\d{4,5}\s+(?P<article>\S+)\s+\d+\s+.+?\s+(?P<qty>\d+)\s+Pair\s*$"
)

# Size row: "230/3.5 64029939 2 Pair 16.67 GBP 33.34 GBPA2"
RE_SIZE = re.compile(
    r"^(?P<size>\S+)\s+\d+\s+(?P<qty>\d+)\s+Pair\s+"
    r"\d+\.\d+\s+GBP\s+\d+\.\d+\s+GBP\S*\s*$"
)

# Order change marker that sits below the sizes of the item it belongs to,
# e.g. "Order 1886093 vom 15.09.2025".
RE_ORDER_CHANGE = re.compile(r"^Order\s+(?P<order>\d+)\s+vom\s+\S+\s*$")

# Total number of items on the invoice: "Sum of pos. 30 Pair 804.20"
# Amount may use a thousands separator, e.g. "Sum of pos. 288 Pair 8,331.37".
RE_TOTAL = re.compile(
    r"^Sum of pos\.\s+(?P<total>\d+)\s+Pair\s+[\d,]+(?:\.\d+)?\s*$"
)


def _clean(line: str) -> str:
    """Strip the underline/strikethrough underscore artifacts pdfplumber emits."""
    return re.sub(r"_+", "", line).strip()


def _extract_header(lines: list[str]) -> dict[str, Any]:
    header: dict[str, Any] = {}
    for i, raw in enumerate(lines):
        line = raw.rstrip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if RE_INVOICE_LABEL.search(line) and "invoice_number" not in header:
            header["invoice_number"] = nxt
        elif RE_ORDER_HEADER.search(line) and "order_number" not in header:
            # e.g. "1927328 / 04.11.2025"
            header["order_number"] = nxt.split("/")[0].strip()
        elif RE_DATE_HEADER.search(line) and "invoice_date" not in header:
            # The "Date" header sits in the right column; the date value is a
            # couple of lines down because the address block is interleaved.
            # Grab the first dd.mm.yyyy token after it.
            for look in lines[i + 1 : i + 8]:
                m = re.search(r"\b(\d{2}\.\d{2}\.\d{4})\b", look)
                if m:
                    header["invoice_date"] = m.group(1)
                    break
    return header


def parse_invoice(pdf_path: str) -> dict[str, Any]:
    with pdfplumber.open(pdf_path) as pdf:
        raw_lines: list[str] = []
        for page in pdf.pages:
            raw_lines.extend((page.extract_text() or "").splitlines())

    header = _extract_header(raw_lines)
    header_order = header.get("order_number", "")

    items: list[dict[str, Any]] = []
    current_item: dict[str, Any] | None = None
    total_invoiced: int | None = None

    for raw in raw_lines:
        line = _clean(raw)
        if not line:
            continue

        m = RE_ITEM.match(line)
        if m:
            current_item = {
                "article_code": m.group("article"),
                "total_quantity": int(m.group("qty")),
                "order_number": header_order,
                "sizes": [],
            }
            items.append(current_item)
            continue

        if current_item is not None:
            m = RE_SIZE.match(line)
            if m:
                current_item["sizes"].append(
                    {
                        "size": m.group("size"),
                        "quantity": int(m.group("qty")),
                    }
                )
                continue

        m = RE_ORDER_CHANGE.match(line)
        if m:
            # Marker sits below the sizes of the item it belongs to and applies
            # ONLY to that item; subsequent items default back to header_order.
            if current_item is not None:
                current_item["order_number"] = m.group("order")
            continue

        m = RE_TOTAL.match(line)
        if m:
            total_invoiced = int(m.group("total"))
            continue

    return {
        "invoice_number": header.get("invoice_number", ""),
        "invoice_date": header.get("invoice_date", ""),
        "order_number": header.get("order_number", ""),
        "total_invoiced": total_invoiced,
        "items": items,
    }


# --- DB integration --------------------------------------------------------

# mm/UK size on the invoice -> EU size suffix in birktracker.code
EU_SIZE = {
    "225/2.5": "35",
    "230/3.5": "36",
    "240/4.5": "37",
    "245/5":   "38",
    "250/5.5": "39",
    "260/7":   "40",
    "265/7.5": "41",
    "270/8":   "42",
    "280/9":   "43",
    "285/9.5": "44",
    "290/10.5": "45",
    "300/11.5": "46",
}


@dataclass
class Plan:
    updates: list[dict[str, Any]] = field(default_factory=list)
    inserts: list[dict[str, Any]] = field(default_factory=list)
    ignored: int = 0


def _padded_article(code: str) -> str:
    return code.zfill(7)


def _padded_order(order: str) -> str:
    return order.zfill(10)


def _flatten(invoice: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand the nested invoice JSON into one row per (article, order, size)."""
    out: list[dict[str, Any]] = []
    for item in invoice["items"]:
        for sz in item["sizes"]:
            out.append({
                "article": item["article_code"],
                "order": item["order_number"],
                "size": sz["size"],
                "qty": sz["quantity"],
            })
    return out


def _find_rows(cur, line: dict[str, Any]) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT code, ordernum, bksize, requested, invoiced, invoicedate, invoicenum
          FROM birktracker
         WHERE ordernum = %s AND bksize = %s AND code LIKE %s
        """,
        (_padded_order(line["order"]), line["size"], f"{_padded_article(line['article'])}-%"),
    )
    return [dict(r) for r in cur.fetchall()]


def _derive_style(cur, padded_article: str) -> str | None:
    """Pick the style portion (e.g. ARIZONA) from any existing row for this article."""
    cur.execute(
        "SELECT code FROM birktracker WHERE code LIKE %s LIMIT 1",
        (f"{padded_article}-%",),
    )
    row = cur.fetchone()
    if not row:
        return None
    parts = row["code"].split("-")
    # Format is "<article>-<STYLE...>-<EU>"; style may itself contain hyphens.
    return "-".join(parts[1:-1]) if len(parts) >= 3 else None


def _prompt(msg: str) -> str:
    return input(msg).strip().lower()


def _prompt_missing(cur, line: dict[str, Any], invoice_num: str, invoice_date: str) -> dict | None:
    article = _padded_article(line["article"])
    style = _derive_style(cur, article)
    eu = EU_SIZE.get(line["size"], "??")
    suggested = f"{article}-{style}-{eu}" if style else ""

    print(f"\n[MISSING] article {article}, order {_padded_order(line['order'])}, "
          f"size {line['size']}, qty {line['qty']}")
    if suggested:
        print(f"  Suggested code: {suggested}")
    else:
        print("  No existing rows found for this article — cannot derive style.")

    while True:
        choice = _prompt("  [a]dd / [e]dit code / [i]gnore > ")
        if choice == "i":
            return None
        if choice in ("a", "e") or (choice == "" and suggested):
            code = suggested
            if choice == "e" or not code:
                code = input(f"  Enter code [{suggested}]: ").strip() or suggested
            if not code:
                print("  Code required.")
                continue
            return {
                "code": code,
                "ordernum": _padded_order(line["order"]),
                "size": line["size"],
                "qty": line["qty"],
                "invoice_date": invoice_date,
                "invoice_num": invoice_num,
            }


def _prompt_ambiguous(rows: list[dict], line: dict[str, Any]) -> dict | None:
    print(f"\n[AMBIGUOUS] article {_padded_article(line['article'])}, "
          f"order {_padded_order(line['order'])}, size {line['size']}, qty {line['qty']}")
    for i, r in enumerate(rows, 1):
        print(f"  [{i}] {r['code']}  invoiced={r['invoiced']}  "
              f"invoicenum={r['invoicenum']}  invoicedate={r['invoicedate']}")
    while True:
        choice = _prompt(f"  pick [1-{len(rows)}] / [i]gnore > ")
        if choice == "i":
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(rows):
            return rows[int(choice) - 1]


def _prompt_already_invoiced(row: dict, line: dict[str, Any]) -> bool:
    print(f"\n[ALREADY_INVOICED] code {row['code']}, ordernum {row['ordernum']}")
    print(f"  already has invoicenum={row['invoicenum']}, invoiced={row['invoiced']}")
    print(f"  this run would add qty {line['qty']} "
          f"(-> {(row['invoiced'] or 0) + line['qty']})")
    return _prompt("  [a]dd anyway / [i]gnore > ") == "a"


def _connect():
    load_dotenv(Path(__file__).resolve().parent / ".env")
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def build_plan(cur, invoice: dict[str, Any]) -> Plan:
    plan = Plan()
    invoice_num = invoice["invoice_number"]
    invoice_date = invoice["invoice_date"]

    for line in _flatten(invoice):
        rows = _find_rows(cur, line)

        if len(rows) == 0:
            new_row = _prompt_missing(cur, line, invoice_num, invoice_date)
            if new_row is None:
                plan.ignored += 1
            else:
                plan.inserts.append(new_row)
            continue

        if len(rows) > 1:
            row = _prompt_ambiguous(rows, line)
            if row is None:
                plan.ignored += 1
                continue
        else:
            row = rows[0]

        if row["invoicenum"] == invoice_num and (row["invoiced"] or 0) > 0:
            if not _prompt_already_invoiced(row, line):
                plan.ignored += 1
                continue

        plan.updates.append({
            "code": row["code"],
            "ordernum": row["ordernum"],
            "qty": line["qty"],
            "invoice_date": invoice_date,
            "invoice_num": invoice_num,
        })

    return plan


def apply_plan(conn, plan: Plan, dry_run: bool) -> None:
    cur = conn.cursor()
    for u in plan.updates:
        cur.execute(
            """
            UPDATE birktracker
               SET invoiced    = COALESCE(invoiced, 0) + %s,
                   invoicedate = %s,
                   invoicenum  = %s
             WHERE code = %s AND ordernum = %s
            """,
            (u["qty"], u["invoice_date"], u["invoice_num"], u["code"], u["ordernum"]),
        )
    for i in plan.inserts:
        cur.execute(
            """
            INSERT INTO birktracker (code, ordernum, bksize, invoiced, invoicedate, invoicenum)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (i["code"], i["ordernum"], i["size"], i["qty"], i["invoice_date"], i["invoice_num"]),
        )

    if dry_run:
        conn.rollback()
        print("Dry run — rolled back.")
    else:
        conn.commit()
        print("Committed.")


def update_db(invoice: dict[str, Any], dry_run: bool) -> None:
    conn = _connect()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        print(f"\nLoaded invoice {invoice['invoice_number']} ({invoice['invoice_date']}) — "
              f"{invoice['total_invoiced']} pairs across {len(invoice['items'])} articles")
        print("Connecting to DB … ok")

        plan = build_plan(cur, invoice)

        print(f"\nSummary:")
        print(f"  {len(plan.updates)} updates · {len(plan.inserts)} inserts · {plan.ignored} ignored")
        if not plan.updates and not plan.inserts:
            print("Nothing to do.")
            return
        if _prompt("Proceed? [y/N] > ") != "y":
            print("Aborted.")
            return

        apply_plan(conn, plan, dry_run)
    finally:
        conn.close()


def _find_pdf(folder: Path) -> Path:
    pdfs = sorted(folder.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"No PDF file found in {folder}")
    if len(pdfs) > 1:
        print(f"Multiple PDFs found, using: {pdfs[0].name}", file=sys.stderr)
    return pdfs[0]


def main() -> None:
    folder = Path(__file__).resolve().parent
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}

    pdf_path = Path(args[0]) if args else _find_pdf(folder)
    out_path = folder / "invoice.json"

    result = parse_invoice(str(pdf_path))
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Parsed {pdf_path.name} -> {out_path.name}")

    if "--no-db" in flags:
        return
    update_db(result, dry_run="--dry-run" in flags)


if __name__ == "__main__":
    main()
