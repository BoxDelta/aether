function aether-vault --description 'Open/close the AETHER encrypted volume and start/stop Postgres on it (ADR-026)'
    # ADR-026, #57. Install (once, stage 4b of docs/runbooks/encrypted-volume.md):
    #   cp scripts/aether-vault.fish ~/.config/fish/functions/
    # Usage: aether-vault open | close | status
    #
    # Holds no passphrase, key or password: cryptsetup asks for the passphrase on
    # the terminal. Fails closed: Postgres is started only when the volume is
    # unlocked, mounted from /dev/mapper/aether-vault at the fixed mount point, and
    # both marker files are there. Any failure stops before the next step.
    set -l file $HOME/.local/share/aether/store/aether-vault.luks
    set -l mnt $HOME/.local/share/aether/vault
    set -l name aether-vault
    set -l mapper /dev/mapper/$name
    set -l repo $HOME/Projects/aether
    set -l compose docker compose --project-directory $repo -f $repo/docker-compose.yml -f $repo/compose.vault.yaml

    switch "$argv[1]"
        case open
            if not test -f $file
                echo "aether-vault: no container file at $file; nothing opened" >&2
                return 1
            end
            if not test -f $repo/compose.vault.yaml
                echo "aether-vault: $repo/compose.vault.yaml missing; nothing opened" >&2
                return 1
            end
            if not mountpoint -q $mnt
                if not sudo cryptsetup status $name >/dev/null 2>&1
                    # Asks for the passphrase on this terminal; creates the loop device itself.
                    if not sudo cryptsetup open --type luks2 $file $name
                        echo "aether-vault: unlock failed; nothing mounted, Postgres not started" >&2
                        return 1
                    end
                end
                if not sudo mount -o nodev,nosuid,noexec $mapper $mnt
                    sudo cryptsetup close $name
                    echo "aether-vault: mount failed; volume locked again, Postgres not started" >&2
                    return 1
                end
            end
            if not __aether_vault_ok $mnt $mapper
                echo "aether-vault: Postgres not started; run 'aether-vault status'" >&2
                return 1
            end
            if not $compose up -d --wait postgres
                echo "aether-vault: Postgres did not become healthy; see 'docker compose logs postgres'" >&2
                return 1
            end
            echo "aether-vault: open; Postgres running on the encrypted volume"
        case close
            # Stop Postgres first; never unmount under a running database.
            if not $compose stop postgres
                echo "aether-vault: could not stop Postgres; volume left mounted" >&2
                return 1
            end
            if mountpoint -q $mnt
                if not sudo umount $mnt
                    echo "aether-vault: unmount failed (something still uses it? try: sudo fuser -vm $mnt)" >&2
                    return 1
                end
            end
            if sudo cryptsetup status $name >/dev/null 2>&1
                if not sudo cryptsetup close $name
                    echo "aether-vault: unmounted, but the volume is still unlocked" >&2
                    return 1
                end
            end
            echo "aether-vault: closed; Postgres stopped, volume locked"
        case status
            test -f $file; and echo "container file: present"; or echo "container file: MISSING"
            sudo cryptsetup status $name >/dev/null 2>&1; and echo "unlocked: yes"; or echo "unlocked: no"
            if __aether_vault_ok $mnt $mapper
                echo "mounted: yes, markers ok"
            else
                echo "mounted: no (or not usable)"
            end
            $compose ps --format '{{.Service}}: {{.State}}' postgres
        case '*'
            echo "usage: aether-vault open | close | status" >&2
            return 1
    end
end

function __aether_vault_ok --argument-names mnt mapper
    if not mountpoint -q $mnt
        echo "aether-vault: $mnt is not mounted" >&2
        return 1
    end
    set -l source (findmnt -n -o SOURCE --target $mnt)
    if test "$source" != "$mapper"
        echo "aether-vault: $mnt is mounted from something else, not $mapper" >&2
        return 1
    end
    for marker in $mnt/.aether-vault $mnt/data/.aether-vault
        if not test -f $marker
            echo "aether-vault: marker $marker missing (not AETHER's volume?)" >&2
            return 1
        end
    end
    if not test -d $mnt/backups
        echo "aether-vault: $mnt/backups missing" >&2
        return 1
    end
end
