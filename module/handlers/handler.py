"""odoo v1.0.0 — governed Odoo ERP quote-to-cash writes.

Vault entry `odoo`:
    {
      "url":      "https://mycompany.odoo.com",   # no trailing /jsonrpc
      "db":       "mycompany",                    # Odoo database name
      "username": "ops@mycompany.com",
      "api_key":  "…"                             # Odoo 14+ API key, NOT a password
    }

Everything goes through Odoo's External API (JSON-RPC 2.0) at
POST <url>/jsonrpc. Two services are used:

  service=common, method=authenticate  → resolves the uid for the api key
  service=object, method=execute_kw    → every model call after that

IMPORTANT — Odoo returns HTTP 200 on *application* errors and puts the
failure in the JSON body under "error". The `http_post_json` helper only
raises on transport/HTTP failures, so every call here goes through
`_rpc()` which inspects the body and raises RuntimeError with Odoo's own
message. Without that, a failed write would look like a success and the
airlock would sign a receipt for something that never happened.

The uid is resolved once per handler invocation and memoised in
_SESSION for the life of the process, keyed by (url, db, username) so a
station talking to two Odoo instances never crosses wires.

Irreversible commands (confirm_sale_order, post_invoice,
register_payment) accept an optional `expected_amount_total` /`amount`
and refuse to run when the live record disagrees. That is the guard
against approving a €400 quote and executing a €40,000 one after an
upstream edit.
"""

import json as _json

# uid cache — {(url, db, username): uid}
_SESSION = {}


# ── credentials + transport ────────────────────────────────────────────────

def _creds():
    helpers = __rc_helpers__  # noqa: F821
    entry = helpers["vault_get"]("odoo")
    if not isinstance(entry, dict):
        raise RuntimeError(
            "no Odoo credential saved — configure the `odoo` vault entry with "
            "url, db, username, api_key"
        )
    url = str(entry.get("url") or "").strip().rstrip("/")
    db = str(entry.get("db") or entry.get("database") or "").strip()
    username = str(entry.get("username") or entry.get("login") or "").strip()
    api_key = str(entry.get("api_key") or entry.get("apikey") or "").strip()
    missing = [
        k for k, v in (("url", url), ("db", db), ("username", username), ("api_key", api_key))
        if not v
    ]
    if missing:
        raise RuntimeError("Odoo credential missing: " + ", ".join(missing))
    if not url.startswith("https://") and not url.startswith("http://"):
        raise RuntimeError("Odoo url must start with https:// (got: " + url[:40] + ")")
    return url, db, username, api_key


def _post(url, payload):
    """POST a JSON-RPC envelope and return the decoded body."""
    helpers = __rc_helpers__  # noqa: F821
    status, raw = helpers["http_post_json"](url + "/jsonrpc", payload, timeout=40)
    try:
        return _json.loads(raw.decode("utf-8"))
    except Exception:
        raise RuntimeError(
            "Odoo returned a non-JSON body (HTTP %s): %s"
            % (status, raw[:200].decode("utf-8", "replace"))
        )


def _odoo_error(body):
    """Turn Odoo's nested error object into one readable line."""
    err = body.get("error") or {}
    data = err.get("data") or {}
    msg = (
        data.get("message")
        or err.get("message")
        or data.get("name")
        or "unknown Odoo error"
    )
    name = data.get("name") or ""
    if name and name not in msg:
        msg = name + ": " + msg
    return str(msg).strip()[:400]


def _uid(url, db, username, api_key):
    key = (url, db, username)
    if key in _SESSION:
        return _SESSION[key]
    body = _post(url, {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "service": "common",
            "method": "authenticate",
            "args": [db, username, api_key, {}],
        },
    })
    if body.get("error"):
        raise RuntimeError("Odoo authenticate failed — " + _odoo_error(body))
    uid = body.get("result")
    if not isinstance(uid, int) or uid <= 0:
        raise RuntimeError(
            "Odoo rejected the credential for db '%s' / user '%s' — check the "
            "database name and that the API key belongs to that user" % (db, username)
        )
    _SESSION[key] = uid
    return uid


def _rpc(model, method, args, kwargs=None):
    """execute_kw against the configured Odoo. Returns the `result` value."""
    url, db, username, api_key = _creds()
    uid = _uid(url, db, username, api_key)
    body = _post(url, {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "service": "object",
            "method": "execute_kw",
            "args": [db, uid, api_key, model, method, args, kwargs or {}],
        },
    })
    if body.get("error"):
        raise RuntimeError("Odoo %s.%s failed — %s" % (model, method, _odoo_error(body)))
    return body.get("result")


def _record_url(model, rec_id):
    """Deep link an operator can paste into a browser to see the record the
    receipt refers to. Best-effort — returns "" if creds can't be read."""
    try:
        url, _db, _u, _k = _creds()
    except Exception:
        return ""
    return "%s/web#id=%s&model=%s&view_type=form" % (url, rec_id, model)


# ── schema probing ─────────────────────────────────────────────────────────

# {model: set(field names)} — memoised for the life of the process, because
# the installed module set does not change under a running station.
_FIELDS = {}


def _has_field(model, field):
    """True when `model` really carries `field` on THIS database.

    Odoo ships fields with modules: `customer_rank` arrives with Accounting,
    so a field present on one database is simply absent on another. Asking for
    it blind fails the entire call with `Invalid field 'customer_rank' on
    'res.partner'`, which is a confusing way to say "the Accounting app is not
    installed here".
    """
    names = _FIELDS.get(model)
    if names is None:
        names = set((_rpc(model, "fields_get", [[], ["type"]]) or {}).keys())
        _FIELDS[model] = names
    return field in names


# ── input coercion ─────────────────────────────────────────────────────────

def _req_str(inputs, field):
    v = inputs.get(field)
    if not isinstance(v, str) or not v.strip():
        raise RuntimeError("%s must be a non-empty string" % field)
    return v.strip()


def _req_int(inputs, field):
    v = inputs.get(field)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        try:
            v = int(str(v).strip())
        except Exception:
            raise RuntimeError("%s must be an integer record id" % field)
    v = int(v)
    if v <= 0:
        raise RuntimeError("%s must be a positive record id" % field)
    return v


def _opt_str(inputs, field):
    v = inputs.get(field)
    return v.strip() if isinstance(v, str) and v.strip() else None


