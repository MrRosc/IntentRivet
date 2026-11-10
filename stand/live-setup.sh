#!/bin/sh
# Operator, on the host, in a terminal: store the live bot token in .stand-live/ (git-ignored,
# mode 0700). The container engine is checked first (stand/check-engine.sh): an unsupported one
# stops before the prompt, with nothing changed. The token is typed at a hidden prompt: it is not
# echoed, not in shell history, not in any command's arguments, and never printed (only its length
# and a SHA-256 prefix, the same fingerprint the executor logs). It is written to a new file, which
# is handed to the executor's container user (uid 10001 in this user's rootless Podman namespace),
# mode 0400, and only then replaces the stored token in one rename: if anything fails, the previous
# token stays. Channels are connected later, in the bot's private chat. Re-run to replace the
# token. stand/setup.sh runs this when needed.
set -eu
cd "$(dirname "$0")/.."
[ -t 0 ] || { echo "Run this in a terminal: the token is typed at a hidden prompt." >&2; exit 1; }
stand/check-engine.sh
umask 077
install -d -m 700 .stand-live
bad() { echo "$1 Nothing was saved; a stored token is kept." >&2; exit 1; }
f=.stand-live/telegram_bot_token
[ ! -d "$f" ] || bad "$f is a directory: the container engine made it when Compose ran before this setup. Remove it, then run this again."

trap 'stty echo; echo' EXIT
trap 'exit 130' INT TERM HUP
stty -echo                        # before the prompt: nothing typed after it is echoed
printf "Publisher bot token from @BotFather, a new bot, not Hermes' own (input hidden): "
IFS= read -r token || token=
stty echo
trap - EXIT INT TERM HUP
echo
# Shell builtins only from here: the token never becomes an argument of a program.
bot_id=${token%%:*}
secret=${token#*:}
case $bot_id in ''|*[!0-9]*) bad "That is not a bot token (digits, a colon, then letters, digits, _ or -)." ;; esac
case $secret in ''|*[!A-Za-z0-9_-]*) bad "That is not a bot token (digits, a colon, then letters, digits, _ or -)." ;; esac

new=$f.new
trap 'rm -f "$new"' EXIT          # a new file that did not replace the stored one is removed
trap 'exit 130' INT TERM HUP
rm -f "$new"
printf '%s\n' "$token" > "$new"
size=${#token}
fingerprint=$(printf '%s' "$token" | sha256sum | cut -c1-12)
unset token secret
chmod 400 "$new"
podman unshare chown 10001:10001 "$new" ||
  bad "Could not hand the token file to the executor's container user (podman unshare chown)."
mv -f "$new" "$f"

if [ ! -e .stand-live/channels.json ]; then   # mounted for the one-time import of stage 3-4 channels
  printf '{\n  "channels": {}\n}\n' > .stand-live/channels.json
  chmod 644 .stand-live/channels.json   # the executor's user reads it; the 0700 directory keeps others out
fi

echo "Saved in .stand-live/ (git-ignored):"
echo "  telegram_bot_token  $size bytes, sha256 $fingerprint... (readable only by the executor's container user)"
echo "Next: stand/setup.sh"
