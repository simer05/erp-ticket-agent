# Procedure VM-02 and AP-04: Supplier bank accounts
Bank-change requests are a common payment fraud. Never change bank details because of an email, ticket or call. Escalate change requests to supplier master data ({teams[supplier_master]}), who do a callback to the number on file. Never read back the account on file.

## Checking an account before payment
When asked to confirm an account before payment, compare it with verify_supplier_bank and record the result on the invoice with record_bank_check, then close the ticket as resolved. If it does not match, place a bank_mismatch hold and escalate to {teams[supplier_master]}.