def _lines_to_commands(raw, qty_field):
    """Normalise a list of {product_id|name, quantity, price_unit} dicts into
    Odoo x2many create commands: [0, 0, {...}].

    qty_field differs by model — sale.order.line stores the quantity in
    `product_uom_qty`, account.move.line in `quantity`. Passing the wrong one
    makes Odoo silently default to 1.0, so the caller names it explicitly.
    """
    field = "lines"
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("%s must be a non-empty array of line objects" % field)
    cmds = []
    for i, line in enumerate(raw):
        if not isinstance(line, dict):
            raise RuntimeError("%s[%d] must be an object" % (field, i))
        vals = {}
        pid = line.get("product_id")
        if pid not in (None, "", False):
            try:
                vals["product_id"] = int(pid)
            except Exception:
                raise RuntimeError("%s[%d].product_id must be an integer id" % (field, i))
        name = line.get("name") or line.get("description")
        if isinstance(name, str) and name.strip():
            vals["name"] = name.strip()[:1000]
        if "product_id" not in vals and "name" not in vals:
            raise RuntimeError(
                "%s[%d] needs a product_id or a name — Odoo cannot create a "
                "blank line" % (field, i)
            )
        qty = line.get("quantity", line.get("qty"))
        if qty not in (None, ""):
            try:
                vals[qty_field] = float(qty)
            except Exception:
                raise RuntimeError("%s[%d].quantity must be a number" % (field, i))
        price = line.get("price_unit", line.get("price"))
        if price not in (None, ""):
            try:
                vals["price_unit"] = float(price)
            except Exception:
                raise RuntimeError("%s[%d].price_unit must be a number" % (field, i))
        cmds.append([0, 0, vals])
    return cmds


def _amount_guard(inputs, live_total, label):
    """Refuse to fire an irreversible write when the live record's total
    disagrees with what the approver signed off on."""
    expected = inputs.get("expected_amount_total")
    if expected in (None, ""):
        return
    try:
        expected = float(expected)
    except Exception:
        raise RuntimeError("expected_amount_total must be a number")
    if abs(float(live_total or 0) - expected) > 0.01:
        raise RuntimeError(
            "refusing to %s: approved total was %.2f but the live record is "
            "now %.2f — re-approve against the current record"
            % (label, expected, float(live_total or 0))
        )


# ── commands ───────────────────────────────────────────────────────────────

def odoo_find_partner(inputs, stamp):
    email = _opt_str(inputs, "email")
    name = _opt_str(inputs, "name")
    if not email and not name:
        raise RuntimeError("provide an email or a name to search on")

    limit = inputs.get("limit")
    try:
        limit = max(1, min(int(limit), 50)) if limit not in (None, "") else 10
    except Exception:
        limit = 10

    domain = [["email", "=ilike", email]] if email else [["name", "ilike", name]]
    partners = _rpc(
        "res.partner", "search_read", [domain],
        {"fields": ["id", "name", "email", "phone", "vat"],
         "limit": limit},
    ) or []

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "found": bool(partners),
        "partner_id": partners[0]["id"] if partners else 0,
        "partners": partners,
        "count": len(partners),
        "searched_on": "email" if email else "name",
    }, None


def odoo_create_partner(inputs, stamp):
    vals = {"name": _req_str(inputs, "name")}
    for field in ("email", "phone", "street", "city", "zip", "vat"):
        v = _opt_str(inputs, field)
        if v:
            vals[field] = v
    if isinstance(inputs.get("is_company"), bool):
        vals["is_company"] = inputs["is_company"]
    # Marks the partner as a customer so they show up in customer lists. The
    # field arrives with Accounting, so it is worth setting when present and
    # must not fail the create when it is not.
    if _has_field("res.partner", "customer_rank"):
        vals["customer_rank"] = 1

    country_code = _opt_str(inputs, "country_code")
    if country_code:
        found = _rpc(
            "res.country", "search_read", [[["code", "=", country_code.upper()]]],
            {"fields": ["id"], "limit": 1},
        ) or []
        if not found:
            raise RuntimeError("unknown country_code '%s' (expected ISO-3166 alpha-2)" % country_code)
        vals["country_id"] = found[0]["id"]

    partner_id = _rpc("res.partner", "create", [vals])
    if not isinstance(partner_id, int):
        raise RuntimeError("Odoo did not return a partner id (got: %r)" % (partner_id,))

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "partner_id": partner_id,
        "name": vals["name"],
        "email": vals.get("email", ""),
        "record_url": _record_url("res.partner", partner_id),
    }, None


def odoo_create_sale_order(inputs, stamp):
    partner_id = _req_int(inputs, "partner_id")
    order_lines = _lines_to_commands(inputs.get("lines"), "product_uom_qty")

    vals = {"partner_id": partner_id, "order_line": order_lines}
    ref = _opt_str(inputs, "client_order_ref")
    if ref:
        vals["client_order_ref"] = ref
    note = _opt_str(inputs, "note")
    if note:
        vals["note"] = note

    order_id = _rpc("sale.order", "create", [vals])
    if not isinstance(order_id, int):
        raise RuntimeError("Odoo did not return a sale.order id (got: %r)" % (order_id,))

    read = _rpc(
        "sale.order", "read", [[order_id]],
        {"fields": ["name", "state", "amount_untaxed", "amount_tax", "amount_total", "currency_id"]},
    ) or [{}]
    rec = read[0]
    currency = rec.get("currency_id")

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "order_id": order_id,
        "order_name": rec.get("name", ""),
        "state": rec.get("state", ""),
        "amount_untaxed": rec.get("amount_untaxed", 0),
        "amount_tax": rec.get("amount_tax", 0),
        "amount_total": rec.get("amount_total", 0),
        "currency": currency[1] if isinstance(currency, list) and len(currency) > 1 else "",
        "line_count": len(order_lines),
        "record_url": _record_url("sale.order", order_id),
    }, None


