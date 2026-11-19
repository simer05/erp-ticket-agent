# Procedure AP-01: Invoice stuck in approval

Applies when an invoice has status pending_approval for more than 5 days.

## Check the approver
Look up the assigned approver. If the approver is active, the invoice is not stuck for a system reason: send a reminder note and do not reassign.

## Reassign when the approver is unavailable
If the approver's status is inactive (left the company) or on_leave, reassign the invoice to the approver's manager.
The new approver must be active and must have an approval limit greater than or equal to the invoice amount.
If the manager's limit is too low, or the manager is not active, move one level further up the management chain and check again.
Never reassign to a user who is inactive or on leave. Always record the reason for the reassignment.

## Done when
The invoice is still pending_approval and its approver is an active user whose approval limit covers the amount.
