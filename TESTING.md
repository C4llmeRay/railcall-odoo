# Testing `ray9/odoo`

How to verify this module on your own machine, from bundle integrity through
one governed write and back out again.

Every command below is copy-pasteable. Outputs are marked **captured** (run on
a real install, pasted verbatim) or **expected shape** (depends on your Odoo
data, so the values will differ).

Run the whole thing against a **throwaway Odoo database**, not production.
Odoo Online gives you a free trial database; `https://demo.odoo.com` also
works. Several of these tests post to a ledger, and posting is not reversible.

---

## 0. What you need

| Thing | How to get it |
| ----- | ------------- |
| RailCall Station | `curl -fsSL https://railcall.ai/install.sh \| bash` |
| An Odoo 14+ database | Odoo Online trial, Odoo.sh, or self-hosted |
| An Odoo API key | Avatar → My Profile → Account Security → New API Key |
| A license for this module | Purchase from the marketplace listing |

Full credential walkthrough: [`module/docs/SETUP.md`](module/docs/SETUP.md).

---

## 1. Bundle integrity

The signature is the first thing to check, because everything after it is only
worth as much as the bundle it ran from. This is a v2 (tree) signature — it
covers the canonical manifest **and** a manifest of every file in the bundle,
so editing `handler.py` after signing invalidates it.

Install the module, then start Station and read the module line at boot:

```bash
PYTHONIOENCODING=utf-8 python ~/.railcall/station/workbench/studio_server.py
```

**Captured** — a bundle whose tree hash verifies, on a machine with no license:

```
[modules] loaded=1 rejected=1
  ✓ sami666/google-sheets v0.2.0 · google.sheets_append_row, …
  ✗ ray9-odoo: license: no license installed for ray9/odoo
```

That is the **success** case for this test. Reaching the license gate means the
signature already passed — Station checks the tree hash first and stops there
on a mismatch. With a license installed the line becomes `✓ ray9/odoo v1.2.0`.

To see the failure mode deliberately, delete one byte from `handler.py` and
restart. **Captured:**

```
  ✗ ray9-odoo: invalid v2 signature (tree hash mismatch — files edited after
    sign, or station older than v0.36)
```

`.moduleignore` is part of the signed tree. If you assemble the bundle by hand,
copy it too, or the hash will not match.

Same check over HTTP:

```bash
curl -s http://127.0.0.1:8799/api/modules/list \
     -H "Origin: http://127.0.0.1:8799" | python -m json.tool
```

---

## 2. Save the credential

Studio → **Integrations** → **Odoo**. Four fields, all required:

| Field | Example |
| ----- | ------- |
| `url` | `https://acme.odoo.com` — no trailing slash, no `/jsonrpc` |
| `db` | `acme` |
| `username` | `ops@acme.com` |
| `api_key` | the key from step 0 — **not** your password |

Stored in the local vault at mode 0600. It never leaves your machine; the
module talks to your Odoo directly.

---

## 3. One read-only command, end to end

`odoo.find_partner` is `read_only`, so it runs without an approval gate. This
is the fastest proof that all four credential fields are right.

Studio → **Commands** → `odoo.find_partner`:

```json
{ "email": "admin@example.com", "limit": 5 }
```

**Expected shape** on a hit:

```json
{
  "found": true,
  "partner_id": 3,
  "count": 1,
  "searched_on": "email",
  "partners": [
    { "id": 3, "name": "Mitchell Admin", "email": "admin@example.com" }
  ]
}
```

A miss is `{"found": false, "partner_id": 0, "count": 0, "partners": []}` — an
answer, not an error. Only a broken credential or an unreachable Odoo raises.

Read commands still produce a signed receipt. Check it:

```bash
railcall receipts list
railcall verify
```

### If it fails

| Message | Cause |
| ------- | ----- |
| `Odoo rejected the credential for db '…' / user '…'` | wrong `db`, or the key belongs to a different user |
| `Odoo credential missing: api_key` | a vault field is blank |
| `Odoo url must start with https://` | no scheme on `url` |
| `no Odoo credential saved` | no `odoo` vault entry at all |
| `Odoo res.partner.search_read failed — …` | Odoo's own message, usually access rights |

That fourth one is worth understanding: **Odoo answers `HTTP 200` even when the
call failed**, putting the error in the response body. Every call in this
module inspects the body and raises with Odoo's own text. Without that, a
failed write would collect a signed receipt for something that never happened.

---

## 4. One governed write, end to end

`odoo.create_partner` is `write_requires_approval`. This is the airlock cycle
the module exists for: **preview → approve → execute**.

Studio → **Commands** → `odoo.create_partner`:

```json
{
  "name": "Airlock Test Customer",
  "email": "airlock-test@example.invalid",
  "is_company": true
}
```

**Step 1 — preview.** Station renders a card showing the exact model, the exact
field values, and a payload hash. Nothing has reached Odoo yet. The payload
hash is what your approval will be bound to.

**Step 2 — approve.** Click approve. The approval is bound to *that* payload
hash, not to the command name — edit any input afterwards and the approval no
longer matches.

**Step 3 — execute.** Now the write happens. **Expected shape:**

```json
{
  "partner_id": 47,
  "record_url": "https://acme.odoo.com/web#id=47&model=res.partner&view_type=form"
}
```

Open `record_url`. The record should exist with exactly the values from the
preview card.

### Prove the gate is real

Call `/api/commands/execute` without approving first:

```bash
curl -s -X POST http://127.0.0.1:8799/api/commands/execute \
  -H "Content-Type: application/json" \
  -H "Origin: http://127.0.0.1:8799" \
  -d '{"command_id":"odoo.create_partner",
       "inputs":{"name":"Should Never Exist"},
       "intent":"testing the gate"}'
```

