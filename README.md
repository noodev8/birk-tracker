# birk-tracker

Parses a Birkenstock invoice PDF, writes the relevant fields out as JSON, and
applies them to the `birktracker` PostgreSQL table.

## Setup

Requires Python 3.10+ and these dependencies:

```
pip install pdfplumber psycopg2-binary python-dotenv
```

Set up your `.env` and fill in your database credentials:

```
DB_HOST=
DB_PORT=5432
DB_NAME=
DB_USER=
DB_PASSWORD=
```

## Usage

Drop the invoice PDF into this folder and run:

```
python birk-tracker.py            # parse PDF, write JSON, then update DB
python birk-tracker.py --dry-run  # do everything except COMMIT
python birk-tracker.py --no-db    # parse PDF only, skip the DB step
```

The script picks up the `*.pdf` it finds next to itself and writes the
result to `invoice.json` in the same folder. If more than one PDF is present
the script will stop and list them — remove the ones you don't want so only
the invoice to process remains, then re-run.

## Database behaviour

Each invoice line is matched against `birktracker` by padded `ordernum` (10
digits), `bksize`, and a `code` prefix matching the padded article code (7
digits). Matched rows have their `invoiced` quantity added to, and
`invoicedate` / `invoicenum` set to the current invoice.

Per-row flags prompt before continuing:

| Flag | Trigger | Options |
| --- | --- | --- |
| `MISSING` | no row found | `[a]`dd new row (auto-constructed code, editable) / `[i]`gnore / `[q]`uit |
| `AMBIGUOUS` | multiple rows found | pick `[1-N]` / `[i]`gnore / `[q]`uit |
| `ALREADY_INVOICED` | matched row already has this invoice number applied | `[a]`dd anyway / `[i]`gnore / `[q]`uit |

`[q]`uit is available at every prompt (including the final `Proceed?`) and
aborts the run immediately without committing anything to the database.

For `[a]`dd, the new `code` is built as `<padded_article>-<STYLE>-<EU_size>`
where `STYLE` is derived from any existing `birktracker` row for the same
article. Everything else (`requested`, `placedate`, `cost`, …) is left NULL
for new rows. All changes happen inside a single transaction with a final
confirm prompt before commit.

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
| `items[].order_number` | Order this article belongs to. Defaults to the header `order_number`; only overridden when an `Order <num> vom <date>` marker sits directly below the item's sizes. Each marker applies only to its own item — subsequent items fall back to the header order. |
| `items[].sizes` | Per-size breakdown with the quantity invoiced |
