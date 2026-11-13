# Python mirror tag policy

The `python-mirror` profile is an explicit opt-in for Python mirrors on the
the forge and group you configure (`--forge-url` and `--group`). Generic publishing
does not enable it by default. The forge must be an HTTPS origin and the group a
plain namespace. Bot and release grants come only from your configuration. Scratch
environments must not enable the production profile.

This repository is public, so it contains no user IDs, bot identities, or internal
deployment paths. Supply them at run time, as shown below. Never commit them to the
code, tests, docs, or examples.

The profile requires all of these protected tag rules:

| Pattern | Allowed creators |
| --- | --- |
| `baseline/*`, `novel-head/*` | Configured release users |
| `pnc/*`, `alignment/*`, `builddep/*`, `reproducer-pin/*`, `constituent-pin/*` | The configured bot user |
| `sme-verified/*` | GitLab Developer role and above |

Keep this table's pattern names in sync with `required_tag_rules` in
`src/taisce_cuan/source/mirror_policy.py` and the downstream consumers' protected
tag pattern list (LWLP-5235). Release creator and bot grants are configuration, so
they are never copied here. When you change any copy of the patterns, review all of
them. No automated drift check or live cross-repo CI compares them.

Get the release user IDs and the pipeline bot user ID from the mirror owner, and
confirm them before running the commands. The examples use `$RELEASE_IDS` for the
comma-separated release IDs, `$BOT_ID` for the bot ID, `$FORGE_URL` for the forge,
and `$MIRROR_GROUP` for the mirror group.

Existing rules with different grants, or overlapping rules
that grant extra creators, require manual review. Reconciliation does not delete
or rewrite rules. It creates missing rules only when the existing policy does
not conflict, and verifies the result with a readback.

To fix a conflict by hand, follow this order:

- Get the mirror owner to review and approve each planned permission change.
  Keep an audit of the existing grants and matching tag names and commit SHAs
  before you change anything.
- Correct each exact rule that differs, and every overlapping rule that grants
  extra creators, through the GitLab UI or API. The protected tags API supports
  DELETE and POST but not PATCH, so a change means deleting and recreating the
  rule. That leaves a temporary window where the tag pattern is unprotected.
  Coordinate a write pause on the affected mirrors for the whole change. Before
  resuming, compare tags with the saved snapshot and review any created, moved,
  or deleted tags. Never delete unrelated rules without review.
- Run a new read-only `reconcile-mirrors` audit with a fresh report path. Apply
  only after all permission conflicts are resolved; missing rules can then be
  created by reconciliation.

## Inspect and apply

Set `GITLAB_TOKEN` in your environment or use `GIT_AUTH_TOKEN`. The token needs
the `api` scope and Maintainer access on the mirrors to create protected tags.
Avoid `--auth-token`, which exposes the credential in the process list, and never
put a credential in a report. Each run requires a **new** report path;
an existing file is never overwritten. The initial report and each project's
before snapshot are saved before any changes. Each update replaces the report
atomically, so a failed update leaves the previous snapshot available. Reports
contain the validated target, UTC start time, project outcomes, created rules,
errors, and remaining IDs. If a snapshot update fails after a remote change,
the previous report may not include that change. Inspect live state before retrying.

Inspect the full active scope without making changes:

```sh
taisce-cuan reconcile-mirrors \
  --forge-url "$FORGE_URL" \
  --group "$MIRROR_GROUP" \
  --mirror-policy python-mirror \
  --tag-protection-user-ids "$RELEASE_IDS" \
  --tag-protection-bot-user-id "$BOT_ID" \
  --report ./mirror-audit.json
```

Apply to a bounded batch after reviewing the audit:

```sh
taisce-cuan reconcile-mirrors \
  --forge-url "$FORGE_URL" \
  --group "$MIRROR_GROUP" \
  --mirror-policy python-mirror \
  --tag-protection-user-ids "$RELEASE_IDS" \
  --tag-protection-bot-user-id "$BOT_ID" \
  --report ./mirror-batch-1.json \
  --apply --max-projects 25
```