def odoo_confirm_sale_order(inputs, stamp):
    order_id = _req_int(inputs, "order_id")

    before = _rpc(
        "sale.order", "read", [[order_id]],
        {"fields": ["name", "state", "amount_total", "currency_id"]},
    ) or []
    if not before:
        raise RuntimeError("sale.order %d not found" % order_id)
    rec = before[0]
    if rec.get("state") in ("sale", "done"):
        raise RuntimeError(
            "sale.order %s is already confirmed (state=%s) — refusing to "
            "double-confirm" % (rec.get("name"), rec.get("state"))
        )
    if rec.get("state") == "cancel":
        raise RuntimeError("sale.order %s is cancelled — cannot confirm" % rec.get("name"))
    _amount_guard(inputs, rec.get("amount_total"), "confirm sale.order %s" % rec.get("name"))

    _rpc("sale.order", "action_confirm", [[order_id]])

    after = _rpc(
        "sale.order", "read", [[order_id]],
        {"fields": ["name", "state", "amount_total", "currency_id"]},
    ) or [{}]
    post = after[0]
    currency = post.get("currency_id")

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "order_id": order_id,
        "order_name": post.get("name", ""),
        "state": post.get("state", ""),
        "state_before": rec.get("state", ""),
        "amount_total": post.get("amount_total", 0),
        "currency": currency[1] if isinstance(currency, list) and len(currency) > 1 else "",
        "record_url": _record_url("sale.order", order_id),
    }, None


def odoo_create_invoice(inputs, stamp):
    partner_id = _req_int(inputs, "partner_id")
    invoice_lines = _lines_to_commands(inputs.get("lines"), "quantity")

    vals = {
        "move_type": "out_invoice",
        "partner_id": partner_id,
        "invoice_line_ids": invoice_lines,
    }
    for src, dest in (("invoice_date", "invoice_date"),
                      ("payment_reference", "payment_reference"),
                      ("narration", "narration")):
        v = _opt_str(inputs, src)
        if v:
            vals[dest] = v

    invoice_id = _rpc("account.move", "create", [vals])
    if not isinstance(invoice_id, int):
        raise RuntimeError("Odoo did not return an account.move id (got: %r)" % (invoice_id,))

    read = _rpc(
        "account.move", "read", [[invoice_id]],
        {"fields": ["name", "state", "amount_untaxed", "amount_tax", "amount_total", "currency_id"]},
    ) or [{}]
    rec = read[0]
    currency = rec.get("currency_id")

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "invoice_id": invoice_id,
        "invoice_name": rec.get("name", ""),
        "state": rec.get("state", ""),
        "amount_untaxed": rec.get("amount_untaxed", 0),
        "amount_tax": rec.get("amount_tax", 0),
        "amount_total": rec.get("amount_total", 0),
        "currency": currency[1] if isinstance(currency, list) and len(currency) > 1 else "",
        "line_count": len(invoice_lines),
        "record_url": _record_url("account.move", invoice_id),
    }, None


def odoo_post_invoice(inputs, stamp):
    invoice_id = _req_int(inputs, "invoice_id")

    before = _rpc(
        "account.move", "read", [[invoice_id]],
        {"fields": ["name", "state", "move_type", "amount_total", "currency_id"]},
    ) or []
    if not before:
        raise RuntimeError("account.move %d not found" % invoice_id)
    rec = before[0]
    if rec.get("move_type") != "out_invoice":
        raise RuntimeError(
            "account.move %d is a '%s', not a customer invoice — refusing to post"
            % (invoice_id, rec.get("move_type"))
        )
    if rec.get("state") == "posted":
        raise RuntimeError("invoice %s is already posted" % rec.get("name"))
    if rec.get("state") == "cancel":
        raise RuntimeError("invoice %s is cancelled — cannot post" % rec.get("name"))
    _amount_guard(inputs, rec.get("amount_total"), "post invoice %s" % rec.get("name"))

    _rpc("account.move", "action_post", [[invoice_id]])

    after = _rpc(
        "account.move", "read", [[invoice_id]],
        {"fields": ["name", "state", "amount_total", "amount_residual",
                    "payment_state", "currency_id"]},
    ) or [{}]
    post = after[0]
    currency = post.get("currency_id")

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "invoice_id": invoice_id,
        "invoice_name": post.get("name", ""),
        "state": post.get("state", ""),
        "state_before": rec.get("state", ""),
        "amount_total": post.get("amount_total", 0),
        "amount_residual": post.get("amount_residual", 0),
        "payment_state": post.get("payment_state", ""),
        "currency": currency[1] if isinstance(currency, list) and len(currency) > 1 else "",
        "record_url": _record_url("account.move", invoice_id),
    }, None


def odoo_register_payment(inputs, stamp):
    """Register a payment through account.payment.register — the same wizard
    the Odoo UI uses, so the payment is reconciled against the invoice rather
    than left floating as an unmatched account.payment."""
    invoice_id = _req_int(inputs, "invoice_id")

    before = _rpc(
        "account.move", "read", [[invoice_id]],
        {"fields": ["name", "state", "move_type", "amount_total",
                    "amount_residual", "payment_state"]},
    ) or []
    if not before:
        raise RuntimeError("account.move %d not found" % invoice_id)
    rec = before[0]
    if rec.get("state") != "posted":
        raise RuntimeError(
            "invoice %s is in state '%s' — only a posted invoice can be paid"
            % (rec.get("name"), rec.get("state"))
        )
    if rec.get("payment_state") in ("paid", "in_payment"):
        raise RuntimeError(
            "invoice %s is already %s — refusing to double-pay"
            % (rec.get("name"), rec.get("payment_state"))
        )

    residual = float(rec.get("amount_residual") or 0)
    amount = inputs.get("amount")
    if amount in (None, ""):
        amount = residual
    else:
        try:
            amount = float(amount)
        except Exception:
            raise RuntimeError("amount must be a number")
    if amount <= 0:
        raise RuntimeError("amount must be greater than zero (residual is %.2f)" % residual)
    if amount - residual > 0.01:
        raise RuntimeError(
            "refusing to overpay: amount %.2f exceeds the %.2f still open on %s"
            % (amount, residual, rec.get("name"))
        )

    wiz_vals = {"amount": amount}
    pay_date = _opt_str(inputs, "payment_date")
    if pay_date:
        wiz_vals["payment_date"] = pay_date
    memo = _opt_str(inputs, "memo")
    if memo:
        wiz_vals["communication"] = memo
    if inputs.get("journal_id") not in (None, ""):
        wiz_vals["journal_id"] = _req_int(inputs, "journal_id")

    ctx = {"context": {"active_model": "account.move", "active_ids": [invoice_id]}}
    wizard_id = _rpc("account.payment.register", "create", [wiz_vals], ctx)
    if not isinstance(wizard_id, int):
        raise RuntimeError("Odoo did not return a payment wizard id (got: %r)" % (wizard_id,))
    _rpc("account.payment.register", "action_create_payments", [[wizard_id]], ctx)

    after = _rpc(
        "account.move", "read", [[invoice_id]],
        {"fields": ["name", "amount_residual", "payment_state"]},
    ) or [{}]
    post = after[0]

    payments = _rpc(
        "account.payment", "search_read",
        [[["partner_id", "!=", False], ["state", "=", "posted"]]],
        {"fields": ["id", "name", "amount"], "limit": 5, "order": "id desc"},
    ) or []

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "invoice_id": invoice_id,
        "invoice_name": post.get("name", ""),
        "amount_paid": amount,
        "amount_residual": post.get("amount_residual", 0),
        "payment_state": post.get("payment_state", ""),
        "payment_ids": [p["id"] for p in payments],
        "record_url": _record_url("account.move", invoice_id),
    }, None


