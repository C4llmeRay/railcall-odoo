# 60-second demo — shot list

For the listing's `video_url`. One governed command, start to finish. The point
to land: **the module refuses a write that drifted after approval.** Anything
can create a record in Odoo; refusing is the part worth sixty seconds.

Record at **1280×720 or larger**, terminal font bumped up two or three sizes.
No audio needed — on-screen captions carry it. Upload unlisted to YouTube and
paste the link into the listing's `video_url`.

**Before you hit record:** Station running, `odoo` vault entry saved, a
throwaway Odoo database open in a second tab, and a draft sales order already
created so you are not filming yourself typing line items.

---

### 0:00–0:08 · What it is

Studio open on the `ray9/odoo` command list. Scroll it once, slowly, so the
count reads.

> **Caption:** 26 governed Odoo commands. 4 read-only, 22 approval-gated.

---

### 0:08–0:18 · A read that just works

Run `odoo.find_partner` with `{"email": "…", "limit": 5}`. Result comes back
immediately, no gate.

> **Caption:** Reads need no approval.

---

### 0:18–0:30 · The airlock

Open `odoo.confirm_sale_order`. Enter the real `order_id` **and** the correct
`expected_amount_total`. Hit preview.

Let the airlock card sit on screen for a full three seconds — the reviewer
needs to read it. Point at the total.

> **Caption:** Preview → approve → execute. Nothing has reached Odoo yet.

---

### 0:30–0:45 · It refuses

**This is the shot.** Do not rush it.

Change `expected_amount_total` to something obviously wrong — `999999`. Approve
and execute.

It refuses. Cut to the Odoo tab: the order is still a draft quotation.

> **Caption:** The total moved after approval. The module re-read the live
> record and stopped.

---

### 0:45–0:55 · Then it works

Set `expected_amount_total` back to the correct value. Approve, execute. The
order confirms. Cut to Odoo — state is now *Sales Order*.

> **Caption:** Correct total. Confirmed.

---

### 0:55–1:00 · The receipt

`railcall receipts list`, then `railcall verify`.

> **Caption:** Every path — including the refusal — is signed.

---

## Notes

- **Blur or fake the Odoo URL** if the database name is identifiable. It
  appears in every `record_url` the module returns.
- Never let the API key on screen. The vault form masks it; the terminal does
  not if you ever `cat` the vault.
- Do not film against production. §5 of [TESTING.md](TESTING.md) posts to a
  ledger, and posting is not reversible.
- If sixty seconds is tight, cut 0:08–0:18. The read-only shot is the least
  interesting thing in the video.
