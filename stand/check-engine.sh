#!/bin/sh
# The container engine check that stand/setup.sh and stand/live-setup.sh run first, before a token
# is asked for or changed. The verified installation is rootless Podman, run by this user, reached
# through the Docker CLI with Compose v2: the token file is handed to the executor's container user
# with podman unshare, which acts in this user's rootless Podman only. Docker Engine, rootful Podman
# or another user's Podman stop here, with nothing changed.
set -u
stop() { printf '%s\nNothing was changed.\n' "$1" >&2; exit 1; }
podman_only="This setup is verified only with rootless Podman, run by the user who runs it and reached \
through the Docker CLI (README: Executor host setup)."

command -v docker >/dev/null 2>&1 || stop "The docker command (the Docker CLI with Compose v2) is not installed."
components=$(docker version --format '{{range .Server.Components}}{{.Name}};{{end}}' 2>/dev/null) ||
  stop "The Docker CLI reaches no container engine: point it at your rootless Podman (DOCKER_CONTEXT or DOCKER_HOST)."
case $components in
  *"Podman Engine"*) ;;
  *) stop "The Docker CLI reaches Docker Engine or another engine, not Podman. $podman_only" ;;
esac
command -v podman >/dev/null 2>&1 ||
  stop "The podman command is not installed; the setup needs it (podman unshare) for the token file. $podman_only"
[ "$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null)" = true ] ||
  stop "podman does not run rootless for this user. $podman_only"
mine=$(podman info --format '{{.Store.GraphRoot}}' 2>/dev/null)
engine=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)
[ -n "$mine" ] && [ "$mine" = "$engine" ] ||
  stop "The Docker CLI reaches a Podman other than this user's rootless Podman (storage ${engine:-unknown}, \
not ${mine:-unknown}). $podman_only"