The limit is required with `--apply` and must be between 1 and 25. It can also
bound a read-only inspection. A project with the two release rules already in
place can require 15 API requests to create the six remaining rules. A mirror
missing all eight rules can require 19, so a batch of 25 can reach 475 requests
plus the inventory pages. That is close to the 500-request-per-minute account
quota, so operators should account for other activity and consider a smaller
batch. The publisher applies the same rules for one project per publish and does
not use this limit. GET requests retry a rate limit at most
twice, using a bounded delay. POST requests are deliberately not retried, even
after a rate limit. Earlier creates in the batch may already have succeeded, and
a transport failure can leave a write's outcome unknown. The run attempts a
policy readback and records the error instead of replaying the write. Each run
lists the entire inventory before changing any project. Direct active Python
mirrors are processed in numeric ID order; Java, shared, and subgroup projects are excluded,
and archived mirrors are recorded separately and never changed. A direct
`pypi.org-*` project with an unexpected path, such as uppercase letters,
is recorded as `unreviewed-python-path`. It is not changed and it
prevents a fleet-ready claim until someone reviews it.

Before each apply, the project is read again by ID and its current identity and
archive state are saved in the report. This check is not atomic with the later
GitLab writes, so a transfer or archive change can still race with those writes.

For the next batch, use the last successfully processed ID as the cursor and
choose a new report path:

```sh
taisce-cuan reconcile-mirrors \
  --forge-url "$FORGE_URL" \
  --group "$MIRROR_GROUP" \
  --mirror-policy python-mirror \
  --tag-protection-user-ids "$RELEASE_IDS" \
  --tag-protection-bot-user-id "$BOT_ID" \
  --report ./mirror-batch-2.json \
  --apply --max-projects 25 --after-project-id 12345
```

Check the report's `remaining_ids`, errors, and project-level readiness before
selecting a cursor. During
`--apply`, a partial creation or API error stops the batch; retry it with a new
report, the same cursor, and the same batch limit so that the failed project is
inspected again. If a rate limit stops the batch, wait for the quota to reset.
To retry with a smaller `--max-projects`, advance the cursor only to the last
fully processed project before the failure, as recorded in the report. Confirm
the new batch still includes the failed project; do not skip it or any
uninspected projects. Always use a new report path. A
project whose existing permissions conflict is not written to. It is recorded as
not ready and the batch continues with the next project. Such a project leaves
`remaining_ids`, so also review each recorded `projects[].after.ready` and its
errors during apply. Repeated failures on the same project require manual
investigation and remediation before retrying; retries do not repair malformed
or unsupported grants. In a read-only
inspection, a project that returns an API error is recorded and the audit
continues, so one run can list every problem mirror. The command reads live
state on every run and does not trust a previous report. A capped or
cursor run returns a nonzero status and cannot claim fleet readiness. After all
batches, run a new, uncapped read-only audit with no cursor. Only a complete,
error-free audit of at least one active mirror, with no archived or unreviewed
exclusions, can report `fleet_ready: true` and exit zero. An empty inventory
never does. Archived mirrors require separate review and
prevent a fleet-ready claim, even when all active mirrors are ready. Missing
rules, incomplete listings, and partial errors cannot claim completion.

## Publisher selection and deployment gap

For a compatible publisher, `--mirror-policy python-mirror` selects this
policy on `push`, `push-git-source`, and the Tekton push-source entry point.
Configure the release creator IDs with `--tag-protection-user-ids "$RELEASE_IDS"`
and the pipeline bot with `--tag-protection-bot-user-id "$BOT_ID"`. The bot flag
is required with `--mirror-policy`; the environment variable
`TAG_PROTECTION_BOT_USER_ID` can supply it on `push` and `push-git-source`.
The flag does not override the authentication, target, remote URL, or verification
gates on a real publish. Publisher `--dry-run` skips the forge, so it does not run
those gates and does not prove remote readiness. A `reconcile-mirrors` run
without `--apply` makes read-only authenticated GET requests and never writes.

The managed pipeline and its release configuration live outside this repository.
The current task image does not support the new flags. Deployment therefore requires
a separate coordinated change:

1. Build and approve a replacement image from the reviewed source commit.
2. Update the task's image digest and supported policy-selection wiring together.
3. Update the managed pipeline's task revision and forward the explicit policy
   selection, release creator IDs, and bot user ID from the private release
   configuration. Enable the production profile only in production; do not enable
   it for scratch or pass new parameters to an older task.
4. Run a complete read-only audit of the existing fleet and review every mirror
   with conflicting or unreviewed permissions. The profile requires exact
   creators, so a conflicting mirror fails real publishes once it is enabled.
5. Validate a provisioning canary, then apply the fleet reconciliation in
   bounded batches and finish with a new uncapped audit.

Do not expose a policy setting that the selected image silently ignores. This
document does not claim deployment activation or fleet completion.

Separate follow-up: this tag-only profile does not protect the
`verification-metadata` branch. Branch protection and the Python read and write
consumers need their own review. See LWLP-4759 for reference.
