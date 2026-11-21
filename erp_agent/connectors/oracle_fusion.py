"""Oracle Fusion connector.

MockOracleFusion stands in for a customer's Oracle Fusion Cloud instance. The `invoices` and
`invoiceHolds` resources use the attribute names from Oracle's published Payables REST API
(InvoiceId, InvoiceNumber, InvoiceAmount, InvoiceCurrency, Supplier, SupplierNumber,
PurchaseOrderNumber, ValidationStatus, ApprovalStatus, PaidStatus, TermsDate; HoldId, HoldName,
HoldReason, ReleaseName, ReleaseReason, ReleaseDate) and return collections as
{"items": [...], "count": n, "hasMore": false}.
The other resources (approvalTasks, users, suppliers, purchaseOrders, payments, accessApprovals,
bankReconciliation, integrationJobs) are simplified stand-ins for the approval worklist, HCM,
procurement, security console and cash management, which live in other Oracle APIs.

OracleFusionConnector is the only code that knows Oracle's field names. It turns Oracle payloads
into the plain records the rest of the agent uses, and turns writes into REST-style calls. To
deploy on another ERP (Workday, SAP) you write another connector; the agent, the rules and the
runbooks stay the same.
"""
import copy
import re
from datetime import date

from ..errors import TransientERPError

API = "/fscmRestApi/resources/11.13.18.05"


class MockOracleFusion:
    """In-memory REST-style API. Every call is recorded, like an API access log."""

    KEYS = {
        "invoices": "InvoiceId", "invoiceHolds": "HoldId", "approvalTasks": "TaskId",
        "users": "PersonNumber", "suppliers": "SupplierNumber", "purchaseOrders": "OrderNumber",
        "payments": "PaymentNumber", "accessApprovals": "ApprovalId",
        "bankReconciliation": "ItemId", "integrationJobs": "JobId",
    }

    def __init__(self, data):
        self.data = copy.deepcopy(data)
        self.calls = []
        self._next_id = 900000
        self.writes = 0    # writes actually applied (tells a clean rejection from a partial one)
        self.faults = []   # HTTP statuses the next writes fail with, e.g. [503, 429] (fault injection for tests)

    def inject_faults(self, *statuses):
        """Make the next writes fail as the ERP would when it rejects a request before applying it."""
        self.faults.extend(statuses)

    def _maybe_fail(self, method, path):
        if self.faults:
            status = self.faults.pop(0)
            self.calls.append((method, path, {"rejected": status}))
            raise TransientERPError(f"HTTP {status} from {method} {path}; nothing was applied")

    def _items(self, resource):
        return self.data.setdefault(resource, [])

    def get(self, resource, key=None, **q):
        """GET /resource/{key} or GET /resource?q=Field=value;Field2=value2"""
        self.calls.append(("GET", f"{API}/{resource}" + (f"/{key}" if key is not None else ""), q))
        items = self._items(resource)
        if key is not None:
            k = self.KEYS[resource]
            for it in items:
                if str(it[k]) == str(key):
                    return copy.deepcopy(it)
            return None
        rows = [copy.deepcopy(it) for it in items if all(str(it.get(f)) == str(v) for f, v in q.items())]
        return {"items": rows, "count": len(rows), "hasMore": False}

    def post(self, resource, body):
        self._maybe_fail("POST", f"{API}/{resource}")
        self.writes += 1
        self.calls.append(("POST", f"{API}/{resource}", body))
        k = self.KEYS[resource]
        if k not in body:
            self._next_id += 1
            body = {k: self._next_id, **body}
        self._items(resource).append(copy.deepcopy(body))
        return copy.deepcopy(body)

    def patch(self, resource, key, body):
        self._maybe_fail("PATCH", f"{API}/{resource}/{key}")
        self.writes += 1
        self.calls.append(("PATCH", f"{API}/{resource}/{key}", body))
        k = self.KEYS[resource]
        for it in self._items(resource):
            if str(it[k]) == str(key):
                it.update(copy.deepcopy(body))
                return copy.deepcopy(it)
        return None


def norm_invoice_number(s):
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


