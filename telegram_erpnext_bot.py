"""
Telegram → ERPNext bot (same command surface as the Invoice Ninja / SolidInvoice bots).

Env (Coolify)
-------------
    TELEGRAM_BOT_TOKEN or TG_TOKEN
    ERPNEXT_URL              https://erp.yourdomain.com   (no trailing slash)
    ERPNEXT_API_KEY
    ERPNEXT_API_SECRET
    ALLOWED_CHAT_IDS         comma-separated Telegram user/chat ids (required in prod)

    ERPNEXT_COMPANY          optional; default company on the site
    ERPNEXT_CUSTOMER_GROUP   default "All Customer Groups"
    ERPNEXT_TERRITORY        default "All Territories"
    ERPNEXT_ITEM_GROUP       default "All Item Groups"
    ERPNEXT_UOM              default "Nos"
    ERPNEXT_CURRENCY         display only, default JMD

First-run on a new Company still needs Chart of Accounts + Selling Settings
in ERPNext. The bot cannot invent those.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from typing import Any

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("erp-bot")

TG_TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TG_TOKEN") or "").strip()
ERP_URL = os.environ.get("ERPNEXT_URL", "").strip().rstrip("/")
ERP_KEY = os.environ.get("ERPNEXT_API_KEY", "").strip()
ERP_SECRET = os.environ.get("ERPNEXT_API_SECRET", "").strip()
ALLOWED_CHAT_IDS = {
    int(x.strip())
    for x in os.environ.get("ALLOWED_CHAT_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

ERP_COMPANY = os.environ.get("ERPNEXT_COMPANY", "").strip()
ERP_CUSTOMER_GROUP = os.environ.get("ERPNEXT_CUSTOMER_GROUP", "All Customer Groups").strip()
ERP_TERRITORY = os.environ.get("ERPNEXT_TERRITORY", "All Territories").strip()
ERP_ITEM_GROUP = os.environ.get("ERPNEXT_ITEM_GROUP", "All Item Groups").strip()
ERP_UOM = os.environ.get("ERPNEXT_UOM", "Nos").strip() or "Nos"
ERP_CURRENCY = os.environ.get("ERPNEXT_CURRENCY", "JMD").strip() or "JMD"

TIMEOUT = 90
DRAFTS: dict[int, dict[str, Any]] = {}

HELP_TEXT = """\
*ERPNext bot*

Quotes and invoices live in ERPNext. This chat drives them.
Nothing is saved until you reply *YES*.

*Setup*
/start /help — this message
/whoami — your Telegram id (put it in ALLOWED\\_CHAT\\_IDS)
/ping — can the bot reach ERPNext?

*Customers*
/client Name, email, phone
    Create a Customer and select them for the next document.
/client Jane
    Find Jane and select her.
/clients
    Recent customers.
/use Jane
    Select an existing customer.

*Start a document*
/quote — start a quotation draft (clears lines)
/invoice — start an invoice draft (clears lines)

*Lines*
Format: `name, description, price, qty`

/item Oil filter, car oil filter, 4275, 5

Several lines in one message:
```
/item
Oil filter, car oil filter, 4275, 5
Cabin air filter, premium filter, 3365, 2
```

One line, groups of 4:
/item Oil filter, car oil filter, 4275, 5, Cabin air filter, premium filter, 3365, 2

New item names are created in ERPNext as non-stock Items.
/items — show the draft
/undo — drop the last line
/cancel — drop the draft

*Save*
Reply *YES* or *Y* to submit.
Reply *NO* to discard.

*Look up*
/quote SAL-QTN-2026-00001
/invoice ACC-SINV-2026-00001
    Summary + PDF. Use the name ERPNext printed.

/convert SAL-QTN-2026-00001
    Submitted quotation → submitted Sales Invoice.

*Money*
/unpaid
/unpaid Jane
/paid ACC-SINV-2026-00001

