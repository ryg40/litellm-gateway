# Issue tracker: Gitea

Issues live in the development repository on Gitea. An agent works them through the Gitea API.
This document is the contract for each skill that creates, reads or changes an issue.
It stays in the snapshot tree on `portable`; it holds no credentials or local work records.

## Where issues live

| Place | Role |
| --- | --- |
| The development repository on Gitea | The only issue tracker. It is the `origin` remote of a development checkout. |
| The GitHub repository | A publication mirror, not an issue tracker. Its URL has the form `https://github.com/<organization>/<repository>.git`. |

A snapshot clone from GitHub has `origin` set to GitHub. Such a clone has no issue tracker.
An agent in such a clone reports that. It does not open an issue on GitHub.

Rules:

- Create, read and change issues through the Gitea API.
- Do not open an issue on GitHub. Do not use `gh issue`, `gh pr`, `gh api`, `gh repo` or `glab` for this tracker.
- Do not write a local ticket file. The Gitea issue is the record.
- When a skill says "publish to the issue tracker", create a Gitea issue with the operations below.

## Authentication

The token is in the environment variable `GITEA_TOKEN`. Send it in the header `Authorization: token $GITEA_TOKEN`.

- Do not print the token. Do not write it into a file, an issue or a commit message.
- If `GITEA_TOKEN` is not set, stop and report it. Do not look for a token in another place.
- `GET /user` gives the account of the token. Its `login` is the account for a claim.

In a development checkout, read the server and repository from the remote:

```sh
git remote get-url origin
```

The Gitea remote has the form `<server>/<owner>/<repo>.git`.
The real server and owner values are in the `origin` remote configuration (`git remote get-url origin`).
The repository is `litellm`. Set these shell variables for the examples in this document:

```sh
API="<server>/api/v1/repos/<owner>/litellm"
OWNER="${API%/*}"
OWNER="${OWNER##*/}"
REPO="${API##*/}"
```

Replace `<server>` and `<owner>` with values from `git remote get-url origin` before you run an example.

Each path below is relative to `/api/v1` of that server. `{index}` is the number of an issue.

## Generic operations

| Operation | Method and path | Body |
| --- | --- | --- |
| Create an issue | `POST /repos/{owner}/{repo}/issues` | `title`, `body`, `labels` (label ids) |
| Read an issue | `GET /repos/{owner}/{repo}/issues/{index}` | none |
| Read its comments | `GET /repos/{owner}/{repo}/issues/{index}/comments` | none |
| List issues | `GET /repos/{owner}/{repo}/issues` | none; query `state`, `type`, `labels`, `page`, `limit` |
| Comment | `POST /repos/{owner}/{repo}/issues/{index}/comments` | `body` |
| List the repository labels | `GET /repos/{owner}/{repo}/labels` | none |
| Create a label | `POST /repos/{owner}/{repo}/labels` | `name`, `color`, `description` |
| Add labels | `POST /repos/{owner}/{repo}/issues/{index}/labels` | `labels` (label ids or label names) |
| Remove a label | `DELETE /repos/{owner}/{repo}/issues/{index}/labels/{id}` | none; `{id}` is the label id |
| Close | `PATCH /repos/{owner}/{repo}/issues/{index}` | `state` with the value `closed` |

Create an issue. The `labels` field takes label ids, not names:

```sh
curl -fsS -X POST -H "Authorization: token $GITEA_TOKEN" -H "Content-Type: application/json" \
  -d '{"title": "<title>", "body": "<body>", "labels": [<label-id>]}' "$API/issues"
```

Read an issue with its comments. A read of an issue is complete only with both requests:

```sh
curl -fsS -H "Authorization: token $GITEA_TOKEN" "$API/issues/$INDEX"
curl -fsS -H "Authorization: token $GITEA_TOKEN" "$API/issues/$INDEX/comments"
```

List the open issues, then the open issues with one label. `type=issues` leaves pull requests out.
Read each page until a page is empty. This also applies to comments and labels.

```sh
curl -fsS -H "Authorization: token $GITEA_TOKEN" "$API/issues?state=open&type=issues&limit=50&page=1"
curl -fsS -H "Authorization: token $GITEA_TOKEN" "$API/issues?state=open&type=issues&labels=ready-for-agent&limit=50&page=1"
```

