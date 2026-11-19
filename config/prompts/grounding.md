# Grounding task

You are given:

1. A screenshot of one desktop application window ({{IMAGE_WIDTH}}x{{IMAGE_HEIGHT}} pixels).
2. A list of UI Automation (UIA) controls found in that window.
3. The business data fields this workflow may enter.

Your job is to identify the data-entry form, its text/number fields and its
buttons, and to map data fields to those controls.

Return ONE JSON object and nothing else. No markdown, no commentary.

## Coordinate system

Every bounding box uses normalized coordinates from 0 to 1000, relative to the
screenshot:

- `(0, 0)` is the top-left corner of the screenshot.
- `(1000, 1000)` is the bottom-right corner.
- `x1 < x2` and `y1 < y2`.

Box the input control itself (the area where text is typed), not the label
next to it.

## UIA candidates

{{CANDIDATE_COUNT}} control(s) were found. When one clearly corresponds to a
field or button, set `uia_candidate_id` to its ID. Otherwise set it to `null`.

{{CANDIDATES}}

## Business data fields

{{FIELDS}}

## Rules

- Only report fields that accept typed text or numbers. Ignore checkboxes,
  dropdowns, tabs, menus, and plain labels.
- `type` must be `text` or `number`.
- `field_id` values must be unique (`f1`, `f2`, ...).
- `button_id` values must be unique (`b1`, `b2`, ...).
- `role` must be `save`, `cancel`, `new`, or `other`.
- Map each data field to at most one control, and each control to at most one
  data field.
- Only map a field when its meaning is clear. List uncertain or irrelevant
  fields in `unmapped_keys`.
- List controls that no data field maps to in `unmapped_fields`.
- `layout_confidence` is your confidence (0 to 1) that the field geometry is
  correct.
- If this is not a data-entry form, set `form_detected` to `false` and leave
  the arrays empty.

## Response schema

```json
{
  "screen_description": "short description",
  "form_detected": true,
  "fields": [
    {
      "field_id": "f1",
      "label": "Invoice Number",
      "type": "text",
      "normalized_bbox": { "x1": 0, "y1": 0, "x2": 0, "y2": 0 },
      "uia_candidate_id": "W1",
      "current_value": "",
      "confidence": 0.0
    }
  ],
  "buttons": [
    {
      "button_id": "b1",
      "label": "Save Record",
      "role": "save",
      "normalized_bbox": { "x1": 0, "y1": 0, "x2": 0, "y2": 0 },
      "uia_candidate_id": "W7",
      "confidence": 0.0
    }
  ],
  "field_mapping": [
    {
      "key": "invoice_number",
      "field_id": "f1",
      "confidence": 0.0,
      "reason": "why"
    }
  ],
  "unmapped_keys": [],
  "unmapped_fields": [],
  "warnings": [],
  "layout_confidence": 0.0
}
```
