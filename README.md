# IntentRivet

IntentRivet lets your existing [Hermes Agent](https://github.com/NousResearch/hermes-agent) prepare
posts for your Telegram channels while you keep the decision. Hermes prepares posts in your usual
chat with it, your own publisher bot shows you each version's exact preview, and one tap decides:
**Allow once** or **Reject**. Only a version you allowed can be published: once, to the channel you
saw.

- **Your Hermes stays as it is:** its model, memory, persona, other plugins, profile and its own
  Telegram bot.
- **You decide in Telegram,** on the exact text, the channel and the version.
- **Publishing stays out of Hermes' reach:** a small separate executor holds the publisher bot's
  token and your decisions. Hermes gets only an agent key, which can prepare posts and ask to
  publish them, never allow them.

Version 0.4.2, MIT license. For Telegram channels (groups cannot be connected) and Hermes 0.20.1 or
later.

## How it works

1. **Prepare.** You ask Hermes for a post for one of your channels. The executor stores that exact
   text as a new version and sends you its preview, in the private chat of your publisher bot.
2. **Check the preview.** It shows the full text, the channel's title and name, its chat_id and the
   version. Its last line, below the text, and the **Allow once** button name the version and the
   channel again: decide on those, whatever the post text itself says.
3. **Allow once or Reject.** **Reject** refuses that version for good. **Allow once** sends nothing
   by itself, but from that moment Hermes may publish exactly that version to that channel, once:
   at once or at any later time, without asking you again. A permission does not expire and cannot
   be withdrawn.
4. **Result.** Hermes asks the executor to publish. The executor makes at most one send attempt per
   permission, with the stored text to the stored channel, and the bot tells you the outcome and the
   post link.

**Two bots.** You keep talking to Hermes as before, through its own Telegram bot or its CLI; you do
not need a new Hermes bot. The **publisher bot** is a second, new bot of yours, with any name and
avatar, for IntentRivet only. Its token is typed in on the executor's machine and stays with the
executor; the bot sends you the previews and posts to your channels. Hermes' own bot must not be
an administrator of those channels.

**Two parts.** The **plugin** goes into a profile of your existing Hermes: three tools (list the
connected channels, prepare a post, ask to publish a prepared version), the `/intentrivet` chat
command and `hermes intentrivet check`. The **executor** is a small trusted service in its own
container, apart from Hermes: it stores each version as an immutable snapshot (the exact text and
the channel's chat_id), sends the previews, keeps your decisions and publishes. It is separate
because an agent with a terminal can edit its own plugin, read its own files and call any address
it reaches, so the decision cannot live inside Hermes.

**One-time setup, then Telegram.** Setting up is a one-time job on one or two Linux machines, with
a few commands for an administrator. After that, everything happens in two chats: yours with
Hermes, and the publisher bot's.

## Before you start

### What you need

- **Your existing Hermes**, 0.20.1 or later (tested: v0.20.1 and v0.22.7), installed natively or in
  a container.
- **A Linux machine for the executor that stays on**, the *executor host*; tested: Ubuntu 24.04. The
  executor runs there under a regular account (not root), on that account's rootless Podman
  ([Executor host setup](#executor-host-setup) lists the packages). It opens no port to the
  internet: it reaches Telegram outbound, over HTTPS to `api.telegram.org`. It can be Hermes' own
  machine only in the setups that [Start here](#start-here-where-to-put-the-executor) names.
- **An administrator of the executor host**, once, for the packages and for keeping the executor
  running without a login. If Hermes runs on another host, an administrator is needed once more on
  each host, for the tunnel ([Start here](#start-here-where-to-put-the-executor) says what for).
- **A Telegram account** that can create a bot with @BotFather and add administrators to your
  channels. IntentRivet connects Telegram channels; groups cannot be connected.
- **If Hermes runs on another host** (the *Hermes host*): SSH from it to the executor host, and
  Linux with systemd there, which keeps the tunnel up.
- **git** on the executor host and on the Hermes host.

### Know your Hermes

Three facts about your Hermes decide the setup. On the machine Hermes runs on:

1. **Natively or in a container?** If `hermes --version` works for the account Hermes runs as,
   Hermes is installed natively. If it runs in a container, `docker ps` lists it;
   `<Hermes container>` below stands for that container's name.
2. **For a container: on which network?**

   ```sh
   docker inspect -f '{{.HostConfig.NetworkMode}}' <Hermes container>
   ```

   `host` is the host's network; anything else, such as `bridge` or `myhermes_default`, is a bridge
   network.
3. **Its version and profile.** Natively, as Hermes' account: `hermes --version` (it must be 0.20.1
   or later) and `hermes profile list` (the active profile is marked). In a container, run Hermes'
   commands as Hermes' own user, never as root: the first line below finds that user, who owns
   Hermes' data.

   ```sh
   U=$(docker exec <Hermes container> sh -c 'stat -c %U "${HERMES_HOME:-$HOME/.hermes}"')   # official image: hermes
   docker exec -u "$U" <Hermes container> hermes --version
   docker exec -u "$U" <Hermes container> hermes profile list
   ```

   `<profile>` below is the profile you connect. For the default profile, leave out `-p <profile>`
   wherever it appears.

### The protection conditions

The executor decides only if Hermes cannot go around it, and that depends on your setup:

- Hermes must not have the publisher bot's token, access to the executor's data or its
  administration (the executor's files, its container engine, administrator rights or any login on
  the executor host other than the tunnel's forward-only key), or any other credentials that can
  post in the channels you connect. That includes a Telegram session of yours that Hermes' account
  can read, e.g. Telegram Desktop signed in under that account: it could post in your channels
  and, as the owner, press Allow once itself.
- Hermes' own Telegram bot must not be an administrator of those channels: no right to post, edit
  or delete messages there.

Under these conditions Hermes can publish only what you allowed. Where they are broken, Hermes could
post around the executor, and no setup rules out every way in, such as a flaw in the kernel or the
container engine. You ensure the conditions with the setup you choose (next) and the channels'
administrator lists (step 2). In full: [docs/placement.md](docs/placement.md).

## Start here: where to put the executor

Find the row that matches your Hermes. All rows take the same steps; step 4 names the differences.

| Your Hermes runs | Hermes reaches the executor | Tested |
|---|---|---|
| On another host, in a container on the host network | through an SSH tunnel, at `http://127.0.0.1:8787` | Yes: Hermes v0.20.1 and v0.22.7, official images (in that test, the tunnel's SSH server ran on the same machine) |
| On another host, natively | through an SSH tunnel, at `http://127.0.0.1:8787` | Not run (the same commands) |
| On another host, in a container on a Docker bridge network | through an SSH tunnel on the bridge's gateway, with one firewall rule, at `http://<gateway>:8787` | Yes: an installation on two hosts, Hermes v0.20.1 |
| On the executor host, in a container on the host network, with no container-engine socket and none of the executor account's files | directly, at `http://127.0.0.1:8787` | Yes: Hermes v0.20.1 and v0.22.7, official images |
| On the executor host, under an account of its own: not root, no `sudo`, in none of the groups `docker`, `lxd`, `incus`, `libvirt`, `disk`, unable to read the executor account's files | directly, at `http://127.0.0.1:8787` | Not run |

- **Another host is recommended** whenever Hermes runs with broad rights, e.g. as your everyday
  account with `sudo`, `docker`, `lxd` or `libvirt`. Its account there (with `sudo` or `docker`:
  every account on that host) must then hold no SSH key or agent that logs in to the executor host,
  other than the tunnel's key, and no Telegram session of yours.
- **Not covered:** a Hermes container on a bridge network on the executor host itself. Put the
  executor on another host instead (the third row).
- **Not protected:** Hermes running as the executor's account, or as an account (or in a
  container) that can become root or drive the executor's container engine on its host; or a Hermes
  whose own bot, or any other account it can use, may post in a connected channel. IntentRivet still
  works there, but Hermes could bypass it.

**Where an administrator is needed, once:** on the executor host, for the packages and linger
([Executor host setup](#executor-host-setup)). For Hermes on another host, also: one sshd setting
on the executor host, linger for Hermes' account on the Hermes host, and for a bridge network one
firewall rule there ([step 4a](#4a-hermes-on-another-host-ssh-tunnel)).

The conditions in full, the placements and a script that checks the usual ways around:
[docs/placement.md](docs/placement.md).

## Install

The steps go in this order; each needs the one before. Each says where to run it and as whom, what
it is for, what you should see, and what comes next. `<...>` marks a value of yours, explained
before its first use; `news` is an example channel name.

| Step | Where, as whom | What |
|---|---|---|
| [Executor host setup](#executor-host-setup) | executor host: an administrator, then the executor's account | packages, linger, the Docker CLI on rootless Podman |
| [1](#step-1-executor-host-the-executor-and-your-publisher-bot) | Telegram; executor host, the executor's account | your publisher bot; the executor |
| [2](#step-2-telegram-you-as-the-owner-your-channels) | Telegram, your account | you as the owner; your channels |
| [3](#step-3-executor-host-the-agent-key) | executor host, the executor's account | the agent key for Hermes |
| [4](#step-4-hermes-host-connect-hermes) | Hermes host, Hermes' account; for another host also the executor host, and an administrator on each | the SSH tunnel if needed; the plugin in your profile |
| [5](#step-5-your-first-post) | your chat with Hermes; the publisher bot | your first post |

Would you rather go through it with an AI assistant? See [Need help setting up?](#need-help-setting-up)

### Executor host setup

- **Where:** the executor host: first an administrator, then the account that will run the
  executor.
- **Goal:** rootless Podman for that account, reached by the Docker CLI, and a user manager that
  runs without a login, so that the executor keeps running and starts after a reboot.

`<executor account>` is that account: a regular account, not root (`id -un` prints its name). Log in
to it directly, e.g. over SSH, not with `su` or `sudo -u`: `systemctl --user` needs the account's
own login session.

As an administrator:

```sh
sudo apt-get install -y podman uidmap slirp4netns passt git
sudo loginctl enable-linger <executor account>
```

IntentRivet also needs the Docker CLI with Compose v2, as a client only. If `docker compose version`
prints a version, it is there: skip the next command. Otherwise an administrator installs Ubuntu's
packages (tested on Ubuntu 24.04; a client-only install was not tried), but never next to Docker's
own packages such as `docker-ce`: they conflict, and `-y` would replace them under the containers
they run.

```sh
sudo apt-get install -y docker.io docker-compose-v2   # also installs Docker Engine, which IntentRivet does not use
```

As the executor's account:

```sh
systemctl --user enable --now podman.socket
systemctl --user enable podman-restart.service
docker context create intentrivet --docker host=unix://$XDG_RUNTIME_DIR/podman/podman.sock
```

**Done when** `loginctl show-user <executor account> -p Linger` prints `Linger=yes` and
`docker --context intentrivet version` shows `Podman Engine` under `Server`. The setup always goes
through this Docker context, never through a system-wide Docker Engine the account may also reach,
and it refuses any engine but this account's rootless Podman.

**Next:** [step 1](#step-1-executor-host-the-executor-and-your-publisher-bot).

#### Executor autostart after a reboot

The linger and `podman-restart.service` lines above start the executor after a reboot:
`compose.live.yaml` gives it `restart: always`, and Podman's user service `podman-restart.service`
starts such containers at boot, which needs linger. Without linger the executor also stops when the
account's last session ends, as its container runs in the account's user manager. If your executor
already runs without them, run them now; nothing needs restarting. A real reboot with this setup has
not been tested yet.

### Step 1. Executor host: the executor and your publisher bot

- **Where:** Telegram, then the executor host as the executor's account.
- **Goal:** create your publisher bot, and start the executor with its token.

1. In Telegram, send `/newbot` to @BotFather and choose any name and username. It replies with the
   bot's token. Use this bot for IntentRivet only: not Hermes' own bot, no webhook, no other program
   reading its updates.
2. On the executor host:

   ```sh
   git clone https://github.com/MrRosc/IntentRivet intentrivet && cd intentrivet
   export DOCKER_CONTEXT=intentrivet       # this account's rootless Podman (Executor host setup)
   export COMPOSE_FILE=compose.live.yaml   # the executor's deployment, for every docker compose command
   stand/setup.sh
   ```

   It says what it sets up, checks the container engine, and asks:
   `Publisher bot token from @BotFather, a new bot, not Hermes' own (input hidden):`. Paste the
   token and press Enter; nothing is shown. The token never becomes an argument of a program, and
   afterwards only the executor's container user can read it. The setup then builds and starts the
   executor alone and checks the bot read-only: a webhook is reported and left in place, because the
   executor cannot poll a bot that has one.

**Done when** it prints `RESULT: ready`, then a one-time link, `https://t.me/<your bot>?start=...`,
and the next steps. If it stops instead, its last message says what to fix (see also
[Troubleshooting](#troubleshooting)); then run it again. It is safe to run again at any time: it
keeps the token, the data and the owner.

The executor's API now listens on `127.0.0.1:8787` of this host only. Later commands on the
executor host run in this `intentrivet` directory with these two exports; in a new terminal,
`cd intentrivet` and export them again. For another port, export `INTENTRIVET_PORT` with them,
before `stand/setup.sh` and in every later terminal, and use that port in place of 8787 throughout
these documents.

**Next:** [step 2](#step-2-telegram-you-as-the-owner-your-channels), within 10 minutes for that
link.

### Step 2. Telegram: you as the owner, your channels

- **Where:** Telegram, signed in as the account that will own the publisher bot.
- **Goal:** make that account the only one whose decisions count, and connect your channels.

1. Open the link from step 1 within 10 minutes and press **Start**. The bot binds your account by
   its numeric user id; the link then stops working, and no Telegram message can change the owner.
   Anyone else who writes to the bot gets "This is a private bot."

   **Done when** the bot answers `You are now the owner of this bot.` and shows its menu: **Add
   channel**, **Channels**, **Pending requests**. If it answers that the link is not valid (expired
   or used), get a new link on the executor host, in the `intentrivet` directory with the exports of
   step 1:

   ```sh
   docker compose exec executor python -I /app/executor.py bind
   ```

2. For each channel, press **Add channel**. The bot gives you a one-time check text, valid for 30
   minutes. Then:
   1. In the channel's settings, under Administrators, add the publisher bot with the right to post
      messages; it needs no other right. Check that list while you are there: Hermes' own bot, and
      any other account Hermes can use, must not be an administrator of the channel.
   2. Post the check text in the channel, exactly as given. **It is a real post that your
      subscribers can see**; delete it once the channel is connected.
   3. The bot shows the channel's title and chat_id. Send it a short name of your own for the
      channel, e.g. `news` (a-z, 0-9 and -): Hermes will use that name as the channel key.

   **Done when** the bot answers `Connected [news]: <title> (chat_id ...)`. No chat_id to look up,
   no file to edit. **Channels** lists the connected channels; to connect another one, now or later,
   use **Add channel** again. A channel that is already connected, or a name already taken, is
   refused; nothing is ever replaced.

**Next:** [step 3](#step-3-executor-host-the-agent-key).

### Step 3. Executor host: the agent key

- **Where:** the executor host, as the executor's account, in the `intentrivet` directory with the
  exports of step 1.
- **Goal:** the key that lets Hermes list your channels, prepare posts and ask to publish them;
  it cannot allow anything.

```sh
docker compose exec executor python -I /app/executor.py agent-key
```

**Done when** it prints `New agent key.` and the key on a line of its own. It is shown only this
once: keep it for step 4, where you type it at a hidden prompt, and do not send it through chats.
Running the command again replaces the key (it asks you to type `replace`); the previous key stops
working at once.

**Next:** [step 4](#step-4-hermes-host-connect-hermes).

### Step 4. Hermes host: connect Hermes

- **Where:** the Hermes host, as Hermes' account (for Hermes in a container: the account you run it
  with). For a tunnel, also the executor host.
- **Goal:** a route from Hermes to the executor, then the plugin in your existing profile.

#### 4a. Hermes on another host: SSH tunnel

Only when Hermes runs on another host than the executor. Follow
[docs/remote-hermes.md](docs/remote-hermes.md): an administrator of the executor host adds one sshd
setting; you create a key that can only forward to the executor's API, and a service on the Hermes
host that keeps the tunnel up. For a Hermes container on a bridge network, it also moves the tunnel
to the bridge's gateway and adds one firewall rule. It ends with the address for `INTENTRIVET_URL`;
then come back to 4b.

When Hermes runs on the executor host, skip 4a: the address is `http://127.0.0.1:8787`.

#### 4b. The plugin in your existing profile

Work in a new terminal, not one with the exports of step 1: their `DOCKER_CONTEXT` would point
`docker` at the executor's Podman instead of Hermes' container engine. Get this repository on the
Hermes host as well (on the executor host, the clone from step 1 will do if you work as the
executor's account; another account clones its own):

```sh
git clone https://github.com/MrRosc/IntentRivet intentrivet && cd intentrivet
```

What the installation changes in your Hermes: the plugin's directory, three tools, the
`hermes intentrivet` command, the `/intentrivet` chat command and two lines in that profile's
`.env`. Enabling it records it in `config.yaml`: `plugins.enabled` and, as Hermes does for every
plugin it enables, `plugins.disabled` (an empty list if there was none) and
`plugins.entries.intentrivet` (`allow_tool_override: false`). Hermes writes `config.yaml` itself,
and v0.20.1 rewrites the whole file: its values and the order of its keys stay, comments and
formatting do not. **Keep a copy of the profile's `config.yaml` first if it has comments.** The
model, memory, persona and other settings are not changed.

The install asks for `INTENTRIVET_URL`, the address from 4a (`http://127.0.0.1:8787`, or
`http://<gateway>:8787` for a container on a bridge network), and for `INTENTRIVET_AGENT_KEY`, the
key from step 3, typed hidden. Hermes saves both in that profile's `.env` and offers to enable the
plugin: answer `y`. Leave out `-p <profile>` for the default profile.

Hermes installed natively, as Hermes' account, in the `intentrivet` directory:

```sh
hermes -p <profile> plugins install "file://$PWD#plugin"
hermes -p <profile> intentrivet check
hermes -p <profile> gateway restart      # if its gateway runs: it loads the plugin
```

Hermes in a container, in the `intentrivet` directory: run every `hermes` command in the container
as Hermes' own user, never as root (root would load plugin code that Hermes' user can change). The
first line finds that user:

```sh
U=$(docker exec <Hermes container> sh -c 'stat -c %U "${HERMES_HOME:-$HOME/.hermes}"')   # official image: hermes
# Only the plugin goes into the container: its committed files as a small git repository; no other
# file of this clone (never copy the clone itself: on an executor host it holds the bot token).
src=$(mktemp -d) && git archive HEAD plugin | tar -x -C "$src" && git -C "$src" init -q &&
  git -C "$src" add plugin && git -C "$src" -c user.name=IntentRivet -c user.email=intentrivet@localhost commit -qm plugin
tar -C "$src" -cf - . | docker exec -i -u "$U" <Hermes container> sh -c 'rm -rf /tmp/intentrivet && mkdir /tmp/intentrivet && tar -xf - -C /tmp/intentrivet'
rm -rf "$src"
docker exec -it -u "$U" <Hermes container> hermes -p <profile> plugins install "file:///tmp/intentrivet#plugin"
docker exec -u "$U" <Hermes container> hermes -p <profile> intentrivet check
docker exec -u "$U" <Hermes container> rm -rf /tmp/intentrivet
docker restart <Hermes container>        # its gateway loads the plugin
```

**Done when** the check prints
`IntentRivet: connected to the executor at <address>; it accepted the agent key.` and
`Channels: [news] <title>`. If it prints `IntentRivet: not connected.` with a reason, see
[Troubleshooting](#troubleshooting).

Recommended: check that Hermes' account cannot take the executor over, with
`stand/check-isolation.sh` ([docs/placement.md](docs/placement.md#check-the-placement)).

**Next:** [step 5](#step-5-your-first-post).

### Step 5. Your first post

- **Where:** your usual chat with Hermes (its CLI or its own Telegram bot), and the publisher bot's
  private chat.
- **Goal:** one full round: prepare, check, allow, publish.

1. Ask Hermes, for example: *"Write a short post about our new release for the channel news. Do not
   publish it yet."* Hermes reports a version id such as `post-1-v1`, and the preview arrives in the
   publisher bot's chat.
2. Read the text, then the last line and the button. The last line reads
   `Allow once → post-1-v1 to [news] <title> (chat_id <id>), <n> characters`, the button
   `Allow once → [news] post-1-v1`. Press **Allow once** (or **Reject**). The bot confirms
   `Allowed one publication of post-1-v1 to [news] ... Nothing is sent yet.`
3. Ask Hermes: *"Publish post-1-v1 to news."* Once you allowed it, Hermes may also do so on its own.

**Done when** the post is in your channel and the bot says `Published post-1-v1 to [news] ...` with
the post link. The setup is complete: from now on, see [Daily use](#daily-use).

## Daily use

Day to day, everything happens in your chat with Hermes and in the publisher bot's private chat.

- **Ask Hermes for a post** for a connected channel, by its name (e.g. *news*). Each version's
  preview arrives in the publisher bot's chat, and Hermes gets the version id and whether the
  preview was delivered.
- **Decide on the last line and the button.** The preview shows the trusted fields, then the full
  text between `BEGIN TEXT` and `END TEXT`. A long text comes in several marked parts, and the
  buttons ride on the last one only once every part is there. The post text may imitate any line of
  the preview; the last line, after the text, and the Allow once button are written by the executor
  and name the version, the channel and its chat_id. Control and invisible characters in the text
  are shown as escapes (e.g. `\u202e`); the stored text and the send stay exact.
- **Allow once** gives Hermes one publication attempt of that version to that channel, usable at
  once or at any later time, without a further message from you. The button itself sends nothing.
  A permission does not expire and cannot be withdrawn; to stop publications to a channel, remove
  the publisher bot's right to post there, or stop the executor.
- **Reject** refuses that version for good. To change a post, ask Hermes to revise it: that makes a
  new version with its own preview, and an older version of the same post can then no longer be
  allowed.
- **Pending requests** lists every request waiting for you, oldest first, ten per message with
  Previous / Next; Open brings any one's preview again (no new version). A preview that could not be
  delivered is listed there too.
- **Channels** lists the connected channels; **Add channel** connects another one, as in step 2.
- **The outcome.** After a publication attempt the bot tells you how it ended:
  - `Published`, with the post link.
  - `Failed`: nothing was published; the reason is in the message. The permission is used: another
    attempt needs a new version and a new Allow once.
  - `Outcome unknown`: the request went out, but no complete answer came back, so the post may or
    may not be in the channel. Check the channel: if the post is there, the publication happened;
    if it is not, a new attempt needs a new version and a new permission. Nothing is retried, and
    nothing is created to replace it automatically.
- **If Hermes reports that a publication got no answer,** its outcome may still be `Publishing`:
  publish nothing again and prepare no new version until the outcome is known. The trusted view is
  the publisher bot: its outcome notice, and Pending requests, which lists `Outcome unknown`
  versions. `/intentrivet <version_id>` in Hermes is convenient, but Hermes shows it.

On the executor host nothing is needed day to day. Restarts, updates, a new owner, bot token or
agent key, and the plugin's settings: [docs/operations.md](docs/operations.md).

## Troubleshooting

- **`stand/setup.sh` stops at the container engine check**, e.g. with
  `The Docker CLI reaches Docker Engine or another engine, not Podman`: finish
  [Executor host setup](#executor-host-setup), run `export DOCKER_CONTEXT=intentrivet` in this
  terminal, and run the setup as the executor's account, not as root. That check changes nothing.
- **`Failed to connect to bus`** from `systemctl --user`: log in to that account directly, e.g. over
  SSH, not with `su` or `sudo -u`.
- **`RESULT: not ready: the bot has a webhook ...`**: another program uses this bot. Create a new bot
  for IntentRivet and replace the token ([docs/operations.md](docs/operations.md#on-the-executor-host)),
  or, if this bot is for IntentRivet only, remove the webhook yourself (Bot API `deleteWebhook`);
  then run `stand/setup.sh` again.
- **`RESULT: not ready: getMe: Unauthorized`**: the stored token is wrong, e.g. mistyped at the
  hidden prompt, or revoked. Running the setup again keeps it: replace it as in
  [docs/operations.md](docs/operations.md#on-the-executor-host) (`stand/live-setup.sh`, then
  recreate the executor), then run `stand/setup.sh` again.
- **The bot says the owner link is not valid:** it expired after 10 minutes or was used. Get a new
  one ([step 2](#step-2-telegram-you-as-the-owner-your-channels)).
- **Add channel: no answer after you post the check text.** The publisher bot must be an
  administrator of that channel, and the text must be exactly the one given, posted in the channel
  itself within 30 minutes; after that, press Add channel again for a new text. If the bot answers
  that it found the text but the bot is not allowed to post there, give it the right to post
  messages and post the same text again.
- **`IntentRivet: not connected. Executor unreachable at ...`**: the executor is not running
  (on the executor host, with the exports of step 1, `docker compose ps` shows it), the tunnel is
  down (on the Hermes host, `systemctl --user status intentrivet-tunnel.service` shows it), or the
  address is wrong. From a container on a bridge network, a timeout usually means the firewall rule
  is missing
  ([docs/remote-hermes.md](docs/remote-hermes.md#5-hermes-in-a-container-on-a-docker-bridge-network)).
- **`Executor (401): Agent key not accepted ...`**: the profile has an old or mistyped key. Issue a
  new one ([step 3](#step-3-executor-host-the-agent-key)) and set it in Hermes
  ([docs/operations.md](docs/operations.md#in-hermes)). `No agent key is set on the executor yet`:
  do step 3.
- **`INTENTRIVET_URL is not set ...` or `INTENTRIVET_AGENT_KEY is not set ...`**: set that line in
  the profile's `.env` ([docs/operations.md](docs/operations.md#in-hermes)).
- **Hermes does not know the channel:** it takes the name you gave in Add channel. **Channels** in
  the bot lists them, and Hermes can list them too.
- **No preview arrived:** Hermes' result says whether the preview was delivered. **Pending
  requests** in the bot lists every request waiting for you; Open shows its preview.
- **Hermes reports no answer for a publication, or the bot says `Outcome unknown`:** publish
  nothing again and prepare no replacement; check the channel first ([Daily use](#daily-use)).
- **The executor stops when you log out, or does not start after a reboot:** linger or
  `podman-restart.service` is missing
  ([Executor autostart after a reboot](#executor-autostart-after-a-reboot)). Then start it:
  `docker compose up -d`, in the `intentrivet` directory with the exports of step 1.

## Need help setting up?

You can go through these steps with an AI assistant, e.g. one that works in a terminal next to you.
It is optional: the steps above are complete without it. A prompt to start with:

```text
Help me set up IntentRivet (https://github.com/MrRosc/IntentRivet) for my existing Hermes Agent.
Read README.md first, then only the documents it links that my setup needs.
1. Find out where and how my Hermes runs (which machine, natively or in a container, which network,
   its version and profile) and which Linux machine can host the executor. Ask me; do not guess.
2. Choose the one row of "Start here" that matches, tell me whether it is marked as tested, and
   name every step that needs an administrator.
3. Guide me one step at a time. For each command, say on which machine and as which account to run
   it, and what I should see. Check my result before any step that depends on it.
4. Keep my Hermes as it is (its model, memory, persona, other plugins, profile, Telegram bot and
   settings), and leave every other service on these machines alone.
5. Never ask me to paste the publisher bot's token, the agent key, a private key or the one-time
   owner link into this conversation: I type them only where the steps say.
6. Do not weaken the protection conditions to make the setup easier.
```

An assistant that runs commands for you with administrator rights acts as your operator during the
setup: give it that access only as you would to a person doing the setup. Never give such rights,
the publisher bot's token or access to the executor to Hermes itself: the executor must stay out of
Hermes' reach.

## More documentation

- [docs/placement.md](docs/placement.md): where to put the executor: the protection conditions in
  full, the placements, and the isolation check.
- [docs/remote-hermes.md](docs/remote-hermes.md): Hermes on another host: the SSH tunnel, and a
  Hermes container on a Docker bridge network.
- [docs/operations.md](docs/operations.md): running it: restarts and updates, a new owner, bot token
  or agent key, the plugin's settings, removal.
- [docs/reference.md](docs/reference.md): one publication in detail, the statuses, trust boundaries
  and the executor's API, the tested setups, limitations, pinned versions, the repository layout.
- [docs/testing.md](docs/testing.md): trying it without your own Hermes (the bundled example Hermes,
  a Telegram stand-in), a scripted demo, and the checks.

## License

MIT. See [LICENSE](LICENSE).
