# How IntentRivet works in detail

The technical reference behind the [README](../README.md): one publication step by step, trust
boundaries, the tested setups, limitations, pinned versions and the repository layout.

## How one publication works

1. Hermes calls `intentrivet_prepare_post`; the executor stores an immutable version
   (`Awaiting approval`) with the channel's chat_id, and sends its preview to the owner: the
   trusted fields (channel title and name, chat_id, version) above the full text between
   markers, in several marked parts if it is long (each later part's first line names the
   version and the channel), with Allow once / Reject on the last part only once every part is
   delivered. The post text may imitate any of those lines; the last line, after the text, where
   nothing from the text can follow, names the version, the channel (its title escaped and
   clipped) and its chat_id, and so does the Allow once button. Previews go out one at a time, so
   the parts of two never interleave, and without link previews. If a part fails, the card is not
   stored, buttons that may have arrived do nothing, and the bot says so. The tool result says
   whether that delivery worked. A change creates a new version of the same draft, with its own
   preview.
2. The owner presses Allow once (or Reject) on the preview; in a terminal,
   `executor.py requests` does the same. Allow once allows exactly that version and its chat_id, once: Hermes
   may use it at once, without asking again; it does not expire and cannot be withdrawn.
   Reject is final for that version. If Hermes created a newer version of the draft in the
   meantime, nothing is allowed: the owner is told and offered the new one. A second press,
   an old card or a stranger's press changes nothing; the card's buttons give way to the
   decision, and the store is the truth if that edit fails. Every view of a text (the
   preview, Permission requests, `executor.py show`, `/intentrivet`, the tool result) keeps
   newlines as line breaks and shows any other control, format, line- or paragraph-separator
   character as an escape (ESC as `\u001b`, CR as `\r`), so none reaches a terminal or a chat raw;
   spaces of any width stay as they are. The stored text and the send stay exact.
3. Hermes calls `intentrivet_publish_post` with the version id and channel. The executor
   refuses, without using the permission and without contacting Telegram, unless that
   version is allowed for that channel; it never takes text or chat_id from the request, and
   the chat_id is the one stored with the version, whatever the channel name means now.
4. Before contacting Telegram, the executor marks the permission used in one SQLite
   transaction (`BEGIN IMMEDIATE`, committed). At most one `sendMessage` call follows (none if
   Telegram cannot be reached), built from the stored snapshot, never retried. The executor logs one
   `sendMessage attempt: <version_id> -> chat_id <id>` line before it and one
   `sendMessage result:` line after it, never the token or the text.
5. The outcome is stored and shown everywhere (`/intentrivet`, `executor.py show`,
   Permission requests). `Published` only if Telegram replies `ok` with a positive integer
   `message_id`, the stored chat_id as `chat.id` and exactly the stored text; the detail
   keeps the `message_id` and the post link when one can be built
   (`https://t.me/<username>/<message_id>` for a public channel,
   `https://t.me/c/<id>/<message_id>` for members of a private one). `Failed`: Telegram
   refused, or was not reachable, so nothing was sent. `Outcome unknown`: the request went
   out but no reply came back, the reply was not a complete success matching the
   snapshot, or the executor stopped during the send. Any repeat sends nothing and answers with
   the stored outcome (or that the send is still in progress), never advising a new version
   before the outcome is known. Hermes gets the outcome in the answer to its request; the plugin
   waits up to 60 s for it. If that answer is lost or incomplete (a timeout, a dropped connection,
   a cut reply), the plugin says the outcome is not known to it and that the version must be
   neither published again nor replaced until it is; the stored outcome is then in the owner's
   notice, Pending requests and `/intentrivet <version_id>`. The owner's notice in the bot chat,
   with the post link, goes out on its own, so a late or refused notice changes neither the answer
   nor the stored outcome.

Statuses: `Awaiting approval`, `Allowed: one publication`, `Rejected`, `Publishing`,
`Published`, `Outcome unknown`, `Failed`.

## Trust boundaries

