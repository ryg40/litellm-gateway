# Secret handling

`scripts/scan.sh` stops a secret or a host value before it goes to a remote or into an image. It runs the same way on a Linux host, on a Mac and in a CI system.

## What the scan finds

| Finding | Source | Result |
| --- | --- | --- |
| Secret | The rules in `.gitleaks.toml` (the default rules of gitleaks v8.28.0) | Always fails. |
| Host value | One literal pattern for each line of `scripts/host-values.deny` and of the untracked `.local/host-values.deny`, and one regular expression for each line of `scripts/host-values.regex` | Fails at level `fail`. Prints a warning at level `warn`. |
| Policy | A tracked file named `.gitleaksignore` or `.gitleaksbaseline`; a tracked file in `.local/`; a tracked archive, database dump or Git bundle | Always fails. |

A host value is text that identifies a private host: its private network, its host name and its state directories. The history of `local-dev` can contain host values. A portable branch, a portable tag and an image must not contain them.

The output gives the file, the line, the rule and, in history mode, the commit. It does not give the secret value (`--redact`). A finding in a commit message shows as `(commit message):<line>`. A finding in a file name shows as `(file names):<n>`: line `<n>` of `git ls-files` (tree), of `git diff --cached --name-only` (staged), or of the files that the commit changes (history).

### Deny lists

The literal patterns come from two files with the same format:

| File | Tracked | Content |
| --- | --- | --- |
| `scripts/host-values.deny` | yes | Generic patterns only. It holds no pattern now, only its header. |
| `.local/host-values.deny` | no (`.gitignore` ignores `.local/`) | The values of one host: its private network, its host name and its state directories. |

A pattern of a real host in a tracked file puts that host value into the tree that the scan forbids. So the values of a host are only in `.local/host-values.deny` of each checkout on that host. Create the file on each host that works on `local-dev`.

The scan reads `scripts/host-values.deny` and the local file of the checkout that holds `scripts/scan.sh`. The local file is optional: a target clone, a CI job, the branch `portable`, a tag `portable-v*` and the mode `image` need no local file. Without it, the scan uses the tracked list and `scripts/host-values.regex`. The deny lists and the regex list together must give at least one rule, else the scan stops with exit 2.

The rule ids are `host-value-<n>` for the tracked list and `host-value-local-<n>` for the local list. The output gives the pattern of a finding. Thus a finding of a local rule prints a host value on the screen of that host; it does not go into a file.

### Private addresses

`scripts/host-values.regex` finds addresses of the private ranges 10/8, 172.16/12 and 192.168/16, of the shared range 100.64/10, and IPv6 unique local addresses (prefix fc00 with length 7). Loopback and documentation addresses give no finding. This document writes the ranges in this short form, because the scan reports a range in address form.

A version number can look like an address. Thus an IPv4 address gives a finding only when the character before it is not a letter, a digit or one of `_ . - + : = < > ~ ^ !` (a single `=`, as in `host=`, is permitted), and the character after it is not a letter, a digit, `_`, `-`, or `.` followed by a letter or a digit. So `pkg==10.2.3.4`, `pkg>=10.2.3.4`, `image:10.2.3.4`, `v10.2.3.4` and `10.2.3.4.dev0` give no finding; an address in a URL, after a blank, a quote, `@` or `=` gives one. `tests/test_scan.sh` checks private address ranges and exclusions for version numbers and public example addresses.
Not verified: all possible address formats and false matches in other source trees.

### Policy findings

The scan refuses what it cannot read or what switches it off:

| Policy | Modes | Why |
| --- | --- | --- |
| A tracked file named `.gitleaksignore` or `.gitleaksbaseline` (any directory, any case) | all repository modes | A `.gitleaksignore` with `<commit>:<file>:<rule>:<line>` hides a finding. |
| A tracked file in `.local/` at the root | all repository modes | `.local/` holds values of one host. The scan does not report host values in `.local/host-values.deny`, so a tracked copy of it must not pass. |
| A tracked archive, database dump or Git bundle | all repository modes | The content is compressed or binary. |

An archive, a dump or a bundle is known by its extension or by its first bytes:

- Extensions: `zip jar war ear whl egg apk aab ipa nupkg tar tgz taz tbz tbz2 tb2 txz tlz tzst gz bz2 xz zst lz lz4 lzma lzo z 7z rar cab cpio ar deb rpm iso dmg sql dump pgdump backup db sqlite sqlite3 db3 mdb accdb rdb bundle pack` (any case). `.gitignore` ignores them.
- Content: gzip, compress, zip, bzip2, xz, zstd, 7z, rar, ar and deb, lz4, lzip, rpm, cab, tar (`ustar` at byte 257), PostgreSQL custom dump (`PGDMP`), SQLite, Git bundle v2 and v3, Git pack, Redis dump, and a text dump with the header of `pg_dump`, `mysqldump` or `mariadb-dump` in the first 264 bytes.

