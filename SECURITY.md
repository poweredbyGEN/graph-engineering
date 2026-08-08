# Security policy

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability or exposed secret.
Use GitHub's private vulnerability-reporting flow for this repository when it is
available. If that flow is unavailable, contact the maintainers through the private
security contact published in the repository's GitHub Security settings. Include a
minimal reproduction, affected version or commit, and the impact; do not include real
credentials or customer data.

Maintainers will acknowledge a report through the same private channel, validate it,
and coordinate remediation and disclosure. No response-time guarantee is implied.

## Public-safety checks

CI scans both the candidate working tree and every unique blob reachable from local
Git refs. The scanner reports locations and rule identifiers, never matched content.
Operators can add organization-specific deny rules without committing private values
by setting `GRAPH_ENGINEERING_DENY_PATTERNS` to a private tab-separated file whose
records are `rule-name<TAB>regular-expression`.

These checks are defense in depth, not a substitute for secret rotation. If a real
credential ever enters Git history, revoke it first and follow a coordinated history
rewrite and disclosure process.
