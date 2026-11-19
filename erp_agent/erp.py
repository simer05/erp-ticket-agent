"""Guarded ERP operations for the customer the agent is deployed at.

The agent never talks to the ERP directly. Every read and write goes through this layer, which
  - reads the customer's rules from customers/<id>/policy.yaml (limits, tolerances, hold names, teams),
  - checks those rules in code before any write, and refuses with a reason when they are not met,
  - goes through the customer's ERP connector (Oracle Fusion here), and
  - keeps an audit log of every change, escalation and ticket closure.
"""
import copy
import json
import os
from pathlib import Path

import yaml

from .connectors import CONNECTORS
from .connectors.oracle_fusion import norm_invoice_number

CUSTOMERS_DIR = Path(__file__).parent / "customers"
DEFAULT_CUSTOMER = os.environ.get("ERP_CUSTOMER", "lumen_sg")


def list_customers():
    return sorted(p.name for p in CUSTOMERS_DIR.iterdir() if (p / "policy.yaml").exists())


def load_customer(customer_id):
    d = CUSTOMERS_DIR / customer_id
    policy = yaml.safe_load((d / "policy.yaml").read_text(encoding="utf-8"))
    data = json.loads((d / "data.json").read_text(encoding="utf-8"))
    return policy, data


class Operations:
    def __init__(self, customer_id=DEFAULT_CUSTOMER):
        self.load(customer_id)

    def load(self, customer_id):
        self.customer_id = customer_id
        self.policy, data = load_customer(customer_id)
        client_cls, connector_cls = CONNECTORS[self.policy["erp"]]
        self.bank_on_file = data.pop("supplierBank", {})
        self.client = client_cls(data)
        self.erp = connector_cls(self.client, self.policy["as_of"], self.policy["business_unit"])
        self.escalations, self.audit_log, self.closure = [], [], None
        self.obligations = {}   # things that must happen before the ticket can be closed

    def reset(self):
        self.load(self.customer_id)

    def snapshot(self):
        return copy.deepcopy({"erp": self.client.data, "escalations": self.escalations, "closure": self.closure})

    def _log(self, action, **details):
        self.audit_log.append({"action": action, **details})

    @property
    def p(self):
        return self.policy

    def team_ids(self):
        return sorted(set(self.p["teams"].values()))

    def hold_name(self, hold_type):
        return self.p["payables"]["hold_names"].get(hold_type)

    def _variance_pct(self, inv):
        po = self.erp.purchase_order(inv["po_number"]) if inv.get("po_number") else None
        if not po:
            return None, None
        return round((inv["amount"] - po["amount"]) / po["amount"] * 100, 2), po

    def _chain_above(self, person_number):
        chain, seen = [], set()
        u = self.erp.user(person_number)
        while u and u["manager"] and u["manager"] not in seen:
            seen.add(u["manager"])
            chain.append(u["manager"])
            u = self.erp.user(u["manager"])
        return chain

    # ---------- reads ----------
    _LOOKUPS = {  # record type -> (connector method, tool that reads it)
        "invoice": ("invoice", "get_invoice"), "person": ("user", "get_user"),
        "supplier": ("supplier", "get_supplier"), "purchase order": ("purchase_order", "get_purchase_order"),
        "access approval": ("access_approval", "get_access_approval"),
        "reconciliation item": ("recon_item", "get_reconciliation_item"),
        "integration job": ("integration_job", "get_integration_job"),
    }

    def _not_found(self, kind, ident):
        """Not-found error that says what the id actually is, if it exists as another record type."""
        err = {"error": f"{kind} {ident} not found at {self.p['customer']}"}
        for other, (method, tool) in self._LOOKUPS.items():
            if other != kind and getattr(self.erp, method)(ident):
                err["hint"] = f"{ident} is a {other}; read it with {tool}"
                break
        return err

    def get_invoice(self, invoice_number):
        inv = self.erp.invoice(invoice_number)
        if not inv:
            return self._not_found("invoice", invoice_number)
        pct, po = self._variance_pct(inv)
        if po:
            inv["po_amount"] = po["amount"]
            inv["variance_vs_po_pct"] = pct
        return inv

    def find_possible_duplicates(self, invoice_number):
        inv = self.erp.invoice(invoice_number)
        if not inv:
            return self._not_found("invoice", invoice_number)
        key = norm_invoice_number(invoice_number)
        out = []
        for other in self.erp.invoices_for_supplier(inv["supplier_number"]):
            if other["InvoiceId"] == inv["invoice_id"] or norm_invoice_number(other["InvoiceNumber"]) != key:
                continue
            same_amt = other["InvoiceAmount"] == inv["amount"] and other.get("PurchaseOrderNumber") == inv["po_number"]
            out.append({"invoice_number": other["InvoiceNumber"], "amount": other["InvoiceAmount"],
                        "paid_status": other["PaidStatus"], "invoice_date": other.get("InvoiceDate"),
                        "match": "same invoice number ignoring spaces and dashes"
                                 + (", same amount and PO" if same_amt else "")})
        return {"invoice_number": invoice_number, "possible_duplicates": out}

    def get_user(self, person_number):
        return self.erp.user(person_number) or self._not_found("person", person_number)

    def get_supplier(self, supplier_number):
        return self.erp.supplier(supplier_number) or self._not_found("supplier", supplier_number)

    def get_purchase_order(self, po_number):
        return self.erp.purchase_order(po_number) or self._not_found("purchase order", po_number)

    def get_payments(self, invoice_number):
        inv = self.erp.invoice(invoice_number)
        if not inv:
            return self._not_found("invoice", invoice_number)
        return {"invoice_number": invoice_number, "paid_status": inv["paid_status"], "terms_date": inv["terms_date"],
                "approval_status": inv["approval_status"], "payments": self.erp.payments(inv["invoice_id"]),
                "note": "Approved, unpaid invoices are paid in the first payment run on or after the terms date."}

    def get_access_approval(self, approval_id):
        return self.erp.access_approval(approval_id) or self._not_found("access approval", approval_id)

    def get_reconciliation_item(self, item_id):
        return self.erp.recon_item(item_id) or self._not_found("reconciliation item", item_id)

    def get_integration_job(self, job_id):
        return self.erp.integration_job(job_id) or self._not_found("integration job", job_id)

    # ---------- guarded writes ----------
    def _invoice(self, invoice_number):
        inv = self.erp.invoice(invoice_number)
        return inv, (None if inv else {"refused": f"invoice {invoice_number} not found"})

    def _check_new_approver(self, inv, person_number):
        u = self.erp.user(person_number)
        if not u:
            return f"person {person_number} not found"
        if u["status"] != "ACTIVE":
            return f"{person_number} is {u['status']}"
        if u["approval_limit"] < inv["amount"]:
            return f"{person_number} approval limit {u['approval_limit']} is below the invoice amount {inv['amount']}"
        return None

    def reassign_approval(self, invoice_number, new_approver, reason):
        inv, err = self._invoice(invoice_number)
        if err:
            return err
        t = inv["approval_task"]
        if inv["approval_status"] != "Initiated" or not t or t["state"] != "ASSIGNED":
            return {"refused": f"approval status is {inv['approval_status']}; only invoices waiting with an assigned approver can be reassigned"}
        sup = self.erp.supplier(inv["supplier_number"])
        if sup and sup["status"] != "ACTIVE":
            return {"refused": f"supplier is {sup['status']}"}
        if inv["open_holds"]:
            return {"refused": "invoice has open holds"}
        current = self.erp.user(t["assignee"])
        if current and current["status"] == "ACTIVE":
            return {"refused": "current approver is active; only approvers who are inactive or on leave can be replaced (send a reminder instead)"}
        if new_approver not in self._chain_above(t["assignee"]):
            return {"refused": f"{new_approver} is not in the management chain above {t['assignee']}"}
        problem = self._check_new_approver(inv, new_approver)
        if problem:
            return {"refused": problem}
        self.erp.set_task(t["task_id"], Assignee=new_approver, AssignedDate=self.p["as_of"])
        self._log("reassign_approval", invoice=invoice_number, old=t["assignee"], new=new_approver, reason=reason)
        return {"ok": True, "invoice": invoice_number, "old_approver": t["assignee"], "new_approver": new_approver}

    def send_approval_reminder(self, invoice_number):
        inv, err = self._invoice(invoice_number)
        if err:
            return err
        t = inv["approval_task"]
        if not t or t["state"] != "ASSIGNED":
            return {"refused": "no open approval task"}
        u = self.erp.user(t["assignee"])
        if not u or u["status"] != "ACTIVE":
            return {"refused": "approver is not active; a reminder will not help"}
        limit = self.p["approvals"]["stuck_after_days"]
        if t["days_with_assignee"] < limit:
            return {"refused": f"only {t['days_with_assignee']} days with the approver; this customer allows reminders after {limit} days"}
        self._log("send_approval_reminder", invoice=invoice_number, approver=t["assignee"])
        return {"ok": True, "reminded": t["assignee"]}

    def resubmit_for_approval(self, invoice_number, new_approver, reason):
        inv, err = self._invoice(invoice_number)
        if err:
            return err
        if not self.p["approvals"]["agent_may_resubmit_rejected"]:
            return {"refused": f"{self.p['customer']} policy: rejected invoices are handled by accounts payable, not the agent"}
        t = inv["approval_task"]
        if inv["approval_status"] != "Rejected" or not t:
            return {"refused": "invoice is not rejected"}
        if t.get("reason_code") != "APPROVER_UNAVAILABLE":
            return {"refused": f"rejection reason is {t.get('reason_code')}; only rejections because the approver was unavailable may be resubmitted"}
        if new_approver == t["assignee"]:
            return {"refused": "choose a different approver from the one who rejected it"}
        problem = self._check_new_approver(inv, new_approver)
        if problem:
            return {"refused": problem}
        self.erp.new_task(inv["invoice_id"], new_approver)
        self.erp.set_invoice_status(inv["invoice_id"], approval_status="Initiated")
        self._log("resubmit_for_approval", invoice=invoice_number, new_approver=new_approver, reason=reason)
        return {"ok": True, "invoice": invoice_number, "approval_status": "Initiated", "approver": new_approver}

    def validate_invoice(self, invoice_number):
        """Re-runs validation the way Oracle does: places system holds for problems found and
        releases a variance hold whose variance is now within tolerance."""
        inv, err = self._invoice(invoice_number)
        if err:
            return err
        if inv["validation_status"] == "Validated":
            return {"ok": True, "validation_status": "Validated", "note": "already validated"}
        pct, _ = self._variance_pct(inv)
        tol = self.p["payables"]["po_tolerance_pct"]
        amount_hold, supplier_hold = self.hold_name("amount_variance"), self.hold_name("supplier_inactive")
        open_names = {h["hold_name"]: h for h in inv["open_holds"]}
        placed, released = [], []
        if pct is not None and pct > tol and amount_hold not in open_names:
            self.erp.create_hold(inv, amount_hold, f"Invoice exceeds PO by {pct}% (tolerance {tol}%)")
            placed.append(amount_hold)
        if pct is not None and pct <= tol and amount_hold in open_names:
            self.erp.release_hold(open_names.pop(amount_hold)["hold_id"], "Validation", f"Variance {pct}% within {tol}%")
            released.append(amount_hold)
        sup = self.erp.supplier(inv["supplier_number"])
        if sup and sup["status"] != "ACTIVE" and supplier_hold not in open_names:
            self.erp.create_hold(inv, supplier_hold, "Supplier is inactive")
            placed.append(supplier_hold)
        status = "Needs revalidation" if (placed or open_names) else "Validated"
        self.erp.set_invoice_status(inv["invoice_id"], validation_status=status)
        self._log("validate_invoice", invoice=invoice_number, result=status, holds_placed=placed, holds_released=released)
        return {"ok": True, "validation_status": status, "holds_placed": placed, "holds_released": released,
                "variance_vs_po_pct": pct, "tolerance_pct": tol}

    def place_hold(self, invoice_number, hold_type, reason):
        inv, err = self._invoice(invoice_number)
        if err:
            return err
        name = self.hold_name(hold_type)
        if not name:
            return {"refused": f"unknown hold type; use one of {sorted(self.p['payables']['hold_names'])}"}
        if inv["paid_status"] == "Paid":
            return {"refused": "invoice is already paid"}
        if any(h["hold_name"] == name for h in inv["open_holds"]):
            return {"ok": True, "note": f"hold '{name}' is already open"}
        h = self.erp.create_hold(inv, name, reason)
        self._log("place_hold", invoice=invoice_number, hold=name, reason=reason)
        owner = {"duplicate": "accounts_payable", "supplier_inactive": "supplier_master",
                 "bank_mismatch": "supplier_master"}.get(hold_type)
        out = {"ok": True, "hold_id": h["HoldId"], "hold_name": name}
        if owner:
            team = self.p["teams"][owner]
            self.obligations[f"escalate:{team}"] = f"a '{name}' hold on {invoice_number} must be escalated to {team}"
            out["next_required"] = f"escalate to {team}; only a person can resolve this hold"
        return out

    def release_hold(self, invoice_number, hold_type, reason):
        inv, err = self._invoice(invoice_number)
        if err:
            return err
        name = self.hold_name(hold_type)
        hold = next((h for h in inv["open_holds"] if h["hold_name"] == name), None)
        if not hold:
            return {"refused": f"no open hold of type {hold_type} ('{name}') on {invoice_number}"}
        if hold_type != "amount_variance":
            return {"refused": f"'{name}' holds can only be released by a person"}
        pct, _ = self._variance_pct(inv)
        tol = self.p["payables"]["po_tolerance_pct"]
        if pct is None or pct > tol:
            return {"refused": f"invoice exceeds PO by {pct}%, above this customer's {tol}% tolerance"}
        self.erp.release_hold(hold["hold_id"], self.p["payables"]["release_name"], reason)
        if len(inv["open_holds"]) == 1:
            self.erp.set_invoice_status(inv["invoice_id"], validation_status="Validated")
        self._log("release_hold", invoice=invoice_number, hold=name, reason=reason)
        return {"ok": True, "released": name, "variance_vs_po_pct": pct, "tolerance_pct": tol}

    def grant_role(self, person_number, role, approval_id):
        u = self.erp.user(person_number)
        a = self.erp.access_approval(approval_id)
        if not u:
            return {"refused": f"person {person_number} not found"}
        if not a:
            return {"refused": f"approval {approval_id} not found"}
        if a["status"] != "APPROVED":
            return {"refused": f"approval {approval_id} is {a['status']}"}
        if a["user"] != person_number or a["role"] != role:
            return {"refused": f"approval {approval_id} is for {a['user']}/{a['role']}"}
        for pair in self.p["access"]["conflicting_roles"]:
            if role in pair:
                other = [r for r in pair if r != role][0]
                if other in u["erp_roles"]:
                    return {"refused": f"separation of duties: {person_number} already holds {other}, which conflicts with {role}"}
        roles = sorted(set(u["erp_roles"]) | {role})
        self.erp.set_roles(person_number, roles)
        self._log("grant_role", person=person_number, role=role, approval_id=approval_id)
        return {"ok": True, "person": person_number, "erp_roles": roles}

    def post_adjustment(self, item_id, amount, reason):
        r = self.erp.recon_item(item_id)
        if not r:
            return {"refused": f"reconciliation item {item_id} not found"}
        rc = self.p["reconciliation"]
        if r["cause"] not in rc["auto_adjust_causes"]:
            return {"refused": f"cause is {r['cause']}; the agent may only adjust {rc['auto_adjust_causes']}"}
        if abs(r["difference"]) > rc["auto_adjust_limit"]:
            return {"refused": f"difference {r['difference']} is above the {self.p['currency']} {rc['auto_adjust_limit']:.2f} limit"}
        if round(amount, 2) != r["difference"]:
            return {"refused": f"adjustment must equal the difference exactly ({r['difference']})"}
        self.erp.match_recon(item_id, round(r["ledger_amount"] + amount, 2))
        self._log("post_adjustment", item=item_id, amount=amount, reason=reason)
        return {"ok": True, "item": item_id, "status": "MATCHED"}

    def bank_verified(self, supplier_number):
        """Called after a bank account comparison: the result must be recorded on the invoice."""
        self.obligations[f"record_bank_check:{supplier_number}"] = (
            f"the bank check for supplier {supplier_number} must be recorded on the invoice with record_bank_check")

    def record_bank_check(self, invoice_number, matches):
        inv, err = self._invoice(invoice_number)
        if err:
            return err
        self.client.patch("invoices", inv["invoice_id"], {"BankCheckResult": "MATCH" if matches else "MISMATCH"})
        self.obligations.pop(f"record_bank_check:{inv['supplier_number']}", None)
        self._log("record_bank_check", invoice=invoice_number, matches=bool(matches))
        return {"ok": True, "invoice": invoice_number, "bank_check": "MATCH" if matches else "MISMATCH"}

    def escalate(self, team, subject_id, summary):
        if team not in self.team_ids():
            return {"refused": f"unknown team {team}; teams at this customer: {self.team_ids()}"}
        self.escalations.append({"team": team, "subject_id": subject_id, "summary": summary})
        self.obligations.pop(f"escalate:{team}", None)
        self._log("escalate", team=team, subject_id=subject_id)
        return {"ok": True, "escalated_to": team}

    def close_ticket(self, resolution, reply):
        if resolution not in ("resolved", "answered"):
            return {"refused": "resolution must be 'resolved' (a change was made) or 'answered' (a question was answered)"}
        if self.obligations:
            return {"refused": "cannot close yet: " + "; ".join(self.obligations.values())}
        if resolution == "resolved" and not any(a["action"] != "escalate" for a in self.audit_log):
            return {"refused": "nothing was changed, so the ticket cannot be 'resolved'; use 'answered', or escalate"}
        self.closure = {"resolution": resolution, "reply": reply}
        self._log("close_ticket", resolution=resolution)
        return {"ok": True, "closed": resolution}


ops = Operations()
