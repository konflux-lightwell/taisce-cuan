# AGENTS.md

## This repository is public

Never commit internal metadata to code, tests, docs, comments, examples, commit
messages, or PR text. Treat all of these as internal:

- GitLab user IDs, group IDs, project IDs, and bot or service account identities
- Internal GitLab paths, such as repository paths and file or constant names in
  private repositories
- Internal deployment details, such as cluster names, release configuration
  paths, image digests, and registry paths for internal builds
- Tokens, keys, and secrets (these are never allowed, even in tests)

Instead:

- Take identities and IDs as run-time configuration (flag, environment variable,
  or Tekton parameter). Do not add them as defaults or constants.
- In docs, use placeholders such as `$RELEASE_IDS` and `$BOT_ID`.
- In tests, use obviously fake values such as `7001` and `example.com`.
- Refer to private systems generically, for example "the downstream consumers".
  Jira keys such as `LWLP-1234` are fine.

Before you push, search your diff for numeric IDs, internal hostnames, and
private repository names. If any of them slipped into a commit, rewrite the
history so it does not remain in earlier commits.
