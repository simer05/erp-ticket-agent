# Procedure AP-02: Invoice on hold for amount mismatch

Applies when an invoice is on hold with reason amount_mismatch.

## Tolerance rule
Compare the invoice amount with the purchase order amount.
If the invoice exceeds the PO by 2% or less, release the hold.
If the invoice exceeds the PO by more than 2%, the hold must stay. Do not release it, even if the vendor or the requester says the amount is correct.
Escalate to the procurement team with the PO amount, invoice amount and the percentage difference, so procurement can raise a PO change.

## Done when
Within tolerance: hold released. Above tolerance: hold kept and procurement escalation raised.