# ── lookup + generic read ──────────────────────────────────────────────────

def odoo_find_product(inputs, stamp):
    code = _opt_str(inputs, "code")
    name = _opt_str(inputs, "name")
    if not code and not name:
        raise RuntimeError("provide a code or a name to search on")

    limit = inputs.get("limit")
    try:
        limit = max(1, min(int(limit), 50)) if limit not in (None, "") else 10
    except Exception:
        limit = 10

    domain = [["default_code", "=ilike", code]] if code else [["name", "ilike", name]]
    products = _rpc(
        "product.product", "search_read", [domain],
        {"fields": ["id", "name", "default_code", "list_price", "uom_id", "type"],
         "limit": limit},
    ) or []

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "found": bool(products),
        "product_id": products[0]["id"] if products else 0,
        "products": products,
        "count": len(products),
        "searched_on": "code" if code else "name",
    }, None


def odoo_search_read(inputs, stamp):
    """Scoped read against any model. read_only — this never writes. The row
    cap is enforced here rather than trusted from the caller so a workflow
    can't accidentally pull an entire table into a receipt."""
    model = _req_str(inputs, "model")
    if not all(ch.isalnum() or ch in "._" for ch in model):
        raise RuntimeError("model must be a dotted Odoo model name, e.g. res.partner")

    domain = inputs.get("domain")
    if domain in (None, ""):
        domain = []
    if not isinstance(domain, list):
        raise RuntimeError("domain must be an array of Odoo domain triples")

    fields = inputs.get("fields")
    if fields in (None, ""):
        fields = ["id", "display_name"]
    if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
        raise RuntimeError("fields must be an array of field-name strings")

    HARD_CAP = 200
    limit = inputs.get("limit")
    try:
        limit = max(1, min(int(limit), HARD_CAP)) if limit not in (None, "") else 50
    except Exception:
        limit = 50

    kwargs = {"fields": fields, "limit": limit}
    order = _opt_str(inputs, "order")
    if order:
        kwargs["order"] = order

    records = _rpc(model, "search_read", [domain], kwargs) or []

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "model": model,
        "records": records,
        "count": len(records),
        "truncated": len(records) >= limit,
        "limit_applied": limit,
    }, None


# ── sales: cancel ──────────────────────────────────────────────────────────

def odoo_cancel_sale_order(inputs, stamp):
    order_id = _req_int(inputs, "order_id")

    before = _rpc(
        "sale.order", "read", [[order_id]],
        {"fields": ["name", "state", "amount_total", "invoice_ids"]},
    ) or []
    if not before:
        raise RuntimeError("sale.order %d not found" % order_id)
    rec = before[0]
    if rec.get("state") == "cancel":
        raise RuntimeError("sale.order %s is already cancelled" % rec.get("name"))
    if rec.get("invoice_ids"):
        raise RuntimeError(
            "sale.order %s already has %d invoice(s) against it — cancel or credit "
            "those first rather than cancelling the order underneath them"
            % (rec.get("name"), len(rec["invoice_ids"]))
        )

    _rpc("sale.order", "action_cancel", [[order_id]])

    reason = _opt_str(inputs, "reason")
    if reason:
        _rpc("sale.order", "message_post", [[order_id]],
             {"body": "Cancelled via RailCall: " + reason,
              "message_type": "comment"})

    after = _rpc("sale.order", "read", [[order_id]], {"fields": ["name", "state"]}) or [{}]
    post = after[0]

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "order_id": order_id,
        "order_name": post.get("name", ""),
        "state": post.get("state", ""),
        "state_before": rec.get("state", ""),
        "reason": reason or "",
        "record_url": _record_url("sale.order", order_id),
    }, None


# ── credit note ────────────────────────────────────────────────────────────

def odoo_create_credit_note(inputs, stamp):
    """Reverse a posted invoice through account.move.reversal — the same
    wizard the Odoo UI uses, so the credit note is properly linked back to
    the original move rather than being a free-floating credit."""
    invoice_id = _req_int(inputs, "invoice_id")
    reason = _req_str(inputs, "reason")

    before = _rpc(
        "account.move", "read", [[invoice_id]],
        {"fields": ["name", "state", "move_type", "amount_total"]},
    ) or []
    if not before:
        raise RuntimeError("account.move %d not found" % invoice_id)
    rec = before[0]
    if rec.get("move_type") != "out_invoice":
        raise RuntimeError(
            "account.move %d is a '%s' — credit notes are raised against "
            "customer invoices" % (invoice_id, rec.get("move_type"))
        )
    if rec.get("state") != "posted":
        raise RuntimeError(
            "invoice %s is in state '%s' — only a posted invoice can be reversed"
            % (rec.get("name"), rec.get("state"))
        )

    wiz_vals = {"move_ids": [[6, 0, [invoice_id]]], "reason": reason}
    rev_date = _opt_str(inputs, "reversal_date")
    if rev_date:
        wiz_vals["date"] = rev_date
    if inputs.get("journal_id") not in (None, ""):
        wiz_vals["journal_id"] = _req_int(inputs, "journal_id")

    ctx = {"context": {"active_model": "account.move", "active_ids": [invoice_id]}}
    wizard_id = _rpc("account.move.reversal", "create", [wiz_vals], ctx)
    if not isinstance(wizard_id, int):
        raise RuntimeError("Odoo did not return a reversal wizard id (got: %r)" % (wizard_id,))
    _rpc("account.move.reversal", "reverse_moves", [[wizard_id]], ctx)

    wiz = _rpc("account.move.reversal", "read", [[wizard_id]], {"fields": ["new_move_ids"]}) or [{}]
    new_ids = wiz[0].get("new_move_ids") or []
    if not new_ids:
        raise RuntimeError(
            "reversal ran but Odoo returned no credit-note id — check %s in the UI "
            "before retrying, so you don't raise a second credit" % rec.get("name")
        )
    credit_id = int(new_ids[0])

    note = _rpc(
        "account.move", "read", [[credit_id]],
        {"fields": ["name", "state", "amount_total", "currency_id"]},
    ) or [{}]
    n = note[0]
    currency = n.get("currency_id")

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "invoice_id": invoice_id,
        "invoice_name": rec.get("name", ""),
        "credit_note_id": credit_id,
        "credit_note_name": n.get("name", ""),
        "state": n.get("state", ""),
        "amount_total": n.get("amount_total", 0),
        "currency": currency[1] if isinstance(currency, list) and len(currency) > 1 else "",
        "reason": reason,
        "record_url": _record_url("account.move", credit_id),
    }, None


