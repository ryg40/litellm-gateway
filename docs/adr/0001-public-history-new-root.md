# Start the public history from a new root

The earlier snapshot chain holds private records. The public history starts from one new root with a neutral identity.
This keeps the earlier chain out of the public history.

## Context

The publication branch had five earlier snapshots with personal identities.
Their trees held host values and work records.

## Decision

The public history starts from one snapshot without a parent, with a neutral identity and a tree checked for public use.
The GitHub repository is deleted and created again, private, at the same name, directly before the push.
The old snapshot chain and its tags stay on the private server only.

Warning: deleting the repository cannot be undone.

## Considered Options

| Option | Reason not chosen |
| --- | --- |
| Continue the chain | The earlier identities and private records would stay in the public history. |
| Use a new repository name | The old repository with its old history stays at the known name, and two repositories need an explanation. The chosen decision keeps one name. |
| Delete refs in place | Deleting refs leaves old objects reachable by hash until the server's garbage collection removes them. The activity view shows the refs. A recreated repository holds no old object. |

## Consequences

- The new root does not fast-forward from the earlier chain. Each target clones again.
- Tags before the public root are private only.
- Copies of the old public history outside our control are accepted.
