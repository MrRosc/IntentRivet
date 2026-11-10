# Running IntentRivet

Day to day, you work in Telegram ([README, Daily use](../README.md#daily-use)). This page is for
the occasional work on the executor host and in Hermes.

## On the executor host

As the executor's account, in the `intentrivet` directory, with the exports of
[step 1](../README.md#step-1-executor-host-the-executor-and-your-publisher-bot)
(`export DOCKER_CONTEXT=intentrivet COMPOSE_FILE=compose.live.yaml`).

A read-only check of the bot, its webhook, the owner and each connected channel, including the
bot's right to post there; it sends nothing and ends `RESULT: ready` when all is well:

```sh
stand/live-check.sh
```

Restarts and updates keep everything, which lives in the executor's volume. After `git pull`, run
`stand/setup.sh` again. `docker compose down -v` would delete the store.

```sh
docker compose down          # stops the executor: also the way to stop all publications at once
docker compose up -d         # starts it again, with everything kept
```

To stop publications to one channel, remove the publisher bot's right to post there.

The host operator's terminal: the decisions (the same `allow()` as the bot's button), and a
version's stored outcome:

```sh
docker compose exec executor python -I /app/executor.py requests
docker compose exec executor python -I /app/executor.py show <version_id>
```

A new owner, e.g. after losing your Telegram account. It asks you to type `reset`; versions,
permissions and channels stay. Then the `bind` command of
[step 2](../README.md#step-2-telegram-you-as-the-owner-your-channels) gives a new link:

```sh
docker compose exec executor python -I /app/executor.py reset-owner
```

A new bot token, e.g. after revoking the old one in @BotFather: `stand/live-setup.sh` asks for it
(if it fails, the stored token stays); then recreate the executor, as a running executor keeps the
token it started with:

```sh
stand/live-setup.sh
docker compose up -d --force-recreate executor
```

For a new bot instead (e.g. because the old one has a webhook), the same; then, in Telegram: open
the new bot's chat as the owner and press **Start** (a bot cannot write to you first), make it an
administrator with the right to post messages in each connected channel, and remove the old bot
from those channels' administrators, as it keeps its rights otherwise. `stand/live-check.sh` then
checks the new bot and its rights in every channel.

A new agent key: run the command of [step 3](../README.md#step-3-executor-host-the-agent-key) again
(it asks you to type `replace`). The previous key stops working at once, so give every connected
Hermes the new one (below).

## In Hermes

As Hermes' account, in a terminal without the exports of step 1, as in
[step 4b](../README.md#4b-the-plugin-in-your-existing-profile). Leave out `-p <profile>` for the
default profile. For Hermes in a container, run the `hermes` commands in it as Hermes' user, never
as root: set `U` in each new terminal, then prefix each command as here:

```sh
U=$(docker exec <Hermes container> sh -c 'stat -c %U "${HERMES_HOME:-$HOME/.hermes}"')   # official image: hermes
docker exec -u "$U" <Hermes container> hermes -p <profile> intentrivet check
```


- **Check the connection:** `hermes -p <profile> intentrivet check`. It prints
  `IntentRivet: connected to the executor at ...; it accepted the agent key` and the connected
  channels, or what is wrong: no address, no key, executor unreachable, key not accepted.
- **Change a setting** (`INTENTRIVET_URL`, `INTENTRIVET_AGENT_KEY`): edit its line in the profile's
  `.env`, as Hermes' user, then restart Hermes as in step 4b. The file is in the profile's home:
  Hermes' home for the default profile (`~/.hermes`, or `HERMES_HOME`: `/opt/data` in the official
  image), and `profiles/<profile>` below it for another profile. Hermes v0.22.7 also takes
  `hermes -p <profile> config set INTENTRIVET_URL ...`; v0.20.1's `config set` writes such a name
  into `config.yaml` instead, where the plugin does not read it.
- **Update the plugin:** `git pull` in the clone, then the commands of
  [step 4b](../README.md#4b-the-plugin-in-your-existing-profile) with `--force` after
  `plugins install`, and the restart there. Hermes keeps the two settings. Hermes copies a
  `#subdir` install without its `.git`, so `hermes plugins list` marks the plugin
  "provenance: drift" and `hermes plugins update` does not apply: reinstall instead.
- **A stored version, in Hermes' chat:** `/intentrivet <version_id>`. It is convenient, but Hermes
  shows it; the publisher bot's notices and Pending requests are the trusted view.
- **Remove IntentRivet:** `hermes -p <profile> plugins remove intentrivet`, then delete the two
  lines from the profile's `.env`.
