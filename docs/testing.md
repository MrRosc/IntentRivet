# Try it and test it

None of this is needed to use IntentRivet with your own Hermes: that is the
[README's installation](../README.md#install). This page is for trying it without your own Hermes,
for a scripted demo, and for the checks.

## Try it with the bundled example Hermes

For a try-out without an existing Hermes, `compose.live.yaml` carries an example Hermes (the
official image, its own volume) next to the executor, started only with the `demo` profile; it
reaches the executor on the stand's internal network (`http://executor:8080`). It is an example
and the test stand's agent, not the product's path. On the executor host, after
[step 1](../README.md#step-1-executor-host-the-executor-and-your-publisher-bot) and
[step 2](../README.md#step-2-telegram-you-as-the-owner-your-channels), with the exports of step 1:

```sh
docker compose --profile demo up -d
stand/install-plugin.sh          # profile intentrivet-demo: plugin, INTENTRIVET_URL, an agent key
stand/configure-model.sh         # a model for that profile (example: OpenRouter, see below)
docker compose exec -u hermes hermes hermes -p intentrivet-demo chat --cli
```

`stand/install-plugin.sh` takes the executor's agent key only while it has none (or keeps the key
already in the profile): it never replaces the key your own Hermes uses.
`stand/hermes-telegram.sh` gives this example its own Telegram bot.

`stand/configure-model.sh` is an example configuration: it pins that profile to an OpenRouter
model (`openai/gpt-4.1-mini` by default; `INTENTRIVET_MODEL=<model>` picks another), also as the
fallback for Hermes' auxiliary tasks, and copies the key from `.stand/model.env`
(`OPENROUTER_API_KEY=<your key>`, never committed, never in an image) into that profile's own
`.env` in the `hermes-data` volume; re-run it after changing the key. To create that file with the
key typed at a hidden prompt:

```sh
install -d -m 700 .stand && (umask 077; stty -echo; printf 'OpenRouter key (input hidden): '; IFS= read -r k; stty echo; echo; printf 'OPENROUTER_API_KEY=%s\n' "$k" > .stand/model.env)
```

The key is deliberately not in the container environment: the local Hermes gateway reads a named
profile's credentials only from that profile's `.env`, and an environment key would also hand
credentials to the container's default profile. The first session in a new profile asks once
about Hermes telemetry; either answer works.

## The stand-in stand

A disposable stand with its own project name, volumes and networks; no model and no
real Telegram. The plugin is installed from the committed `HEAD`. Its channels come from
`stand/channels.json` (two synthetic channels, keys `test` and `main`), imported once; with
`INTENTRIVET_LEGACY_CHANNELS=/dev/null` it starts empty, like a new installation, for
`stand/e2e_owner.py`.

Run it in a terminal of its own: its exports below would otherwise also apply to your
installation's commands, and `COMPOSE_PROJECT_NAME` overrides the project name of
`compose.live.yaml`.

```sh
export DOCKER_CONTEXT=intentrivet DOCKER_BUILDKIT=0   # classic build by Podman: no extra builder container
export COMPOSE_FILE=compose.yaml                      # the stand-in stand, not compose.live.yaml of the installation
export COMPOSE_PROJECT_NAME=intentrivet-sim           # own volumes and networks
install -d -m 700 .stand
[ -s .stand/telegram_bot_token ] || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > .stand/telegram_bot_token
docker compose up -d --build
stand/install-plugin.sh

# Agent: prepare a post (prints its version_id, e.g. post-1-v1)
stand/agent-call.sh intentrivet_prepare_post '{"channel": "test", "text": "Hello from IntentRivet.\nSecond line."}'
# Agent: try to publish before anyone allowed it -> refused (403), nothing sent
stand/agent-call.sh intentrivet_publish_post '{"version_id": "post-1-v1", "channel": "test"}'
# Operator: Permission requests -> 1 -> allow -> Enter (the same allow() as the bot's button)
docker compose exec executor python -I /app/executor.py requests
# Agent: another channel -> refused (403), the permission stays unused
stand/agent-call.sh intentrivet_publish_post '{"version_id": "post-1-v1", "channel": "main"}'
# Boundary test (scripted, not a model): the same request straight from the agent's terminal
stand/boundary-test.sh post-1-v1 main
# Agent: the allowed version and channel -> Published; again -> refused (409), nothing sent
stand/agent-call.sh intentrivet_publish_post '{"version_id": "post-1-v1", "channel": "test"}'
stand/agent-call.sh intentrivet_publish_post '{"version_id": "post-1-v1", "channel": "test"}'
# What the Telegram stand-in received: 1 call, 1 accepted post, the exact stored text
docker compose exec telegram-mock python -I /app/telegram_mock.py stats
docker compose exec executor python -I /app/executor.py show post-1-v1

# Lost reply: the stand-in accepts the post and cuts the connection before replying
docker compose exec telegram-mock python -I /app/telegram_mock.py mode drop
#   prepare a new post, allow it as above, publish it -> "Outcome unknown";
#   publish again, also after `docker compose restart executor` -> 409, still 1 call
docker compose exec telegram-mock python -I /app/telegram_mock.py mode ok

# Every case with assertions (exit code 0 = accepted), then the boundary checks
python3 stand/e2e_stage2.py
stand/check-boundary.sh post-1-v1
docker compose down -v                                # drop the demo stand and its volumes

# The owner's path in Telegram, on a new empty stand: binding, channels, previews, decisions
export INTENTRIVET_LEGACY_CHANNELS=/dev/null
docker compose up -d --build && stand/install-plugin.sh
python3 stand/e2e_owner.py
docker compose down -v
```

The stand-in's other commands: `mode hold` (accept the post and never reply, to stop the
executor mid-send), `mode no-result` and `mode null-result` (accept the post and reply
HTTP 200 `{"ok": true}` or `{"ok": true, "result": null}`), `mode altered` (accept the
post and report another text back), `private fail` (refuse messages to private chats),
`private delay SECONDS` (take them, answer that late),
`webhook URL`, the updates `message`, `press`, `post` and `redeliver`, and `reset`. The
check also runs against it: `docker compose exec executor python -I /app/executor.py check`.

## A scripted demo

A walkthrough for a recording, on a working installation with two of your channels connected (the
keys `news` and `blog` below are examples): the owner's Telegram with the publisher bot's chat
open, the agent, and a diagnostics terminal on the executor host with
`export DOCKER_CONTEXT=intentrivet COMPOSE_FILE=compose.live.yaml`.

- A, the agent: the connected Hermes, in its CLI (it shows each tool call) or its own Telegram
  bot; with the bundled example, `docker compose exec -u hermes hermes hermes -p intentrivet-demo chat --cli`.
- C, diagnostics: `docker compose logs --no-log-prefix executor | grep '^sendMessage'`,
  the executor's own line before and after each send attempt to a channel (messages to the
  owner are not counted there); and the boundary test.

