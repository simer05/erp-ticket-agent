"""Offline checks of the ERP guardrails and procedure search (no API key needed)."""
from erp_agent.erp import ERP
from erp_agent import knowledge


def test_reassign_rules():
    e = ERP()
    assert "refused" in e.reassign_approver("INV-1003", "U200", "x")      # approver active
    assert "refused" in e.reassign_approver("INV-1004", "U400", "x")      # limit too low
    assert e.reassign_approver("INV-1004", "U200", "on leave")["ok"]
    assert "refused" in e.reassign_approver("INV-1005", "U200", "x")      # vendor blocked
    assert e.reassign_approver("INV-1001", "U200", "left company")["ok"]


def test_holds():
    e = ERP()
    assert "refused" in e.release_hold("INV-1002", "vendor says ok")      # 8% over PO
    e.place_hold("INV-1003", "duplicate")
    assert "refused" in e.release_hold("INV-1003", "x")
    assert len(e.find_invoices_by_vendor_ref("V01", "AC-7781")) == 2


def test_access():
    e = ERP()
    assert e.grant_role("U500", "AP_CLERK", "APR-77")["ok"]
    assert "refused" in e.grant_role("U600", "AP_APPROVER", "APR-78")    # pending
    assert "refused" in e.grant_role("U500", "AP_APPROVER", "APR-77")    # mismatched record


def test_recon():
    e = ERP()
    assert "refused" in e.post_adjustment("REC-301", 1.0, "x")           # must equal diff
    assert e.post_adjustment("REC-301", 1.38, "fx rounding")["ok"]
    assert "refused" in e.post_adjustment("REC-302", 1500.0, "x")


def test_search():
    top = knowledge.search("approver left the company invoice stuck")[0]
    assert top["source"] == "ap_stuck_approval"
    assert knowledge.search("bank difference rounding")[0]["source"] == "reconciliation"