Content does not switch the scan off. A "gitleaks:allow" comment does not hide a finding (`--ignore-gitleaks-allow`), also not a host value. gitleaks reads a `.gitleaksignore` from the scanned directory; the scan gives it the Git directory (modes `staged`, `history`) or a directory of its own (`tree`), so a `.gitleaksignore` in the working tree does not apply, also when it is not tracked.

## Modes

```sh
scripts/scan.sh tree                   # tracked files (working tree) and the staged content
scripts/scan.sh staged                 # the staged changes only (pre-commit, pre-merge-commit)
scripts/scan.sh history [<range>]      # commits in <range>, git log syntax, default HEAD
scripts/scan.sh image <ref> [<base>]   # the file system of a local image
scripts/scan.sh all                    # tree and history
scripts/scan.sh selftest               # negative control
```

Exit code: `0` clean, `1` finding, `2` usage or tool error.

Untracked and ignored files (`.env`, `secrets/`, `state/`) are not in the scan of `tree`. They cannot go to a remote. The `.gitignore` rules keep them out of the index.

What each mode reads:

| Content | `tree` | `staged` | `history` |
| --- | --- | --- | --- |
| File content | working tree and index | staged changes | each commit |
| Content of an archive | yes, 3 levels deep (`--max-archive-depth 3`) | no | no |
| Target text of a symlink | yes | yes | yes |
| File names | tracked files | staged files | changed files of each commit |
| Commit messages | no | no | yes |
| Policy checks | tracked files | all files of the index | new files of each commit |

`all` is `tree` and `history HEAD`.

### Blind spots

- `staged` and `history` do not read into an archive. The policy check stops a tracked archive by its extension or its content in each mode, so it cannot enter unseen through a hook or a CI scan. An archive of a format that is not on the list, with a name that is not on the list, is not found; a binary blob of an unknown format is scanned as text only.
- A secret that is encoded (base64 of a token, split over lines, encrypted) is not found, unless a gitleaks rule matches the encoded form.
- The author and committer names and e-mail addresses, tag messages and notes are not scanned. Check commit metadata separately before you publish history.
  The promotion command adds identity and message checks with `public_check.sh`.
- A pin with a blank after the operator (`pkg >= <version>`) of a version that is also a private address gives a false finding. An IPv4 address directly after `:`, `-` or a letter (`host:10.1.2.3`) gives no finding.
- The hooks run only where `scripts/install-hooks.sh` ran, and `--no-verify` skips them (section "Hooks").

### Levels for host values

| Where | Default level |
| --- | --- |
| Branch `portable` | `fail` |
| A tag `portable-v*` at `HEAD` | `fail` |
| Mode `image` | `fail` |
| Other branches | `warn` |

`--level warn` or `--level fail` sets the level. `SCAN_REF=<ref>` sets the ref that gives the default level, for example `SCAN_REF=refs/tags/portable-v0.1.0` in a CI job with a detached `HEAD`.

### Image mode

`scripts/scan.sh image <ref>` scans the whole file system of the image. Docker 28 or later mounts the image read-only (`--mount type=image`) and starts no container of it. An older Docker uses `docker create` and `docker export`.

The LiteLLM base image contains many third-party test keys and private-network addresses in `site-packages`. At level `fail` a scan of the whole image cannot pass. For an image built `FROM` the LiteLLM image, scan only the layers that the build adds:

```sh
scripts/scan.sh image litellm-custom:dev ghcr.io/berriai/litellm:<version>
```

The script compares `RootFS.Layers` of the two images. The base layers must be the first layers of the image, else the script stops. It then reads only the add-on layers from `docker save`.

## Rules for changes

1. Allowlist by content only: `regexes` or `stopwords` in `.gitleaks.toml`. Do not add `paths`. A path allowlist hides each future secret in that path.
2. Keep each allowlist entry narrow. Write the reason in a comment above it.
3. Do not set `targetRules`. `scripts/scan.sh` limits each allowlist to the secret rules, so that no allowlist hides a host value.
4. After each change of `.gitleaks.toml`, `scripts/host-values.deny`, `.local/host-values.deny`, `scripts/host-values.regex` or `scripts/scan.sh`, run `sh tests/test_scan.sh`. `tests/test_scan.sh` uses a copy of the scan with a local deny list of its own, so the local file of the checkout does not change its result. After a change of `.local/host-values.deny`, also run `scripts/scan.sh selftest`. Its negative control puts a synthetic token in a temporary file and requires a finding. The pre-commit hook runs `scripts/scan.sh selftest` when one of the tracked files is staged.
5. A deny pattern is a literal string, case-insensitive. Quotes and backslashes are not allowed. `scripts/scan.sh selftest` requires a finding for each pattern of both deny lists. Do not add a value of a real host to `scripts/host-values.deny`; add it to `.local/host-values.deny`.
6. A regex rule in `scripts/host-values.regex` has a name, a sample and a regular expression (format in the file). `scripts/scan.sh selftest` requires a finding for each sample. The file must not contain an address.
7. Do not write a token or a host value into a tracked file, also not into a test. Build it at run time from parts, as `tests/test_scan.sh` does.
8. Do not use `gitleaks:allow`, `.gitleaksignore` or a baseline to accept a finding. The scan ignores the comment and refuses the files. Correct the content, or add a narrow content allowlist (rules 1 and 2).

