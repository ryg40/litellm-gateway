# Branches, tags and remotes

`local-dev` keeps the development history. `portable` publishes an exact copy of its files without that history.
Only `portable` and its release tags go to a shared Git server.

## Remotes

| Remote | Use |
| --- | --- |
| `upstream` | Upstream LiteLLM. Fetch only. See [remotes.md](remotes.md). |
| `origin` | The private integration server, or the source of a target clone. |
| `github` | The default publication remote. Configure its URL before promotion. |

The promotion command accepts a different remote with `--remote NAME`.
It refuses `upstream`, `DISABLED`, credentials in a URL, multiple push URLs and a mirror remote.
Use a credential helper or an SSH configuration for authentication. Do not put user information in the URL.
Supported network forms are `https://host/path` and `ssh://host/path` without user information.
For SSH, set `User` in your SSH configuration. SCP-style URLs and `ssh://user@host/path` are refused.
Local paths and `file://` targets support isolated tests. Chained URL rewrites are refused.
The plan prints a scheme, host and URL digest, not the URL path.

## Branches

| Branch | Content | Rule |
| --- | --- | --- |
| `local-dev` | Private integration history. | Never push it to a shared Git server. |
| `topic/*` | One change. | Start at `local-dev`, then integrate after review. |
| `portable` | Snapshot commits with the files of `local-dev`. | Shares no development history. |

By default, a snapshot has the previous `portable` commit as its only parent.
The first snapshot has no parent. A deleted source file stays absent from the snapshot.
The commit message is `Promote local-dev <revision>`.

`--new-root` builds a snapshot without a parent. It does not erase the old commits or tags.
When the remote has `portable`, a preview needs `--new-repository` as an explicit assertion.
A new-root push needs a remote with no refs, including tags. An assertion cannot bypass this check.
The script never force-pushes. Non-push previews and local application retain the explicit assertion rule.
Keep the old chain and its tags on the private server if you choose a new public history.

Warning: a new root does not fast-forward from the old chain. Clone again at each target.
A work Mac is one example target.
On an old `portable` clone, `git pull` stops with `refusing to merge unrelated histories`. Clone again.

## Promotion

Use `scripts/promote.sh` in a separate clean clone with `HEAD` at `local-dev`.
A linked worktree also works when no checkout holds `portable`.
A hook override must match the current repository hooks that `scripts/install-hooks.sh --check` accepts.
Foreign or stale settings are refused, including settings from global or system configuration.
The command refuses a primary checkout with runtime files, a shallow clone and a dirty tree.
Copy the ignored `.local/host-values.deny` into this clone. At least one non-comment pattern is required.
Use this clean-clone procedure before the preview:

```sh
git clone --no-local --no-tags --branch local-dev <private-source> <release-clone>
cd <release-clone>
git remote add github <publication-url>
git config remote.github.fetch '+refs/heads/portable:refs/remotes/github/portable'
# Skip this fetch when the publication remote is empty; use --new-root.
git fetch --no-tags github
# Only when github/portable exists and its history is the intended publication chain:
git branch portable refs/remotes/github/portable
mkdir -p .local
cp <private-deny-file> .local/host-values.deny
chmod 600 .local/host-values.deny
scripts/install-hooks.sh
scripts/install-hooks.sh --check
```

Confirm the publication URL through trusted configuration before fetching. Do not print the private deny file.
Do not use `origin/portable` unless `origin` holds the exact intended publication chain.
When the publication remote is empty, `git fetch --no-tags github` fails because `refs/heads/portable` is absent.
Skip the fetch and `git branch` commands in that case, and use `--new-root` for promotion.
With no local or cached portable history and no release tags, chain mode reports a first root.
A source clone can contain `origin/portable`; chain mode then refuses a missing local `portable`, even with an empty publication remote.
Use `--new-root` in that case rather than restoring an unrelated publication chain.
If remote or cached portable history exists without local `portable`, chain promotion stops with a restoration instruction.
Local or remote release tags also stop an implicit first root when the local branch is absent.

1. Write a release message in a file outside the tracked tree.
2. Run the default preview with the next version.

   ```sh
   scripts/promote.sh --version 0.3.0 --message-file .local/release-message.txt
   ```

   The preview checks the exact source tree, release messages, identities and outgoing snapshot history.
   It creates unreachable Git objects, but changes no branch, tag or remote ref.
3. Read the plan. Check the URL, visibility, source, parent, new commit, identities and two refspecs.
4. Run the command with `--push` only after you approve that plan.

   ```sh
   scripts/promote.sh --version 0.3.0 --message-file .local/release-message.txt --push
   ```

   The command creates local refs, runs `git push --dry-run`, then pushes those two refs atomically.
   It disables automatic tag following. A failure leaves local refs for inspection; it deletes nothing.

