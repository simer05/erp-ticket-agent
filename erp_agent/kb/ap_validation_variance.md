# Procedure AP-02: Validation and PO amount variance
Applies when an invoice shows ValidationStatus Needs revalidation or Not validated, or has an amount variance hold ({payables[hold_names][amount_variance]}).

## Revalidate first
Run invoice validation. Validation places a variance hold when the invoice exceeds its PO by more than {payables[po_tolerance_pct]}%, and clears it when the variance is within tolerance.

## Tolerance at {customer}
Invoice amount may exceed the PO amount by up to {payables[po_tolerance_pct]}%. Within tolerance, the variance hold can be released.
Above tolerance the hold must stay, whoever asks and however urgent. Escalate to procurement ({teams[procurement]}) with the PO amount, invoice amount and percentage so they can raise a PO change.