A deny list contains the patterns that it forbids. The scan does not report host values in `scripts/host-values.deny` and in `.local/host-values.deny`. This exception is for host-value rules and these two paths only: the secret rules scan the files, and a copy at another path is reported. The mode `tree` reads tracked files only, so it does not read the untracked local file. A tracked file in `.local/` is a policy finding. The mode `image` has no exception.

`.gitleaks.toml` is the default configuration of gitleaks v8.28.0 with the differences that its header lists: each path allowlist is removed, and the global allowlist entry `(?i)^true|false|null$` is replaced with `^(?i:true|false|null)$`. The default configuration skips `lib/python3.*`, `*.dist-info`, `node_modules` and more by path; with it, an image scan does not read the Python packages. The default entry for `true|false|null` has no group, so it hides each secret that contains `false` or `null`. To update gitleaks, take `config/gitleaks.toml` of the new version, apply the differences again, and change the pinned digest in `scripts/scan.sh`.

## Public check

`scripts/public_check.sh` adds publication rules without changing the secret scanner.
It needs POSIX `sh`, Git and Python 3.9 or later.

```sh
scripts/public_check.sh --level warn tree
scripts/public_check.sh --level fail tree
scripts/public_check.sh --ref portable --message-file .local/release-message.txt
scripts/public_check.sh selftest
```

Without `--ref`, the check reads the index and working tree. With `--ref`, it reads the exact Git blobs and commit metadata.
It runs `scan.sh --level fail` on a temporary repository with the blobs and message text.
It also scans the working tree without `--ref`. Scanner failures stop the check; matched values remain hidden.

`scripts/public-patterns.tsv` holds generic regular expressions in three tab-separated fields: class, scope and expression.
ISO, slash, dot and month-name dates use scope `all`. Compact dates use `docs` only.
Documents include `.md`, `.rst`, `.txt`, `.markdown`, `.mdx`, `.adoc` and extensionless files.
Messages and identities use `all` patterns, not the compact-document rule.
Matching is per-line. Patterns cannot find every semantic reference or a phrase wrapped across lines.
The classes cover dated records, private tracker wording, process wording and machine paths.
Installation-specific values stay in the ignored local deny list.

`scripts/public-allow.tsv` accepts a path-bound exact line by SHA-256 hash plus a reason, separated by a tab.
Hash the repository-relative UTF-8 path, one NUL byte, then the exact UTF-8 line without its line ending.
This exception applies only to wording, never private deny values, secrets, forbidden paths or identities.
No file-wide exception exists. Editing or moving an accepted line requires a new review and hash.
Copying it into another file does not inherit the exception.
The tracked reasons identify upstream citations, scanner regex fixtures and synthetic format examples.

The check refuses private state directories, environment files, credential directories and key files, even at `warn`.
Credential paths include `.netrc`, `.git-credentials`, `credentials.json`, `.envrc`, `.npmrc`, `.pypirc`, `.htpasswd` and `.aws/`.
The policy also covers `.docker/config.json`, SSH identity files, their public keys and `.kdbx` files.
Public keys are not secrets, but their identity must not enter accidentally.
No credential-path exceptions exist. Use noncredential filenames for synthetic examples; all content checks still apply.
It refuses Git submodules because their content is outside the scanned tree.
UTF-16 BOM and plausible NUL-interleaved text are decoded strictly and checked with wording and private literal rules.
The secret scanner reads both original bytes and decoded text. Unsupported or malformed text encoding fails closed.
No binary-file exemption exists. Ordinary UTF-8 content with NUL bytes still reaches the scanner.
It reads symlink target text, not the target file. Unsupported control characters in file names cause an error.

`--identity-name` and `--identity-email` check the planned identity; the default is the neutral publication identity.
`--accept-identity` explicitly permits another email domain. Record each acceptance in the release record.
`--message-file` checks tag text. `--commit-message-file` checks planned commit text.
In ref mode, the check also reads the commit's author, committer and message.
The promotion creates an unsigned tag with the same checked identity and message.

