# Git remotes

This repo has two remotes:

| Remote | URL | Use |
| --- | --- | --- |
| `origin` | the Git server of this clone | Fetch and push of this repo. |
| `upstream` | `https://github.com/BerriAI/litellm.git` | Fetch only. Read upstream LiteLLM releases to compare files. |

A clone does not copy the remote configuration. Run the setup script once in each new clone:

```sh
scripts/setup-remotes.sh          # create or repair the remote upstream
scripts/setup-remotes.sh --check  # change nothing; exit 1 if the configuration is wrong
```

The script does not change `origin`. It prints the `origin` URL without credentials, and it warns if `origin` is absent. A second run gives the same configuration.

To use a mirror, set `UPSTREAM_URL` for both scripts:

```sh
UPSTREAM_URL=https://mirror.example/litellm.git scripts/setup-remotes.sh
```

## A clone from a Git bundle

A Git bundle moves `portable` and its release tags to a Git server without a network path between the two hosts
([branches.md](branches.md)). After a clone from the bundle, `origin` points at the bundle file, not at a Git server.

1. Clone the branch `portable` from the bundle.

   ```sh
   git clone -b portable litellm-portable.bundle litellm
   cd litellm
   git remote -v
   ```

   `git remote -v` shows the path of the bundle file for `origin`. The clone has the branch `portable`, the
   remote-tracking branch `origin/portable` and the tags of the bundle.
2. Create the remote `upstream`. The script prints the bundle path as the `origin` URL and does not change it.

   ```sh
   scripts/setup-remotes.sh
   scripts/setup-remotes.sh --check
   ```

3. Install the Git hooks. The pre-push hook scans each pushed ref at level `fail` ([secret-handling.md](secret-handling.md)).

   ```sh
   scripts/install-hooks.sh
   ```

4. Point `origin` at the empty repository on the Git server, for example a private repository on GitHub.

   ```sh
   git remote set-url origin https://github.com/<organization>/<repository>.git
   ```

5. Push the branch `portable` and each release tag by name. Do not use `git push --all`, `--mirror` or `--tags`.

   ```sh
   git push origin portable
   git push origin refs/tags/portable-v0.1.0
   ```

   Replace `portable-v0.1.0` with the name of each release tag in the bundle (`git tag --list 'portable-v*'`).
6. Check the remote. The result lists only `refs/heads/portable` and the `portable-v*` tags, each tag also with its `^{}` line.

   ```sh
   git ls-remote origin
   ```

`portable` keeps `origin/portable` as its upstream branch, so `git pull` and `git push` use the Git server after step 4.

## Configuration of `upstream`

```ini
[remote "upstream"]
	url = https://github.com/BerriAI/litellm.git
	pushurl = DISABLED
	tagOpt = --no-tags
	fetch = +refs/upstream-none/*:refs/upstream/none/*
```

| Setting | Reason |
| --- | --- |
| `pushurl = DISABLED` | `git push upstream` fails, because `DISABLED` is not a valid repository. The value is a relative path. If the working directory holds a local directory with the name `DISABLED`, a push reaches that directory, never the upstream server. |
| `tagOpt = --no-tags` | Upstream tags do not enter `refs/tags/`. `refs/tags/` holds only the `portable-v*` release tags of this repo. |
| `fetch = +refs/upstream-none/*:refs/upstream/none/*` | No upstream ref matches this pattern. A plain `git fetch upstream` lists the upstream refs and transfers no objects. |

### Why the default fetch refspec matches nothing

The upstream history is large: many thousand commits and tags. This repo needs only single release snapshots of it.

- With the usual refspec `+refs/heads/*:refs/remotes/upstream/*`, a plain `git fetch upstream` downloads the full history of every upstream branch.
- With no refspec at all, `git fetch upstream` fetches the upstream `HEAD` into `FETCH_HEAD`. That also downloads the full history of `main`.
- A wildcard refspec that matches no ref is valid. Git exits with code 0 and transfers nothing.

The destination `refs/upstream/none/*` is under `refs/upstream/`. If a ref ever matches, it still cannot enter `refs/heads/`, `refs/tags/` or `refs/remotes/`.

## Fetch one upstream release

```sh
scripts/fetch-upstream.sh v1.103.0
```

Start the script by its path, not through a symlink. The script finds `setup-remotes.sh` in the directory of the path that starts it.

The script:

1. Refuses a name that does not match `v<digits>.<digits>.<digits>` with an optional suffix, for example `v1.80.0-stable`. It also refuses a name that contains white space, for example a newline. The exit code is 2.
2. Runs `scripts/setup-remotes.sh --check`, and stops if the remote `upstream` is wrong.
3. Fetches the one tag with `--depth 1 --filter=blob:none --no-tags` into `refs/upstream/tags/<tag>`.
4. Prints the commit ID of the tag.

`--depth 1` fetches the tagged commit without its parents. `--filter=blob:none` fetches the commit and its trees, but no file contents. Git gets a file content from `upstream` the first time that a command needs it. One tag adds approximately 0.5 MB to `.git`.

The first filtered fetch changes the repository configuration. Git sets these values itself:

- `core.repositoryformatversion = 1`
- `remote.upstream.promisor = true`
- `remote.upstream.partialclonefilter = blob:none`

`--depth 1` also writes the upstream commit IDs to `.git/shallow`. This file marks only the upstream commits as shallow. It has no effect on the history of this repo.

List the fetched releases:

```sh
git for-each-ref refs/upstream
```

## Compare one upstream file between two versions

```sh
scripts/fetch-upstream.sh v1.101.0
scripts/fetch-upstream.sh v1.103.0

f=litellm/completion_extras/litellm_responses_transformation/transformation.py
git diff refs/upstream/tags/v1.101.0 refs/upstream/tags/v1.103.0 -- "$f"

# Show the file at one version:
git show "refs/upstream/tags/v1.103.0:$f"

# List the files that changed between the two versions:
git diff --stat refs/upstream/tags/v1.101.0 refs/upstream/tags/v1.103.0
```

`git diff` and `git show` need network access to `github.com` for a file content that is not yet in `.git`. `git diff --stat` of many files fetches many file contents.

## Remove the upstream data

Warning: delete the refs under `refs/upstream/` before `git remote remove upstream`. In the other order, `git fsck` reports broken links and `git gc` fails.

```sh
git for-each-ref --format='%(refname)' refs/upstream | xargs -n 1 git update-ref -d
git remote remove upstream
```

`git remote remove` also deletes the `promisor` and `partialclonefilter` settings. Objects stay in `.git` until `git gc` removes them.
