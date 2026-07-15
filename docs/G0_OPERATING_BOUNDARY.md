# G0 Operating Boundary

Status: `PENDING HUMAN APPROVAL`

Recorded: 2026-07-14

This record fixes the implementation boundary for the G1+G2 release train. It
is not a legal opinion, an investment mandate, a hosted-environment approval,
or evidence that G0 has passed.

## Authorized implementation scope

- One legal entity using its own capital and one dedicated internal account.
- Execution environments are only `paper` and the zero-network local
  `contract_test` simulator.
- Toss connectivity is read-only for documented authentication, market-data,
  and account-data operations.
- Production order create, modify, cancel, status/history URL access, an
  order-capable credential, and any UI or database Live activation path are
  prohibited.
- Customer assets, investment advisory/discretionary management, multiple
  entities, multiple brokerage accounts, and Production Live are outside this
  program. Any such requirement starts a new G0 business and regulatory
  review; it is not an extension of this architecture.
- Repository implementation and isolated local verification are authorized by
  the current work request. Applying migrations or services to hosted Supabase
  or Render remains a separate user-approved change requiring dedicated
  staging credentials.

## Mandatory human assignments

The following values must be completed in an approved, access-controlled
operating record before G0 can pass. Do not put account numbers, TOTP secrets,
tokens, or personal data in this repository.

| Assignment | Required evidence | Current repository status |
| --- | --- | --- |
| Executive/program sponsor | named accountable owner and approval timestamp | not provided |
| Risk owner | risk appetite and NO-LIVE acceptance | not provided |
| Legal/compliance reviewer | internal proprietary-capital perimeter review | not provided |
| Operations owner | incident, reconciliation, and restore accountability | not provided |
| Operator A | Supabase user UUID, TOTP enrolled, AAL2 test evidence | not provided |
| Operator B / checker | distinct user UUID, TOTP enrolled, approval test evidence | not provided |
| Release manager | release and provider-artifact custody | not provided |
| Auditor | read-only audit/archive review assignment | not provided |

## Binary gate

G0 remains `FAIL` until all of the following are retained outside source
control and linked by non-secret evidence identifiers:

- the accountable assignments above are complete;
- the two operating users are distinct and their AAL2/maker-checker tests pass;
- the dedicated account and proprietary-capital mandate are approved;
- the NO-LIVE policy is accepted by the program, risk, and legal owners;
- hosted staging, if requested, has a separately approved project, credentials,
  alert destination, immutable archive destination, and test-data policy.

Passing repository tests does not change this gate. G1 and G2 release evidence
must refer to the approved G0 record identifier, never to this draft alone.
