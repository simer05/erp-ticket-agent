# Procedure INT-01: Failed integrations and imports
Applies when an import, sync or integration job has failed, often after a quarterly ERP update or an HR system release (retired API versions, renamed fields, changed security domains).

## Action
Read the job record. Do not rerun the job, change mappings or touch the integration user's permissions. Escalate to {teams[integrations]} with the job id, the exact error, the likely cause (for example the update named in the error) and how many records are affected.
