# IntentRivet

IntentRivet lets your existing [Hermes Agent](https://github.com/NousResearch/hermes-agent) prepare
posts for your Telegram channels while you keep the decision: Hermes can publish only a version
you allowed, to the channel you saw.

It has two parts:

- **The plugin**, installed into a profile of your existing Hermes. It adds three tools (list the
  connected channels, prepare a post, ask to publish a prepared version), the `/intentrivet`
  chat command and `hermes intentrivet check`.
- **The executor**, a small trusted service that runs apart from Hermes, in its own container. It
  stores each version of a post as an immutable snapshot (the exact text and the channel's
  chat_id), sends you its preview in Telegram, and publishes a version only once you allowed it.

Why a separate executor: an agent with a terminal can edit its own plugin, read its own files and
call any address it reaches, so the decision cannot live inside Hermes. The executor holds the
publisher bot's token, the owner and the permissions; Hermes holds only an agent key, which lets
it list channels, prepare, read and ask to publish, never allow.

## How it works

1. You ask Hermes to prepare a post for one of your channels. The executor stores a new version
   and sends its preview to you, in the private chat of your **publisher bot**.
2. The preview shows the full text, the channel's title and name, its chat_id and the version.
   Its last line, below the text, and the **Allow once** button name the version and the channel
   again: decide on those, whatever the post text itself says.
3. **Reject** refuses that version for good. **Allow once** sends nothing by itself, but from that
   moment Hermes may publish exactly that version to that channel, once: at once or at any later
   time, without asking you again. A permission does not expire and cannot be withdrawn.
4. Hermes asks the executor to publish. The executor makes at most one send attempt per
   permission, with the stored text to the stored chat_id, and the bot tells you the outcome and
   the post link.

**Two bots, two kinds of authority.** Hermes keeps its own Telegram bot, the one you already chat
with: nothing changes there, and you do not need a new Hermes bot. The **publisher bot** is a
second bot of yours (any name and avatar), for IntentRivet only: its token is held by the
executor alone, it sends you the previews, and it must be the only bot that can post in your
connected channels.

## Before you start

- **Your existing Hermes**, in any profile: it keeps its model, memory, persona, other plugins and
  Telegram bot. The plugin requires Hermes 0.20.1 or later; it was verified with v0.20.1 and
  v0.22.7.
- **A Linux host for the executor**, with rootless Podman run by the account that will run the
  executor, the Docker CLI with Docker Compose v2 as its client, and git
  ([Executor host setup](#executor-host-setup)). It may be Hermes' own host only under the
  conditions in [Where to put the executor](#where-to-put-the-executor).
- **A Telegram account** that can create a bot with @BotFather and add administrators to your
  channels.
- **If Hermes runs on another host:** SSH from the Hermes host to the executor host, and an
  administrator of the executor host for one sshd setting
  ([Hermes on another host](#hermes-on-another-host-ssh-tunnel)).
- **The protection conditions**, which only you can ensure
  ([Where to put the executor](#where-to-put-the-executor)): Hermes cannot reach the executor's
  files, its container engine, the publisher bot's token or administrator rights on the executor's
  host, and no credential Hermes holds can post in your connected channels. In particular, Hermes'
  own Telegram bot must not be an administrator of them.

Names below such as `news`, `<profile>`, `<executor account>` or `hermes` (a container's name) are
examples or placeholders: use your own.

## Install

### Step 1. Executor host: the executor and your publisher bot

1. In Telegram, create your publisher bot with @BotFather (`/newbot`): any name and avatar. Use
   it for IntentRivet only: no webhook, no other program reading its updates, and not Hermes' own
   bot.
2. On the executor host, as the executor's account:

   ```sh
   git clone https://github.com/MrRosc/IntentRivet intentrivet && cd intentrivet
   export DOCKER_CONTEXT=intentrivet       # the Docker CLI's context for your rootless Podman
   export COMPOSE_FILE=compose.live.yaml   # for every docker compose command below
   stand/setup.sh
   ```

   It first says what it sets up and why the two bots stay apart, checks the container engine,
   asks for the publisher bot's token at a hidden prompt (never shown, never an argument of a
   program, readable afterwards only by the executor's container user), builds and starts the
   executor alone, checks the bot read-only (a webhook is reported and left in place: the
   executor cannot poll a bot that has one), prints a one-time link to the bot's private chat,
   and lists the next steps. Safe to run again: it keeps the token, the data and the owner.

The executor's API listens on `127.0.0.1:8787` of this host only (`INTENTRIVET_PORT` changes the
port). For a start after a reboot, see [Executor autostart](#executor-autostart-after-a-reboot)
(it needs an administrator once).

### Step 2. Telegram: you as the owner, your channels

1. Open the link from step 1 in Telegram within 10 minutes, signed in as the account that will own
   the bot, and press Start. The bot binds that account by its numeric user id and shows its menu:
   **Add channel**, **Channels**, **Pending requests**. The link then stops working, and no
   Telegram message can change the owner. Anyone else who writes to the bot gets "This is a private
   bot."
2. **Add channel** in the bot's private chat, for each of your channels. Within 30 minutes:
   - in Telegram, open the channel's settings, Administrators, add the publisher bot and give it
     the right to post messages (it needs no other right). Check that list: Hermes' own bot, and
     any other account Hermes can use, must not be there;
   - post in the channel the one-time check text the bot gives you. **It is a real post that your
     subscribers can see**; delete it once the channel is connected;
   - the bot shows the channel's title and chat_id; send a short name of your own (e.g. `news`):
     Hermes will use it as the channel key.

   No chat_id to look up, no file to edit. A channel already connected, or a name already taken,
   is refused; nothing is replaced. To connect another channel, now or later, use **Add channel**
   again; **Channels** lists the connected ones.

### Step 3. Executor host: the agent key

On the executor host, in the repository directory, with the two exports of step 1:

```sh
docker compose exec executor python -I /app/executor.py agent-key
```

It shows the key once; keep it for step 4. Running it again replaces the key (it asks you to type
`replace`); the previous one stops working at once.

### Step 4. Hermes: the plugin in your existing profile

Hermes reaches the executor at `http://127.0.0.1:8787` when both run on one host (natively, or
Hermes in a container on the host's network). If Hermes runs on another host, set up the
[SSH tunnel](#hermes-on-another-host-ssh-tunnel) first; for a Hermes container on a Docker bridge
network, also see [that section](#hermes-in-a-container-on-a-docker-bridge-network): it gives the
address to use instead.

On the Hermes host, with a clone of this repository there, install the plugin into your profile
(`-p <profile>`; leave it out for the default profile). Hermes asks for `INTENTRIVET_URL` (the
address above) and `INTENTRIVET_AGENT_KEY` (the key from step 3, typed hidden), saves both in that
profile's `.env`, and offers to enable the plugin: answer `y`.

Hermes installed natively, as the account that runs Hermes:

```sh
hermes -p <profile> plugins install "file://$PWD#plugin"
hermes -p <profile> intentrivet check
hermes -p <profile> gateway restart      # if its gateway runs: it loads the plugin
```

Hermes in a container (`hermes` below stands for your container's name, e.g. in Hermes' official
Docker setup), on the Hermes host: run every `hermes` command in the container as Hermes' own
user, never as root (root would load plugin code that Hermes' user can change). That user owns
Hermes' data; find it, then set `U`:

```sh
docker exec hermes sh -c 'stat -c %U "${HERMES_HOME:-$HOME/.hermes}"'   # official image: hermes
U=hermes                                 # the user printed above
# Only the plugin goes into the container: its committed files as a small git repository; no other
# file of this clone (never copy the clone itself: on an executor host it holds the bot token).
src=$(mktemp -d) && git archive HEAD plugin | tar -x -C "$src" && git -C "$src" init -q &&
  git -C "$src" add plugin && git -C "$src" -c user.name=IntentRivet -c user.email=intentrivet@localhost commit -qm plugin
tar -C "$src" -cf - . | docker exec -i -u "$U" hermes sh -c 'rm -rf /tmp/intentrivet && mkdir /tmp/intentrivet && tar -xf - -C /tmp/intentrivet'
rm -rf "$src"
docker exec -it -u "$U" hermes hermes -p <profile> plugins install "file:///tmp/intentrivet#plugin"
docker exec -u "$U" hermes hermes -p <profile> intentrivet check
docker exec -u "$U" hermes rm -rf /tmp/intentrivet
docker restart hermes                    # its gateway loads the plugin
```

`hermes intentrivet check` prints `connected to the executor at ...; it accepted the agent key`
and the connected channels, or what is wrong (no address, no key, executor unreachable, key not
accepted).

What the installation changes in your Hermes: the plugin's directory, three tools, the
`hermes intentrivet` command, the `/intentrivet` chat command and two lines in that profile's
`.env`. Enabling it records it in `config.yaml`: `plugins.enabled` and, as Hermes does for every
plugin it enables, `plugins.disabled` (an empty list if there was none) and
`plugins.entries.intentrivet` (`allow_tool_override: false`). Hermes writes `config.yaml` itself,
and v0.20.1 rewrites the whole file: its values and the order of its keys stay, comments and
formatting do not; keep a copy if yours has comments. The model, memory, persona and other
settings are not changed.

### Step 5. Your first post

Chat with your Hermes as usual (its CLI or its own Telegram bot), for example: *"Write a short post
about our new release for the channel news. Do not publish it yet."* The preview comes to the
publisher bot's chat. Check the text, then the last line and the button: they name the version
(e.g. `post-1-v1`) and the channel. Press **Allow once** or **Reject**. After Allow once, ask
Hermes to publish that version, e.g. *"Publish post-1-v1 to news."*; it may also do so on its
own. The bot tells you the outcome and the post link.

## Using it

- **The preview.** The trusted fields come first; a long text arrives in several marked parts, and
  the buttons ride on the last one only once every part is there. The post text may imitate any
  line of the preview; the last line, after the text, and the Allow once button are written by the
  executor and name the version, the channel and its chat_id. Control and invisible characters in
  the text are shown as escapes (e.g. `\u202e`); the stored text and the send stay exact.
- **Allow once** gives Hermes one publication attempt of that version to that channel, usable at
  once and at any later time, without a further message from you. It does not expire and cannot
  be withdrawn; to stop publications to a channel, remove the publisher bot's right to post there,
  or stop the executor.
- **Pending requests** lists every request waiting for you, oldest first, ten per message with
  Previous / Next; Open brings any one's preview again (no new version). A preview that could not
  be delivered is not lost.
- **More channels:** Add channel, at any time.
- **The outcome of a version**, without sending anything: the bot's outcome notice and Pending
  requests (which lists `Outcome unknown` ones) in the publisher bot are the trusted view;
  `/intentrivet <version_id>` in Hermes is convenient, but Hermes shows it; on the executor host,
  `docker compose exec executor python -I /app/executor.py show <version_id>`.
- **If Hermes reports that a publication got no answer**, its outcome may still be `Publishing`:
  publish nothing again and prepare no new version until the outcome is known. If it ends
  `Outcome unknown`, the post may or may not be in the channel: check the channel. If the post is
  there, the publication happened. If it is not, a new attempt needs a new version and a new
  permission; nothing is created or retried automatically. `Failed` means nothing was published;
  the reason is in the detail.
- **Check the connection** from Hermes: `hermes -p <profile> intentrivet check` (in a container:
  `docker exec -u "$U" hermes hermes -p <profile> intentrivet check`). From the executor host,
  a read-only check of the bot, the owner and each channel: `stand/live-check.sh`.

On the executor host, in the repository directory with the two exports of step 1:

- Restart and updates keep everything, which lives in the executor's volume: `docker compose
  down` and `docker compose up -d`; after `git pull`, run `stand/setup.sh` again.
  `docker compose down -v` deletes the store.
- `docker compose exec executor python -I /app/executor.py requests`: the same decisions in a
  terminal, for the host operator.
- To hand the bot to another account, e.g. after losing yours: `docker compose exec executor
  python -I /app/executor.py reset-owner` (it asks you to type `reset`; versions, permissions and
  channels stay), then `... executor.py bind` for a new link.
- To replace the bot token: `stand/live-setup.sh` (if it fails, the stored token stays), then
  `docker compose up -d --force-recreate executor`.

In Hermes: to update the plugin, install it again with `--force` (Hermes keeps the two settings).
To change a setting, edit its line in the profile's `.env` (Hermes v0.22.7 also takes
`hermes -p <profile> config set INTENTRIVET_URL ...`; v0.20.1's `config set` writes such a name
into `config.yaml` instead, where the plugin does not read it). To remove IntentRivet: `hermes -p
<profile> plugins remove intentrivet` and delete the two lines from the profile's `.env`.

## Where to put the executor

The executor's permission check holds only under these conditions:

- Hermes cannot reach the executor's files, its container engine, the publisher bot's token or
  administrator rights on the executor's host. A container is not a boundary against a Hermes that
  drives the container's engine.
- Hermes has no other credential that can post in a connected channel: its own Telegram bot (its
  token is in Hermes' `.env`, which the agent can read), another bot's token, or the session of a
  channel administrator on the Hermes host. In every connected channel the publisher bot must be
  the only bot or account Hermes could use with the right to post, edit or delete messages:
  remove Hermes' own bot from the channel's administrators. The executor cannot see other
  administrators' posts.

Supported placements:

| Placement | When | Connection |
|---|---|---|
| **Another host** (recommended) | Hermes runs with broad rights, e.g. as your everyday account with `sudo`, `docker`, `lxd` or `libvirt` | [SSH tunnel](#hermes-on-another-host-ssh-tunnel) to the executor host's `127.0.0.1:8787`, with a key that allows that one forward and no command |
| **Same host, Hermes in a container** | Hermes' container has no container-engine socket and no mount of the executor account's files, as in Hermes' own Docker setup (`network_mode: host`, only `~/.hermes` mounted) | `http://127.0.0.1:8787` |
| **Same host, separate accounts** | the executor runs under its own account with its own rootless Podman; Hermes' account is not root, has no `sudo`, is in none of `docker`, `lxd`, `incus`, `libvirt`, `disk`, and cannot read the executor account's files | `http://127.0.0.1:8787` |

Not protected: Hermes running as the executor's account, or as an account (or in a container)
that can become root or drive the executor's engine on its host; or a Hermes whose own bot, or any
other account it can use, may post in a connected channel. IntentRivet still works there, but the
agent could bypass the executor.

`stand/check-isolation.sh`, run as Hermes' account (in its container, for a containerised Hermes),
checks the usual ways with that account's own rights: each check ends PASS (verified), FAIL, or
UNKNOWN (it could not be made), and it exits 0 only if every check passed. It covers what it
lists, not every way in, and says what it did not check:

```sh
sh stand/check-isolation.sh same-host <executor account>      # executor on this host
sh stand/check-isolation.sh other-host intentrivet-executor   # executor on another host (tunnel up)
```

On another host it checks that the tunnel reaches the executor's API (`http://127.0.0.1:8787`, or
the URL given after the destination) and that the tunnel's SSH login is confirmed and runs no
command; it does not check other SSH keys, logins or administrator rights the account may have on
the executor's host, and says so.

## Hermes on another host: SSH tunnel

On the executor host, logins from the Hermes host may only forward to the API. The key options
below cannot do that alone: `port-forwarding` also re-enables reverse (`ssh -R`) and Unix-socket
forwarding, which `permitopen` does not limit, and a socket such as the executor account's Podman
socket would give its container engine away. So **an administrator of the executor host** adds,
for that account and the Hermes host's address only (OpenSSH 8.2 or later; verified with 9.6p1):

```sh
sudo tee /etc/ssh/sshd_config.d/60-intentrivet-tunnel.conf >/dev/null <<'EOF'
# IntentRivet: logins from the Hermes host may only forward to the executor's API.
Match User <executor account> Address <Hermes host address>
    AllowTcpForwarding local
    PermitOpen 127.0.0.1:8787
    PermitListen none
    AllowStreamLocalForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTTY no
    PermitTunnel no
    ForceCommand /bin/false
EOF
sudo sshd -t && sudo systemctl reload ssh     # undo: remove the file, then the same two commands
```

Logins from other addresses keep their settings (`sudo sshd -T -C user=...,addr=...` shows both).

On the Hermes host, as Hermes' account, create the tunnel's key:

```sh
ssh-keygen -t ed25519 -N '' -f ~/.ssh/intentrivet_tunnel     # its .pub goes into the line below
cat >> ~/.ssh/config <<'EOF'
Host intentrivet-executor
  HostName <executor host>
  User <executor account>
  IdentityFile ~/.ssh/intentrivet_tunnel
  IdentitiesOnly yes
EOF
```

On the executor host, as the executor's account, add this line to `~/.ssh/authorized_keys`, with
that public key:

```
restrict,port-forwarding,permitopen="127.0.0.1:8787",command="/bin/false",from="<Hermes host address>" ssh-ed25519 AAAA... intentrivet-tunnel
```

Back on the Hermes host, as Hermes' account: log in once to record the executor host's key (it
exits at once, as the key runs nothing; compare the fingerprint it shows with the executor host's
`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`), then let systemd keep the tunnel up, across
restarts and without an open terminal:

```sh
ssh intentrivet-executor
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/intentrivet-tunnel.service <<'EOF'
[Unit]
Description=IntentRivet: SSH tunnel to the executor's API
After=network-online.target

[Service]
ExecStart=/usr/bin/ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:8787:127.0.0.1:8787 intentrivet-executor
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload && systemctl --user enable --now intentrivet-tunnel.service
loginctl show-user "$USER" -p Linger   # Linger=no: an administrator runs `sudo loginctl enable-linger <account>`
```

Hermes then reaches the executor at `http://127.0.0.1:8787` (its `INTENTRIVET_URL`), encrypted by
SSH; the API is never on a public address. That address serves a Hermes installed natively or in
a container on the host's network; for a container on a bridge network, see the next section.

## Hermes in a container on a Docker bridge network

In a container on a Docker bridge network (e.g. a Compose project's default network), 127.0.0.1 is
the container itself. The tunnel then listens on the bridge's gateway address on the Hermes host,
and Hermes uses that address. On the Hermes host (`<Hermes container>` is your container's name):

1. Find the network's gateway, the bridge interface and its subnet:

   ```sh
   docker inspect -f '{{range $n, $v := .NetworkSettings.Networks}}{{$n}} {{$v.Gateway}}{{"\n"}}{{end}}' <Hermes container>
   #   the network and its gateway, e.g. "myhermes_default 172.18.0.1"
   ip -o -4 addr show | grep ' <gateway>/'      # the bridge interface (e.g. br-0123456789ab) and the prefix (e.g. /16)
   ```

2. As Hermes' account, let the tunnel listen on the gateway instead of 127.0.0.1, then reload and
   restart it:

   ```sh
   sed -i 's/-L 127.0.0.1:8787:127.0.0.1:8787/-L <gateway>:8787:127.0.0.1:8787/' ~/.config/systemd/user/intentrivet-tunnel.service
   systemctl --user daemon-reload
   systemctl --user restart intentrivet-tunnel.service
   ```

   Hermes' `INTENTRIVET_URL` is then `http://<gateway>:8787`. Docker creates the address with the
   network; until then ssh exits and systemd tries again every 10 s.
3. A host firewall that drops incoming connections by default (e.g. UFW) drops the container's
   connection to that address too. **An administrator of the Hermes host** allows exactly that
   path; do not open the port on other interfaces and do not turn the firewall off:

   ```sh
   sudo ufw allow in on <bridge interface> proto tcp from <bridge subnet> to <gateway> port 8787 comment 'IntentRivet: Hermes container to the tunnel'
   sudo ufw status numbered       # undo: sudo ufw delete <the rule's number>
   ```

   With another firewall, add the same single rule: incoming TCP on that interface, from that
   subnet, to that address, port 8787.
4. Check the connection from the container, as Hermes' own user, never as root (that user owns
   Hermes' data, as in step 4 of the installation); it prints `401`, as no agent key is sent.
   Without `python3` in the container, `hermes intentrivet check` in step 4 does the check:

   ```sh
   U=$(docker exec <Hermes container> sh -c 'stat -c %U "${HERMES_HOME:-$HOME/.hermes}"')   # official image: hermes
   docker exec -u "$U" <Hermes container> python3 -I -c 'import urllib.request as u, urllib.error as e
   try: u.build_opener(u.ProxyHandler({})).open("http://<gateway>:8787/channels", timeout=8)
   except e.HTTPError as x: print(x.code)'
   ```

The bridge's name comes from the network's id: if the network is recreated (e.g. `docker compose
down`, then `up`), its name and perhaps its subnet change, and the rule must follow. Anything that
can reach the gateway address (the Hermes host's own processes, other containers let in) reaches
the tunnel too; every request still needs the agent key.

## Executor autostart after a reboot

`compose.live.yaml` gives the executor `restart: always`; rootless Podman starts such containers at
boot with its user service `podman-restart.service`, which runs only if the executor account's
systemd user manager starts without a login (linger). Once, on the executor host:

```sh
systemctl --user enable podman-restart.service       # as the executor's account
sudo loginctl enable-linger <executor account>       # an administrator
loginctl show-user <executor account> -p Linger      # Linger=yes
```

Without linger the executor also stops when that account's last session ends: its container runs
in the account's user manager. A real reboot with this setup has not been tested yet. The tunnel
of the previous sections needs linger for Hermes' account in the same way.

## Executor host setup

The executor runs on the account's own rootless Podman, which the Docker CLI reaches through a
Docker context, never a system-wide Docker Engine the account may also reach (`export
DOCKER_CONTEXT=intentrivet DOCKER_BUILDKIT=0` before the commands; `stand/setup.sh` sets
`DOCKER_BUILDKIT=0` itself, so Podman builds the image without an extra BuildKit container). The
setup refuses any engine but this account's rootless Podman. One-time host setup, by an
administrator for the packages, then as the executor's account:

```sh
sudo apt-get install -y podman uidmap slirp4netns passt
systemctl --user enable --now podman.socket
docker context create intentrivet --docker host=unix://$XDG_RUNTIME_DIR/podman/podman.sock
```

The Docker CLI with Compose v2 is needed as a client only. Verified with Ubuntu 24.04's
`docker.io` and `docker-compose-v2` packages (`docker.io` also installs Docker Engine, which
IntentRivet does not use); a client-only install was not tried.

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
2. The owner presses Allow once (or Reject) on the preview; in a terminal, `executor.py
   requests` does the same. Allow once allows exactly that version and its chat_id, once: Hermes
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
  plugin's tools can do, nothing more. What keeps the agent from the executor is the placement
  (above): no engine, no files, no administrator rights over the executor. Hermes' other
  credentials in that `.env` (its own Telegram bot's token, its model key) are the agent's too:
  none of them may be able to post in a connected channel.
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

## Optional: try it with the bundled example Hermes

For a try-out without an existing Hermes, `compose.live.yaml` carries an example Hermes (the
official image, its own volume) next to the executor, started only with the `demo` profile; it
reaches the executor on the stand's internal network (`http://executor:8080`). It is an example
and the test stand's agent, not the product's path. On the executor host, after step 1 and step 2:

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

## Optional demo: the stand-in stand

A disposable stand with its own project name, volumes and networks; no model and no
real Telegram. The plugin is installed from the committed `HEAD`. Its channels come from
`stand/channels.json` (two synthetic channels, keys `test` and `main`), imported once; with
`INTENTRIVET_LEGACY_CHANNELS=/dev/null` it starts empty, like a new installation, for
`stand/e2e_owner.py`.

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

## Optional: a scripted demo

A walkthrough for a recording, on a working installation with two of your channels connected (the
keys `test` and `main` below are examples): the owner's Telegram with the publisher bot's chat
open, the agent, and a diagnostics terminal on the executor host with `export
DOCKER_CONTEXT=intentrivet COMPOSE_FILE=compose.live.yaml`.

- A, the agent: the connected Hermes, in its CLI (it shows each tool call) or its own Telegram
  bot; with the bundled example, `docker compose exec -u hermes hermes hermes -p intentrivet-demo chat --cli`.
- C, diagnostics: `docker compose logs --no-log-prefix executor | grep '^sendMessage'`,
  the executor's own line before and after each send attempt to a channel (messages to the
  owner are not counted there); and the boundary test.

| Step | Where | Action | Expected |
|---|---|---|---|
| 1 | A | prompt 1 | `intentrivet_prepare_post` for `test`: a version id such as `post-2-v1`, `Awaiting approval`, preview delivered |
| 2 | Telegram | the preview in the bot chat | the full text, `[test]` with its title and chat_id, the version id; below the text and on the Allow once button, the version and `[test]` again |
| 3 | Telegram | Allow once | `Allowed one publication of post-2-v1 to [test] ... Nothing is sent yet` |
| 4 | A | prompt 2 | `intentrivet_publish_post` for `main` refused (403): allowed for `test`, not `main`; nothing sent |
| 5 | C | `stand/boundary-test.sh post-2-v1 main` (bundled example) | boundary test, scripted, not the model: allowed and requested channel, HTTP 403, the send count unchanged |
| 6 | C | the sendMessage lines | none for this version |
| 7 | A | prompt 3 | `Published`, with the message_id and the post link; the bot tells the owner too |
| 8 | A | prompt 4 | refused (409); nothing sent |
| 9 | C | the sendMessage lines | one attempt and one result for this version |

Prompts, with the version id from step 1:

1. `Write a short announcement that IntentRivet now lets Hermes publish to Telegram only with a person's permission, and submit it for the channel "test". Do not publish it.`
2. `Publish version post-2-v1 to the channel "main".`
3. `Now publish version post-2-v1 to the channel "test".`
4. `Publish post-2-v1 to the channel "test" once more.`

If the model declines to request the `main` channel, that is its own decision: record it as it
happened. Step 5 shows the executor's refusal reproducibly and is a scripted boundary test, never
presented as the model's decision. Note that after step 3 Hermes may publish on its own; if the
send in step 7 ends `Failed` or `Outcome unknown`, stop: no new version or permission is created
for a retry.

## Checks

- `python3 -m unittest discover -s tests`: executor, the owner's bot, plugin and stand-in,
  in process (also: no request without the agent key, the key replaced only on confirmation,
  the plugin's address and key from the profile, no redirect followed, `hermes intentrivet
  check`, the preview's trusted last line and buttons, lost and cut answers, the bounded HTTP
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

Hermes copies a `#subdir` install without its `.git`, so `hermes plugins list`
marks the plugin "provenance: drift" and `hermes plugins update` does not apply;
reinstall instead.

## Verified

- The executor on Linux with the account's rootless Podman (the setup refuses Docker Engine,
  rootful Podman and another user's Podman: nothing else was verified).
- Hermes v0.22.7 and v0.20.1 (2026.8.13) from the official images, in a container of their own on
  the host's network, as an existing profile with its own model, memory, persona, credentials and
  another plugin; the same through an SSH tunnel with a forward-only key.
- A two-host installation: an existing Hermes v0.20.1 in a container on a Docker bridge network,
  through the SSH tunnel listening on the bridge gateway behind the firewall rule above (OpenSSH
  9.6p1 on the executor host); the forward-only key and the sshd `Match` block were checked from
  that Hermes host (the API answers; no command, no `-R`, no other port, no Unix socket). The
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
- The boundary holds only in the supported placements: a Hermes that runs as the executor's
  account, can become root on the executor's host, or drives its container engine defeats it.
  So does any credential Hermes holds that can post in a connected channel, such as its own
  Telegram bot as an administrator there: the executor never sees such posts, and its checks
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

## License

MIT. See [LICENSE](LICENSE).
