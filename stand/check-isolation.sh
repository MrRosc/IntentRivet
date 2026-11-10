#!/bin/sh
# Run on the Hermes host, as the account Hermes runs as (inside its container, for a containerised
# Hermes), before connecting it to the executor. It checks, with that account's own rights, the usual
# ways the agent could take the executor over: root, sudo, root-equivalent groups, engine sockets, the
# executor account's files (same host), or a shell through the tunnel's SSH login (other host). Nothing
# is changed. Each check ends PASS (verified), FAIL, or UNKNOWN (it could not be made); exit 0 only if
# every check passed. That covers what is listed, not every way in; the result says what was not checked.
#   stand/check-isolation.sh same-host ACCOUNT            the executor runs under ACCOUNT on this host
#   stand/check-isolation.sh other-host DESTINATION [URL] it runs on another host; Hermes reaches its API
#        at URL (default http://127.0.0.1:8787) through an SSH tunnel to DESTINATION (as in ~/.ssh/config)
set -u
failed=0 broken=0 unknown=0
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; failed=$((failed + 1)); }         # a way around the executor
broken() { echo "FAIL  $1"; broken=$((broken + 1)); }       # the connection does not work
unknown() { echo "UNKNOWN  $1"; unknown=$((unknown + 1)); }
info() { echo "INFO  $1"; }
case "${1:-}:${2:-}:$#" in same-host:?*:2|other-host:?*:2|other-host:?*:3) ;; *)
  echo "Usage: $0 same-host EXECUTOR_ACCOUNT | other-host SSH_DESTINATION [URL]" >&2; exit 2 ;;
esac
mode=$1 target=$2 url=${3:-http://127.0.0.1:8787} me=$(id -un) groups=" $(id -nG) "
echo "Hermes account: $me (uid $(id -u)); executor: $mode $target"

# Rights that reach everything on this host. On the executor's host they defeat any placement; on
# another host they do not reach the executor, so they are shown only.
risk() { if [ "$mode" = same-host ]; then fail "$1"; else info "$1 (this host only)"; fi; }
[ "$(id -u)" -ne 0 ] && pass "not root" || risk "runs as root"
if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then risk "sudo works without a password"
else pass "no sudo without a password"; fi
for g in sudo wheel admin; do
  case $groups in *" $g "*) risk "member of $g: becomes root with its password" ;; esac
done
for g in docker lxd incus libvirt disk; do
  case $groups in *" $g "*) risk "member of $g: root-equivalent without a password" ;; esac
done
for s in /var/run/docker.sock /run/docker.sock /run/podman/podman.sock /run/containerd/containerd.sock; do
  [ -e "$s" ] && [ -w "$s" ] && risk "can write $s: the system container engine"
done
for v in DOCKER_HOST CONTAINER_HOST; do
  eval "value=\${$v:-}"; [ -n "$value" ] && info "$v is set ($value); make sure it is not the executor's engine"
done

if [ "$mode" = same-host ]; then
  if entry=$(getent passwd "$target"); then
    uid=$(echo "$entry" | cut -d: -f3) home=$(echo "$entry" | cut -d: -f6)
    [ "$target" != "$me" ] && pass "a separate account" || fail "Hermes runs as the executor's account $target"
  else  # e.g. Hermes in a container: the host's accounts are not visible; check where they would be
    info "no account $target visible from here (a container?); checking /home/$target and /run/user"
    uid='*' home=/home/$target
  fi
  if [ "$target" != "$me" ]; then  # the same account owns everything anyway
    if ls "$home" >/dev/null 2>&1; then fail "can list $target's home $home (the executor's files and volumes)"
    else pass "cannot list $target's home"; fi
    entered=$(for d in /run/user/$uid; do [ -d "$d" ] && [ "$d" != "/run/user/$(id -u)" ] && ls "$d" >/dev/null 2>&1 && echo "$d"; done)
    if [ -n "$entered" ]; then fail "can enter $entered ($target's runtime directory: its Podman socket)"
    else pass "cannot enter $target's runtime directory (its Podman socket)"; fi
  fi
else
  # The API through the tunnel: without the agent key, the executor answers 401 naming the key.
  reply=$(curl -s -m 10 -w ' HTTP%{http_code}' "$url/channels" 2>/dev/null) && code=0 || code=$?
  case $code:$reply in
    127:*) unknown "no curl here: the executor's API at $url was not checked" ;;
    0:*[Aa]"gent key"*" HTTP401") pass "the executor's API answers at $url (401 without the agent key)" ;;
    0:*) broken "$url answers, but not as the executor's API (${reply##* })" ;;
    *) broken "the executor's API is not reachable at $url (curl exit $code): is the tunnel up?" ;;
  esac
  # The tunnel's login (README: restrict,port-forwarding,permitopen,command="/bin/false"): it logs in
  # and runs nothing, so ssh exits 1 with no output. Only a login confirmed in ssh's own log counts.
  log=$(mktemp) && trap 'rm -f "$log"' EXIT
  said=$(ssh -v -o BatchMode=yes -o ConnectTimeout=10 "$target" echo shell-allowed 2>"$log") && code=0 || code=$?
  grep -q -e "^Authenticated to " -e "Authentication succeeded" "$log" && in=yes || in=no
  case $code:$in:$said in
    *shell-allowed*) fail "the SSH login to $target runs commands: Hermes' account gets a shell there" ;;
    1:yes:) pass "the SSH login to $target is confirmed and runs no command (forced command)" ;;
    255:no:*) broken "no SSH login to $target (ssh exit 255): set up the tunnel's login first" ;;
    *) unknown "ssh exited $code (login confirmed: $in): whether its login runs commands was not verified" ;;
  esac
fi

if [ "$failed" -gt 0 ]; then
  echo "RESULT: NOT isolated ($failed problem(s)): Hermes could bypass the executor from this account."
  [ "$mode" = same-host ] && echo "Put the executor on another host, or run Hermes under an account without these rights."
  exit 1
fi
if [ "$broken" -gt 0 ]; then
  echo "RESULT: not connected ($broken problem(s) above), so the tunnel's limits were not fully verified."
  exit 1
fi
if [ "$unknown" -gt 0 ]; then
  echo "RESULT: incomplete: $unknown check(s) could not be made, so this proves nothing; fix them and run again."
  exit 1
fi
if [ "$mode" = same-host ]; then
  echo "RESULT: none of the checked rights found. Not checked: other ways in, such as another account's"
  echo "credentials or a flaw in the kernel or the container engine."
else
  echo "RESULT: the tunnel reaches the executor, and its SSH login runs no command. Not checked: other SSH keys,"
  echo "logins or administrator rights this account (or this host's administrators) may have on $target's host."
fi
exit 0