# ── procure-to-pay ─────────────────────────────────────────────────────────

def odoo_create_vendor_bill(inputs, stamp):
    partner_id = _req_int(inputs, "partner_id")
    bill_lines = _lines_to_commands(inputs.get("lines"), "quantity")

    vals = {
        "move_type": "in_invoice",
        "partner_id": partner_id,
        "invoice_line_ids": bill_lines,
    }
    inv_date = _opt_str(inputs, "invoice_date")
    if inv_date:
        vals["invoice_date"] = inv_date
    vendor_ref = _opt_str(inputs, "vendor_reference")
    if vendor_ref:
        vals["ref"] = vendor_ref
    narration = _opt_str(inputs, "narration")
    if narration:
        vals["narration"] = narration

    bill_id = _rpc("account.move", "create", [vals])
    if not isinstance(bill_id, int):
        raise RuntimeError("Odoo did not return a vendor-bill id (got: %r)" % (bill_id,))

    read = _rpc(
        "account.move", "read", [[bill_id]],
        {"fields": ["name", "state", "amount_untaxed", "amount_total", "currency_id"]},
    ) or [{}]
    rec = read[0]
    currency = rec.get("currency_id")

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "bill_id": bill_id,
        "bill_name": rec.get("name", ""),
        "state": rec.get("state", ""),
        "amount_untaxed": rec.get("amount_untaxed", 0),
        "amount_total": rec.get("amount_total", 0),
        "currency": currency[1] if isinstance(currency, list) and len(currency) > 1 else "",
        "vendor_reference": vendor_ref or "",
        "line_count": len(bill_lines),
        "record_url": _record_url("account.move", bill_id),
    }, None


def odoo_create_purchase_order(inputs, stamp):
    partner_id = _req_int(inputs, "partner_id")
    # purchase.order.line stores quantity in product_qty, not product_uom_qty.
    po_lines = _lines_to_commands(inputs.get("lines"), "product_qty")

    date_planned = _opt_str(inputs, "date_planned")
    if date_planned:
        for cmd in po_lines:
            cmd[2]["date_planned"] = date_planned

    vals = {"partner_id": partner_id, "order_line": po_lines}
    partner_ref = _opt_str(inputs, "partner_ref")
    if partner_ref:
        vals["partner_ref"] = partner_ref

    po_id = _rpc("purchase.order", "create", [vals])
    if not isinstance(po_id, int):
        raise RuntimeError("Odoo did not return a purchase.order id (got: %r)" % (po_id,))

    read = _rpc(
        "purchase.order", "read", [[po_id]],
        {"fields": ["name", "state", "amount_untaxed", "amount_total", "currency_id"]},
    ) or [{}]
    rec = read[0]
    currency = rec.get("currency_id")

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "purchase_order_id": po_id,
        "purchase_order_name": rec.get("name", ""),
        "state": rec.get("state", ""),
        "amount_untaxed": rec.get("amount_untaxed", 0),
        "amount_total": rec.get("amount_total", 0),
        "currency": currency[1] if isinstance(currency, list) and len(currency) > 1 else "",
        "line_count": len(po_lines),
        "record_url": _record_url("purchase.order", po_id),
    }, None


def odoo_confirm_purchase_order(inputs, stamp):
    po_id = _req_int(inputs, "purchase_order_id")

    before = _rpc(
        "purchase.order", "read", [[po_id]],
        {"fields": ["name", "state", "amount_total", "currency_id"]},
    ) or []
    if not before:
        raise RuntimeError("purchase.order %d not found" % po_id)
    rec = before[0]
    if rec.get("state") in ("purchase", "done"):
        raise RuntimeError(
            "purchase.order %s is already confirmed (state=%s) — refusing to "
            "double-confirm" % (rec.get("name"), rec.get("state"))
        )
    if rec.get("state") == "cancel":
        raise RuntimeError("purchase.order %s is cancelled — cannot confirm" % rec.get("name"))
    _amount_guard(inputs, rec.get("amount_total"), "confirm purchase.order %s" % rec.get("name"))

    _rpc("purchase.order", "button_confirm", [[po_id]])

    after = _rpc(
        "purchase.order", "read", [[po_id]],
        {"fields": ["name", "state", "amount_total", "currency_id"]},
    ) or [{}]
    post = after[0]
    currency = post.get("currency_id")

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "purchase_order_id": po_id,
        "purchase_order_name": post.get("name", ""),
        "state": post.get("state", ""),
        "state_before": rec.get("state", ""),
        "amount_total": post.get("amount_total", 0),
        "currency": currency[1] if isinstance(currency, list) and len(currency) > 1 else "",
        "record_url": _record_url("purchase.order", po_id),
    }, None


# ── inventory ──────────────────────────────────────────────────────────────

