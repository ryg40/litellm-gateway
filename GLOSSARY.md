# Public release

Terms for snapshots and releases on the publication branch.

## Language

**Snapshot**:
A single commit of the development branch tree on the publication branch.
_Avoid_: squash, export

**Promotion**:
The step that turns a snapshot into a release: the checked build of the snapshot and its tag, and their push.
_Avoid_: deploy, publish (as a verb for the build)

**Candidate**:
A snapshot and tag built and verified but not pushed, waiting for approval.
_Avoid_: preview, draft

**Release**:
A pushed snapshot with its tag.
_Avoid_: version, drop

**New root**:
A snapshot without a parent that starts the public history.
_Avoid_: orphan commit, fresh history
