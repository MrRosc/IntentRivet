# Where to put the executor

Part of [Start here](../README.md#start-here-where-to-put-the-executor) in the README, where you
choose your setup and see which setups were tested.

The executor's permission check holds only under these conditions:

- Hermes cannot reach the executor's files, its container engine, the publisher bot's token or
  administrator rights on the executor's host. A container is not a boundary against a Hermes that
  drives the container's engine. Nor may it log in to the executor's host in another way: no SSH
  key or agent of its account (with `sudo` or `docker`: of any account on its host) may log in
  there, other than the tunnel's forward-only key.
- Hermes has no other credential that can post in a connected channel: its own Telegram bot (its
  token is in Hermes' `.env`, which the agent can read), another bot's token, or a Telegram session
  of the owner or of a channel administrator on the Hermes host (the owner's session could also
  press Allow once). In every connected channel the publisher bot must be
  the only bot or account Hermes could use with the right to post, edit or delete messages:
  remove Hermes' own bot from the channel's administrators. The executor cannot see other
  administrators' posts.

## Supported placements

| Placement | When | Connection |
|---|---|---|
| **Another host** (recommended) | Hermes runs with broad rights, e.g. as your everyday account with `sudo`, `docker`, `lxd` or `libvirt` | [SSH tunnel](remote-hermes.md) to the executor host's `127.0.0.1:8787`, with a key that allows that one forward and no command |
| **Same host, Hermes in a container on the host network** | Hermes' container has no container-engine socket and no mount of the executor account's files, as in Hermes' own Docker setup (`network_mode: host`, only `~/.hermes` mounted); a container on a bridge network cannot reach the host's `127.0.0.1` | `http://127.0.0.1:8787` |
| **Same host, separate accounts** | the executor runs under its own account with its own rootless Podman; Hermes' account is not root, has no `sudo`, is in none of `docker`, `lxd`, `incus`, `libvirt`, `disk`, and cannot read the executor account's files | `http://127.0.0.1:8787` |

Not protected: Hermes running as the executor's account, or as an account (or in a container)
that can become root or drive the executor's engine on its host; or a Hermes whose own bot, or any
other account it can use, may post in a connected channel. IntentRivet still works there, but the
agent could bypass the executor.

## Check the placement

`stand/check-isolation.sh` checks the usual ways around the executor with the rights of the account
it runs as: each check ends PASS (verified), FAIL, or UNKNOWN (it could not be made), and it exits 0
only if every check passed. It covers what it lists, not every way in, and says what it did not
check. It changes nothing.

Executor on the same host: run it as Hermes' account, from a clone of this repository, with the
executor's account as the argument:

```sh
sh stand/check-isolation.sh same-host <executor account>
```

For Hermes in a container, run it inside the container as Hermes' user, never as root, in a
terminal without the exports of step 1 (as in
[step 4b](../README.md#4b-the-plugin-in-your-existing-profile)). The first line finds that user;
the script itself goes in on standard input:

```sh
U=$(docker exec <Hermes container> sh -c 'stat -c %U "${HERMES_HOME:-$HOME/.hermes}"')   # official image: hermes
docker exec -i -u "$U" <Hermes container> sh -s same-host <executor account> < stand/check-isolation.sh
```

Executor on another host, with the tunnel up: run it on the Hermes host as the account that runs
the tunnel, which has the tunnel's SSH configuration. For a container on a bridge network, add the
address Hermes uses, `http://<gateway>:8787`:

```sh
sh stand/check-isolation.sh other-host intentrivet-executor   # executor on another host (tunnel up)
```

On another host it checks that the tunnel reaches the executor's API (`http://127.0.0.1:8787`, or
the URL given after the destination) and that the tunnel's SSH login is confirmed and runs no
command; it does not check other SSH keys, logins or administrator rights the account may have on
the executor's host, and says so.

Back to [Start here](../README.md#start-here-where-to-put-the-executor) to choose your setup. If
you came here from the end of [step 4b](../README.md#4b-the-plugin-in-your-existing-profile), go on
with [step 5](../README.md#step-5-your-first-post).
