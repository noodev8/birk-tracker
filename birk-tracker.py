"""Parse a Birkenstock invoice PDF into a structured JSON file.

Run from the project folder:
    python birk-tracker.py

The script picks up the first *.pdf it finds next to itself and writes the
result to invoice.json in the same folder. 
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pdfplumber


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

# Order change marker that applies to the NEXT item: "Order 1886093 vom 15.09.2025"
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
    current_order = header.get("order_number", "")

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
                "order_number": current_order,
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
            current_order = m.group("order")
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


def _find_pdf(folder: Path) -> Path:
    pdfs = sorted(folder.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"No PDF file found in {folder}")
    if len(pdfs) > 1:
        print(f"Multiple PDFs found, using: {pdfs[0].name}", file=sys.stderr)
    return pdfs[0]


def main() -> None:
    folder = Path(__file__).resolve().parent
    pdf_path = Path(sys.argv[1]) if len(sys.argv) >= 2 else _find_pdf(folder)
    out_path = Path(sys.argv[2]) if len(sys.argv) >= 3 else folder / "invoice.json"

    result = parse_invoice(str(pdf_path))
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Parsed {pdf_path.name} -> {out_path.name}")


if __name__ == "__main__":
    main()
