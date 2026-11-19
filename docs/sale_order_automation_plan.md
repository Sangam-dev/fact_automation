# Sale Order form automation — build plan

Target: fill the FACT ERP.NG **Accounts Receivable → Sales → Entry → Sale Order**
form from a JSON payload, then prove every value landed before Save.

Captured evidence: `scripts/uia/first_part.json` and `scripts/uia/second_part.json`
(same window, hwnd `199608`, `FACTERPNG.exe`, auto-id `40200`, 265/266 nodes).
Everything below is derived from those two dumps, not from assumptions.

## 1. What the dumps establish

### Window and actions

| Thing | UIA target |
| --- | --- |
| Window | `automation_id=40200`, class `WindowsForms10.Window.8.app...`, title contains `Sale Order` |
| Edit tab | `tabpageEdit` → `LayoutControlEdit` |
| Save & New | `hlblSave_New` (Button, Invoke) |
| Save & Close | `hlblSave_Close` |
| Copy | `hlblCopy` |
| Cancel & Exit | `hlblCancel_Ext` |
| Status text | `txtMemoStatus` (captured value `DRAFT`) |
| Product grid | `gcProduct` (Table) |
| Sub-tabs | `btnTERMS`, `btnREMARKS`, `btnADDRESS`, `btnBANKDETL`, `btnExportDoc` (open dialogs) |

### Header fields → automation ids

Writable: `deOa_date` (Date), `pePro_no` (Proforma No), `deProf_dt` (Proforma Date),
`peCustOrd_no` (Customer Order No), `deCustOrd_dt` (Customer Order Date),
`deEDD` (Expected Delivery Date), `peCurr_desc` (Currency), `peCustcode` (Customer),
`peAgentcode` (Salesman), `peClass` (Document Class), `peAccountID` (Account ID),
`cboGST` (VAT Inclusive).

Application-derived (read-only in the capture, used as proof the lookup took):
`TxtCurrdescript`, `txtCurr_rate` (exchange rate), `txtCustname`, `txtAgentname`,
`txtClassDesc`, `peOwner`. Read-only identifiers: `peDocScheme`, `TxtBody`
(`AUTOMATIC` order number), `txtMemoStatus`.

Disabled **until a dependency is filled**: `deProf_dt` (needs `pePro_no`),
`deCustOrd_dt` (needs `peCustOrd_no`), `cboGST` (needs currency), all derived
text fields. So field order is not cosmetic — the filler must follow the form's
dependency chain and re-check `enabled` before typing.

### Product grid

Cells have **no automation ids**. They are `DataItem`s named
`"<column> row <index>"`, with index zero-based while the visible `Sl#` starts at
1 (`row_index_base: 1`). Header labels keep trailing spaces (`"Quantity "`,
`"Rate "`, `"Amount "`) and cell names therefore contain a double space — match
with whitespace normalised, never by pixel or by index.

Both captures expose all 17 header labels but **different cell sets**:
`Quantity`/`UOM` cells exist only in `first_part`, `Export Documentation`,
`Additional Description`, `Expected Delivery Date` only in `second_part`. The grid
is horizontally virtualized, so a column must be scrolled into view before its
cells exist in the tree.

## 2. Built in this step

```
config/forms/sale_order.source.json   curated: business key -> automation id, grid columns, actions
execution/forms/form_map.py           compiler + pydantic models (FormMap, FieldSpec, GridSpec, ...)
scripts/build_form_map.py             CLI: build / --check
config/forms/sale_order.json          compiled map: per-capture path, inner edit, enabled, value, rect
tests/unit/test_form_map.py           10 tests against the real dumps
```

The compiled map is the contract every later step reads. A stale or wrong entry
fails at build time:

```bash
myvenv/Scripts/python.exe scripts/build_form_map.py           # rebuild
myvenv/Scripts/python.exe scripts/build_form_map.py --check   # CI / pre-run guard
```

Compiling currently reports 11 warnings that are real behaviour, not defects:
three dependency-disabled fields and five virtualized grid columns.

### Test filler (built ahead of the plan, to prove the mechanics)

```
config/forms/test_payload.json    header + 2 lines, mapped to form-map keys
execution/forms/test_filler.py    --dry-run / fill, stops before Save
```

It types only the fields we own: header scheme, date, currency, customer,
document class, salesman, proforma no, customer order no, EDD; rows product
code, description, quantity, rate, document class. Everything else is tabbed
past or read back.

