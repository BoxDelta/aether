function safemerge --description 'Wait for Sentinel checks, then merge the PR (optionally only if its head is the commit you reviewed)'
    set -l pr $argv[1]
    set -l reviewed $argv[2]
    if test -z "$pr"
        echo "usage: safemerge <PR number> [reviewed commit sha]"
        return 1
    end
    set -l sha (gh pr view $pr --repo BoxDelta/aether --json headRefOid --jq .headRefOid)
    or return 1
    if not string match -qr -- '^[0-9a-f]{40}$' "$sha"
        echo "could not read the PR head commit; not merging"
        return 1
    end
    if test -n "$reviewed"; and not string match -qr -- '^[0-9a-f]{7,40}$' "$reviewed"
        echo "reviewed commit must be 7-40 lowercase hex characters; not merging"
        return 1
    end
    if test -n "$reviewed"; and test (string sub -l (string length -- $reviewed) -- $sha) != "$reviewed"
        echo "PR head is $sha, not the reviewed commit $reviewed; not merging"
        return 1
    end
    gh pr checks $pr --repo BoxDelta/aether --watch --fail-fast
    or return 1
    gh pr merge $pr --repo BoxDelta/aether --merge --delete-branch --match-head-commit $sha
    or return 1
    if string match -q -- '*BoxDelta/aether*' (git remote get-url origin 2>/dev/null)
        git switch main; and git pull
    else
        echo "merged; in your aether checkout run: git switch main; and git pull"
    end
end