| Step | Where | Action | Expected |
|---|---|---|---|
| 1 | A | prompt 1 | `intentrivet_prepare_post` for `news`: a version id such as `post-2-v1`, `Awaiting approval`, preview delivered |
| 2 | Telegram | the preview in the bot chat | the full text, `[news]` with its title and chat_id, the version id; below the text and on the Allow once button, the version and `[news]` again |
| 3 | Telegram | Allow once | `Allowed one publication of post-2-v1 to [news] ... Nothing is sent yet` |
| 4 | A | prompt 2 | `intentrivet_publish_post` for `blog` refused (403): allowed for `news`, not `blog`; nothing sent |
| 5 | C | `stand/boundary-test.sh post-2-v1 blog` (bundled example) | boundary test, scripted, not the model: allowed and requested channel, HTTP 403, the send count unchanged |
| 6 | C | the sendMessage lines | none for this version |
| 7 | A | prompt 3 | `Published`, with the message_id and the post link; the bot tells the owner too |
| 8 | A | prompt 4 | refused (409); nothing sent |
| 9 | C | the sendMessage lines | one attempt and one result for this version |

Prompts, with the version id from step 1:

1. `Write a short announcement that IntentRivet now lets Hermes publish to Telegram only with a person's permission, and submit it for the channel "news". Do not publish it.`
2. `Publish version post-2-v1 to the channel "blog".`
3. `Now publish version post-2-v1 to the channel "news".`
4. `Publish post-2-v1 to the channel "news" once more.`

