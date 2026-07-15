"""JSON/CSV aggregation skill for Lyzr agents (zero third-party dependencies).

Public entry point: :func:`aggregate_data`, which loads tabular data from a file
path or inline text, performs a dynamic group-by aggregation described by the
caller, and returns the result as a structured JSON string.

Uses only the Python standard library (``json``, ``csv``, ``statistics``) so it runs
in any sandbox with no ``pip install`` step. Handles JSON (records, column-oriented,
nested, NDJSON, and multi-table "datasets" envelopes) and CSV/TSV. Excel is not
supported.

Register with a Lyzr agent::

    from lyzr import Studio
    from aggregate import aggregate_data

    studio = Studio()
    agent = studio.create_agent(...)
    agent.add_tool(aggregate_data)
"""

from __future__ import annotations

import json
import math
import os
import statistics
from io import StringIO


class AggregationError(Exception):
    """Raised for recoverable, user-facing errors. Surfaced as JSON, not a stack trace."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message


# Aggregation functions supported in metrics (alias -> canonical name).
_FUNC_ALIASES = {
    "sum": "sum",
    "mean": "mean",
    "avg": "mean",
    "average": "mean",
    "min": "min",
    "max": "max",
    "count": "count",
    "nunique": "nunique",
    "count_distinct": "nunique",
    "distinct": "nunique",
    "median": "median",
    "std": "std",
    "var": "var",
    "first": "first",
    "last": "last",
}

# Functions that operate on numbers; their target columns are coerced to numeric.
_NUMERIC_FUNCS = {"sum", "mean", "median", "std", "var"}

_TSV_EXTS = {".tsv"}


# --------------------------------------------------------------------------- #
# Minimal table container (replaces the pandas DataFrame)
# --------------------------------------------------------------------------- #
class Table:
    """A tiny column/row table: ``columns`` is a list of names, ``rows`` a list of lists."""

    __slots__ = ("columns", "rows")

    def __init__(self, columns, rows):
        self.columns = [str(column) for column in columns]
        self.rows = rows

    @property
    def shape(self):
        return (len(self.rows), len(self.columns))


# --------------------------------------------------------------------------- #
# Argument normalization (robust to the loose types an LLM may pass)
# --------------------------------------------------------------------------- #
def _normalize_str_list(value) -> list:
    """Accept a list/tuple, a JSON-array string, or a comma-separated string."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text[:1] == "[":
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            except json.JSONDecodeError:
                pass
        return [part.strip() for part in text.split(",") if part.strip()]
    raise AggregationError(
        "BadArgument",
        f"Expected a list or string but got {type(value).__name__}.",
    )


def _to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "desc", "descending"}
    return bool(value)


