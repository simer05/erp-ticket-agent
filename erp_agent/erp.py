"""Mock ERP system with server-side guardrails.

The agent never changes data directly. Every write goes through a function here,
and each function checks the business rules itself before it changes anything.
If the model asks for something the rules do not allow, the call is refused
with a reason, and nothing changes.
"""
import copy
import json
from pathlib import Path

SEED = Path(__file__).parent / "data" / "seed.json"

FX_ROUNDING_LIMIT = 5.00      # FIN-04
PO_TOLERANCE = 0.02           # AP-02
STUCK_AFTER_DAYS = 5          # AP-01
CONFLICTING_ROLES = {"AP_CLERK": "AP_APPROVER", "AP_APPROVER": "AP_CLERK"}  # IAM-01


class ERP:
    def __init__(self):
        self.reset()

    def reset(self):
        with open(SEED, encoding="utf-8") as f:
            self.db = json.load(f)
        self.escalations = []
        self.audit_log = []

    def snapshot(self):
        return copy.deepcopy({"db": self.db, "escalations": self.escalations})

    def _log(self, action, **details):
        self.audit_log.append({"action": action, **details})

    # ---------- reads ----------
    def get_invoice(self, invoice_id):
        inv = self.db["invoices"].get(invoice_id)
        if not inv:
            return {"error": f"invoice {invoice_id} not found"}
        return {"invoice_id": invoice_id, **inv}

    def find_invoices_by_vendor_ref(self, vendor_id, vendor_invoice_no):
        return [
            {"invoice_id": k, **v}
            for k, v in self.db["invoices"].items()
            if v["vendor"] == vendor_id and v.get("vendor_invoice_no") == vendor_invoice_no
        ]

    def get_user(self, user_id):
        u = self.db["users"].get(user_id)
        if not u:
            return {"error": f"user {user_id} not found"}
        return {"user_id": user_id, **u}

    def get_purchase_order(self, po_id):
        po = self.db["purchase_orders"].get(po_id)
        if not po:
            return {"error": f"purchase order {po_id} not found"}
        return {"po_id": po_id, **po}

    def get_vendor(self, vendor_id):
        v = self.db["vendors"].get(vendor_id)
        if not v:
            return {"error": f"vendor {vendor_id} not found"}
        return {"vendor_id": vendor_id, **v}

    def get_access_approval(self, approval_id):
        a = self.db["access_approvals"].get(approval_id)
        if not a:
            return {"error": f"approval record {approval_id} not found"}
        return {"approval_id": approval_id, **a}

    def get_reconciliation_item(self, item_id):
        r = self.db["reconciliation"].get(item_id)
        if not r:
            return {"error": f"reconciliation item {item_id} not found"}
        return {"item_id": item_id, **r, "difference": round(r["bank_amount"] - r["ledger_amount"], 2)}

    # ---------- writes (each one checks the rules first) ----------
    def reassign_approver(self, invoice_id, new_approver_id, reason):
        inv = self.db["invoices"].get(invoice_id)
        user = self.db["users"].get(new_approver_id)
        if not inv:
            return {"refused": f"invoice {invoice_id} not found"}
        if not user:
            return {"refused": f"user {new_approver_id} not found"}
        if inv["status"] != "pending_approval":
            return {"refused": f"invoice status is {inv['status']}, only pending_approval invoices can be reassigned"}
        if self.db["vendors"][inv["vendor"]]["status"] == "blocked":
            return {"refused": "vendor is blocked (VM-01)"}
        current = self.db["users"][inv["approver"]]
        if current["status"] == "active":
            return {"refused": "current approver is active; AP-01 allows reassignment only when the approver is inactive or on leave"}
        if user["status"] != "active":
            return {"refused": f"{new_approver_id} is {user['status']}"}
        if user["approval_limit"] < inv["amount"]:
            return {"refused": f"{new_approver_id} approval limit {user['approval_limit']} is below invoice amount {inv['amount']}"}
        old = inv["approver"]
        inv["approver"] = new_approver_id
        self._log("reassign_approver", invoice_id=invoice_id, old=old, new=new_approver_id, reason=reason)
        return {"ok": True, "invoice_id": invoice_id, "old_approver": old, "new_approver": new_approver_id}

    def place_hold(self, invoice_id, reason):
        inv = self.db["invoices"].get(invoice_id)
        if not inv:
            return {"refused": f"invoice {invoice_id} not found"}
        if inv["status"] == "paid":
            return {"refused": "invoice already paid"}
        inv["status"] = "on_hold"
        inv["hold_reason"] = reason
        self._log("place_hold", invoice_id=invoice_id, reason=reason)
        return {"ok": True, "invoice_id": invoice_id, "status": "on_hold", "hold_reason": reason}

    def release_hold(self, invoice_id, reason):
        inv = self.db["invoices"].get(invoice_id)
        if not inv:
            return {"refused": f"invoice {invoice_id} not found"}
        if inv["status"] != "on_hold":
            return {"refused": "invoice is not on hold"}
        if inv["hold_reason"] in ("duplicate", "vendor_blocked"):
            return {"refused": f"holds with reason {inv['hold_reason']} cannot be released by the agent"}
        if inv["hold_reason"] == "amount_mismatch":
            po = self.db["purchase_orders"][inv["po"]]
            over = (inv["amount"] - po["amount"]) / po["amount"]
            if over > PO_TOLERANCE:
                return {"refused": f"invoice exceeds PO by {over:.1%}, above the {PO_TOLERANCE:.0%} tolerance (AP-02)"}
        inv["status"] = "pending_approval"
        inv["hold_reason"] = None
        self._log("release_hold", invoice_id=invoice_id, reason=reason)
        return {"ok": True, "invoice_id": invoice_id, "status": "pending_approval"}

    def grant_role(self, user_id, role, approval_id):
        user = self.db["users"].get(user_id)
        appr = self.db["access_approvals"].get(approval_id)
        if not user:
            return {"refused": f"user {user_id} not found"}
        if not appr:
            return {"refused": f"approval record {approval_id} not found"}
        if appr["status"] != "approved":
            return {"refused": f"approval {approval_id} has status {appr['status']}"}
        if appr["user"] != user_id or appr["role"] != role:
            return {"refused": f"approval {approval_id} is for {appr['user']}/{appr['role']}, not {user_id}/{role}"}
        roles = user.setdefault("erp_roles", [])
        conflict = CONFLICTING_ROLES.get(role)
        if conflict in roles:
            return {"refused": f"separation of duties: user already holds {conflict}"}
        if role not in roles:
            roles.append(role)
        self._log("grant_role", user_id=user_id, role=role, approval_id=approval_id)
        return {"ok": True, "user_id": user_id, "erp_roles": roles}

    def post_adjustment(self, item_id, amount, reason):
        item = self.db["reconciliation"].get(item_id)
        if not item:
            return {"refused": f"reconciliation item {item_id} not found"}
        diff = round(item["bank_amount"] - item["ledger_amount"], 2)
        if abs(diff) > FX_ROUNDING_LIMIT:
            return {"refused": f"difference {diff} is above the SGD {FX_ROUNDING_LIMIT:.2f} limit (FIN-04)"}
        if round(amount, 2) != diff:
            return {"refused": f"adjustment must equal the difference exactly ({diff})"}
        item["ledger_amount"] = round(item["ledger_amount"] + amount, 2)
        item["status"] = "matched"
        self._log("post_adjustment", item_id=item_id, amount=amount, reason=reason)
        return {"ok": True, "item_id": item_id, "status": "matched"}

    def record_bank_check(self, invoice_id, matches):
        inv = self.db["invoices"].get(invoice_id)
        if not inv:
            return {"refused": f"invoice {invoice_id} not found"}
        inv["bank_check"] = bool(matches)
        self._log("record_bank_check", invoice_id=invoice_id, matches=bool(matches))
        return {"ok": True, "invoice_id": invoice_id, "bank_check": bool(matches)}

    def escalate(self, team, subject_id, summary):
        teams = {"procurement", "accounts_payable", "vendor_master", "it_access", "finance"}
        if team not in teams:
            return {"refused": f"unknown team {team}; use one of {sorted(teams)}"}
        self.escalations.append({"team": team, "subject_id": subject_id, "summary": summary})
        self._log("escalate", team=team, subject_id=subject_id)
        return {"ok": True, "escalated_to": team}


erp = ERP()