def odoo_validate_delivery(inputs, stamp):
    """Validate a stock.picking. button_validate can return an ACTION dict
    instead of True when Odoo wants a wizard (backorder confirmation,
    immediate-transfer). We do not auto-dismiss those — silently answering a
    backorder prompt would move stock the operator never approved. We report
    it and leave the picking untouched."""
    picking_id = _req_int(inputs, "picking_id")

    before = _rpc(
        "stock.picking", "read", [[picking_id]],
        {"fields": ["name", "state", "picking_type_id", "origin"]},
    ) or []
    if not before:
        raise RuntimeError("stock.picking %d not found" % picking_id)
    rec = before[0]
    if rec.get("state") == "done":
        raise RuntimeError("picking %s is already validated" % rec.get("name"))
    if rec.get("state") == "cancel":
        raise RuntimeError("picking %s is cancelled — cannot validate" % rec.get("name"))
    if rec.get("state") == "draft":
        raise RuntimeError(
            "picking %s is still a draft — mark it ready before validating"
            % rec.get("name")
        )

    result = _rpc("stock.picking", "button_validate", [[picking_id]])

    after = _rpc(
        "stock.picking", "read", [[picking_id]],
        {"fields": ["name", "state", "date_done"]},
    ) or [{}]
    post = after[0]

    needs_wizard = isinstance(result, dict) and bool(result.get("res_model"))
    if needs_wizard and post.get("state") != "done":
        return {
            "ok": False,
            "loaded_from": "module:odoo",
            "picking_id": picking_id,
            "picking_name": post.get("name", ""),
            "state": post.get("state", ""),
            "needs_wizard": True,
            "wizard_model": result.get("res_model", ""),
            "note": (
                "Odoo asked for a %s confirmation. The picking was NOT validated. "
                "Resolve it in the Odoo UI — auto-answering a backorder prompt would "
                "move stock nobody approved." % result.get("res_model", "wizard")
            ),
            "record_url": _record_url("stock.picking", picking_id),
        }, None

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "picking_id": picking_id,
        "picking_name": post.get("name", ""),
        "state": post.get("state", ""),
        "state_before": rec.get("state", ""),
        "date_done": post.get("date_done", ""),
        "needs_wizard": False,
        "origin": rec.get("origin", ""),
        "record_url": _record_url("stock.picking", picking_id),
    }, None


# ── chatter ────────────────────────────────────────────────────────────────

def odoo_post_chatter_note(inputs, stamp):
    model = _req_str(inputs, "model")
    if not all(ch.isalnum() or ch in "._" for ch in model):
        raise RuntimeError("model must be a dotted Odoo model name, e.g. sale.order")
    record_id = _req_int(inputs, "record_id")
    body = _req_str(inputs, "body")

    exists = _rpc(model, "search_read", [[["id", "=", record_id]]],
                  {"fields": ["id"], "limit": 1}) or []
    if not exists:
        raise RuntimeError("%s %d not found" % (model, record_id))

    message_id = _rpc(
        model, "message_post", [[record_id]],
        {"body": body[:60000], "message_type": "comment",
         "subtype_xmlid": "mail.mt_note"},
    )

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "model": model,
        "record_id": record_id,
        "message_id": message_id if isinstance(message_id, int) else 0,
        "body_preview": body[:120] + ("…" if len(body) > 120 else ""),
        "record_url": _record_url(model, record_id),
    }, None


# ── partners: update + archive ─────────────────────────────────────────────

def odoo_update_partner(inputs, stamp):
    partner_id = _req_int(inputs, "partner_id")

    vals = {}
    for field in ("name", "email", "phone", "street", "city", "zip", "vat"):
        v = _opt_str(inputs, field)
        if v:
            vals[field] = v
    if not vals:
        raise RuntimeError(
            "nothing to update — pass at least one of name, email, phone, "
            "street, city, zip, vat"
        )

    before = _rpc("res.partner", "read", [[partner_id]],
                  {"fields": ["name"] + list(vals.keys())}) or []
    if not before:
        raise RuntimeError("res.partner %d not found" % partner_id)

    _rpc("res.partner", "write", [[partner_id], vals])

    after = _rpc("res.partner", "read", [[partner_id]],
                 {"fields": ["name"] + list(vals.keys())}) or [{}]

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "partner_id": partner_id,
        "updated_fields": sorted(vals.keys()),
        "before": before[0],
        "after": after[0],
        "record_url": _record_url("res.partner", partner_id),
    }, None


def odoo_archive_partner(inputs, stamp):
    """Archive (active=False) rather than delete. Odoo blocks unlink on any
    partner with accounting history anyway, and archiving is reversible —
    deletion would not be."""
    partner_id = _req_int(inputs, "partner_id")

    before = _rpc("res.partner", "read", [[partner_id]],
                  {"fields": ["name", "active"]}) or []
    if not before:
        raise RuntimeError("res.partner %d not found" % partner_id)
    rec = before[0]
    if not rec.get("active"):
        raise RuntimeError("partner %s is already archived" % rec.get("name"))

    reason = _opt_str(inputs, "reason")
    if reason:
        _rpc("res.partner", "message_post", [[partner_id]],
             {"body": "Archived via RailCall: " + reason,
              "message_type": "comment", "subtype_xmlid": "mail.mt_note"})

    _rpc("res.partner", "write", [[partner_id], {"active": False}])

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "partner_id": partner_id,
        "name": rec.get("name", ""),
        "active": False,
        "reason": reason or "",
        "note": "Archived, not deleted — re-activate in Odoo by clearing the Archived filter.",
        "record_url": _record_url("res.partner", partner_id),
    }, None


# ── products ───────────────────────────────────────────────────────────────

def odoo_create_product(inputs, stamp):
    name = _req_str(inputs, "name")

    vals = {"name": name}
    code = _opt_str(inputs, "default_code")
    if code:
        vals["default_code"] = code
    desc = _opt_str(inputs, "description_sale")
    if desc:
        vals["description_sale"] = desc

    ptype = (_opt_str(inputs, "type") or "consu").lower()
    if ptype not in ("consu", "service", "product"):
        raise RuntimeError(
            "type must be one of: consu (goods), service, product (storable) — got %r" % ptype
        )
    vals["type"] = ptype

    for field in ("list_price", "standard_price"):
        v = inputs.get(field)
        if v not in (None, ""):
            try:
                vals[field] = float(v)
            except Exception:
                raise RuntimeError("%s must be a number" % field)
            if vals[field] < 0:
                raise RuntimeError("%s cannot be negative" % field)

    product_id = _rpc("product.product", "create", [vals])
    if not isinstance(product_id, int):
        raise RuntimeError("Odoo did not return a product id (got: %r)" % (product_id,))

    read = _rpc("product.product", "read", [[product_id]],
                {"fields": ["name", "default_code", "list_price", "standard_price", "type"]}) or [{}]
    rec = read[0]

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "product_id": product_id,
        "name": rec.get("name", ""),
        "default_code": rec.get("default_code") or "",
        "list_price": rec.get("list_price", 0),
        "standard_price": rec.get("standard_price", 0),
        "type": rec.get("type", ""),
        "record_url": _record_url("product.product", product_id),
    }, None