Exit codes: `0` clean or wording warnings, `1` finding, `2` usage or tool error.
Only wording changes to a warning at `--level warn`. Secrets, host values, paths and identities still fail.
The self-test covers the generic classes, clean text, identities and a synthetic literal deny value.
It also runs the scanner self-test with output withheld.

Not verified by patterns: whether an otherwise valid provider choice describes a private installation. Read the release tree before publication.
The pre-push hook enforces ref names and the existing secret scan; it does not replace the full promotion check.

## Scanner

The scanner is `gitleaks` in the image `zricethezav/gitleaks:v8.28.0`, pinned by digest in `scripts/scan.sh`. Each run uses `--network none`, read-only mounts and `--redact`. With the image present, the scan needs no network.

| Variable | Use |
| --- | --- |
| `GITLEAKS_IMAGE` | Another image reference, for example a mirror. Pin it by digest. |
| `SCAN_ENGINE` | `auto` (default: the container, else a `gitleaks` binary of version v8.28.0), `docker` or `binary`. |
| `SCAN_NAME_PREFIX` | Name prefix of the helper containers. |

To get the image once:

```sh
docker pull zricethezav/gitleaks:v8.28.0@sha256:cdbb7c955abce02001a9f6c9f602fb195b7fadc1e812065883f695d1eeaba854
```

On a Mac, the temporary directory (`$TMPDIR`) and the repository must be in a path that Docker shares. Docker Desktop shares `/Users` and `/var/folders` by default. For Colima or another runtime, check the shared paths.

Host needs: a POSIX shell with `od` and `readlink`, and Git 2.5 or later (`git worktree`, `--git-common-dir`; tested with Git 2.39.5). The script does not use `git rev-parse --path-format=absolute` (Git 2.31). A path with a blank, a comma or a colon works for the repository and for `$TMPDIR` (tested); a path with a double quote is not tested.

## Hooks

```sh
scripts/install-hooks.sh           # install the dispatcher, set core.hooksPath
scripts/install-hooks.sh --check   # exit 0 if installed and current
scripts/install-hooks.sh --uninstall
```

| Hook | Scan |
| --- | --- |
| `pre-commit` | `scripts/scan.sh staged`. The level follows the current branch. |
| `pre-merge-commit` | The same as `pre-commit`. `git merge` runs this hook, not `pre-commit`, when it records a merge commit without a conflict. |
| `pre-push` | Only portable sources and valid `portable-vX.Y.Z` tags may go outside `origin`. Object-ID sources support checked promotion. All deletions outside `origin` are refused. The existing history scan still runs for each permitted ref. A new portable ref is scanned in all of its history. |

The install script copies `scripts/git-hooks/dispatch` to `<git-common-dir>/scan-hooks/`, once for each hook, and sets `core.hooksPath` to that absolute directory. Git stores this in the shared configuration, so it applies to the main checkout and to each worktree. The dispatcher runs `scripts/git-hooks/<hook>` of the working tree where the hook runs. Thus each worktree uses the hooks and `scripts/scan.sh` of its own branch. Each worktree also uses its own `.local/host-values.deny` at the top of the worktree. A worktree without that file scans with the tracked rules only; copy the file into each worktree on the host.

| State of the branch | Result |
| --- | --- |
| The hook and `scripts/scan.sh` are in the working tree | The scan runs. |
| The hook or `scripts/scan.sh` is missing from the working tree, but is in the index or in `HEAD` | The commit, merge or push stops (fail closed). This includes the commit that deletes `scripts/scan.sh`. |
| The branch never had them (for example `main`) | No scan. The hook prints `this branch has no scripts/git-hooks/<hook> and no scripts/scan.sh; no scan`. |
| A branch from before `pre-merge-commit` existed | The dispatcher runs its `pre-commit` for a merge. |

A hook fails closed: if Docker or the scanner image is not available, the commit or the push stops.

After a change of `scripts/git-hooks/dispatch`, or after the repository moves to another path, run `scripts/install-hooks.sh` again. `--check` reports both cases. If the path in `core.hooksPath` does not exist, Git runs no hook and prints nothing.

If `core.hooksPath` points at `scripts/git-hooks`, the install script replaces it and `--uninstall` removes it.

Warning: `git commit --no-verify` and `git push --no-verify` skip the hooks. A CI job must run the scan again.

## CI

A CI job runs the same commands:

```sh
docker pull zricethezav/gitleaks:v8.28.0@sha256:cdbb7c955abce02001a9f6c9f602fb195b7fadc1e812065883f695d1eeaba854
SCAN_REF=$CI_REF scripts/scan.sh all
scripts/scan.sh image <built image> <base image>
```

The job needs the full history for `history` (no shallow clone), or a range that the job has.

## Test

```sh
sh tests/test_scan.sh
```

The test uses temporary repositories and a temporary bare remote. It does not change the configuration of this repository.