class OracleFusionConnector:
    def __init__(self, client, as_of, business_unit):
        self.c = client
        self.as_of = date.fromisoformat(as_of)
        self.business_unit = business_unit

    def _days_since(self, d):
        return (self.as_of - date.fromisoformat(d)).days if d else None

    # ---------- reads ----------
    def _invoice_rows(self, invoice_number):
        return self.c.get("invoices", InvoiceNumber=invoice_number)["items"]

    def invoice(self, invoice_number):
        rows = self._invoice_rows(invoice_number)
        if not rows:
            return None
        inv = rows[0]
        holds = self.c.get("invoiceHolds", InvoiceNumber=inv["InvoiceNumber"], Supplier=inv["Supplier"])["items"]
        tasks = self.c.get("approvalTasks", InvoiceId=inv["InvoiceId"])["items"]
        task = tasks[-1] if tasks else None
        return {
            "invoice_number": inv["InvoiceNumber"], "invoice_id": inv["InvoiceId"],
            "supplier": inv["Supplier"], "supplier_number": inv["SupplierNumber"],
            "po_number": inv.get("PurchaseOrderNumber"), "amount": inv["InvoiceAmount"],
            "currency": inv["InvoiceCurrency"], "invoice_date": inv.get("InvoiceDate"),
            "description": inv.get("Description", ""), "requester": inv.get("Requester"),
            "validation_status": inv["ValidationStatus"], "approval_status": inv["ApprovalStatus"],
            "paid_status": inv["PaidStatus"], "amount_paid": inv.get("AmountPaid", 0),
            "terms_date": inv.get("TermsDate"),
            "open_holds": [{"hold_id": h["HoldId"], "hold_name": h["HoldName"], "hold_reason": h.get("HoldReason")}
                           for h in holds if not h.get("ReleaseDate")],
            "approval_task": None if not task else {
                "task_id": task["TaskId"], "state": task["State"], "assignee": task.get("Assignee"),
                "days_with_assignee": self._days_since(task.get("AssignedDate")),
                "outcome_comment": task.get("Comments"), "reason_code": task.get("ReasonCode"),
                "error_message": task.get("ErrorMessage"),
            },
        }

    def invoices_for_supplier(self, supplier_number):
        return self.c.get("invoices", SupplierNumber=supplier_number)["items"]

    def user(self, person_number):
        u = self.c.get("users", person_number)
        if not u:
            return None
        return {"person_number": u["PersonNumber"], "name": u["DisplayName"], "job": u.get("Job"),
                "status": u["AssignmentStatus"], "manager": u.get("ManagerPersonNumber"),
                "approval_limit": u.get("ApprovalLimit", 0), "erp_roles": u.get("Roles", [])}

    def supplier(self, supplier_number):
        s = self.c.get("suppliers", supplier_number)
        return None if not s else {"supplier_number": s["SupplierNumber"], "name": s["Supplier"], "status": s["Status"]}

    def purchase_order(self, po_number):
        p = self.c.get("purchaseOrders", po_number)
        return None if not p else {"po_number": p["OrderNumber"], "supplier_number": p["SupplierNumber"],
                                   "amount": p["Total"], "currency": p["Currency"], "status": p["Status"]}

    def payments(self, invoice_id):
        return [{"payment_number": p["PaymentNumber"], "payment_date": p["PaymentDate"], "amount": p["Amount"],
                 "method": p.get("PaymentMethod")} for p in self.c.get("payments", InvoiceId=invoice_id)["items"]]

    def access_approval(self, approval_id):
        a = self.c.get("accessApprovals", approval_id)
        return None if not a else {"approval_id": a["ApprovalId"], "user": a["PersonNumber"], "role": a["Role"],
                                   "approved_by": a.get("ApprovedBy"), "status": a["Status"]}

    def recon_item(self, item_id):
        r = self.c.get("bankReconciliation", item_id)
        if not r:
            return None
        return {"item_id": r["ItemId"], "bank_amount": r["BankAmount"], "ledger_amount": r["LedgerAmount"],
                "currency": r["Currency"], "cause": r.get("SuggestedCause", "unknown"), "status": r["Status"],
                "difference": round(r["BankAmount"] - r["LedgerAmount"], 2)}

    def integration_job(self, job_id):
        j = self.c.get("integrationJobs", job_id)
        return None if not j else {"job_id": j["JobId"], "name": j["JobName"], "status": j["Status"],
                                   "last_run": j.get("LastRun"), "error": j.get("ErrorMessage")}

    # ---------- writes ----------
    def set_task(self, task_id, **fields):
        return self.c.patch("approvalTasks", task_id, fields)

    def new_task(self, invoice_id, assignee):
        return self.c.post("approvalTasks", {"InvoiceId": invoice_id, "State": "ASSIGNED", "Assignee": assignee,
                                             "AssignedDate": self.as_of.isoformat()})

    def set_invoice_status(self, invoice_id, **fields):
        # ApprovalStatus and ValidationStatus are read-only in Oracle's REST API; the real system changes
        # them through invoice actions (validate, initiate approval). The mock applies them directly.
        mapping = {"approval_status": "ApprovalStatus", "validation_status": "ValidationStatus"}
        return self.c.patch("invoices", invoice_id, {mapping[k]: v for k, v in fields.items()})

    def create_hold(self, inv, hold_name, reason):
        return self.c.post("invoiceHolds", {
            "InvoiceNumber": inv["invoice_number"], "Supplier": inv["supplier"], "BusinessUnit": self.business_unit,
            "HoldName": hold_name, "HoldReason": reason, "HoldDate": self.as_of.isoformat(), "HeldBy": "svc_agent"})

    def release_hold(self, hold_id, release_name, reason):
        return self.c.patch("invoiceHolds", hold_id, {"ReleaseName": release_name, "ReleaseReason": reason,
                                                      "ReleaseDate": self.as_of.isoformat()})

    def set_roles(self, person_number, roles):
        return self.c.patch("users", person_number, {"Roles": roles})

    def match_recon(self, item_id, new_ledger_amount):
        return self.c.patch("bankReconciliation", item_id, {"LedgerAmount": new_ledger_amount, "Status": "MATCHED"})