def odoo_update_product_price(inputs, stamp):
    """Price changes flow straight into every new quotation, so this carries
    the same drift guard the irreversible money commands use: pass
    expected_current_price and it refuses if someone moved the price between
    approval and execution."""
    product_id = _req_int(inputs, "product_id")

    new_price = inputs.get("list_price")
    try:
        new_price = float(new_price)
    except Exception:
        raise RuntimeError("list_price must be a number")
    if new_price < 0:
        raise RuntimeError("list_price cannot be negative")

    before = _rpc("product.product", "read", [[product_id]],
                  {"fields": ["name", "default_code", "list_price"]}) or []
    if not before:
        raise RuntimeError("product.product %d not found" % product_id)
    rec = before[0]
    old_price = float(rec.get("list_price") or 0)

    expected = inputs.get("expected_current_price")
    if expected not in (None, ""):
        try:
            expected = float(expected)
        except Exception:
            raise RuntimeError("expected_current_price must be a number")
        if abs(old_price - expected) > 0.01:
            raise RuntimeError(
                "refusing to reprice %s: approved against %.2f but the live price "
                "is %.2f — re-approve against the current record"
                % (rec.get("name"), expected, old_price)
            )

    _rpc("product.product", "write", [[product_id], {"list_price": new_price}])

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "product_id": product_id,
        "name": rec.get("name", ""),
        "default_code": rec.get("default_code") or "",
        "old_price": old_price,
        "new_price": new_price,
        "delta": round(new_price - old_price, 2),
        "record_url": _record_url("product.product", product_id),
    }, None


# ── CRM ────────────────────────────────────────────────────────────────────

def odoo_create_lead(inputs, stamp):
    name = _req_str(inputs, "name")

    vals = {"name": name}
    # type='opportunity' puts it straight on the pipeline; 'lead' keeps it in
    # the pre-qualification inbox. Odoo defaults to lead when the CRM
    # "leads" setting is on, so we set it explicitly rather than guessing.
    vals["type"] = "opportunity" if inputs.get("is_opportunity") else "lead"

    if inputs.get("partner_id") not in (None, ""):
        vals["partner_id"] = _req_int(inputs, "partner_id")
    for field in ("contact_name", "email_from", "phone", "description"):
        v = _opt_str(inputs, field)
        if v:
            vals[field] = v
    rev = inputs.get("expected_revenue")
    if rev not in (None, ""):
        try:
            vals["expected_revenue"] = float(rev)
        except Exception:
            raise RuntimeError("expected_revenue must be a number")

    lead_id = _rpc("crm.lead", "create", [vals])
    if not isinstance(lead_id, int):
        raise RuntimeError("Odoo did not return a crm.lead id (got: %r)" % (lead_id,))

    read = _rpc("crm.lead", "read", [[lead_id]],
                {"fields": ["name", "type", "stage_id", "expected_revenue"]}) or [{}]
    rec = read[0]
    stage = rec.get("stage_id")

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "lead_id": lead_id,
        "name": rec.get("name", ""),
        "type": rec.get("type", ""),
        "stage": stage[1] if isinstance(stage, list) and len(stage) > 1 else "",
        "expected_revenue": rec.get("expected_revenue", 0),
        "record_url": _record_url("crm.lead", lead_id),
    }, None


def odoo_update_lead_stage(inputs, stamp):
    lead_id = _req_int(inputs, "lead_id")

    before = _rpc("crm.lead", "read", [[lead_id]], {"fields": ["name", "stage_id"]}) or []
    if not before:
        raise RuntimeError("crm.lead %d not found" % lead_id)
    rec = before[0]
    old_stage = rec.get("stage_id")

    if inputs.get("stage_id") not in (None, ""):
        stage_id = _req_int(inputs, "stage_id")
    else:
        stage_name = _opt_str(inputs, "stage_name")
        if not stage_name:
            raise RuntimeError("provide a stage_id or a stage_name")
        found = _rpc("crm.stage", "search_read", [[["name", "=ilike", stage_name]]],
                     {"fields": ["id", "name"], "limit": 2}) or []
        if not found:
            available = _rpc("crm.stage", "search_read", [[]],
                             {"fields": ["name"], "limit": 20}) or []
            raise RuntimeError(
                "no CRM stage named %r — available: %s"
                % (stage_name, ", ".join(s["name"] for s in available) or "(none)")
            )
        if len(found) > 1:
            raise RuntimeError(
                "stage name %r is ambiguous (%d matches) — pass stage_id instead"
                % (stage_name, len(found))
            )
        stage_id = found[0]["id"]

    _rpc("crm.lead", "write", [[lead_id], {"stage_id": stage_id}])

    after = _rpc("crm.lead", "read", [[lead_id]], {"fields": ["stage_id"]}) or [{}]
    new_stage = after[0].get("stage_id")

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "lead_id": lead_id,
        "name": rec.get("name", ""),
        "stage_before": old_stage[1] if isinstance(old_stage, list) and len(old_stage) > 1 else "",
        "stage_after": new_stage[1] if isinstance(new_stage, list) and len(new_stage) > 1 else "",
        "record_url": _record_url("crm.lead", lead_id),
    }, None


# ── projects ───────────────────────────────────────────────────────────────

def odoo_create_project_task(inputs, stamp):
    name = _req_str(inputs, "name")
    project_id = _req_int(inputs, "project_id")

    proj = _rpc("project.project", "read", [[project_id]], {"fields": ["name"]}) or []
    if not proj:
        raise RuntimeError("project.project %d not found" % project_id)

    vals = {"name": name, "project_id": project_id}
    desc = _opt_str(inputs, "description")
    if desc:
        vals["description"] = desc
    deadline = _opt_str(inputs, "date_deadline")
    if deadline:
        vals["date_deadline"] = deadline
    if inputs.get("partner_id") not in (None, ""):
        vals["partner_id"] = _req_int(inputs, "partner_id")
    hours = inputs.get("planned_hours")
    if hours not in (None, ""):
        try:
            vals["planned_hours"] = float(hours)
        except Exception:
            raise RuntimeError("planned_hours must be a number")

    task_id = _rpc("project.task", "create", [vals])
    if not isinstance(task_id, int):
        raise RuntimeError("Odoo did not return a project.task id (got: %r)" % (task_id,))

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "task_id": task_id,
        "name": name,
        "project": proj[0].get("name", ""),
        "project_id": project_id,
        "record_url": _record_url("project.task", task_id),
    }, None


