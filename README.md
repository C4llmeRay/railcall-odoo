# ray9/odoo — governed Odoo ERP operations for RailCall

Twenty-six commands against Odoo's External API (JSON-RPC 2.0), spanning
quote-to-cash, procure-to-pay, CRM, inventory, projects and documents — each
one behind RailCall's airlock, so a human sees the exact record and total
before anything reaches your ledger.

Works with **Odoo 14+**, Community or Enterprise, on Odoo Online, Odoo.sh or
self-hosted. Every model it touches ships in Community.

- **Module** — [`ray9/odoo`](https://railcall.ai/marketplace/cmshl6t1h0001cbkhijygwywn) · $50
- **Workflow** — [`ray9/odoo-quote-to-cash-airlock`](https://railcall.ai/marketplace/cmshl93cv0003cbkhp7klf7ps) · free
- **Testing** — [TESTING.md](TESTING.md)
- **Setup** — [module/docs/SETUP.md](module/docs/SETUP.md)

---

## Why this exists

An ERP integration that can post an invoice can also post the *wrong* invoice,
and Odoo will not cleanly undo it. The interesting part of this module is not
that it can reach Odoo — anything can reach Odoo. It is what stands between the
intent and the ledger.

**Four commands are read-only** and run without a gate. **Twenty-two are
approval-gated**: the airlock previews the exact record and total and waits for
a human. **Eight are flagged irreversible**, because Odoo will not cleanly undo
them.

**The drift guard.** Pass `expected_amount_total` on the irreversible money
commands and the module re-reads the live record before acting, refusing when
the value has moved by more than 0.01 since approval. That closes the window
where a quote is edited between someone approving it and the command firing —
approve a €400 quote, execute a €40,000 one. It costs nothing to pass and it
turns a silent mispost into a clean failure.

**Guards you get for free.** Double-posts, double-payments, overpayments beyond
the residual, cancels on already-invoiced orders, posting anything that is not
an `out_invoice`. `archive_partner` archives; nothing in this module deletes.

**Failure is not silent.** Odoo answers `HTTP 200` even when a call failed,
reporting the error in the response body. Every call here inspects the body and
raises with Odoo's own message — otherwise a failed write would collect a
signed receipt for something that never happened.

---

## The commands

### Read-only — no approval

| Command | Does |
| ------- | ---- |
| `odoo.find_partner` | look a customer up by email or name |
| `odoo.find_product` | look a product up by code or name |
| `odoo.list_journals` | list accounting journals (ids differ per database) |
| `odoo.search_read` | scoped read against any model — hard 200-row cap |

### Sales & billing

| Command | Irreversible |
| ------- | :----------: |
| `odoo.create_sale_order` | |
| `odoo.confirm_sale_order` | ● |
| `odoo.cancel_sale_order` | ● |
| `odoo.create_invoice` | |
| `odoo.post_invoice` | ● |
| `odoo.register_payment` | ● |
| `odoo.create_credit_note` | ● |
| `odoo.send_invoice_email` | ● |

### Purchasing & stock

| Command | Irreversible |
| ------- | :----------: |
| `odoo.create_purchase_order` | |
| `odoo.confirm_purchase_order` | ● |
| `odoo.create_vendor_bill` | |
| `odoo.validate_delivery` | ● |

### CRM, products, projects, records

| Command | |
| ------- | - |
| `odoo.create_partner` · `odoo.update_partner` · `odoo.archive_partner` | customers |
| `odoo.create_product` · `odoo.update_product_price` | catalogue |
| `odoo.create_lead` · `odoo.update_lead_stage` | pipeline |
| `odoo.create_project_task` | projects |
| `odoo.attach_file` · `odoo.post_chatter_note` | any record |

---

## Credentials

One vault entry, four fields, mode 0600:

| Field | Example |
| ----- | ------- |
| `url` | `https://acme.odoo.com` |
| `db` | `acme` |
| `username` | `ops@acme.com` |
| `api_key` | an Odoo 14+ API key — **not** your password |

The key inherits the permissions of the user it belongs to. Create a dedicated
integration user scoped to what you want automated rather than using an
administrator account — this module can post invoices and register payments.

Nothing leaves your machine. Station talks to your Odoo directly, and there is
exactly one `vault_get` in the handler: every command re-reads the vault on
every call rather than holding a cached credential.

Full walkthrough: [module/docs/SETUP.md](module/docs/SETUP.md).

---

## The companion workflow

[`ray9/odoo-quote-to-cash-airlock`](workflow/odoo-quote-to-cash-airlock.json)
(free) drives one inbound order request end to end: normalise and validate,
look the customer up, create them only on a miss, quote, confirm, invoice, post
and settle when prepaid.

Thirteen nodes across four lanes — intake, order, billing, settlement. It ships
both a display canvas and an `engine_spec` that runs on the DAG engine, built
from the same node code so the two cannot drift. Each effect carries a `cond`,
so a failed validation stops the run before Odoo instead of raising mid-flight,
and both `for_each` nodes declare `for_each_max_size: 1` so the planner can
bound the spend rather than escalating on an unknown count.

It declares `module_dependency` on `ray9/odoo` at `minimum_version: 1.2.0` —
every effect resolves to a command this module signs, and none of them run
without it.

---

## Repository layout

```
module/                     the signed bundle, verbatim as published
  module.json               manifest — 26 commands, credential_spec
  handlers/handler.py       every command
  module.sig                v2 tree signature
  docs/SETUP.md             ships inside the bundle
  .moduleignore             part of the signed tree — copy it if you rebuild
workflow/
  odoo-quote-to-cash-airlock.json
TESTING.md                  verify it yourself
CHANGELOG.md
```

`module/` is byte-for-byte what was published. The signature is a v2 tree
signature covering the canonical manifest and a manifest of every file, so you
can verify it independently — see [TESTING.md](TESTING.md) §1.

---

## Known limits

- `odoo.search_read` caps at 200 rows and has no `offset`. It is a scoped read
  for a governed run, not a bulk export or a sync primitive.
- `odoo.validate_delivery` will not answer a backorder or immediate-transfer
  wizard. It reports `needs_wizard: true` and leaves the picking untouched,
  because answering blind would move stock nobody approved.
- Journal ids differ per database. `register_payment` and `create_credit_note`
  take an optional `journal_id`; use `list_journals` to find yours.

---

## License

MIT for the contents of this repository. The marketplace module itself is a
paid listing and requires a license to load — see the listing.