### Editing the plan (add/remove a step in one line)

`execution/forms/sale_order_plan.py` is the only file to change for business
behaviour; `test_filler.py` is a generic runner that never names a field.

```python
header=(
    Field("document_scheme"),
    Field("order_date"),
    ...
    Field("customer_code", proof="customer_name"),   # delete this line to stop
)                                                    # filling customers
columns=(
    LineColumn("product_code", "Product Code"),
    LineColumn("product_description", "Product Description"),
    LineColumn("quantity", "Quantity"),
    LineColumn("rate", "Rate"),
    LineColumn("document_class", "Document Class"),
)
readback=("Product Description", "Quantity", "Rate", "Amount"),
```

- add/remove a header field -> one `Field(...)` line
- reorder the form -> move the line (the list is the fill order)
- add a row column -> one `LineColumn(payload_key, grid_column_name)` line
- add a read-back check -> one grid column name in `readback`
- change what proves a lookup -> the `proof=` argument

`tests/unit/test_sale_order_plan.py` fails if an edited plan names a field or
column that does not exist in the compiled form map, or breaks grid tab order.

Two live-only facts learned while testing it (both now handled):

- A **minimized form exposes no UIA children at all** — every id lookup fails
  until `ShowWindow(hwnd, SW_RESTORE)` runs, so attach must restore first.
- **Grid cell text is not in UIA.** `DataItem` wrappers expose the cell name via
  UIA and the cell value only through MSAA `legacy_properties()["Value"]`
  (e.g. `'Rate  row 0' -> '100.0000'`, `'Amount  row 0' -> '1,00,010.00'`).
  Read-back must use that path; UIA value patterns return the name instead.

Header typed values land on the inner `Edit` of a wrapper, so read-back also
falls back from `get_value()` to legacy properties to `window_text()`.

Grid mechanics confirmed live:

- Tab order is `Product Code → Product Description → Quantity → Pre-Close
  Quantity → Rate → Difference → Amount → Document Class → Export
  Documentation → Additional Description → Expected Delivery Date`; UOM is not
  focusable. Tabbing past the end opens the next row (no Enter).
- The grid does not move forward until **both** Product Code and Product
  Description are present; with an empty description Tab bounces back to
  Product Code.
- The grid scrolls itself horizontally while tabbing, so cells have to be
  addressed by the focused cell's name, and read-back scrolls left first.

Message boxes are not top-level windows: FACT draws `frmMessageBox` **inside**
the form tree, with the text in `lblDetail` and Pane buttons `btnYes2` / `btnNo2`.
Policy: never answer Yes. A "Product Code … Does Not Exist / Add New?" prompt is
declined and that field/line is skipped; anything unrecognised stops the run.

## 3. Remaining steps

### Step 2 — Input contract (`config/schemas` + pydantic)

Define the payload the automation receives, validated before any window is touched:

```json
{
  "form": "sale_order",
  "key": "SO-2026-000123",
  "header": {
    "order_date": "2083/03/32",
    "customer_code": "CUST001",
    "currency_code": "NPR",
    "document_class": "SALE",
    "salesman_code": "AG001",
    "account_id": "",
    "proforma_no": "",
    "customer_order_no": "CO-001",
    "expected_delivery_date": "2083/04/07",
    "vat_inclusive": "[ Not Inclusive ]"
  },
  "lines": [
    {
      "product_code": "PRD001",
      "quantity": 10,
      "uom": "PCS",
      "rate": 100,
      "document_class": "SALE",
      "export_documentation": "N",
      "additional_description": "",
      "expected_delivery_date": "2083/04/10"
    }
  ],
  "expect": { "customer_name_contains": "Pvt", "min_amount": 1000 }
}
```

Rules: reject unknown keys and missing required fields (`order_date`,
`customer_code`, `currency_code`, `document_class`), normalise numbers and dates,
keep `key` as the idempotency key, and never carry row numbers.

**Acceptance:** a bad payload fails with a field-level message and no desktop
interaction.

### Step 3 — Live target resolution

Compile-time paths are evidence, not runtime handles. Add `execution/forms/resolver.py`:

1. attach to the window by `title_contains` + `process`;
2. fingerprint the screen (window title, presence of `tabpageEdit`, `gcProduct`,
   `hlblSave_New`, node count within ±20%) and refuse a wrong/opened-elsewhere screen;
