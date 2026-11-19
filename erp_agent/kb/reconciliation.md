# Procedure FIN-04: Bank reconciliation differences
Applies to unmatched items between the bank statement and the ledger.

## Agent may adjust
If the cause is one of {causes_text} and the absolute difference is {currency} {reconciliation[auto_adjust_limit]:.2f} or less, post an adjustment for exactly the difference (bank minus ledger). The item is then matched.

## Everything else
Larger differences, or any other cause including unknown, go to finance ({teams[finance]}) with both amounts. Do not post an adjustment.