| Where | What runs | Writable |
|---|---|---|
| Executor host terminal | `docker compose`, `git` (the operator) | repository, `.stand/`, `.stand-live/` |
| `executor` container | `executor.py` server, the owner's Telegram interface and the operator CLI (uid 10001, read-only root, no capabilities, 256 MiB memory) | its own volume `executor-data` only |
| Your Hermes (another host, a container, or another account) | Hermes, the plugin, Hermes' terminal tool | what Hermes could write before; the plugin adds its directory, two `.env` lines and its entry in `config.yaml` |
| `hermes` container (bundled example only) | Hermes CLI, the plugin, the Hermes terminal tool (uid `hermes`) | its own volume `hermes-data` only |
| `telegram-mock` container (stand-in stand only) | the Telegram stand-in (read-only root, no capabilities) | its own volume `telegram-mock-data` only |

- The executor's API is published on the executor host's `127.0.0.1` only (Compose port
  `127.0.0.1:8787:8080`), never on a public address; another host reaches it through an SSH
  tunnel whose key allows that one forward and no command. Every request must carry the agent
  key (`Authorization: Bearer`), checked before anything else is read; the executor stores its
  SHA-256 and `executor.py agent-key` replaces it. The plugin sends it only to `INTENTRIVET_URL`:
  it follows no redirect and uses no proxy. The executor serves at most 16 connections at a time.
- The agent key is in Hermes' `.env`, so the agent itself can read it; it gives exactly what the
  plugin's tools can do, nothing more. What keeps the agent from the executor is the
  [placement](placement.md): no engine, no files, no administrator rights over the executor.
  Hermes' other credentials in that `.env` (its own Telegram bot's token, its model key) are the
  agent's too: none of them may be able to post in a connected channel.
- The executor API (reachable from Hermes, with the agent key) offers four operations: create a new
  version (`POST /versions`), read one by its exact id (`GET /versions/<id>`), list the
  connected channels (`GET /channels`), and ask to publish one (`POST /publications` with
  `version_id` and `channel` only). There is no route to allow, reject, approve, update or
  delete anything, to bind an owner, to connect or change a channel, or to hand in a
  Telegram update.
- Allowing is for the owner, and both ways call the same `allow()`: Allow once on a preview in
  the publisher bot's private chat, or `executor.py requests` inside the executor container,
  which only the host operator can enter (`docker compose exec`; against the real Telegram it
  refuses piped input). The executor reads the bot's updates itself, by long polling (no
  webhook, no port). An update counts only from the owner's private chat, by the numeric user
  id Telegram puts in it, never by name or text; a button counts only on a stored preview
  card, and its callback data names no channel, text or owner. Each update is applied in one
  transaction with the stored update offset, so a redelivered update changes nothing.
- The owner is bound once with a one-time code that `executor.py bind` prints in the
  operator's terminal only (valid 10 minutes; the store keeps its SHA-256; no log has it). A
  bound owner cannot be replaced from Telegram; `executor.py reset-owner` on the host
  removes them, keeping all data.
- The executor rejects a missing, repeated, signed or non-numeric `Content-Length`
  before reading the body, and drops a client that stalls for 10 s on one read.
- The bot token is a Compose secret, mounted only into the executor (and, on the stand-in stand,
  the stand-in): `.stand-live/telegram_bot_token` (directory mode 0700), typed by the operator at
  a hidden prompt; the file belongs to the executor's container user, mode 0400, so not even the
  host user reads it without `podman unshare`. The stand-in stand uses `.stand/telegram_bot_token`,
  a local test marker. The executor logs only the token's size and a SHA-256 prefix; the Bot API
  path that holds it is never logged or printed.
