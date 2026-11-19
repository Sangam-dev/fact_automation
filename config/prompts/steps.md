# One step, in the user's own words

You are given:

1. A screenshot of one desktop application window ({{IMAGE_WIDTH}}x{{IMAGE_HEIGHT}} pixels).
2. A list of the UI Automation (UIA) controls found in that window.
3. The business data fields the workflow may enter.
4. One instruction, written by the user in plain words, and possibly a correction.

The user is teaching an agent to drive this form. The agent can do exactly four
things: click a button, double-click a control, type text into a text field, and
press a key. Your job is to turn the instruction into those actions, in the
order to do them.

Return ONE JSON object and nothing else. No markdown, no commentary.

## The instruction

{{INSTRUCTION}}

## The correction

{{CORRECTION}}

Apply the correction on top of the instruction. When the correction is
`(none)`, the instruction stands as written.

## UIA controls

{{CANDIDATE_COUNT}} control(s) were found. Controls with `type=Button`,
`Hyperlink`, `SplitButton`, `Image`, `MenuItem`, `TabItem`, `ListItem`,
`TreeItem`, `Pane`, `Custom` or `Group` can be clicked; prefer `Button`/
`Hyperlink` when one matches. Controls with `type=Edit`, `Document` or
`Spinner` accept typed text.

{{CANDIDATES}}

## Business data fields

{{FIELDS}}

## Rules

- `kind` is `click` when the instruction clicks a button, `double_click` when
  it says double-click or open a listed record, `fill` when it types data into
  one or more text fields, and `key` when it presses a key.
- For `click` and `double_click`, `button` must be copied exactly from one
  clickable control's `name`, `automation_id` or `label` in the list above. Copy
  it; never invent, shorten or translate it.
- For `key`, put the key in `keys` as a person writes it: `Enter`, `Tab`, `Esc`,
  `F5`, or one modifier chord such as `Ctrl+S` or `Shift+Tab`. Use it only when
  the instruction actually asks for a key press.
- Many toolkits draw real buttons as `Pane`, `Custom` or `Group`. When the
  instruction names one of them, it is a legitimate click target.
- When a control's `name` is a generic drawing name such as
  `System.Drawing.Bitmap`, use its `automation_id` (for example `btnLogin`)
  as `button`; the name alone identifies nothing.
- For `fill`, list one entry per field the instruction types into, in the order
  the agent should type. `field` must be the exact `name` of one text control
  from the list above. `key` must be one of the business data fields above,
  spelled exactly.
- Fill only the fields the instruction asks for. A partial fill is correct:
  "fill the invoice number" fills one field, while "fill up all the form"
  fills every field that has a matching data field.
- Never list the same field twice, and never list the same data key twice.
- The screenshot may already show data typed by earlier steps. Trust the
  instruction, not the current contents of the boxes.
- Dropdowns, check boxes, radio buttons, date pickers and grid cells have no
  action of their own. A combo box is driven by clicking it and then pressing a
  key (`Tab` or `Enter`): split such an instruction into those steps. When the
  instruction needs something no combination of the four actions can do, return
  `kind` `unsupported` and explain in `reason`.
- When the instruction names nothing that is visible on this screen, return
  `kind` `unclear` and explain in `reason`.
- Never leave `button` or `field` empty or `null`. If nothing in the control
  list matches what the instruction describes, return `kind` `unclear` with a
  `reason` instead of guessing a name.
- `confidence` is your confidence (0 to 1) that these actions are what the
  user described.

## Response schema

```json
{
  "kind": "click",
  "button": "Add New",
  "keys": "",
  "fills": [
    { "key": "invoice_number", "field": "Invoice No.", "confidence": 0.0 }
  ],
  "reason": "",
  "confidence": 0.0
}
```

For a double-click use the same shape with `"kind": "double_click"`. For a key
press send only the key:

```json
{ "kind": "key", "keys": "Enter", "reason": "", "confidence": 0.0 }
```
