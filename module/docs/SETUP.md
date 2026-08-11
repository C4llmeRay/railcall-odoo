# ray9/odoo — setup

Governed Odoo ERP operations over Odoo's External API (JSON-RPC 2.0).
Twenty-six commands across quote-to-cash, procure-to-pay, CRM, inventory,
projects and documents.

Works against Odoo Online (`*.odoo.com`), Odoo.sh, and self-hosted
Odoo 14 or newer. Community and Enterprise both work — every model this
module touches (`sale.order`, `account.move`, `purchase.order`,
`stock.picking`, `crm.lead`, `project.task`) ships in Community.

---

## 1. Create an Odoo API key

**Odoo 14+ only.** Log in to Odoo as the user this module should act as,
then:

1. Click your avatar → **My Profile** (or **Preferences**)
2. Open the **Account Security** tab
3. **New API Key** → give it a label like `railcall` → copy the key

Copy it now — Odoo shows it exactly once.

That key inherits **that user's permissions**. Create a dedicated
integration user with only the access rights you want automated rather
than using an administrator account. The module can post invoices and
register payments; scope the user accordingly.

## 2. Find your database name

- **Odoo Online** — it's the subdomain. `https://acme.odoo.com` → `acme`
- **Self-hosted** — the database selector on the login page, or the
  `db_name` in your Odoo config

## 3. Save the credential in Studio

Studio → **Integrations** → **Odoo**. Four fields:

| Field      | Example                     | Notes                                |
| ---------- | --------------------------- | ------------------------------------ |
| `url`      | `https://acme.odoo.com`     | No trailing slash, no `/jsonrpc`      |
| `db`       | `acme`                      | Database name, not the display name   |
| `username` | `ops@acme.com`              | The login the API key belongs to      |
| `api_key`  | `…`                         | From step 1 — **not** your password   |

Stored in the local vault at mode 0600. It never leaves your machine —
the module talks to your Odoo directly.

## 4. Verify

Run `odoo.find_partner` with an email you know exists. A `found: true`
with a `partner_id` means url, db, username and key are all correct.

If it fails:

| Error                                    | Cause                                          |
| ---------------------------------------- | ---------------------------------------------- |
| `Odoo rejected the credential for db …`  | Wrong `db`, or the key belongs to another user |
| `Odoo credential missing: …`             | A vault field is blank                         |
| `url must start with https://`           | Missing scheme on `url`                        |
| `Odoo <model>.<method> failed — …`       | Odoo's own message; usually access rights      |

---

## Approval model

Four commands are `read_only` and run without a gate:
`find_partner`, `find_product`, `search_read`, `list_journals`.

The other twenty-two are `write_requires_approval` — the airlock shows a
preview and waits for you before anything reaches Odoo.

Eight are flagged irreversible, because Odoo will not cleanly undo them:

- `confirm_sale_order` · `confirm_purchase_order`
- `post_invoice` · `register_payment` · `create_credit_note`
- `validate_delivery`
- `cancel_sale_order`
- `send_invoice_email` — an email cannot be unsent

## Drift guards

`confirm_sale_order`, `post_invoice` and `confirm_purchase_order` accept
an optional `expected_amount_total`. `update_product_price` accepts
`expected_current_price`.

When present, the module re-reads the live record and **refuses** if the
value has moved by more than 0.01 since approval. This closes the window
where a quote is edited between someone approving it and the command
firing. Pass the total you approved against — it costs nothing and turns
a silent mispost into a clean failure.

## Guards you get for free

- Double-posting: `post_invoice` refuses an already-posted invoice,
  `confirm_sale_order` refuses an already-confirmed order,
  `register_payment` refuses an invoice already `paid` or `in_payment`
- Overpayment: `register_payment` refuses an amount above the residual
- Wrong document type: `post_invoice` and `send_invoice_email` refuse
  anything that is not an `out_invoice`
- Orphaned cancels: `cancel_sale_order` refuses when invoices already
  exist against the order
- `archive_partner` archives; it never deletes

## Notes

**JSON-RPC returns HTTP 200 on failure.** Odoo reports application errors
in the response body, not the status code. Every call here inspects the
body and raises with Odoo's own message — otherwise a failed write would
receive a signed receipt for something that never happened.

**`validate_delivery` will not auto-dismiss wizards.** When Odoo asks for
a backorder or immediate-transfer confirmation, `button_validate` returns
an action instead of `True`. The command reports `needs_wizard: true`,
leaves the picking untouched, and returns `ok: false`. Answering that
prompt blind would move stock nobody approved.

**Journal ids differ per database.** `register_payment` and
`create_credit_note` take an optional `journal_id`; use `list_journals`
to find yours. Omit it and Odoo picks the default for the operation.

**`send_invoice_email` resolves the template by model.** Template xmlids
drift between Odoo versions, so it looks up a `mail.template` registered
for `account.move` rather than hardcoding one. Pass `template_id` to
choose a specific template.

## Companion workflow

`ray9/odoo-quote-to-cash-airlock` — free — drives one inbound order
request end to end through this module: normalise, look the customer up,
create them only on a miss, quote, confirm, invoice, post, and settle
when prepaid. Install it to see the commands composed under the airlock.