- The bundled example Hermes gets no Docker socket, host home, SSH/GitHub keys, operator config,
  repository mount or bot token. Plugin code reaches it only through `stand/install-plugin.sh`: a
  one-off container mounts `.git` read-only, clones it and runs `hermes plugins install` and
  `hermes plugins enable`. It is on `internal`, for the executor's HTTP API, and on its own
  `egress` network, the route out for the model provider. That route is not limited to the
  provider: Hermes reaches the public internet, the Telegram Bot API included, but holds no
  publisher bot token, so it cannot act as that bot (after `stand/hermes-telegram.sh` it holds its
  own bot's token: that bot must not administer a connected channel). Only the executor joins
  `telegram`, its route out to `https://api.telegram.org`, over TLS with certificate and host name
  verification (the pinned image's system CA bundle). The network does not limit that route to
  Telegram's hosts; the executor connects only to its configured API base.
- Stand-in stand: network `internal` has no route out; network `telegram` joins only the executor
  and the stand-in, so Hermes cannot reach the stand-in at all; network `egress` is Hermes-only,
  for the model provider API.

## Tested setups

- The executor on Linux with the account's rootless Podman (the setup refuses Docker Engine,
  rootful Podman and another user's Podman: nothing else was verified), with the Docker CLI and
  Compose v2 from Ubuntu 24.04's `docker.io` and `docker-compose-v2` packages.
- Hermes v0.22.7 and v0.20.1 (2026.8.13) from the official images, in a container of their own on
  the host's network, as an existing profile with its own model, memory, persona, credentials and
  another plugin; the same through an SSH tunnel with a forward-only key, to an SSH server on the
  same machine.
- A two-host installation: an existing Hermes v0.20.1 in a container on a Docker bridge network,
  through the SSH tunnel listening on the bridge gateway behind the firewall rule of
  [Hermes on another host](remote-hermes.md#5-hermes-in-a-container-on-a-docker-bridge-network)
  (OpenSSH 9.6p1 on the executor host); the forward-only key and the sshd `Match` block were checked
  from that Hermes host (the API answers; no command, no `-R`, no other port, no Unix socket). The
  plugin was installed into its existing profile with the settings kept, and the preview card was
  checked in the real Telegram.
- The plugin declares `requires_hermes: ">=0.20.1"` (Hermes v0.20.1 does not read that field;
  v0.22.7 enforces it). `hermes plugins validate` passes on v0.22.7 (v0.20.1 has no `validate`;
  its `hermes plugins doctor` passes).
- Not run: a Hermes installed natively (it takes the same commands), the separate-accounts
  placement, a real reboot of either host.

## Limitations

- Allow once does not resume Hermes, but it allows Hermes to publish that version at once, or at
  any later time, without asking the owner again. A permission does not expire and cannot be
  withdrawn (Reject after Allow once changes nothing); to stop publications to a channel, the
  owner removes the publisher bot's right to post there, or the host operator stops the executor.
- One owner per installation. Channels can be connected in the bot, not renamed or removed
  there (nothing is ever replaced silently); a version keeps the chat_id it was prepared with.
- The boundary holds only in the [supported placements](placement.md): a Hermes that runs as the
  executor's account, can become root on the executor's host, or drives its container engine
  defeats it. So does any credential Hermes holds that can post in a connected channel, such as its
  own Telegram bot as an administrator there: the executor never sees such posts, and its checks
  look only at the publisher bot's rights. `stand/check-isolation.sh` checks the rights it can see
  from Hermes' account (root, sudo, the usual root-equivalent groups and engine sockets, the
  executor account's files, a shell over SSH); it cannot prove there is no other way in, such as
  a kernel or engine flaw.
- One agent key per executor: every Hermes that connects uses the same key, and replacing it
  disconnects all of them until they get the new one.
- The executor serves at most 16 HTTP connections at a time (more get 503 at once), and its
  container has 256 MiB of memory, swap included (`compose.live.yaml`). That bounds what slow or
  many clients can take from the host, but anything that reaches the API port, with or without
  the agent key, can still hold all 16 and keep Hermes from the API until it lets go. The owner's
  Telegram interface and the stored decisions are not affected; a container killed for memory
  restarts, and a send it interrupted ends `Outcome unknown`.
- The connection is plain HTTP to 127.0.0.1; across hosts only through the SSH tunnel, which
  must be kept running. There is no TLS endpoint.
- The bundled example Hermes' route out, for the model, is not limited to the model provider:
  Hermes reaches the public internet, the Telegram Bot API included (without the publisher bot's
  token), and host services listening on all interfaces, such as SSH (which still needs
  credentials). Host services bound to loopback and container-engine ports were not reachable in
  our tests; `check-boundary.sh` checks the engine ports on every run.
- The executor's route out is not limited to Telegram's hosts by the network; only its
  configuration points it at `https://api.telegram.org`.
- If the executor stops between storing a decision and answering in Telegram, that answer is
  lost (the decision stands; Pending requests and the card show it). An outcome notice to the
  owner is tried once; one that fails, or still waits when the executor stops, is lost (failures
  are logged), and at most 100 wait at a time. The stored outcome stays in `/intentrivet`,
  `executor.py show` and the answer to any repeat. Check texts posted for Add channel stay in
  the channel until the owner deletes them.
- Sends are counted by the executor, not on Telegram's side: its `sendMessage attempt`
  log lines (channels only) cover the running executor container since it started, its
  used permissions the whole store. The owner confirms the post in the channel.
- Telegram may show a preview of a link in a published post; it builds it when the post is sent.
  The owner's preview card has link previews turned off.
- Whether the model requests a given channel when asked is the model's decision; the boundary
  test shows the executor's refusal reproducibly and is scripted by the operator.
- `Published` requires Telegram to report back exactly the stored text. Telegram trims
  leading and trailing whitespace, so such a text would end `Outcome unknown` even when
  posted.
- Refusing piped input to Permission requests is a guard against scripted allows, not
  a security boundary: a pseudo-terminal passes it. The boundary is that only the host
  operator can enter the executor container.
- One permission is used by its first send attempt, whatever the outcome. After
  `Failed` or `Outcome unknown` there is no in-product resolution: the owner checks
  the channel, and another publication needs a new version and a new permission.
- `Outcome unknown` is not reconciled against Telegram (the Bot API has no lookup by
  request), so a post may be in the channel without the executor knowing it.

## Pinned versions

| Component | Pin |
|---|---|
| Hermes Agent | v0.22.7, commit `b200f5bc60e31588ba2cb60e288029c4a54ba5fa` |
| Hermes image | `nousresearch/hermes-agent:v0.22.7@sha256:e6d81ae52ebf6a43efa529701405509f20054ba818f179ddd748bed4f8af584c` (official, unmodified) |
| Executor base, Telegram stand-in | `python:3.13.17-slim-trixie@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962` |

IntentRivet adds no runtime dependencies: the plugin, the executor (with its Telegram
interface) and the stand-in use the Python standard library only.

## Layout

```
plugin/                  Hermes plugin: tools intentrivet_prepare_post, intentrivet_publish_post,
                         intentrivet_list_channels; command /intentrivet; hermes intentrivet check
executor/executor.py     trusted executor: HTTP API (agent key), SQLite store, decisions, operator CLI
executor/owner_bot.py    the owner's Telegram interface in the executor (long polling)
compose.live.yaml        the executor's deployment: real Telegram, API on 127.0.0.1 only; with
                         --profile demo also the bundled example Hermes
docs/                    placement, Hermes on another host, operations, this reference, testing
stand/setup.sh           executor host: engine, token, the executor alone, check, one-time owner link
stand/check-engine.sh    the setup's first step: only this user's rootless Podman goes on
stand/live-setup.sh      the publisher bot's token at a hidden prompt -> .stand-live/
stand/live-check.sh      read-only check by the executor: bot, webhook, owner, channels
stand/check-isolation.sh Hermes host: can Hermes' account take the executor over? (exit 0: by none of the checked ways)
stand/e2e_existing.py    acceptance with an existing Hermes deployed apart (any deployment)
stand/test-existing-hermes.sh
                         runs it on a disposable setup, then through an SSH tunnel
stand/install-plugin.sh  bundled example Hermes only: plugin, address and key into its profile
stand/configure-model.sh bundled example Hermes: an example model and its key, that profile only
stand/hermes-telegram.sh bundled example Hermes: a second bot for chatting with it in Telegram
stand/agent-call.sh      calls a plugin tool through Hermes' tool pipeline, without a model
stand/boundary-test.sh   demo boundary test: a direct request from Hermes' terminal, refused
                         before sendMessage (scripted by the operator, not the model)
stand/check-boundary.sh  boundary checks from the agent's real context (bundled Hermes)
stand/channels.json      stand-in stand: channels test and main (synthetic), imported once
stand/telegram_mock.py   test-only Telegram Bot API stand-in: channel posts and private
                         messages counted apart, queued updates for getUpdates
stand/e2e_stage2.py      end-to-end acceptance on the stand-in stand (refuses a real deployment)
stand/e2e_owner.py       end-to-end acceptance of the owner's path in Telegram (stand-in)
compose.yaml             the stand-in stand with the bundled Hermes: no real Telegram
tests/                   standard-library unit tests (python3 -m unittest discover -s tests)
```
