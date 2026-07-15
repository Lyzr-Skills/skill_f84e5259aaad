---
name: aggregate-json-only
description: >-
  Aggregate tabular data from a JSON or CSV/TSV file (or inline CSV/JSON) by grouping on
  one or more columns and computing metrics such as sum, average, count, distinct count,
  min, max, median, and standard deviation. Returns the result as structured JSON. Pure
  Python standard library — no dependencies, no install. Use this whenever a user needs
  grouped summaries, subtotals, pivot-style rollups, or overall totals of JSON/CSV data.
license: MIT
---

# Aggregate JSON / CSV (zero dependencies)

Group rows of tabular data and compute aggregate metrics, returning structured JSON.
The logic lives in `aggregate.py` and can be run as a command-line program or imported
as a Python function (`aggregate_data`). It uses **only the Python standard library**
(`json`, `csv`, `statistics`) — there is nothing to `pip install`.

## Running the skill (read this first)

`aggregate.py` is **pre-installed in the sandbox image** at a fixed path:

```
/opt/skills/aggregate-json-only/aggregate.py
```

Invoke it directly. **Do not** call `get_skill_content`, **do not** search the
filesystem (`find`, `ls /skills`, etc.), **do not** run any `pip install`, and **do not**
recreate, rewrite, or re-emit `aggregate.py` — it already exists and needs no
dependencies. Just run it:

```bash
python /opt/skills/aggregate-json-only/aggregate.py --file-path data.json \
    --dataset all --group-by "IncurredMonth,PlanID" \
    --metrics "sum:MedPaid,sum:RxPaid" --output-format envelope
```

For a multi-table *datasets* envelope, prefer a **single call** with `--dataset all`
and `--output-format envelope` — this rolls up every dataset at once and returns the
same `{"datasets": [{name, headers, rows, rowCount}]}` shape used downstream, so no
manual reshaping or a second cleanup pass is needed.

## When to use this skill

Use it when the user asks for grouped summaries of JSON or CSV data, for example:

- "Total sales amount and order count per region."
- "Average price by product category."
- "How many distinct customers per store?"
- "Give me the grand total of the revenue column."
- "Roll up every table in this agent's JSON output by month and plan."

## Requirements

Python 3.8+ only. **No third-party packages** — no pandas, numpy, or openpyxl. The
`requirements.txt` file is intentionally empty (standard library only), so there is no
install step in the sandbox.

## How to run

Command line (invoke the pre-installed file at its fixed path):

```bash
# Roll up every table in a multi-dataset JSON envelope in ONE call (preferred)
python /opt/skills/aggregate-json-only/aggregate.py --file-path agent_output.json \
    --dataset all --group-by "IncurredMonth,PlanID" \
    --metrics "sum:MedPaid,sum:RxPaid,sum:Members" --output-format envelope

# Group by one column, compute two metrics, sort and take the top rows
python /opt/skills/aggregate-json-only/aggregate.py --file-path sales.csv --group-by region \
    --metrics "sum:amount,count:*" --sort-by amount_sum --descending --limit 5

# Multiple group-by columns
python /opt/skills/aggregate-json-only/aggregate.py --file-path sales.csv \
    --group-by "region,product" --metrics "avg:amount,max:quantity"

# Grand total over all rows (omit --group-by)
python /opt/skills/aggregate-json-only/aggregate.py --file-path sales.csv \
    --metrics "sum:amount,nunique:customer_id"

# Inline data instead of a file
python /opt/skills/aggregate-json-only/aggregate.py --data "cat,val
A,1
A,3
B,10" --group-by cat --metrics "sum:val,avg:val"

# Pick one table from a multi-table JSON "datasets" envelope (e.g. another agent's output)
python /opt/skills/aggregate-json-only/aggregate.py --file-path agent_output.json \
    --dataset medical_claims --group-by "PlanID,IncurredMonth" \
    --metrics "sum:MedPaid,sum:RxPaid"
```

Python import:

```python
from aggregate import aggregate_data

json_result = aggregate_data(
    group_by=["region"],
    metrics=["sum:amount", "count:*"],
    file_path="sales.csv",
)
```

## Parameters