If the model declines to request the `blog` channel, that is its own decision: record it as it
happened. Step 5 shows the executor's refusal reproducibly and is a scripted boundary test, never
presented as the model's decision. Note that after step 3 Hermes may publish on its own; if the
send in step 7 ends `Failed` or `Outcome unknown`, stop: no new version or permission is created
for a retry.

## Checks

- `python3 -m unittest discover -s tests`: executor, the owner's bot, plugin and stand-in,
  in process (also: no request without the agent key, the key replaced only on confirmation,
  the plugin's address and key from the profile, no redirect followed,
  `hermes intentrivet check`, the preview's trusted last line and buttons, lost and cut answers, the bounded HTTP
  handlers); the setup scripts against stub `docker` and `podman` commands.
- `stand/test-existing-hermes.sh`: `stand/e2e_existing.py` with an existing Hermes deployed apart
  from the executor (a disposable setup: the executor of `compose.live.yaml` with the stand-in,
  and the official Hermes image in its own container on the host's network, with a profile that
  already has its own model, memory, persona, credentials and another plugin). The plugin is
  installed with `hermes plugins install`, answering its prompts; then: the earlier settings
  kept, an owner and two channels with random names and ids, the preview with its buttons,
  refusals without permission and for another channel, one publication of the stored text, the
  repeat refused, nothing served without the key, no owner operation with it, no executor file,
  engine socket or bot token reachable from the agent. Then `stand/check-isolation.sh` as Hermes'
  user, the API closed on the host's other addresses, and the same Hermes through an SSH tunnel
  with a forward-only key (an unprivileged sshd on 127.0.0.1). `stand/e2e_existing.py` itself
  takes every command and address from the environment, so it runs against any deployment.
- `python3 stand/e2e_owner.py`: on a new empty stand-in stand, the owner's path: binding by
  the one-time code (a guessed, an expired and a used code bind nobody), two channels
  connected only after the owner confirms, a prepared post's complete preview, Allow once
  publishing nothing, Main refused (tool and boundary test) with the permission unused, one
  send to Test answered while the owner's notice is held 11 s, repeats refused also after a
  restart, Reject, a stale card, a stranger's and a double press, a redelivered update,
  Outcome unknown, and Pending requests in pages, where a late request with a lost preview is
  opened and allowed.
- `python3 stand/e2e_stage2.py`: on a running stand-in stand, the operator-decided cases and
  the demo flow on two channels (Main refused through the tool and in the boundary test,
  Test published once, a repeat refused); for each it prints what the operator allowed,
  what the agent asked, what the executor answered and what the stand-in counted. It
  types `allow` itself, so it refuses to run against a real deployment.
- `stand/check-boundary.sh <version_id>` exits 0 only if every mandatory check passes; a
  violation, or a check that cannot run, exits 1. It works on either stand and sends no
  message; against the bundled example Hermes, with the agent's own address and key from its
  profile (and 401 without the key or with another one). The token never goes into Hermes or to
  the host: the executor's log must report the fingerprint of the secret it has mounted, computed
  inside the executor; a probe run through Hermes' terminal tool (no model call) reports only
  whether the secret file, the executor volume, a container-engine socket or TCP port (2375/2376
  on the host side) or the stand-in is reachable, plus the HTTP codes of attempts to update,
  approve, allow, reject, redirect or send, to connect or change a channel, bind an owner or hand
  in an update; whether the model provider and (live) the public Telegram Bot API are reachable is
  shown for information. Hermes' files and process environments are streamed into the executor
  and searched there for its token. Afterwards the executor store (versions, permissions,
  rejections, channels, the owner) and the send counters (the executor's attempts; on the
  stand-in stand also the stand-in's channel posts) must be unchanged.
