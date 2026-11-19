"""Offline checks of the guarded operations, the Oracle connector and procedure search (no API key needed).
The same requests are run against both customers to show the rules come from each customer's policy."""
from erp_agent import knowledge
from erp_agent.erp import Operations
from erp_agent.security import Broker, find_leaks


def lumen():
    return Operations("lumen_sg")


def kestrel():
    return Operations("kestrel_au")


def test_connector_reads_oracle_shape():
    o = lumen()
    inv = o.get_invoice("NW-2210")
    assert inv["invoice_id"] == 300101 and inv["variance_vs_po_pct"] == 4.0
    assert inv["open_holds"][0]["hold_name"] == "Amount"
    assert o.client.calls[0][1].startswith("/fscmRestApi/resources/11.13.18.05/invoices")


def test_same_variance_different_customers():
    # 4% over PO: above Lumen's 2% tolerance, within Kestrel's 5%
    assert "refused" in lumen().release_hold("NW-2210", "amount_variance", "supplier says ok")
    k = kestrel()
    assert k.release_hold("OF-7710", "amount_variance", "within tolerance")["ok"]
    assert k.get_invoice("OF-7710")["open_holds"] == []


def test_validation_places_and_clears_holds():
    o = lumen()
    r = o.validate_invoice("NW-2350")             # 5% over, Lumen tolerance 2%
    assert r["holds_placed"] == ["Amount"] and r["validation_status"] == "Needs revalidation"
    r = o.validate_invoice("AC-1102")             # 1% over
    assert r["validation_status"] == "Validated" and not r["holds_placed"]
    k = kestrel()
    r = k.validate_invoice("OF-7710")             # 4% over with an existing hold, Kestrel tolerance 5%
    assert r["holds_released"] == ["PO Price Variance"] and r["validation_status"] == "Validated"


def test_reassign_rules():
    o = lumen()
    assert "refused" in o.reassign_approval("PP-310", "P1002", "x")       # approver active
    assert "refused" in o.reassign_approval("AC-1044", "P1004", "x")      # limit too low
    assert "refused" in o.reassign_approval("AC-1001", "P1009", "x")      # not in management chain
    assert o.reassign_approval("AC-1044", "P1002", "approver on leave")["ok"]
    assert "refused" in o.reassign_approval("BH-552", "P1002", "x")       # supplier inactive
    assert o.reassign_approval("AC-1001", "P1002", "approver left")["ok"]


def test_reminder_days_come_from_policy():
    assert "refused" in lumen().send_approval_reminder("PP-330")          # 2 days < 5
    assert lumen().send_approval_reminder("PP-310")["ok"]                 # 8 days
    assert kestrel().send_approval_reminder("SC-118")["ok"]               # 4 days >= 3


def test_resubmit_policy():
    assert "refused" in lumen().resubmit_for_approval("NW-2301", "P1003", "approver was away")
    k = kestrel()
    assert "refused" in k.resubmit_for_approval("HF-3360", "E202", "x")   # data error, not unavailability
    assert k.resubmit_for_approval("HF-3351", "E201", "approver on leave")["ok"]
    assert k.get_invoice("HF-3351")["approval_status"] == "Initiated"


def test_duplicates_ignore_formatting():
    assert lumen().find_possible_duplicates("AC7781")["possible_duplicates"][0]["paid_status"] == "Paid"
    assert kestrel().find_possible_duplicates("HF 3301")["possible_duplicates"][0]["invoice_number"] == "HF-3301"


def test_access_and_separation_of_duties():
    o = lumen()
    assert o.grant_role("P1005", "AP_CLERK", "APR-77")["ok"]
    assert "refused" in o.grant_role("P1006", "AP_APPROVER", "APR-78")    # pending
    assert "refused" in o.grant_role("P1009", "AP_CLERK", "APR-79")       # holds AP_APPROVER
    assert "refused" in kestrel().grant_role("E208", "AP_APPROVER", "AR-502")  # Kestrel-only conflict


def test_reconciliation_limits_from_policy():
    assert "refused" in lumen().post_adjustment("REC-304", 12.5, "fx")    # 12.50 > SGD 5
    assert kestrel().post_adjustment("BR-11", 12.5, "fx")["ok"]           # 12.50 <= AUD 20
    assert kestrel().post_adjustment("BR-12", -15.0, "bank fee")["ok"]    # bank_fee allowed at Kestrel
    assert "refused" in lumen().post_adjustment("REC-303", 4.2, "x")      # cause unknown


def test_close_and_escalate():
    o = lumen()
    assert "refused" in o.close_ticket("resolved", "done")                # nothing changed yet
    assert o.close_ticket("answered", "Paid on 2026-11-12")["ok"]
    assert "refused" in o.escalate("procurement_au", "X", "wrong customer's team")


def test_sealed_bank_details():
    o = lumen()
    b = Broker()
    b.reset(o.bank_on_file)
    assert b.get_supplier_bank_details("S2001")["bank_account_masked"] == "DBS ****9136"
    assert b.verify_supplier_bank("S2001", "DBS 072-845913-6")["matches"]
    b.verify_supplier_bank("S2001", "1")
    assert "refused" in b.verify_supplier_bank("S2001", "2")              # cap of 2 per supplier
    assert find_leaks(["account 0728459136"], "", o.bank_on_file)
    assert not find_leaks(["DBS ****9136"], "", o.bank_on_file)


def test_search_uses_customer_numbers():
    o, k = lumen(), kestrel()
    lt = knowledge.search("lumen_sg", o.policy, "invoice amount above purchase order tolerance")[0]
    kt = knowledge.search("kestrel_au", k.policy, "invoice amount above purchase order tolerance")[0]
    assert lt["source"] == kt["source"] == "ap_validation_variance"
    assert "2%" in lt["text"] and "5%" in kt["text"]
    top = knowledge.search("kestrel_au", k.policy, "rejected because approver was on leave")[0]
    assert top["source"] == "ap_rejected" and "resubmit it to a different ACTIVE approver" in top["text"]


def test_closure_obligations():
    o = lumen()
    o.place_hold("AC7781", "duplicate", "duplicate of AC-7781")
    assert "refused" in o.close_ticket("resolved", "held")                # must escalate first
    o.escalate("ap_team", "AC7781", "duplicate")
    assert o.close_ticket("resolved", "held and escalated")["ok"]
    o = lumen()
    o.bank_verified("S2001")
    assert "refused" in o.close_ticket("answered", "matches")             # result not recorded
    o.record_bank_check("AC-1150", True)
    assert o.close_ticket("resolved", "recorded")["ok"]


def test_date_mentions():
    from eval.run_eval import mentions
    assert mentions("paid on or after November 28, 2026", "2026-11-28")
    assert mentions("due 28 November 2026", "2026-11-28")
    assert not mentions("due 29 November 2026", "2026-11-28")


def test_not_found_says_what_the_id_is():
    r = kestrel().get_invoice("BR-13")
    assert "not found" in r["error"] and r["hint"] == "BR-13 is a reconciliation item; read it with get_reconciliation_item"
    assert "hint" not in lumen().get_invoice("XX-999")