`--apply` creates only the local branch and tag. It does not push.
An existing release tag stops a later run with the same version, including after a failed push.
Stop after a push failure. Preserve local refs and ask a maintainer to inspect local and remote object IDs.
Do not retry with a manual push. This command has no checked resume mode.
A maintainer must establish a new checked release plan before any further publication.

| Option or variable | Meaning |
| --- | --- |
| `--version X.Y.Z` | Required version; must exceed each local and remote portable version. No leading zeros. |
| `--message-file FILE` | Required nonempty tag message. The public check scans it as text. |
| `--remote NAME` | Publication remote; default `github`. |
| `--apply` | Create local refs after all checks. |
| `--push` | Create local refs, preview the push, then push. |
| `--new-root` | Use no parent. Default is the existing snapshot chain. |
| `--new-repository` | Permit a new-root preview when remote `portable` exists. Never permits a force push. |
| `--public-remote` | Accept public or unverified visibility for this push. |
| `--name NAME`, `--email EMAIL` | Set the author, committer and tagger identity. |
| `PORTABLE_PUBLISH_NAME`, `PORTABLE_PUBLISH_EMAIL` | Identity defaults before command options. |
| `--accept-identity` | Explicitly accept an email domain outside `.invalid` and `.example`. Record the reason. |

The default identity is `litellm-gateway portable <portable@litellm-gateway.invalid>`.
The command ignores the caller's author, committer, signing and tag-signing settings.
The snapshot and annotated tag have no signature. Generated commit and tag times use UTC.
A nondefault identity name or email local part gives a warning. The private deny list still checks both.
The warning and `--accept-identity` do not waive wording or private-value checks.

For GitHub URLs, `gh repo view --json isPrivate` supplies the visibility.
Without a successful check, the plan says `Not verified: visibility`.
`--push` requires `--public-remote` when visibility is public or unverified.
The command never changes visibility and never creates or deletes a repository.

After a new-root public push, update the private server with `git push origin --delete portable`, then `git push origin <snapshot-id>:refs/heads/portable`.
`<snapshot-id>` is the id of the pushed snapshot commit, the same commit as on the public remote.
The hook permits the deletion on `origin` only.

Before and after a push, the remote may contain only `HEAD`, `refs/heads/portable` and `refs/tags/portable-v*`.
A peeled annotated tag ref is permitted. Any other ref stops the command, including `refs/pull/*`. It deletes nothing.
Remote access is required even for preview and `--apply`, because both check remote refs and versions.
Exit codes: `0` success, `1` refused operation or finding, `2` usage or tool error.

## Release checklist

1. Review the source changes and update `EXPLAINER.md` using its section "Keep this file current".
2. Run the tests in [tests/README.md](../tests/README.md), including the image build and Compose configuration checks.
3. Run `scripts/scan.sh selftest` and `scripts/scan.sh --level fail tree`.
4. Run `scripts/public_check.sh selftest` and `scripts/public_check.sh --level fail tree`.
5. Review each accepted line in `scripts/public-allow.tsv`. A changed line loses its exception automatically.
6. Run the promotion preview and approve the exact source, history mode, identity, visibility and refs.
7. Verify the release from a clean clone. Build and service changes need separate approval.
8. Record the tag, commit, identities, accepted findings, test results and remote refs in your private release record.
9. Add one row for the release to the section Release record.

Pattern checks cannot decide whether provider choices describe one installation. Review the text as a new reader.
On the first push to an empty remote, chain mode checks every outgoing snapshot tree and identity.
Old personal identities or old wording can stop that check. Fix the publication plan; do not silently skip history.

## Release record

| Tag | Snapshot id | Date | Note |
| --- | --- | --- | --- |
| `portable-v0.3.0` | `a61e506d` | 2026-10-06 | first public snapshot, new root |

Tags before the public root are private only and never leave the private server.

## Tags and transfer

`portable-vX.Y.Z` is an annotated release tag on a snapshot, for example `portable-v0.1.0`.
`scripts/build.sh` uses `X.Y.Z` in the image tag. See [image.md](image.md).

To transfer an already checked, existing snapshot without a network connection:

```sh
git bundle create litellm-portable.bundle portable portable-v0.1.0
```

Replace the example tag with your release tag.
The pre-push hook permits only portable sources and valid `portable-vX.Y.Z` tags outside `origin`.
It also permits object-ID sources used by the checked promotion command. It refuses all deletions outside `origin`.
A hook scan does not replace the promotion checks.
It scans portable history at level `fail`. See [secret-handling.md](secret-handling.md).

Warning: the `origin` exception does not make development history safe for a public server. Check the URL before each push.
