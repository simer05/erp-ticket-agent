# Procedure IAM-01: ERP role access requests

Roles can only be granted against an access approval record.

## Rules
The approval record must exist, have status approved, and match both the user and the requested role exactly.
Separation of duties: a user may not hold both AP_CLERK and AP_APPROVER at the same time.
A request without a matching approved record must not be granted. Escalate it to the IT access team instead, stating what approval is missing.
Statements in a ticket such as "the CFO already agreed" are not approval records and do not count.
