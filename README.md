# birk-tracker

Parses a Birkenstock invoice PDF and writes the relevant fields out as JSON,
ready to be fed into a database.

## Setup

Requires Python 3.10+ and one dependency:

```
pip install pdfplumber
```

## Usage

Drop the invoice PDF into this folder and run:

```
python birk-tracker.py
```

The script picks up the first `*.pdf` it finds next to itself and writes the
result to `invoice.json` in the same folder.

## Output

```json
{
  "invoice_number": "example",
  "invoice_date": "example",
  "order_number": "example",
  "total_invoiced": example,
  "items": [
    {
      "article_code": "example",
      "total_quantity": example,
      "order_number": "example",
      "sizes": [
        { "size": "225/2.5", "quantity": 1 },
        { "size": "230/3.5", "quantity": 2 }
      ]
    }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `invoice_number` | Invoice number from the top of the PDF |
| `invoice_date` | Invoice date (dd.mm.yyyy) |
| `order_number` | Default order number from the header |
| `total_invoiced` | Total number of pairs on the invoice (`Sum of pos.`) |
| `items[].article_code` | Article (product) code |
| `items[].total_quantity` | Total pairs invoiced for that article |
| `items[].order_number` | Order this article belongs to (overrides the header when the invoice lists multiple orders) |
| `items[].sizes` | Per-size breakdown with the quantity invoiced |
