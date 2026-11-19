# Python mirror protection policy

The `python-mirror` profile is an explicit opt-in for Python mirrors on the
forge and group you configure (`--forge-url` and `--group`). Generic publishing
does not enable it by default. The forge must be an HTTPS origin and the group a
plain namespace. When a policy is selected, a forge URL without `gitlab` in it is
refused, because the checks use the GitLab API. Bot and release grants come only
from your configuration. Scratch environments must not enable the production
profile.

This repository is public, so it contains no user IDs, bot identities, or internal
deployment paths. Supply them at run time, as shown below. Never commit them to the
code, tests, docs, or examples.

The profile requires these protected tag rules and one protected branch rule. The
branch rule is described in [Branch protection](#branch-protection).

| Pattern | Allowed creators |
| --- | --- |
| `baseline/*`, `novel-head/*` | Configured release users |
| `pnc/*`, `alignment/*`, `builddep/*`, `reproducer-pin/*`, `constituent-pin/*` | The configured bot user |
| `sme-verified/*` | GitLab Developer role and above |

Keep this table's pattern names in sync with `required_tag_rules` in
`src/taisce_cuan/source/mirror_policy.py` and the downstream consumers' protected
tag pattern list (LWLP-5235). Keep the branch name `verification-metadata` in sync
with `src/taisce_cuan/source/branch_policy.py` and the downstream consumers' copies.
Release creator and bot grants are configuration, so they are never copied here.
When you change any copy of the patterns or the branch name, review all of them. No
automated drift check or live cross-repo CI compares them.

Get the release user IDs and the pipeline bot user ID from the mirror owner, and
confirm them before running the commands. The examples use `$RELEASE_IDS` for the
comma-separated release IDs, `$BOT_ID` for the bot ID, `$FORGE_URL` for the forge,
and `$MIRROR_GROUP` for the mirror group.

Existing tag or branch rules with different grants, or overlapping rules that grant
extra creators or extra push, merge, or force-push access, require manual review.
Reconciliation does not delete or rewrite rules. It creates missing rules only when
the existing policy does not conflict, and verifies the result with a readback.

To fix a conflict by hand, follow this order:

- Get the mirror owner to review and approve each planned permission change.
  Keep an audit of the existing grants, the matching tag names and commit SHAs,
  and the branch tip SHA before you change anything.
- Correct each exact rule that differs, and every overlapping rule that grants
  extra access, through the GitLab UI or API. The protected tags API supports
  DELETE and POST but not PATCH, so a change means deleting and recreating the
  rule. That leaves a temporary window where the tag pattern is unprotected. The
  protected branches API supports PATCH, so a branch rule can be corrected in place
  without that window. Coordinate a write pause on the affected mirrors for the
  whole change. Before resuming, compare tags and the branch tip with the saved
  snapshot, and review any created, moved, or deleted tags. Never delete unrelated
  rules without review.
- Run a new read-only `reconcile-mirrors` audit with a fresh report path. Apply
  only after all permission conflicts are resolved; missing rules can then be
  created by reconciliation.

## Branch protection

Each mirror also needs one protected branch rule for `verification-metadata`. The
branch rule is evaluated on its own. A tag conflict does not block the branch rule,
and a branch conflict does not block the tag rules.

| Branch | Force push | Push and merge | Unprotect |
| --- | --- | --- | --- |
| `verification-metadata` | Not allowed | The configured bot user only | Empty, or Maintainer role and above |

Reconciliation checks two kinds of rules:

- The exact rule `verification-metadata` must match the table above, and it must
  be the only rule with that name. If it differs, the project is not ready. The
  rule is never changed.
- Any other rule whose name matches `verification-metadata` under GitLab wildcard
  matching (`*` only, case sensitive), such as `*` or `verification-*`, is an
  overlap. It must forbid force push and grant no push or merge access beyond the
  bot. The role "No one" is fine. Otherwise the overlap is reported as a conflict
  that blocks the write. Rules that do not match the branch name are ignored.

Failure handling:

- If listing tags fails, the project stops before the branch step and nothing is
  written.
- If a tag write fails, the project still gets its branch step: the branch rule is
  created or checked. Then `apply_incomplete` is true, which stops an apply batch.
- If listing branches fails before any tag write, the project's `before` snapshot
  is null, the error is recorded for that project, and an apply batch stops.
- If listing branches fails after tag rules were written, the run does not raise.
  The report keeps the created tag rules, the project is not ready, and
  `apply_incomplete` is true. That stops an apply batch.
- A created branch rule is recorded even if its readback fails.

Each project's `before` and `after` results include `branch_rules`,
`branch_created`, and `apply_incomplete`. The `ready`, `errors`, and
`writes_attempted` fields cover tags and the branch rule together, so `fleet_ready`
requires the branch rule too.

Bot rotation hazard: the tag pin rules and the branch rule each name one bot user by
ID. A token swap that creates a new bot user makes every mirror differ from the
policy. Reconciliation reports the difference, and publishes fail until each rule
is corrected by hand: PATCH for the branch rule, and delete and recreate for each
tag rule. Rotate the existing bot's token instead of creating a new bot whenever
possible. Read the downstream consumers' CI variable runbook before any rotation.

The branch rule does not stop a Maintainer from unprotecting the branch. While the
rule exists, it prevents force push and pushes from anyone other than the bot.

Canary (required operator steps, not yet done): before the pipeline image enables
the branch rule, run this check on one mirror. The tool has no project ID selector,
so it uses the cursor:

1. Run an inspect with `--after-project-id <project-id minus 1> --max-projects 1`
   and the other flags from the commands above. Check that the report's
   `selected_ids` is exactly `[<project-id>]`. An archived, excluded, or mistyped
   ID silently selects the next mirror, so stop if the list differs.
2. Repeat the same command with `--apply` and a new report path. This also creates
   any missing tag rules on that mirror, so pick a mirror whose tag rules are
   already ready, or expect those writes.
3. Read the project's protected branches with the GitLab API at
   `$FORGE_URL/api/v4/projects/<project-id>/protected_branches`. Check the grants
   in the table above.
4. Confirm that this list includes rules inherited from the group, such as a
   group-level wildcard rule. The evaluator treats every listed rule alike, so a
   group rule that the list hides would go unseen. Stop if the list omits a group
   rule that you know exists.
5. Have the pipeline bot push a commit that creates `verification-metadata` if it
   is absent, then push one that fast-forwards it. Confirm that a force push and a
   push from another user are rejected.

GitLab is expected to accept a rule for a branch that does not exist yet, so the
first push is already protected. Confirm this on the canary. It is not verified yet.

Gate: the publisher applies the same rules to one project on every publish, and
the tool never rewrites a rule. Enabling the branch rule with a wrong bot ID
therefore locks out the real writer until someone repairs each rule by hand. Do not
deploy the image that enables the branch rule before the canary passes.

## Inspect and apply

Set `GITLAB_TOKEN` in your environment or use `GIT_AUTH_TOKEN`. The token needs
the `api` scope and Maintainer access on the mirrors to create protected tags and
branch rules. Avoid `--auth-token`, which exposes the credential in the process
list, and never put a credential in a report. Each run requires a **new** report path;
an existing file is never overwritten. The initial report and each project's
before snapshot are saved before any changes. Each update replaces the report
atomically, so a failed update leaves the previous snapshot available. Reports
contain the validated target, UTC start time, request rate, project outcomes,
created rules, errors, and remaining IDs. If a snapshot update fails after a
remote change, the previous report may not include that change. Inspect live
state before retrying.

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
  --apply --max-projects 25 \
  --max-requests-per-minute 200
```

The limit is required with `--apply` and must be between 1 and 25. It can also
bound a read-only inspection. A project that already has the two release rules
needs 15 tag requests to create the six remaining tag rules: one project read, one
tag list during inspect, one tag list during apply, and a create and a readback for
each rule. A mirror missing every rule needs 23 requests: 19 for tags (the same
steps, with eight creates and eight readbacks) and 4 for the branch rule (one list
during inspect, one list during apply, one create, and one readback). A batch of 25
can therefore reach 575 requests plus the inventory pages. At the default rate,
575 requests take about 2 minutes.

The run paces every GitLab request to `--max-requests-per-minute`. This value
defaults to 300 and must be between 1 and 450. Pacing covers inventory pages, GET
retries, identity checks, creates, and readbacks. Request starts are spaced at
least 60 divided by that value seconds apart, so a batch of 25 no longer bursts
over the limit of 500 requests per minute that the GitLab instance allows per
token. Leave headroom for other users of the same token, and lower the value if
other jobs run at the same time. A larger audit takes longer in proportion:
minutes equal total requests divided by the rate. For example, 3,000 requests
take about 10 minutes at the default rate. A read-only audit sends two list
requests per mirror, one for tags and one for branches. So 5,000 requests take
about 17 minutes at the default rate, which covers about 2,500 mirrors plus the
inventory pages.

The publisher applies the same rules for one project per publish and does not
use this limit. GET requests retry a rate limit at most twice, using a bounded
delay. POST requests are deliberately not retried, even after a rate limit.
Earlier creates in the batch may already have succeeded, and a transport failure
can leave a write's outcome unknown. The run attempts a policy readback and
records the error instead of replaying the write. Each run lists the entire
inventory before changing any project. Direct active Python mirrors are processed
in numeric ID order; Java, shared, and subgroup projects are excluded, and
archived mirrors are recorded separately and never changed. A direct
`pypi.org-*` project with an unexpected path, such as uppercase letters, is
recorded as `unreviewed-python-path`. It is not changed and it prevents a
fleet-ready claim until someone reviews it.

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

1. Build and approve a replacement image from the reviewed source commit. Pass the
   branch rule canary in [Branch protection](#branch-protection) with that build
   before any pipeline deployment.
2. Update the task's image digest and supported policy-selection wiring together.
3. Update the managed pipeline's task revision and forward the explicit policy
   selection, release creator IDs, and bot user ID from the private release
   configuration. Enable the production profile only in production; do not enable
   it for scratch or pass new parameters to an older task.
4. Run a complete read-only audit of the existing fleet and review every mirror
   with conflicting or unreviewed permissions. The profile requires exact
   creators, so a conflicting mirror fails real publishes once it is enabled.
5. Validate a provisioning canary through the publisher path in the pipeline. This
   is separate from the `reconcile-mirrors` canary in
   [Branch protection](#branch-protection). Then apply the fleet reconciliation in
   bounded batches, and finish with a new uncapped audit.

Do not expose a policy setting that the selected image silently ignores. This
document does not claim deployment activation or fleet completion.

Non-goals: a read-side check that refuses to trust an unprotected
`verification-metadata` branch is not part of this change. Reconsider it after the
backfill, because until then unprotected mirrors would stop every Python flow that
reads them. The Python read and write consumers need their own review. See LWLP-4759
for reference.