| Parameter     | Type            | Required | Description |
|---------------|-----------------|----------|-------------|
| `group_by`    | list / CSV text | No       | Columns to group by (e.g. `region,product`). Empty ⇒ one grand-total record. |
| `metrics`     | list / CSV text | Yes      | Aggregations as `func:column` items (optional `:alias`). See functions below. Use `count:*` for the row count. |
| `file_path`   | string          | One of file_path / data | Path to a `.csv`, `.tsv`, or `.json` file. |
| `data`        | string          | One of file_path / data | Inline CSV text or JSON (see JSON input shapes below). |
| `data_format` | string          | No       | `auto` (default), `csv`, `tsv`, or `json`. |
| `dataset`     | string          | No       | For a multi-table JSON *datasets* envelope, which table to aggregate — a dataset name or 0-based index. Empty auto-selects when there is only one dataset. Use `all` (or `*`) to roll up **every** dataset in one call. |
| `sort_by`     | string          | No       | Output column to sort by (a group column or a metric alias such as `amount_sum`). |
| `descending`  | boolean         | No       | Sort descending when true. Default false. |
| `limit`       | integer         | No       | Keep only the first N result rows. `0` (default) keeps all. |
| `output_format` | string        | No       | `records` (default) for detailed per-row objects, or `envelope` for a compact `{name, headers, rows, rowCount}` shape suited to downstream pipeline chaining. |

**Supported metric functions:** `sum`, `mean`/`avg`, `min`, `max`, `count`,
`nunique`/`distinct`, `median`, `std`, `var`, `first`, `last`.

**Null handling for `sum`:** a group whose values are **all** null/missing sums to
`null` (not `0`). Groups with at least one number sum normally. This preserves the
distinction between "no data" and a real zero.

**Metric alias rule:** each metric is named `{column}_{func}` (e.g. `amount_sum`), or
`count` for `count:*`. Override with a third part, e.g. `sum:amount:total_sales`.

## JSON input shapes

JSON is accepted both as a `.json` file (`file_path`) and inline (`data`). The following
shapes are all recognized automatically:

- **Array of records:** `[{"region": "North", "amount": 100}, ...]`
- **Single record object:** `{"region": "North", "amount": 100}`
- **Records wrapped under a key:** `{"data": [ ... ]}` (also `records`, `rows`, `items`,
  `results`, `values`).
- **Column-oriented:** `{"region": ["North", "South"], "amount": [100, 200]}`
- **Nested objects:** flattened into dotted columns, e.g. `{"order": {"region": "North"}}`
  becomes the column `order.region` (group by `order.region`).
- **JSON Lines (NDJSON):** one JSON object per line.
- **"Datasets" envelope (headers + rows):** one or more tables, each given as a `headers`
  list plus `rows` (a list of value lists), optionally nested under wrapper keys — e.g.
  `{"response": {"result": {"datasets": [{"name": "claims", "headers": [...], "rows": [[...], ...]}]}}}`.
  This is the shape emitted by some upstream agents. With more than one dataset, pass
  `dataset` (a name or 0-based index) to choose which to aggregate, or `all`/`*` to
  aggregate every dataset in one call; the selected name is echoed back under
  `source.dataset`.

## Output

On success (JSON):

```json
{
  "status": "success",
  "source": { "type": "file", "location": "sales.csv", "format": "csv", "dataset": null },
  "rows_read": 8,
  "columns": ["region", "product", "amount", "quantity", "customer_id"],
  "group_by": ["region"],
  "metrics": ["sum:amount", "count:*"],
  "record_count": 3,
  "records": [
    { "region": "North", "amount_sum": 420.5, "count": 3 },
    { "region": "South", "amount_sum": 511.5, "count": 3 },
    { "region": "West",  "amount_sum": 500.0, "count": 2 }
  ]
}
```

With `--dataset all` (records format), results for every table are returned under a
`datasets` array:

```json
{ "status": "success", "datasets": [ { "status": "success", "source": {...}, "records": [ ... ] } ] }
```

### Envelope output (`--output-format envelope`)

With `--output-format envelope`, results come back as a compact list of tables instead
of verbose records — ideal for chaining into another step. Every run (single dataset or
`--dataset all`) is wrapped in a `datasets` array:

```json
{
  "datasets": [
    {
      "name": "medical_claims",
      "headers": ["IncurredMonth", "PlanID", "MedPaid_sum", "RxPaid_sum"],
      "rows": [
        ["2026-01-01", "1", 216209.75, 21395.27],
        ["2026-01-01", "2", 782565.27, 377350.32]
      ],
      "rowCount": 2
    }
  ]
}
```

On failure (JSON, never a stack trace):

```json
{ "status": "error", "error_type": "InvalidColumn", "message": "Column 'foo' not found. Available columns: [...]." }
```

Error types include: `NoInput`, `FileNotFound`, `ParseError`, `EmptyData`, `NoMetrics`,
`UnknownFunction`, `InvalidColumn`, `BadMetric`, `InvalidSort`, `AmbiguousDataset`,
`DatasetNotFound`, and `UnexpectedError`.

## Files

- `aggregate.py` — the skill (CLI + `aggregate_data` function). Pure standard library,
  pre-installed in the sandbox image at `/opt/skills/aggregate-json-only/aggregate.py`.
- `requirements.txt` — intentionally empty (no third-party dependencies).
- `sample_data.json` / `sample_data.csv` — small datasets for trying the examples above.