It is refused, and the refusal is itself receipted. Nothing reaches Odoo.

The four lifecycle endpoints all take `{command_id, inputs, intent}`:

```
POST /api/commands/validate    semantic firewall
POST /api/commands/preview     airlock card + pending_approval receipt
POST /api/commands/approve     binds a human approval to the exact payload
POST /api/commands/execute     writes require a matching approval
```

All require an `Origin` header (CSRF). If you have turned dual-control on in
the security panel, `approve` also needs `X-RailCall-Approve:` set to the code
printed in the terminal that launched Studio — deliberately never sent to the
browser, so the drafting surface cannot approve its own sends.

---

## 5. The drift guard refuses

This is the test worth running twice, because it covers the failure that costs
real money: a quote edited in the window between a human approving it and the
command firing.

Create and confirm a small sales order:

```json
// odoo.create_sale_order
{ "partner_id": 47,
  "lines": [ { "name": "Drift guard test", "quantity": 1, "price_unit": 100 } ] }
```

Note the `amount_total` that comes back. Then confirm it — but pass an
`expected_amount_total` that is deliberately **wrong**:

```json
// odoo.confirm_sale_order
{ "order_id": <the id>, "expected_amount_total": 999999 }
```

**Expected:** the command refuses. It re-reads the live record, sees the total
disagrees by more than 0.01, and stops. The order stays a draft quotation. The
refusal is receipted.

Run it again with the correct `expected_amount_total` and it confirms.

The same guard is on `post_invoice` and `confirm_purchase_order`;
`update_product_price` takes `expected_current_price`. All optional — pass them
and a silent mispost becomes a clean failure.

Other guards worth poking at, each of which should refuse:

- `post_invoice` twice on the same invoice → refuses the second
- `register_payment` for more than the residual → refuses
- `cancel_sale_order` on an order that already has invoices → refuses
- `post_invoice` on a vendor bill → refuses, it is not an `out_invoice`

---

## 6. The companion workflow plans

The free `ray9/odoo-quote-to-cash-airlock` workflow composes these commands
into one run. Its `engine_spec` is what executes on the DAG engine.

Plan it — `plan_workflow` runs the transforms (they are pure) and only *plans*
the effects, so this makes **zero calls to Odoo**:

```python
import io, json, os, sys
STATION = os.path.expanduser("~/.railcall/station")
sys.path.insert(0, STATION)
sys.path.insert(0, os.path.join(STATION, "workbench"))
from workbench import workflow_engine as E

wf = json.load(io.open("workflow/odoo-quote-to-cash-airlock.json", encoding="utf-8"))
plan = E.plan_workflow(wf["engine_spec"], signing=your_signing)
for n in plan["nodes"]:
    print(n["id"], n["type"], n["policy"]["decision"])
```

**Captured** — on a machine where the module is present but unlicensed:

```
normalize        transform  auto_approve
lookup_customer  effect     block          unknown effect: action_id='odoo_find_partner'
route_customer   transform  auto_approve
create_customer  effect     block          unknown effect: action_id='odoo_create_partner'
resolve_partner  transform  auto_approve
draft_quote      effect     block          unknown effect: action_id='odoo_create_sale_order'
confirm_order    effect     block          unknown effect: action_id='odoo_confirm_sale_order'
invoice_basis    transform  auto_approve
draft_invoice    effect     block          unknown effect: action_id='odoo_create_invoice'
post_invoice     effect     block          unknown effect: action_id='odoo_post_invoice'
settle_decision  transform  auto_approve
settle           effect     block          unknown effect: action_id='odoo_register_payment'
close_out        transform  auto_approve
```

All six transforms pass the engine's transform gate. The seven `block`s are the
license gate, not a defect in the spec: module commands are bridged into
`integration_registry.ACTIONS` by `_load_modules()` at Station boot, and an
unlicensed module never loads, so its action ids are absent. **With a license
installed all thirteen nodes resolve.**

That gate is strict about what a transform may contain. It allowlists AST nodes
and permits neither `try` nor `raise`, so every transform here does numeric
coercion with a type check plus a regex, and reports a validation failure as
`ok: false` with a `problems` list rather than throwing. Each effect carries a
`cond` reading that flag, so a bad order stops before Odoo rather than raising
mid-run.

---

## 7. Clean up

The test records are real. Remove them in Odoo:

- Draft quotations and draft invoices can be deleted outright.
- A **posted** invoice cannot. Reverse it with `odoo.create_credit_note`, or
  set it to cancelled in Odoo.
- `odoo.archive_partner` archives the test customer. It never deletes — and
  neither does anything else in this module.

Remove the local install:

```bash
rm -rf ~/.railcall/station/modules/ray9-odoo
```

---

## Notes for reviewers

**Credentials.** There is exactly one `vault_get` in `handler.py`, inside
`_creds()`. `_rpc()` calls `_creds()` on every invocation, so all 26 commands
re-read the vault on every call — no credential is cached anywhere. The only
memoised value is the integer `uid` from `authenticate`, keyed by
`(url, db, username)`; the API key itself is still transmitted and revalidated
by Odoo on every `execute_kw`. `grep -n '__rc_helpers__' module/handlers/handler.py`
returns two lines and only two: `vault_get` in `_creds`, `http_post_json` in
`_post`.

**Wizards.** `odoo.validate_delivery` does not auto-dismiss backorder or
immediate-transfer prompts. When `button_validate` returns an action instead of
`True`, it reports `needs_wizard: true`, leaves the picking untouched, and
returns `ok: false`. Answering that prompt blind would move stock nobody
approved.

**Row cap.** `odoo.search_read` is capped at 200 rows and has no `offset`. It
is a scoped read for a governed run, not a bulk export.