3. locate each mapped target by automation id, verify unique, visible, and inside
   the window rect; fall back to the compiled relative path only when the id is gone;
4. return a typed handle (`field`, `grid`, `action`) that raises `TargetMissing`
   with the screenshot crop and the id.

**Acceptance:** on the live window every field in the map resolves; a renamed id
produces one clear error, not a wrong-field fill.

### Step 4 — Widget write primitives

One function per kind, each returning what it typed and what it read back:

| kind | behaviour |
| --- | --- |
| `text`/`number` | focus, `Ctrl+A`, type, commit with Tab, verify value |
| `lookup` | clear, type code, Tab, wait for the description field to become non-empty, retry once, then fail |
| `date` | type BS date, Tab, verify normalised equality (dates are Nepali, not Gregorian) |
| `combo` | open with `Alt+Down`, match item text, Enter, verify |
| grid cell | click the cell rect, type, Tab, verify neighbouring derived cell updated |

Never click the small `Open` buttons unless the keyboard path fails: they open
modal pickers that the map does not model yet.

**Acceptance:** each primitive can round-trip a value on the live form in isolation.

### Step 5 — Dependency-ordered header fill

Order: currency → customer → document class → salesman → account id → dates →
reference numbers → VAT. Before each write, re-inspect `enabled`; skip empty
optional values; after the whole pass, assert the derived fields are populated
(`txtCustname`, `TxtCurrdescript`, `txtCurr_rate`, `txtClassDesc`).

**Acceptance:** filling a payload leaves every derived field non-empty and the
status still `DRAFT`.

### Step 6 — Line-item engine

For each line: ensure a blank row (`row_index = lines_so_far - 1`), scroll the
grid horizontally until the target column's cell appears, fill writable columns
in order, commit with Tab, and read the derived cells back (`Product Description`,
`Difference`, `Amount`, `Document Class Description`). Verify
`|amount − quantity × rate| ≤ 0.01`. Handle the `Export Documentation` cell as a
combo, not text.

**Acceptance:** two lines with different products land on rows 1 and 2 with
correct amounts and no duplicated row.

### Step 7 — Whole-form read-back verification

Walk the map and produce a report per field/line:
`OK | MISSING | MISMATCH | DERIVED_EMPTY | DISABLED`. Normalise whitespace,
case, and numeric formatting before comparing. Block Save when any required
field or derived expectation fails; attach a screenshot of the failing control.

**Acceptance:** verification passes on a correct entry and names the exact field
when one value is deliberately corrupted.

### Step 8 — Save and post-save evidence

`hlblSave_New` or `hlblSave_Close` per request. Detect the validation dialog and
read its text before deciding success. Evidence: status change from `DRAFT`,
generated order number in `TxtBody`, screenshot of the saved form. Persist the
save intent first so a crash between click and evidence can never save twice.

**Acceptance:** one Save, one record, evidence stored under `data/`, and a rerun
with the same `key` refuses to save again.

### Step 9 — Orchestrator, CLI, and drift

`python main.py form fill --form sale_order --payload data/inbox/so.json` runs
steps 2–8 and writes an artifact folder per document (payload, report,
screenshots, log). Batch mode reads a directory; failures isolate per document.
Weekly/CI: recapture the window, run `build_form_map.py --check`, and fail on
drift so a FACT upgrade cannot silently change a control id.

## 4. Hard constraints to design around

1. **Numeric automation ids are unstable** (e.g. `265604`, `265544`). Only named
   ids go in the map; the wrapper id is the handle, the inner Edit is the target.
2. **Duplicate ids exist** (`NepDateEdit` ×4, plus unnamed duplicate Edits). The
   map requires unique ids and rejects ambiguity at build time.
3. **Derived fields report `editable=True` but `enabled=False`.** Never write
   them; read them as evidence instead.
4. **Grid cells are virtualized and unnamed.** Address them by normalised
   `"<column> row <index>"` plus scroll position, never by coordinates in the map.
5. **Dates are Bikram Sambat** (`2083/03/32` is a valid BS date, invalid
   Gregorian). Conversion/normalisation belongs in step 2, not in the filler.
6. **The same window can host other documents.** `title_contains: "Sale Order"`
   and the fingerprint check are mandatory before typing.