The `labels` parameter takes label names, separated by commas.
With more than one name, this Gitea version returns issues that carry all of the names.
A name that does not exist is ignored, so the result can hold every open issue.
Confirm that each name exists, and check the `labels` field of each result on the client.

Comment:

```sh
curl -fsS -X POST -H "Authorization: token $GITEA_TOKEN" -H "Content-Type: application/json" \
  -d '{"body": "<text>"}' "$API/issues/$INDEX/comments"
```

Create a label. Only `wayfinder:parent:<map>` is created this way (section [Labels](#labels)):

```sh
curl -fsS -X POST -H "Authorization: token $GITEA_TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "wayfinder:parent:<map>", "color": "#<hex>", "description": "<text>"}' "$API/labels"
```

The `color` value is a six-digit hex colour with `#`. The colour has no meaning.

Find a label id, add a label, remove a label:

```sh
curl -fsS -H "Authorization: token $GITEA_TOKEN" "$API/labels?limit=50&page=1"
curl -fsS -X POST -H "Authorization: token $GITEA_TOKEN" -H "Content-Type: application/json" \
  -d '{"labels": ["ready-for-agent"]}' "$API/issues/$INDEX/labels"
curl -fsS -X DELETE -H "Authorization: token $GITEA_TOKEN" "$API/issues/$INDEX/labels/$LABEL_ID"
```

Close:

```sh
curl -fsS -X PATCH -H "Authorization: token $GITEA_TOKEN" -H "Content-Type: application/json" \
  -d '{"state": "closed"}' "$API/issues/$INDEX"
```

Build a JSON body with a JSON tool when the text has quotes or line breaks.
Do not build it by hand in the shell.

## Labels

This table lists the labels that a skill can use, with the restrictions below.
Do not apply a label outside the table. Do not create a label outside the table without approval.
Read the repository labels before use. If a required label is absent, stop and report it, except for a new parent label.

| Label | Kind | Meaning |
| --- | --- | --- |
| `size:medium` | size (earlier tickets) | Earlier tickets carry it. A new issue does not get a size label. |
| `wayfinder:map` | map | The issue is a wayfinding map. |
| `wayfinder:parent:<map>` | child | The issue is a child of map `<map>`. A spec issue with tickets uses the same label with its own number. |
| `wayfinder:grilling` | ticket type | A grilling ticket resolves a decision through questions. |
| `wayfinder:research` | ticket type | A research ticket. |
| `wayfinder:prototype` | ticket type | A prototype ticket. |
| `wayfinder:task` | ticket type (retired) | Earlier implementation tickets carry it. A new ticket does not get it. |
| `ready-for-agent` | triage | An agent can take the issue. |
| `needs-approval` | triage | Approval is required before a worker starts. |

Rules:

- An implementation ticket is a child with no type label. Keep the labels on earlier tickets unchanged.
- `needs-approval` wins over `ready-for-agent`. An agent does not claim or start an issue that carries `needs-approval`.
- This also applies when the issue carries `ready-for-agent`. Such an issue is not in the frontier.
- A skill creates `wayfinder:parent:<map>` when it charts a map or publishes a spec with tickets. It creates no other label.
- The repository can hold other labels from earlier work. A skill does not apply them.

## Wayfinding operations

### Map and children

- The map is one issue with the label `wayfinder:map`.
- Gitea has no sub-issues. A child ticket carries `wayfinder:parent:<map>`, where `<map>` is the number of the map.
- For a new map, create `wayfinder:parent:<map>` after the map issue exists. Then publish the children.
- A scratch run deletes each label that it created, with `DELETE /repos/{owner}/{repo}/labels/{id}`.
- A child carries one ticket-type label when needed: `wayfinder:research`, `wayfinder:prototype` or `wayfinder:grilling`.
- Each map and ticket has a stable title. Refer to an issue with a named link, not a bare number.
- A named link has the form `[<issue title>](<issue URL>)`. The URL is the `html_url` field of the issue.

List the open children of a map. First read the repository labels and confirm that `wayfinder:parent:<map>` exists.
If it does not exist, stop and report it. A query with an absent label name can return every open issue.

```sh
curl -fsS -H "Authorization: token $GITEA_TOKEN" "$API/labels?limit=50&page=1"
curl -fsS -H "Authorization: token $GITEA_TOKEN" "$API/issues?state=open&type=issues&labels=wayfinder:parent:$MAP&limit=50&page=1"
```

Read all pages. Check the `labels` field of each result. Drop each issue that does not carry `wayfinder:parent:<map>`.

### Map body

The map body holds no open-task checklist and no resolution detail.
Under "Decisions so far", it holds one line for each resolved ticket.
The line has a named link to the ticket and a short result. The detail stays in the ticket resolution comment.

Change the map body with `PATCH /repos/{owner}/{repo}/issues/{index}` and the field `body`.
Read the body first, add the line, and send the whole body.

### Native dependencies

Blocking relations are native Gitea dependencies. Do not replace them with prose in an issue body.

| Operation | Method and path | Body |
| --- | --- | --- |
| Add a blocker | `POST /repos/{owner}/{repo}/issues/{index}/dependencies` | `IssueMeta`: `owner`, `repo`, `index` |
| Read the blockers | `GET /repos/{owner}/{repo}/issues/{index}/dependencies` | none |

The issue in the URL depends on the issue in the body.
GET returns each issue that blocks the issue in the URL.

```sh
curl -fsS -X POST -H "Authorization: token $GITEA_TOKEN" -H "Content-Type: application/json" \
  -d "{\"owner\": \"$OWNER\", \"repo\": \"$REPO\", \"index\": <blocker-index>}" "$API/issues/$INDEX/dependencies"
curl -fsS -H "Authorization: token $GITEA_TOKEN" "$API/issues/$INDEX/dependencies"
```

Add dependencies in two passes:

1. Create all issues. A blocker needs a real number before a relation can name it.
2. Add each relation with POST. Then read the blockers of each issue back with GET, through all pages.

Compare the result with the approved dependency graph. Report each difference. Do not continue with a graph that differs.

### Frontier

The frontier is the set of tickets that a worker can take now. A ticket is in the frontier when all conditions hold:

- It is open and carries `wayfinder:parent:<map>`.
- It has no assignee. The `assignees` field of an unassigned issue is `null` or an empty list.
- Each blocker is closed.
- It does not carry `needs-approval` (section [Labels](#labels)).

Compute it from the tracker on each use:

1. Read all pages of `GET /repos/{owner}/{repo}/labels` and confirm that `wayfinder:parent:<map>` exists. Stop and report an absent label.
2. List the open children of the map, all pages. Drop each result that does not carry `wayfinder:parent:<map>`.
3. Keep a child only when its `assignees` field is empty or `null`. Drop each child that carries `needs-approval`.
4. Read each remaining child's blockers with `GET /repos/{owner}/{repo}/issues/{index}/dependencies`, through all pages.
5. Keep the child when each blocker has the `state` value `closed`.

An empty frontier is valid only after step 1 passes. A checklist in an issue body is not a source for the frontier.

### Claim

Claim a ticket before work starts. A claim is an assignment to the token account and a claim comment.
Read the issue with its comments first. If it has an assignee, do not claim it.
Do not claim a ticket that carries `needs-approval` (section [Labels](#labels)).

```sh
curl -fsS -H "Authorization: token $GITEA_TOKEN" "${API%/repos/*}/user"
curl -fsS -X PATCH -H "Authorization: token $GITEA_TOKEN" -H "Content-Type: application/json" \
  -d '{"assignees": ["<login>"]}' "$API/issues/$INDEX"
curl -fsS -X POST -H "Authorization: token $GITEA_TOKEN" -H "Content-Type: application/json" \
  -d '{"body": "Claimed: <session and role>"}' "$API/issues/$INDEX/comments"
```

Use the `login` from `GET /user`. Read the issue again to confirm the assignment and claim comment.

### Resolve

1. Post the resolution comment on the ticket. It holds the decision or result, and the evidence.
2. Close the ticket with `PATCH` and the `state` value `closed`.
3. Add one line for the ticket to "Decisions so far" in the map body.

Gitea refuses to close an issue while one of its blockers is open. The response is HTTP 412.
Close the blockers first. A dry run closes the child before the map, or removes the dependency first.

## Publication safety

- Before a create, list the existing maps, labels and issues, all pages. Do not create a duplicate.
- After an unclear network result, read the tracker before a second create. Do not repeat a POST blindly.
- Do not overwrite an issue body that differs from the expected text. Report the difference.
- Publication of a map or tickets does not assign a ticket and does not start a worker.
- Publish no token value, credential-bearing URL, host value or local work record in an issue body or comment.
