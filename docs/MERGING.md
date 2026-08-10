# Merging without leaking a personal identity

This is a public repository. Its history is permanent, forkable, and indexed. The
`public-safety` scan therefore refuses any commit whose author or committer carries a
personal display name — see `DEFAULT_ALLOWED_COMMIT_NAMES` in
`src/graph_engineering/public_safety.py`.

## Do not merge with `gh pr merge`

**GitHub stamps the merging user as the committer of a squash or merge commit.** No repository
setting changes that: `squash_merge_commit_title` and `squash_merge_commit_message` control
the *text*, never the identity. A maintainer whose GitHub profile shows a personal name will
put that name into public history the moment they click Merge or run `gh pr merge`.

That is exactly how it happened on 2026-08-10: a `gh pr merge` landed a commit committed by a
personal display name, `public-safety` failed closed on `main`, and the guard was right.

## Merge locally instead

```bash
git config user.name  "poweredbyGEN"                          # once per clone
git config user.email "poweredbygen@users.noreply.github.com"

git fetch origin
git checkout main && git pull --ff-only
git merge --squash origin/pr/<N>          # or: git merge --squash <branch>
git commit -m "<title>"                   # uses the local generic identity
git push origin main
```

Then close the PR with a comment naming the squashed commit. The work is identical; only the
identity on the resulting commit differs — and that is the whole point.

## Verify before pushing

```bash
git log --format='%an <%ae> | %cn <%ce>' origin/main..HEAD | sort -u
PYTHONPATH=src python3 -m graph_engineering.public_safety --repo . --mode candidate-metadata
```

Both must be clean. The scan is not a formality: it is the only thing standing between a
routine merge and a permanent record.

## If a personal identity has already landed

Rewrite and republish, then fix the source in the same pass — a scrub without a source fix
just resets the clock:

```bash
cp -r .git /mnt/data/tmp/backup-$(date +%s)      # rewrite is irreversible

FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f --env-filter '
  export GIT_AUTHOR_NAME="poweredbyGEN"
  export GIT_AUTHOR_EMAIL="poweredbygen@users.noreply.github.com"
  export GIT_COMMITTER_NAME="poweredbyGEN"
  export GIT_COMMITTER_EMAIL="poweredbygen@users.noreply.github.com"
' -- main

git diff --stat <old-ref> main    # MUST be empty: identities changed, content did not
```

A force-push leaves the old objects unreferenced but still fetchable by SHA for a while. When
the leak matters, delete and recreate the repository rather than relying on garbage
collection — that is the only way to be certain the old commits are gone.
