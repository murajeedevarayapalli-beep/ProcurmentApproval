# Synthetic procurement data source

This source pack is fictional portfolio data, created for local demonstration. Do not use it as real policy or supplier evidence. The indexer imports every JSON file here at startup. Each record provides `source_id`, `title`, `owner`, `version`, `effective_date`, `expiry_date`, `classification`, `access_scope`, and `content`; metadata remains attached to every retrieved citation.

`policies.json`: purchase thresholds, competition, prohibited splitting, and restricted categories.
`approval_matrix.json`: amount and risk-based approval roles.
`budgets.json`: sample cost-center limits and committed spend.
`suppliers.json`: fictional supplier records, risk status, evidence status, and diversity flags.
`contracts.json`: fictional contract coverage and expiry.
`requests.json`: seeded scenarios covering standard, threshold escalation, over-budget, restricted category, and stale evidence cases.