Do not put commas inside a name or description. 15k means 15000.
"""


def require_env() -> None:
    missing = [
        n
        for n, v in (
            ("TELEGRAM_BOT_TOKEN or TG_TOKEN", TG_TOKEN),
            ("ERPNEXT_URL", ERP_URL),
            ("ERPNEXT_API_KEY", ERP_KEY),
            ("ERPNEXT_API_SECRET", ERP_SECRET),
        )
        if not v
    ]
    if missing:
        raise SystemExit("Missing env: " + ", ".join(missing))


def authorized(update: Update) -> bool:
    if not update.effective_chat:
        return False
    if not ALLOWED_CHAT_IDS:
        return True
    return update.effective_chat.id in ALLOWED_CHAT_IDS


def headers() -> dict[str, str]:
    return {
        "Authorization": f"token {ERP_KEY}:{ERP_SECRET}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def erp_request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_data: Any = None,
    expect_json: bool = True,
) -> Any:
    path = path.lstrip("/")
    if not path.startswith("api/"):
        path = f"api/{path}"
    url = f"{ERP_URL}/{path}"
    resp = requests.request(
        method,
        url,
        headers=headers(),
        params=params,
        json=json_data,
        timeout=TIMEOUT,
    )
    if not resp.ok:
        snippet = (resp.text or "")[:500]
        raise RuntimeError(f"ERPNext {resp.status_code} {path}: {snippet}")
    if not expect_json:
        return resp.content
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"_raw": resp.text}


def unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    if isinstance(payload, dict) and "message" in payload:
        return payload["message"]
    return payload


def parse_number(raw: str) -> float:
    cleaned = (
        raw.strip()
        .replace(",", "")
        .replace("$", "")
        .replace("J$", "")
        .replace("j$", "")
    )
    if cleaned.lower().endswith("k") and cleaned[:-1].replace(".", "", 1).isdigit():
        return float(cleaned[:-1]) * 1000
    return float(cleaned)


def parse_item_groups(blob: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    lines = [ln.strip() for ln in blob.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("No items.")
    for line in lines:
        parts = [p.strip() for p in line.split(",") if p.strip() or p == ""]
        parts = [p.strip() for p in line.split(",")]
        parts = [p.strip() for p in parts]
        # keep empty description
        nonempty = line.split(",")
        bits = [b.strip() for b in nonempty]
        if len(bits) == 3:
            groups = [bits]
        elif len(bits) >= 4 and len(bits) % 4 == 0:
            groups = [bits[i : i + 4] for i in range(0, len(bits), 4)]
        elif len(bits) == 4:
            groups = [bits]
        else:
            raise ValueError(
                f"Need name, description, price, qty — got: {line}"
            )
        for group in groups:
            if len(group) == 3:
                name, price_s, qty_s = group
                desc = name
            else:
                name, desc, price_s, qty_s = group
                desc = desc or name
            if not name:
                raise ValueError("Item name is empty.")
            cost = parse_number(price_s)
            qty = parse_number(qty_s)
            if cost <= 0 or qty <= 0:
                raise ValueError("Price and qty must be greater than zero.")
            items.append(
                {
                    "name": name,
                    "description": desc,
                    "price": cost,
                    "qty": qty,
                }
            )
    return items


def command_body(update: Update, command: str) -> str:
    text = update.message.text or ""
    return re.sub(rf"^/{re.escape(command)}(@\w+)?\s*", "", text, count=1).strip()


def money(amount: Any) -> str:
    try:
        return f"{ERP_CURRENCY} {float(amount):,.2f}"
    except (TypeError, ValueError):
        return str(amount)


def item_code_from_name(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name.strip()).strip("-").upper()
    return (slug or "ITEM")[:140]


def format_draft(draft: dict[str, Any]) -> str:
    kind = "QUOTE" if draft.get("kind") == "quote" else "INVOICE"
    cust = draft.get("customer") or {}
    cname = cust.get("customer_name") or cust.get("name") or "(no customer — /client Name)"
    lines = [f"DRAFT {kind} — not saved", f"Customer: {cname}", ""]
    items = draft.get("items") or []
    if not items:
        lines.append("No items. /item Name, description, price, qty")
    else:
        total = 0.0
        for i, row in enumerate(items, start=1):
            lt = row["price"] * row["qty"]
            total += lt
            lines.append(
                f"{i}. {row['name']}\n"
                f"    {row['description']}\n"
                f"    {money(row['price'])} × {row['qty']:g} = {money(lt)}"
            )
        lines += ["", f"TOTAL  {money(total)}"]
    lines += ["", "YES to save    NO to discard    /undo last item"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ERPNext
# ---------------------------------------------------------------------------


def list_customers(query: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "fields": json.dumps(["name", "customer_name", "territory", "email_id"]),
        "limit_page_length": 20,
        "order_by": "modified desc",
    }
    if query:
        params["filters"] = json.dumps([["customer_name", "like", f"%{query}%"]])
    return unwrap(erp_request("GET", "resource/Customer", params=params)) or []


def create_customer(name: str, email: str, phone: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "customer_name": name,
        "customer_type": "Individual",
        "customer_group": ERP_CUSTOMER_GROUP,
        "territory": ERP_TERRITORY,
    }
    if email:
        body["email_id"] = email
    if phone:
        body["mobile_no"] = phone
    if ERP_COMPANY:
        body["default_company"] = ERP_COMPANY
    created = unwrap(erp_request("POST", "resource/Customer", json_data=body))
    # Best-effort Contact so quotations have a person.
    if email or phone:
        first, *rest = name.split()
        contact = {
            "first_name": first,
            "last_name": " ".join(rest),
            "links": [{"link_doctype": "Customer", "link_name": created.get("name") or name}],
        }
        if email:
            contact["email_ids"] = [{"email_id": email, "is_primary": 1}]
        if phone:
            contact["phone_nos"] = [{"phone": phone, "is_primary_phone": 1}]
        try:
            erp_request("POST", "resource/Contact", json_data=contact)
        except RuntimeError as exc:
            log.warning("Contact create skipped: %s", exc)
    return created


def ensure_item(name: str, description: str, rate: float) -> str:
    code = item_code_from_name(name)
    try:
        existing = unwrap(erp_request("GET", f"resource/Item/{code}"))
        if existing and existing.get("name"):
            return existing["name"]
    except RuntimeError:
        pass
    # Search by item_name
    found = unwrap(
        erp_request(
            "GET",
            "resource/Item",
            params={
                "filters": json.dumps([["item_name", "=", name]]),
                "fields": json.dumps(["name", "item_code"]),
                "limit_page_length": 1,
            },
        )
    )
    if found:
        return found[0].get("item_code") or found[0]["name"]
    body = {
        "item_code": code,
        "item_name": name,
        "description": description or name,
        "item_group": ERP_ITEM_GROUP,
        "stock_uom": ERP_UOM,
        "is_stock_item": 0,
        "is_sales_item": 1,
        "standard_rate": rate,
    }
    created = unwrap(erp_request("POST", "resource/Item", json_data=body))
    return created.get("item_code") or created.get("name") or code


def line_payloads(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in items:
        code = ensure_item(row["name"], row["description"], row["price"])
        out.append(
            {
                "item_code": code,
                "item_name": row["name"],
                "description": row["description"],
                "qty": row["qty"],
                "rate": row["price"],
                "uom": ERP_UOM,
            }
        )
    return out


def submit_doc(doctype: str, name: str) -> dict[str, Any]:
    doc = unwrap(erp_request("GET", f"resource/{doctype}/{name}"))
    doc["doctype"] = doctype
    result = erp_request("POST", "method/frappe.client.submit", json_data={"doc": doc})
    return unwrap(result) or doc


def create_quotation(customer: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    party = customer.get("name") or customer.get("customer_name")
    body: dict[str, Any] = {
        "quotation_to": "Customer",
        "party_name": party,
        "order_type": "Sales",
        "items": line_payloads(items),
    }
    if ERP_COMPANY:
        body["company"] = ERP_COMPANY
    created = unwrap(erp_request("POST", "resource/Quotation", json_data=body))
    name = created["name"]
    return submit_doc("Quotation", name)


def create_invoice(customer: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    party = customer.get("name") or customer.get("customer_name")
    body: dict[str, Any] = {
        "customer": party,
        "update_stock": 0,
        "items": line_payloads(items),
    }
    if ERP_COMPANY:
        body["company"] = ERP_COMPANY
    created = unwrap(erp_request("POST", "resource/Sales Invoice", json_data=body))
    return submit_doc("Sales Invoice", created["name"])


def get_doc(doctype: str, name: str) -> dict[str, Any]:
    return unwrap(erp_request("GET", f"resource/{doctype}/{name}"))


def find_named(doctype: str, number: str) -> dict[str, Any] | None:
    number = number.strip()
    try:
        return get_doc(doctype, number)
    except RuntimeError:
        pass
    rows = unwrap(
        erp_request(
            "GET",
            f"resource/{doctype}",
            params={
                "filters": json.dumps([["name", "like", f"%{number}%"]]),
                "limit_page_length": 5,
            },
        )
    ) or []
    if len(rows) == 1:
        return get_doc(doctype, rows[0]["name"])
    return None


def convert_quotation(name: str) -> dict[str, Any]:
    mapped = unwrap(
        erp_request(
            "POST",
            "method/erpnext.selling.doctype.quotation.quotation.make_sales_invoice",
            json_data={"source_name": name},
        )
    )
    if not isinstance(mapped, dict):
        raise RuntimeError("Convert did not return an invoice document. Is the quotation submitted?")
    mapped["doctype"] = "Sales Invoice"
    mapped["update_stock"] = 0
    if ERP_COMPANY and not mapped.get("company"):
        mapped["company"] = ERP_COMPANY
    created = unwrap(erp_request("POST", "resource/Sales Invoice", json_data=mapped))
    inv_name = created.get("name") if isinstance(created, dict) else None
    if not inv_name:
        # mapper sometimes returns the unsaved doc; insert already done above
        raise RuntimeError(f"Could not save converted invoice: {created}")
    return submit_doc("Sales Invoice", inv_name)


def unpaid_invoices(customer_query: str | None = None) -> list[dict[str, Any]]:
    filters: list[Any] = [["docstatus", "=", 1], ["outstanding_amount", ">", 0]]
    if customer_query:
        matches = list_customers(customer_query)
        names = [c["name"] for c in matches]
        if not names:
            return []
        if len(names) == 1:
            filters.append(["customer", "=", names[0]])
        else:
            filters.append(["customer", "in", names])
    return (
        unwrap(
            erp_request(
                "GET",
                "resource/Sales Invoice",
                params={
                    "fields": json.dumps(
                        [
                            "name",
                            "customer_name",
                            "outstanding_amount",
                            "grand_total",
                            "status",
                            "posting_date",
                        ]
                    ),
                    "filters": json.dumps(filters),
                    "limit_page_length": 25,
                    "order_by": "posting_date desc",
                },
            )
        )
        or []
    )


def mark_paid(invoice_name: str) -> dict[str, Any]:
    inv = get_doc("Sales Invoice", invoice_name)
    outstanding = float(inv.get("outstanding_amount") or inv.get("grand_total") or 0)
    if outstanding <= 0:
        return inv
    pe_src = unwrap(
        erp_request(
            "POST",
            "method/erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry",
            json_data={
                "dt": "Sales Invoice",
                "dn": invoice_name,
                "party_amount": outstanding,
            },
        )
    )
    if not isinstance(pe_src, dict):
        raise RuntimeError("Could not build Payment Entry. Check default cash/bank account.")
    pe_src["doctype"] = "Payment Entry"
    pe_src["reference_no"] = pe_src.get("reference_no") or f"BOT-{invoice_name}"
    pe_src["reference_date"] = pe_src.get("reference_date") or inv.get("posting_date")
    created = unwrap(erp_request("POST", "resource/Payment Entry", json_data=pe_src))
    submit_doc("Payment Entry", created["name"])
    return get_doc("Sales Invoice", invoice_name)


def download_pdf(doctype: str, name: str) -> bytes | None:
    params = {
        "doctype": doctype,
        "name": name,
        "format": "Standard",
        "no_letterhead": 0,
    }
    for path in (
        "method/frappe.utils.print_format.download_pdf",
        "method/frappe.utils.print.format.download_pdf",
    ):
        try:
            resp = requests.get(
                f"{ERP_URL}/api/{path}",
                headers={
                    "Authorization": f"token {ERP_KEY}:{ERP_SECRET}",
                    "Accept": "application/pdf",
                },
                params=params,
                timeout=TIMEOUT,
            )
            if resp.status_code == 200 and (
                "pdf" in resp.headers.get("Content-Type", "").lower()
                or resp.content[:5] == b"%PDF-"
            ):
                return resp.content
        except Exception:
            continue
    return None


def describe(doctype: str, doc: dict[str, Any]) -> str:
    name = doc.get("name", "?")
    status = doc.get("status") or ("Submitted" if doc.get("docstatus") == 1 else "Draft")
    total = doc.get("grand_total") or doc.get("rounded_total") or 0
    party = doc.get("customer_name") or doc.get("party_name") or doc.get("customer") or ""
    extra = ""
    if doctype == "Sales Invoice" and doc.get("outstanding_amount") is not None:
        extra = f"\nOutstanding: {money(doc.get('outstanding_amount'))}"
    return f"{doctype} *{name}* — {status} — {money(total)}\nCustomer: {party}{extra}"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


async def guard(update: Update) -> bool:
    if not update.message:
        return False
    if not authorized(update):
        await update.message.reply_text("This bot is locked to specific Telegram users.")
        return False
    return True


async def send_pdf(update: Update, doctype: str, doc: dict[str, Any]) -> None:
    name = doc.get("name") or "document"
    pdf = download_pdf(doctype, name)
    if not pdf:
        await update.message.reply_text(
            "Saved in ERPNext. PDF download failed — open the document there or check Print Format."
        )
        return
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf)
        path = tmp.name
    try:
        with open(path, "rb") as fh:
            await update.message.reply_document(document=fh, filename=f"{name}.pdf")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await update.message.reply_markdown(HELP_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    await update.message.reply_text(f"Your Telegram chat id: {update.effective_chat.id}")


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    try:
        data = erp_request("GET", "method/frappe.handler.ping")
        ok = unwrap(data) == "pong" or data.get("message") == "pong"
        await update.message.reply_text("ERPNext reachable." if ok else f"Unexpected: {data}")
    except RuntimeError as exc:
        await update.message.reply_text(f"ERPNext not reachable.\n{exc}")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    DRAFTS.pop(update.effective_chat.id, None)
    await update.message.reply_text("Draft cleared.")


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    draft = DRAFTS.get(update.effective_chat.id)
    if not draft or not draft.get("items"):
        await update.message.reply_text("Nothing to undo.")
        return
    draft["items"].pop()
    await update.message.reply_text(format_draft(draft))


async def cmd_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    draft = DRAFTS.get(update.effective_chat.id)
    if not draft:
        await update.message.reply_text("No draft. /quote or /invoice first.")
        return
    await update.message.reply_text(format_draft(draft))


async def cmd_clients(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    try:
        rows = list_customers()
    except RuntimeError as exc:
        await update.message.reply_text(str(exc))
        return
    if not rows:
        await update.message.reply_text("No customers. /client Name, email, phone")
        return
    lines = [f"• {r.get('customer_name')} ({r.get('name')})" for r in rows[:20]]
    await update.message.reply_text("\n".join(lines))


def select_customer(chat_id: int, row: dict[str, Any]) -> None:
    draft = DRAFTS.get(chat_id) or {}
    draft["customer"] = row
    DRAFTS[chat_id] = draft


async def cmd_client(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    raw = command_body(update, "client")
    if not raw:
        await update.message.reply_text("Usage: /client Name, email, phone   or   /client Jane")
        return
    parts = [p.strip() for p in raw.split(",")]
    chat_id = update.effective_chat.id
    if len(parts) == 1:
        try:
            rows = list_customers(parts[0])
        except RuntimeError as exc:
            await update.message.reply_text(str(exc))
            return
        if not rows:
            await update.message.reply_text("No match. Create with /client Name, email, phone")
            return
        if len(rows) > 1:
            names = ", ".join(r.get("customer_name", "?") for r in rows[:8])
            await update.message.reply_text(f"Several matches: {names}. Be more specific.")
            return
        select_customer(chat_id, rows[0])
        await update.message.reply_text(f"Using {rows[0].get('customer_name')}.")
        return
    name, email = parts[0], parts[1]
    phone = parts[2] if len(parts) > 2 else ""
    if email and "@" not in email:
        await update.message.reply_text("Second field must be an email (or leave it empty: Name,, phone).")
        return
    try:
        created = create_customer(name, email, phone)
    except RuntimeError as exc:
        await update.message.reply_text(str(exc))
        return
    select_customer(chat_id, created)
    await update.message.reply_text(
        f"Saved customer {created.get('customer_name') or created.get('name')}. Selected for the next quote/invoice."
    )


async def cmd_use(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    q = " ".join(context.args or []).strip()
    if not q:
        await update.message.reply_text("Usage: /use Jane")
        return
    try:
        rows = list_customers(q)
    except RuntimeError as exc:
        await update.message.reply_text(str(exc))
        return
    if not rows:
        await update.message.reply_text("No matching customer.")
        return
    if len(rows) > 1:
        names = ", ".join(r.get("customer_name", "?") for r in rows[:8])
        await update.message.reply_text(f"Several matches: {names}. Be more specific.")
        return
    select_customer(update.effective_chat.id, rows[0])
    await update.message.reply_text(f"Using {rows[0].get('customer_name')}.")


def start_kind(kind: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        arg = " ".join(context.args or []).strip()
        doctype = "Quotation" if kind == "quote" else "Sales Invoice"
        if arg:
            await show_document(update, doctype, arg)
            return
        chat_id = update.effective_chat.id
        draft = DRAFTS.get(chat_id) or {}
        draft["kind"] = kind
        draft["items"] = []
        DRAFTS[chat_id] = draft
        if not draft.get("customer"):
            await update.message.reply_text(
                f"{kind.title()} started. Pick a customer with /client or /use, then /item."
            )
            return
        await update.message.reply_text(
            f"{kind.title()} started for {draft['customer'].get('customer_name')}. Add /item, then YES."
        )

    return handler


async def show_document(update: Update, doctype: str, number: str) -> None:
    try:
        doc = find_named(doctype, number)
    except RuntimeError as exc:
        await update.message.reply_text(str(exc))
        return
    if not doc:
        await update.message.reply_text(f"No {doctype} named {number}.")
        return
    await update.message.reply_markdown(describe(doctype, doc))
    await send_pdf(update, doctype, doc)


async def cmd_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    chat_id = update.effective_chat.id
    draft = DRAFTS.get(chat_id)
    if not draft or not draft.get("kind"):
        await update.message.reply_text("Start with /quote or /invoice first.")
        return
    blob = command_body(update, "item")
    if not blob:
        draft["awaiting_items"] = True
        DRAFTS[chat_id] = draft
        await update.message.reply_text("Send items now (name, description, price, qty).")
        return
    try:
        rows = parse_item_groups(blob)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    draft.setdefault("items", []).extend(rows)
    draft["awaiting_items"] = False
    DRAFTS[chat_id] = draft
    await update.message.reply_text(format_draft(draft))


async def cmd_convert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    number = " ".join(context.args or []).strip()
    if not number:
        await update.message.reply_text("Usage: /convert SAL-QTN-2026-00001")
        return
    try:
        quote = find_named("Quotation", number)
        if not quote:
            await update.message.reply_text(f"Quotation {number} not found.")
            return
        if int(quote.get("docstatus") or 0) != 1:
            quote = submit_doc("Quotation", quote["name"])
        invoice = convert_quotation(quote["name"])
    except RuntimeError as exc:
        await update.message.reply_text(str(exc))
        return
    await update.message.reply_markdown("Converted.\n" + describe("Sales Invoice", invoice))
    await send_pdf(update, "Sales Invoice", invoice)


async def cmd_unpaid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    q = " ".join(context.args or []).strip() or None
    try:
        rows = unpaid_invoices(q)
    except RuntimeError as exc:
        await update.message.reply_text(str(exc))
        return
    if not rows:
        await update.message.reply_text("Nothing unpaid.")
        return
    lines = []
    for row in rows:
        lines.append(
            f"• {row.get('name')} — {row.get('customer_name')} — "
            f"{money(row.get('outstanding_amount'))} ({row.get('status')})"
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_paid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    number = " ".join(context.args or []).strip()
    if not number:
        await update.message.reply_text("Usage: /paid ACC-SINV-2026-00001")
        return
    try:
        inv = find_named("Sales Invoice", number)
        if not inv:
            await update.message.reply_text(f"Invoice {number} not found.")
            return
        updated = mark_paid(inv["name"])
    except RuntimeError as exc:
        await update.message.reply_text(str(exc))
        return
    await update.message.reply_markdown("Marked paid.\n" + describe("Sales Invoice", updated))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id
    draft = DRAFTS.get(chat_id)
    upper = text.upper()

    if upper in {"YES", "Y"}:
        if not draft or not draft.get("kind") or not draft.get("items"):
            await update.message.reply_text("No draft. /quote or /invoice then /item.")
            return
        if not draft.get("customer"):
            await update.message.reply_text("Pick a customer first: /client or /use.")
            return
        try:
            if draft["kind"] == "quote":
                doc = create_quotation(draft["customer"], draft["items"])
                doctype = "Quotation"
            else:
                doc = create_invoice(draft["customer"], draft["items"])
                doctype = "Sales Invoice"
        except RuntimeError as exc:
            await update.message.reply_text(str(exc))
            return
        DRAFTS.pop(chat_id, None)
        await update.message.reply_markdown("Saved.\n" + describe(doctype, doc))
        await send_pdf(update, doctype, doc)
        return

    if upper in {"NO", "N"}:
        DRAFTS.pop(chat_id, None)
        await update.message.reply_text("Draft discarded.")
        return

    if draft and draft.get("awaiting_items"):
        try:
            rows = parse_item_groups(text)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        draft.setdefault("items", []).extend(rows)
        draft["awaiting_items"] = False
        DRAFTS[chat_id] = draft
        await update.message.reply_text(format_draft(draft))


def main() -> None:
    require_env()
    log.info("Starting bot. ERPNext: %s", ERP_URL)
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("items", cmd_items))
    app.add_handler(CommandHandler("client", cmd_client))
    app.add_handler(CommandHandler("clients", cmd_clients))
    app.add_handler(CommandHandler("use", cmd_use))
    app.add_handler(CommandHandler("quote", start_kind("quote")))
    app.add_handler(CommandHandler("invoice", start_kind("invoice")))
    app.add_handler(CommandHandler("item", cmd_item))
    app.add_handler(CommandHandler("convert", cmd_convert))
    app.add_handler(CommandHandler("unpaid", cmd_unpaid))
    app.add_handler(CommandHandler("paid", cmd_paid))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