def _to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _to_number(value):
    """Coerce a single value to int/float, or None when it is not numeric."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return value
    text = str(value).strip()
    if text == "":
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        number = float(text)
    except ValueError:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


# --------------------------------------------------------------------------- #
# JSON input parsing
# --------------------------------------------------------------------------- #
# Keys that commonly wrap a list of records in a JSON response.
_JSON_RECORD_KEYS = ("data", "records", "rows", "items", "results", "result", "values")


def _parse_json_text(text: str):
    """Parse a JSON document, falling back to JSON Lines (NDJSON) when needed."""
    text = text.lstrip("\ufeff").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) > 1:
            try:
                return [json.loads(line) for line in lines]
            except json.JSONDecodeError:
                pass
        raise AggregationError("ParseError", f"Invalid JSON: {exc.msg}") from exc


def _is_dataset_like(obj) -> bool:
    """True for a dict describing one table via parallel ``headers`` and ``rows`` lists."""
    return (
        isinstance(obj, dict)
        and isinstance(obj.get("headers"), list)
        and isinstance(obj.get("rows"), list)
    )


def _all_dataset_like(value) -> bool:
    """True for a non-empty list whose every item is :func:`_is_dataset_like`."""
    return (
        isinstance(value, list)
        and len(value) > 0
        and all(_is_dataset_like(item) for item in value)
    )


def _find_datasets(obj) -> list:
    """Locate a list of ``headers``/``rows`` datasets anywhere in a parsed JSON value.

    Recognizes a single dataset object, a bare list of dataset objects, an explicit
    ``{"datasets": [...]}`` container, and datasets nested under wrapper keys such as
    ``{"response": {"result": {"datasets": [...]}}}`` (e.g. another agent's output).
    Returns an empty list for record-oriented JSON, which is handled elsewhere.
    """
    if _is_dataset_like(obj):
        return [obj]
    if _all_dataset_like(obj):
        return list(obj)
    if isinstance(obj, dict):
        if _all_dataset_like(obj.get("datasets")):
            return list(obj["datasets"])
        for value in obj.values():
            if isinstance(value, dict):
                found = _find_datasets(value)
                if found:
                    return found
            elif _all_dataset_like(value):
                return list(value)
    return []


def _select_dataset(datasets, dataset):
    """Pick one dataset by name or 0-based index; return ``(dataset, name)``.

    With a single dataset, selection is optional. With several, ``dataset`` must name one
    (case-insensitive) or give its index; otherwise an ``AmbiguousDataset`` error lists the
    available names so the caller can retry with a specific one.

    Use ``"all"`` or ``"*"`` to return every dataset as a list of ``(dataset, name)`` tuples.
    """
    names = [str(item.get("name") or index) for index, item in enumerate(datasets)]
    selection = (dataset or "").strip()

    if selection.lower() in {"all", "*"}:
        return [(ds, name) for ds, name in zip(datasets, names)]

    if not selection:
        if len(datasets) == 1:
            return datasets[0], names[0]
        raise AggregationError(
            "AmbiguousDataset",
            f"The input contains {len(datasets)} datasets ({names}). Select one with "
            "'dataset' (a dataset name or a 0-based index), or use 'all' to aggregate all datasets.",
        )

    for item, name in zip(datasets, names):
        if name.lower() == selection.lower():
            return item, name
    if selection.isdigit():
        index = int(selection)
        if 0 <= index < len(datasets):
            return datasets[index], names[index]
    raise AggregationError(
        "DatasetNotFound",
        f"Dataset '{selection}' not found. Available datasets: {names}.",
    )


def _flatten_record(record, prefix="") -> dict:
    """Flatten nested dicts into dotted keys, e.g. {"a": {"b": 1}} -> {"a.b": 1}."""
    items = {}
    for key, value in record.items():
        full_key = f"{prefix}{key}"
        if isinstance(value, dict):
            items.update(_flatten_record(value, full_key + "."))
        else:
            items[full_key] = value
    return items


def _table_from_records(records) -> Table:
    """Build a Table from a list of record dicts (flattening nested objects)."""
    flat = [_flatten_record(record) for record in records]
    columns = []
    seen = set()
    for record in flat:
        for key in record.keys():
            if key not in seen:
                seen.add(key)
                columns.append(key)
    rows = [[record.get(column) for column in columns] for record in flat]
    return Table(columns, rows)


def _table_from_dataset(dataset, name) -> Table:
    """Turn one ``{"headers": [...], "rows": [...]}`` dataset into a Table.

    ``rows`` is normally a list of value lists aligned to ``headers``; a list of row
    objects (dicts) is also tolerated and re-ordered to the declared header order.
    """
    headers = dataset.get("headers")
    rows = dataset.get("rows")
    if not isinstance(headers, list) or not isinstance(rows, list):
        raise AggregationError(
            "ParseError", f"Dataset '{name}' must provide list 'headers' and 'rows'."
        )
    columns = [str(header) for header in headers]

    if rows and all(isinstance(row, dict) for row in rows):
        table = _table_from_records(rows)
        ordered = [column for column in columns if column in table.columns]
        extra = [column for column in table.columns if column not in ordered]
        final_columns = ordered + extra
        index = {column: position for position, column in enumerate(table.columns)}
        new_rows = [[row[index[column]] for column in final_columns] for row in table.rows]
        return Table(final_columns, new_rows)

    for position, row in enumerate(rows):
        if not isinstance(row, (list, tuple)):
            raise AggregationError(
                "ParseError", f"Dataset '{name}' row {position} is not a list of values."
            )
        if len(row) != len(columns):
            raise AggregationError(
                "ParseError",
                f"Dataset '{name}' row {position} has {len(row)} values but there are "
                f"{len(columns)} headers.",
            )
    return Table(columns, [list(row) for row in rows])


def _table_from_json(obj):
    """Build a Table from a record-oriented parsed JSON value.

    Handles a list of record objects, a single record object, records wrapped under a
    key (e.g. {"data": [...]}), column-oriented data ({"col": [...], ...}), and nested
    objects (flattened into dotted column names).
    """
    if isinstance(obj, dict):
        record_list = None
        for key in _JSON_RECORD_KEYS:
            candidate = obj.get(key)
            if (
                isinstance(candidate, list)
                and candidate
                and all(isinstance(item, dict) for item in candidate)
            ):
                record_list = candidate
                break
        if record_list is None:
            list_keys = [key for key, value in obj.items() if isinstance(value, list)]
            if len(list_keys) == 1:
                candidate = obj[list_keys[0]]
                if candidate and all(isinstance(item, dict) for item in candidate):
                    record_list = candidate
        if record_list is not None:
            obj = record_list
        else:
            values = list(obj.values())
            if values and all(isinstance(value, list) for value in values):
                # Column-oriented: {"col": [...], ...} aligned by index.
                columns = list(obj.keys())
                length = max(len(value) for value in values)
                rows = [
                    [obj[column][i] if i < len(obj[column]) else None for column in columns]
                    for i in range(length)
                ]
                return Table(columns, rows)
            obj = [obj]  # a single record object

    if isinstance(obj, list):
        if obj and all(isinstance(item, dict) for item in obj):
            return _table_from_records(obj)
        return Table(["value"], [[item] for item in obj])

    return Table(["value"], [[obj]])


def _tables_from_json(obj, dataset=""):
    """Return one or more ``(Table, dataset_name)`` pairs from a parsed JSON value.

    A single value yields one pair (``dataset_name`` is the selected table's name for a
    "datasets" envelope, else None). ``dataset="all"``/``"*"`` yields a list of pairs.
    """
    datasets = _find_datasets(obj)
    if datasets:
        result = _select_dataset(datasets, dataset)
        if isinstance(result, list):
            return [(_table_from_dataset(ds, name), name) for ds, name in result]
        chosen, name = result
        return _table_from_dataset(chosen, name), name
    return _table_from_json(obj), None


# --------------------------------------------------------------------------- #
# CSV / TSV input parsing
# --------------------------------------------------------------------------- #
def _infer_column_types(columns, rows) -> None:
    """In place: convert wholly-numeric columns to numbers (like pandas dtype inference)."""
    for index in range(len(columns)):
        values = [row[index] for row in rows]
        has_value = False
        numeric = True
        for value in values:
            if value is None or value == "":
                continue
            has_value = True
            if _to_number(value) is None:
                numeric = False
                break
        if numeric and has_value:
            for row in rows:
                cell = row[index]
                row[index] = None if cell is None or cell == "" else _to_number(cell)


def _table_from_delimited(text: str, delimiter: str) -> Table:
    import csv

    reader = csv.reader(StringIO(text), delimiter=delimiter)
    all_rows = list(reader)
    if not all_rows:
        return Table([], [])
    headers = all_rows[0]
    width = len(headers)
    rows = []
    for raw in all_rows[1:]:
        row = [raw[i] if i < len(raw) else None for i in range(width)]
        row = [None if (cell is None or cell == "") else cell for cell in row]
        rows.append(row)
    _infer_column_types(headers, rows)
    return Table(headers, rows)


# --------------------------------------------------------------------------- #
# Input loading
# --------------------------------------------------------------------------- #
def _load_from_file(file_path: str, data_format: str, dataset: str):
    if not os.path.isfile(file_path):
        raise AggregationError("FileNotFound", f"File not found: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()
    resolved = data_format
    if resolved == "auto":
        if ext == ".json":
            resolved = "json"
        elif ext in _TSV_EXTS:
            resolved = "tsv"
        else:
            resolved = "csv"

    dataset_name = None
    try:
        if resolved == "json":
            with open(file_path, "r", encoding="utf-8-sig") as handle:
                result = _tables_from_json(_parse_json_text(handle.read()), dataset)
            if isinstance(result, list):
                return [
                    (table, {
                        "type": "file",
                        "location": file_path,
                        "format": "json",
                        "dataset": name,
                    })
                    for table, name in result
                ]
            table, dataset_name = result
        else:
            with open(file_path, "r", encoding="utf-8-sig") as handle:
                delimiter = "\t" if resolved == "tsv" else ","
                table = _table_from_delimited(handle.read(), delimiter)
    except AggregationError:
        raise
    except Exception as exc:  # noqa: BLE001 - report parse issues as JSON
        raise AggregationError("ParseError", f"Failed to read '{file_path}': {exc}") from exc

    source = {
        "type": "file",
        "location": file_path,
        "format": "csv" if resolved == "tsv" else resolved,
        "dataset": dataset_name,
    }
    return table, source


def _load_from_inline(data: str, data_format: str, dataset: str):
    text = data.lstrip("\ufeff").strip()
    resolved = data_format
    if resolved in {"auto", "excel"}:
        resolved = "json" if text[:1] in {"[", "{"} else "csv"

    dataset_name = None
    try:
        if resolved == "json":
            result = _tables_from_json(_parse_json_text(text), dataset)
            if isinstance(result, list):
                return [
                    (table, {
                        "type": "inline",
                        "location": None,
                        "format": "json",
                        "dataset": name,
                    })
                    for table, name in result
                ]
            table, dataset_name = result
        else:
            delimiter = "\t" if resolved == "tsv" else ","
            table = _table_from_delimited(text, delimiter)
    except AggregationError:
        raise
    except Exception as exc:  # noqa: BLE001 - report parse issues as JSON
        raise AggregationError(
            "ParseError", f"Failed to parse inline data as {resolved}: {exc}"
        ) from exc

    source = {
        "type": "inline",
        "location": None,
        "format": resolved,
        "dataset": dataset_name,
    }
    return table, source


def _load_table(file_path: str, data: str, data_format: str, dataset: str):
    file_path = (file_path or "").strip()
    data = data or ""
    data_format = (data_format or "auto").strip().lower()

    if file_path:
        return _load_from_file(file_path, data_format, dataset)
    if data.strip():
        return _load_from_inline(data, data_format, dataset)
    raise AggregationError(
        "NoInput", "No input provided. Set either 'file_path' or 'data'."
    )


# --------------------------------------------------------------------------- #
# Metric parsing and aggregation
# --------------------------------------------------------------------------- #
def _parse_metrics(metrics: list, columns, group_by=(), use_source_names=False) -> list:
    if not metrics:
        raise AggregationError(
            "NoMetrics", "Provide at least one metric, e.g. 'sum:amount' or 'count:*'."
        )

    columns = list(columns)
    specs = []
    # Reserve the group-by names so a metric alias never collides with a group column.
    used_aliases: set = set(group_by)

    for raw in metrics:
        parts = [part.strip() for part in str(raw).split(":")]
        func_key = parts[0].lower()
        column = parts[1] if len(parts) > 1 else "*"
        custom_alias = parts[2] if len(parts) > 2 and parts[2] else None

        if func_key not in _FUNC_ALIASES:
            raise AggregationError(
                "UnknownFunction",
                f"Unknown function '{parts[0]}' in metric '{raw}'. "
                f"Supported: {sorted(set(_FUNC_ALIASES))}.",
            )

        is_count_star = column in {"", "*"}
        if is_count_star:
            if func_key != "count":
                raise AggregationError(
                    "BadMetric",
                    f"Metric '{raw}' targets all rows ('*') but only 'count' is valid there.",
                )
            column = "*"
        elif column not in columns:
            raise AggregationError(
                "InvalidColumn",
                f"Column '{column}' in metric '{raw}' not found. Available columns: {columns}.",
            )

        if custom_alias:
            alias = custom_alias
        elif is_count_star:
            alias = "count"
        elif use_source_names and column not in used_aliases:
            # Emit the original column name as the header (canonical output), when free.
            alias = column
        else:
            alias = f"{column}_{func_key}"
        base_alias = alias
        suffix = 2
        while alias in used_aliases:
            alias = f"{base_alias}_{suffix}"
            suffix += 1
        used_aliases.add(alias)

        specs.append(
            {
                "raw": str(raw),
                "func_key": _FUNC_ALIASES[func_key],
                "column": column,
                "alias": alias,
                "is_count_star": is_count_star,
            }
        )
    return specs


def _aggregate_group(spec, rows, col_index):
    """Compute a single metric over a list of rows."""
    if spec["is_count_star"]:
        return len(rows)

    index = col_index[spec["column"]]
    non_null = [row[index] for row in rows if row[index] is not None]
    func = spec["func_key"]

    if func == "count":
        return len(non_null)
    if func == "nunique":
        return len(set(non_null))
    if func == "sum":
        # Null-preservation: an all-null group returns None, not 0.
        return sum(non_null) if non_null else None
    if func == "mean":
        return float(statistics.fmean(non_null)) if non_null else None
    if func == "median":
        return float(statistics.median(non_null)) if non_null else None
    if func == "std":
        return float(statistics.stdev(non_null)) if len(non_null) >= 2 else None
    if func == "var":
        return float(statistics.variance(non_null)) if len(non_null) >= 2 else None
    if func == "min":
        return min(non_null) if non_null else None
    if func == "max":
        return max(non_null) if non_null else None
    if func == "first":
        return non_null[0] if non_null else None
    if func == "last":
        return non_null[-1] if non_null else None
    raise AggregationError("UnknownFunction", f"Unsupported function '{func}'.")


def _sorted_group_keys(keys):
    """Sort group-key tuples ascending, placing None last, tolerant of mixed types."""
    try:
        return sorted(keys, key=lambda key: tuple((value is None, value) for value in key))
    except TypeError:
        return sorted(keys, key=lambda key: tuple((value is None, str(value)) for value in key))


def _sort_result(result: Table, sort_by: str, descending: bool) -> None:
    """Sort ``result.rows`` in place by ``sort_by`` (None values always last)."""
    index = result.columns.index(sort_by)
    with_value = [row for row in result.rows if row[index] is not None]
    without_value = [row for row in result.rows if row[index] is None]
    try:
        with_value.sort(key=lambda row: row[index], reverse=descending)
    except TypeError:
        with_value.sort(key=lambda row: str(row[index]), reverse=descending)
    result.rows = with_value + without_value


def _run_aggregation(table: Table, group_by, specs, sort_by, descending, limit) -> Table:
    columns = table.columns
    for column in group_by:
        if column not in columns:
            raise AggregationError(
                "InvalidColumn",
                f"Group-by column '{column}' not found. Available columns: {columns}.",
            )

    col_index = {column: position for position, column in enumerate(columns)}

    coerce_columns = {
        spec["column"]
        for spec in specs
        if spec["func_key"] in _NUMERIC_FUNCS and not spec["is_count_star"]
    }
    rows = table.rows
    if coerce_columns:
        rows = [list(row) for row in rows]
        for column in coerce_columns:
            position = col_index[column]
            for row in rows:
                row[position] = _to_number(row[position])

    out_columns = list(group_by) + [spec["alias"] for spec in specs]

    if group_by:
        key_positions = [col_index[column] for column in group_by]
        groups: dict = {}
        for row in rows:
            key = tuple(row[position] for position in key_positions)
            groups.setdefault(key, []).append(row)
        out_rows = []
        for key in _sorted_group_keys(groups.keys()):
            group_rows = groups[key]
            out_rows.append(
                list(key) + [_aggregate_group(spec, group_rows, col_index) for spec in specs]
            )
    else:
        out_rows = [[_aggregate_group(spec, rows, col_index) for spec in specs]]

    result = Table(out_columns, out_rows)

    if sort_by:
        if sort_by not in out_columns:
            raise AggregationError(
                "InvalidSort",
                f"sort_by '{sort_by}' is not an output column. "
                f"Available: {out_columns}.",
            )
        _sort_result(result, sort_by, descending)

    if limit and limit > 0:
        result.rows = result.rows[:limit]

    return result


# --------------------------------------------------------------------------- #
# JSON-safe output building
# --------------------------------------------------------------------------- #
def _json_safe(value):
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _round_value(value, digits):
    """Round a float to ``digits`` decimals; pass other values through unchanged."""
    if digits is not None and digits >= 0 and isinstance(value, float):
        return round(value, digits)
    return value


def _build_output(
    table, source, group_by, metrics, result, output_format="records", round_digits=None
) -> dict:
    group_set = set(group_by)

    def cell(column, value):
        safe = _json_safe(value)
        # Round metric values only; never touch the group-by key columns.
        if column not in group_set:
            safe = _round_value(safe, round_digits)
        return safe

    records = [
        {column: cell(column, value) for column, value in zip(result.columns, row)}
        for row in result.rows
    ]

    if output_format == "envelope":
        headers = list(result.columns)
        rows = [
            [cell(column, value) for column, value in zip(result.columns, row)]
            for row in result.rows
        ]
        return {
            "name": source.get("dataset") or "aggregation_result",
            "headers": headers,
            "rows": rows,
            "rowCount": len(rows),
        }

    return {
        "status": "success",
        "source": source,
        "rows_read": int(len(table.rows)),
        "columns": [str(column) for column in table.columns],
        "group_by": list(group_by),
        "metrics": list(metrics),
        "record_count": len(records),
        "records": records,
    }


# --------------------------------------------------------------------------- #
# Public skill
# --------------------------------------------------------------------------- #
def aggregate_data(
    group_by: list,
    metrics: list,
    file_path: str = "",
    data: str = "",
    data_format: str = "auto",
    dataset: str = "",
    sort_by: str = "",
    descending: bool = False,
    limit: int = 0,
    output_format: str = "records",
    use_source_names: bool = False,
    round_digits: int = -1,
) -> str:
    """Aggregate rows from a JSON/CSV file or inline data and return grouped results as JSON.

    Use this to compute grouped summaries (totals, averages, counts, distinct counts,
    etc.) over tabular data. Provide the data EITHER via ``file_path`` OR via inline
    ``data``. Uses only the Python standard library (no pandas/numpy) — no install needed.

    Args:
        group_by: Column names to group by, e.g. ["region", "product"]. Pass an empty
            list [] to aggregate over all rows and return a single grand-total record.
        metrics: Aggregations to compute, each written as "function:column". Examples:
            "sum:amount", "avg:price", "min:qty", "max:qty", "nunique:customer_id".
            Use "count:*" (or "count") for the number of rows in each group. Optionally
            add a custom output name as a third part, e.g. "sum:amount:total_sales".
            Supported functions: sum, mean/avg, min, max, count, nunique/distinct,
            median, std, var, first, last.
        file_path: Path to a .csv, .tsv, or .json file. Leave empty when passing inline
            ``data``.
        data: Inline data as CSV text or JSON. Leave empty when using ``file_path``.
            If both are given, ``file_path`` is used.
        data_format: One of "auto", "csv", "tsv", "json". "auto" detects from the file
            extension or inline content. Default "auto".
        dataset: When the input JSON is a multi-table "datasets" envelope (each table
            given as ``headers`` + ``rows``, e.g. another agent's output), selects which
            dataset to aggregate, by name or 0-based index. Empty auto-selects when there
            is only one dataset; with several, the error lists the available names.
            Use ``"all"`` or ``"*"`` to aggregate all datasets in a single call.
            Ignored for CSV and record-oriented JSON.
        sort_by: Output column to sort by (a group column or a metric output name such
            as "amount_sum"). Empty keeps the natural group order.
        descending: Sort in descending order when True. Default False (ascending).
        limit: Keep only the first N result rows after sorting. 0 (default) keeps all.
        output_format: Output format for results. "records" (default) returns detailed
            results with records as objects. "envelope" returns a compact headers+rows
            format suitable for downstream pipeline chaining.
        use_source_names: When True, each metric's output column is named after its
            source column (e.g. "MedPaid" instead of "MedPaid_sum"), producing canonical
            headers that match the input schema. Falls back to "column_func" if the same
            source column is used by more than one metric (to keep names unambiguous).
            Explicit ":alias" overrides and "count:*" are unaffected. Default False.
        round_digits: When >= 0, round every floating-point metric value to this many
            decimals (e.g. 2 for currency). Group-by key columns and integers are left
            unchanged. -1 (default) disables rounding.
    Returns:
        A JSON string. On success:
        {"status": "success", "source": {...}, "rows_read": int, "columns": [...],
         "group_by": [...], "metrics": [...], "record_count": int, "records": [ {...} ]}.
        When dataset="all" or dataset="*", returns results for all datasets:
        {"status": "success", "datasets": [ {...result for each dataset...} ]}.
        When output_format="envelope", returns compact headers+rows format:
        {"datasets": [{"name": "...", "headers": [...], "rows": [[...]], "rowCount": N}]}.
        On failure:
        {"status": "error", "error_type": str, "message": str}.
    """
    try:
        group_by = _normalize_str_list(group_by)
        metrics = _normalize_str_list(metrics)
        descending = _to_bool(descending)
        limit = _to_int(limit)
        use_source_names = _to_bool(use_source_names)
        round_digits = _to_int(round_digits)
        round_digits = round_digits if round_digits >= 0 else None

        loaded = _load_table(file_path, data, data_format, dataset)

        # Multi-dataset case (dataset="all" or "*").
        if isinstance(loaded, list):
            dataset_results = []
            for table, source in loaded:
                if table.shape[1] == 0:
                    continue  # Skip empty datasets.
                specs = _parse_metrics(metrics, table.columns, group_by, use_source_names)
                result = _run_aggregation(table, group_by, specs, sort_by, descending, limit)
                dataset_results.append(
                    _build_output(
                        table, source, group_by, metrics, result, output_format, round_digits
                    )
                )
            if output_format == "envelope":
                return json.dumps({"datasets": dataset_results}, ensure_ascii=False)
            return json.dumps(
                {"status": "success", "datasets": dataset_results}, ensure_ascii=False
            )

        # Single dataset case.
        table, source = loaded
        if table.shape[1] == 0:
            raise AggregationError("EmptyData", "The input contains no columns.")

        specs = _parse_metrics(metrics, table.columns, group_by, use_source_names)
        result = _run_aggregation(table, group_by, specs, sort_by, descending, limit)
        output = _build_output(
            table, source, group_by, metrics, result, output_format, round_digits
        )

        if output_format == "envelope":
            return json.dumps({"datasets": [output]}, ensure_ascii=False)
        return json.dumps(output, ensure_ascii=False)
    except AggregationError as err:
        return json.dumps(
            {"status": "error", "error_type": err.error_type, "message": err.message},
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001 - never leak a stack trace to the agent
        return json.dumps(
            {"status": "error", "error_type": "UnexpectedError", "message": str(exc)},
            ensure_ascii=False,
        )


def _cli(argv=None) -> int:
    """Command-line entry point. Prints the aggregation result as JSON.

    Exit code 0 on success, 1 when the result has status "error".
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="aggregate.py",
        description="Aggregate a JSON/CSV file or inline data and print JSON to stdout.",
    )
    parser.add_argument(
        "--group-by",
        default="",
        help='Comma-separated columns to group by, e.g. "region,product". '
        "Omit for a single grand-total record.",
    )
    parser.add_argument(
        "--metrics",
        default="",
        help='Comma-separated "func:column" items, e.g. "sum:amount,count:*,avg:price". '
        'Use "count:*" for the row count.',
    )
    parser.add_argument("--file-path", default="", help="Path to a .csv/.tsv/.json file.")
    parser.add_argument("--data", default="", help="Inline CSV text or JSON.")
    parser.add_argument(
        "--data-format",
        default="auto",
        choices=["auto", "csv", "tsv", "json"],
        help="Input format. Default: auto-detect.",
    )
    parser.add_argument(
        "--dataset",
        default="",
        help="For a multi-table JSON 'datasets' envelope, the dataset name or 0-based index "
        "to aggregate. Use 'all' or '*' to aggregate all datasets in one call.",
    )
    parser.add_argument("--sort-by", default="", help="Output column to sort by.")
    parser.add_argument("--descending", action="store_true", help="Sort in descending order.")
    parser.add_argument("--limit", type=int, default=0, help="Keep only the first N result rows.")
    parser.add_argument(
        "--output-format",
        default="records",
        choices=["records", "envelope"],
        help='Output format: "records" (default) for detailed output, '
        '"envelope" for compact headers+rows format for pipeline chaining.',
    )
    parser.add_argument(
        "--source-names",
        action="store_true",
        help="Name each metric column after its source column (e.g. 'MedPaid' instead of "
        "'MedPaid_sum') for canonical headers that match the input schema.",
    )
    parser.add_argument(
        "--round",
        type=int,
        default=-1,
        dest="round_digits",
        help="Round floating-point metric values to this many decimals (e.g. 2). "
        "-1 (default) disables rounding.",
    )
    args = parser.parse_args(argv)

    result = aggregate_data(
        group_by=args.group_by,
        metrics=args.metrics,
        file_path=args.file_path,
        data=args.data,
        data_format=args.data_format,
        dataset=args.dataset,
        sort_by=args.sort_by,
        descending=args.descending,
        limit=args.limit,
        output_format=args.output_format,
        use_source_names=args.source_names,
        round_digits=args.round_digits,
    )
    print(result)
    parsed = json.loads(result)
    if "datasets" in parsed:
        return 0  # envelope / multi-dataset success
    return 0 if parsed.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