# ── accounting reference data ──────────────────────────────────────────────

def odoo_list_journals(inputs, stamp):
    """Journal ids are needed by register_payment and create_credit_note, and
    they differ per database — so buyers need a way to look them up without
    opening the Odoo UI."""
    domain = []
    jtype = _opt_str(inputs, "type")
    if jtype:
        allowed = ("sale", "purchase", "cash", "bank", "general")
        if jtype.lower() not in allowed:
            raise RuntimeError("type must be one of: " + ", ".join(allowed))
        domain.append(["type", "=", jtype.lower()])

    limit = inputs.get("limit")
    try:
        limit = max(1, min(int(limit), 100)) if limit not in (None, "") else 50
    except Exception:
        limit = 50

    journals = _rpc("account.journal", "search_read", [domain],
                    {"fields": ["id", "name", "code", "type", "currency_id"],
                     "limit": limit, "order": "type,name"}) or []

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "journals": journals,
        "count": len(journals),
        "filtered_by_type": jtype or "",
    }, None


# ── attachments ────────────────────────────────────────────────────────────

def odoo_attach_file(inputs, stamp):
    import base64 as _b64

    model = _req_str(inputs, "model")
    if not all(ch.isalnum() or ch in "._" for ch in model):
        raise RuntimeError("model must be a dotted Odoo model name, e.g. account.move")
    record_id = _req_int(inputs, "record_id")
    filename = _req_str(inputs, "filename")
    content = _req_str(inputs, "content_base64")

    try:
        raw = _b64.b64decode(content, validate=True)
    except Exception as e:
        raise RuntimeError("content_base64 is not valid base64: %s" % str(e)[:120])
    if not raw:
        raise RuntimeError("content_base64 decoded to zero bytes")
    MAX = 25 * 1024 * 1024
    if len(raw) > MAX:
        raise RuntimeError(
            "attachment is %.1f MB — over the %d MB cap this module enforces"
            % (len(raw) / 1048576.0, MAX // 1048576)
        )

    exists = _rpc(model, "search_read", [[["id", "=", record_id]]],
                  {"fields": ["id"], "limit": 1}) or []
    if not exists:
        raise RuntimeError("%s %d not found" % (model, record_id))

    vals = {
        "name": filename,
        "datas": content,
        "res_model": model,
        "res_id": record_id,
    }
    mimetype = _opt_str(inputs, "mimetype")
    if mimetype:
        vals["mimetype"] = mimetype

    attachment_id = _rpc("ir.attachment", "create", [vals])
    if not isinstance(attachment_id, int):
        raise RuntimeError("Odoo did not return an ir.attachment id (got: %r)" % (attachment_id,))

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "attachment_id": attachment_id,
        "filename": filename,
        "bytes": len(raw),
        "model": model,
        "record_id": record_id,
        "record_url": _record_url(model, record_id),
    }, None


# ── outbound email ─────────────────────────────────────────────────────────

def odoo_send_invoice_email(inputs, stamp):
    """Send a posted invoice to the customer using Odoo's own mail template,
    so the buyer's branding, layout and PDF attachment are exactly what the
    Odoo UI would send. Irreversible — an email cannot be unsent, which is
    why this is approval-gated and refuses on a draft invoice."""
    invoice_id = _req_int(inputs, "invoice_id")

    inv = _rpc("account.move", "read", [[invoice_id]],
               {"fields": ["name", "state", "move_type", "partner_id", "amount_total"]}) or []
    if not inv:
        raise RuntimeError("account.move %d not found" % invoice_id)
    rec = inv[0]
    if rec.get("move_type") != "out_invoice":
        raise RuntimeError(
            "account.move %d is a '%s' — this sends customer invoices only"
            % (invoice_id, rec.get("move_type"))
        )
    if rec.get("state") != "posted":
        raise RuntimeError(
            "invoice %s is in state '%s' — refusing to email a document that "
            "isn't posted" % (rec.get("name"), rec.get("state"))
        )

    partner = rec.get("partner_id")
    partner_id = partner[0] if isinstance(partner, list) and partner else 0
    recipient = ""
    if partner_id:
        pr = _rpc("res.partner", "read", [[partner_id]], {"fields": ["email", "name"]}) or [{}]
        recipient = pr[0].get("email") or ""
    if not recipient:
        raise RuntimeError(
            "customer on %s has no email address — set one before sending"
            % rec.get("name")
        )

    if inputs.get("template_id") not in (None, ""):
        template_id = _req_int(inputs, "template_id")
        tpl = _rpc("mail.template", "read", [[template_id]], {"fields": ["name", "model_id"]}) or []
        if not tpl:
            raise RuntimeError("mail.template %d not found" % template_id)
        template_name = tpl[0].get("name", "")
    else:
        # Template xmlids drift between Odoo versions, so resolve by model
        # instead of hardcoding account.email_template_edi_invoice.
        found = _rpc("mail.template", "search_read", [[["model", "=", "account.move"]]],
                     {"fields": ["id", "name"], "limit": 5, "order": "id"}) or []
        if not found:
            raise RuntimeError(
                "no mail.template registered for account.move on this database — "
                "pass template_id explicitly"
            )
        template_id = found[0]["id"]
        template_name = found[0].get("name", "")

    force = inputs.get("force_send")
    force_send = True if force in (None, "") else bool(force)

    mail_id = _rpc("mail.template", "send_mail", [[template_id], invoice_id],
                   {"force_send": force_send})

    return {
        "ok": True,
        "loaded_from": "module:odoo",
        "invoice_id": invoice_id,
        "invoice_name": rec.get("name", ""),
        "template_used": template_name,
        "template_id": template_id,
        "mail_id": mail_id if isinstance(mail_id, int) else 0,
        "recipient": recipient,
        "amount_total": rec.get("amount_total", 0),
        "queued_only": not force_send,
        "record_url": _record_url("account.move", invoice_id),
    }, None
