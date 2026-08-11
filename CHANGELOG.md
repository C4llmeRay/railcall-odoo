# Changelog

## Module — `ray9/odoo`

### 1.2.0

Twenty-six commands. Added procure-to-pay (`create_purchase_order`,
`confirm_purchase_order`, `create_vendor_bill`, `validate_delivery`), CRM
(`create_lead`, `update_lead_stage`), catalogue (`create_product`,
`update_product_price`), projects (`create_project_task`), records
(`attach_file`, `post_chatter_note`, `update_partner`, `archive_partner`),
`create_credit_note`, `cancel_sale_order`, `send_invoice_email`,
`list_journals` and `search_read`.

Drift guards (`expected_amount_total`, `expected_current_price`) on the
irreversible money commands. Double-post, double-payment, overpayment and
orphaned-cancel refusals.

`validate_delivery` reports `needs_wizard` instead of dismissing backorder
prompts.

`send_invoice_email` resolves its `mail.template` by model rather than by
xmlid, because template xmlids drift between Odoo versions.

### 1.0.0

Initial listing. Quote-to-cash: `find_partner`, `create_partner`,
`create_sale_order`, `confirm_sale_order`, `create_invoice`, `post_invoice`,
`register_payment`, `find_product`.

---

## Workflow — `ray9/odoo-quote-to-cash-airlock`

### 1.1.0

Added a thirteen-node `engine_spec` so the run executes on the DAG engine.
Nodes carry `parent` and `branch` across four lanes (intake, order, billing,
settlement) instead of `edges`, and every effect declares `provider: odoo`.

Rewrote the six transforms to pass the engine's transform gate, which
allowlists AST nodes and permits neither `try` nor `raise`. Numeric coercion is
now a type check plus a regex on strings; a validation failure emits
`ok: false` with a `problems` list rather than throwing.

Each effect carries a `cond` reading that flag, so a bad order stops before
Odoo instead of raising mid-run — `create_customer` fires only when the email
lookup missed, `settle` only when the order is prepaid and the posted invoice
still has a residual.

Both `for_each` nodes declare `for_each_max_size: 1`, since each list is
zero-or-one by construction, so the planner can bound the spend instead of
escalating on an unknown iteration count.

Declared `module_dependency` on `ray9/odoo` at `minimum_version: 1.2.0`.

The thirteen-node canvas is retained for display and now carries the same node
code, so the two representations cannot drift.

### 1.0.0

Initial listing. Thirteen-node canvas: normalise, look the customer up, create
on a miss, quote, confirm, invoice, post, settle when prepaid, close out. The
invoice bills what was *confirmed* and stamps each line with the sales-order
reference.
