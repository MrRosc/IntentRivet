# Hermes on another host: SSH tunnel

Part of [step 4a](../README.md#4a-hermes-on-another-host-ssh-tunnel) of the README's installation.
When it is done, go on with [step 4b](../README.md#4b-the-plugin-in-your-existing-profile).

The executor's API listens only on the executor host's `127.0.0.1:8787`, never on a public address.
Hermes on another host reaches it through an SSH tunnel, encrypted by SSH, with a key that allows
that one forward and no command. Parts 1 to 4 set it up; part 5 is only for a Hermes container on a
Docker bridge network.

You need:

- SSH from the Hermes host to the executor host. The executor host runs OpenSSH 8.2 or later
  (tested: 9.6p1); the Hermes host runs Linux with systemd, which keeps the tunnel up.
- An administrator of the executor host, once (part 1), and an administrator of the Hermes host,
  once, for linger (part 4) and, on a bridge network, one firewall rule (part 5).
- A way to work on the executor host as the executor's account other than SSH from the Hermes
  host: its console, or SSH from another machine. Part 1 takes that account's shell away from
  logins that come from the Hermes host.

The values to fill in:

- `<executor account>`: the account that runs the executor
  ([Executor host setup](../README.md#executor-host-setup)).
- `<executor host>`: the executor host's name or address as the Hermes host reaches it over SSH.
- `<Hermes host address>`: the Hermes host's address as the executor host sees it; it must not
  change. On the Hermes host, `ip route get` followed by the executor host's IP address prints it
  after `src`, unless a NAT router stands between the hosts. Otherwise, before part 1, log in from
  the Hermes host to the executor host and read the first field of `echo $SSH_CONNECTION` there.
- Hermes' account: the account on the Hermes host that runs Hermes (for Hermes in a container, the
  account you run it with). The tunnel runs as this account.

## 1. Executor host, an administrator: allow only the forward

On the executor host, logins from the Hermes host may only forward to the API. The key options of
part 3 cannot do that alone: `port-forwarding` also re-enables reverse (`ssh -R`) and Unix-socket
forwarding, which `permitopen` does not limit, and a socket such as the executor account's Podman
socket would give its container engine away. So **an administrator of the executor host** adds,
for that account and the Hermes host's address only:

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

**Done when** `sshd -t` prints nothing and the reload succeeds. Logins from other addresses keep
their settings (`sudo sshd -T -C user=...,addr=...` shows both).

From then on, every login as `<executor account>` from the Hermes host's address gets no shell and
runs no command, also with your own keys or a password. Do part 3, and any later work as the
executor's account, on the executor host itself or from another machine; bring the public key of
part 2 over by copy and paste, as `ssh-copy-id` from the Hermes host no longer works.

## 2. Hermes host, Hermes' account: the tunnel's key

```sh
ssh-keygen -t ed25519 -N '' -f ~/.ssh/intentrivet_tunnel     # its .pub goes into the line of part 3
cat >> ~/.ssh/config <<'EOF'
Host intentrivet-executor
  HostName <executor host>
  User <executor account>
  IdentityFile ~/.ssh/intentrivet_tunnel
  IdentitiesOnly yes
EOF
```

**Done when** `~/.ssh/intentrivet_tunnel.pub` exists: its one line is the public key for part 3.

## 3. Executor host, the executor's account: authorize that key

Add this line to `~/.ssh/authorized_keys`, with the public key from part 2 in place of
`ssh-ed25519 AAAA... intentrivet-tunnel`:

```
restrict,port-forwarding,permitopen="127.0.0.1:8787",command="/bin/false",from="<Hermes host address>" ssh-ed25519 AAAA... intentrivet-tunnel
```

**Done when** the line is in the file; the first login in part 4 shows that it works.

## 4. Hermes host, Hermes' account: keep the tunnel up

Log in once to record the executor host's key. Compare the fingerprint it shows with the executor
host's `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`, and accept it only if they match; the
login then ends at once, as the key runs nothing. Then let systemd keep the tunnel up, across
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

**Done when** `systemctl --user status intentrivet-tunnel.service` shows `active (running)`, with
its recent log lines, and `loginctl show-user "$USER" -p Linger` prints `Linger=yes`. If the first
login was refused with `Permission denied (publickey)`, the executor host sees another address than
`<Hermes host address>`: check it as described above, in part 1 and in the key's line.

Hermes then reaches the executor at `http://127.0.0.1:8787` (its `INTENTRIVET_URL`). That address
serves a Hermes installed natively or in a container on the host's network: go on with
[step 4b](../README.md#4b-the-plugin-in-your-existing-profile). For a container on a bridge network,
do part 5 first.

## 5. Hermes in a container on a Docker bridge network

In a container on a Docker bridge network (e.g. a Compose project's default network), 127.0.0.1 is
the container itself. The tunnel then listens on the bridge's gateway address on the Hermes host,
and Hermes uses that address. On the Hermes host (`<Hermes container>` is your container's name):

1. Find the network's gateway, the bridge interface and its subnet:

   ```sh
   docker inspect -f '{{range $n, $v := .NetworkSettings.Networks}}{{$n}} {{$v.Gateway}}{{"\n"}}{{end}}' <Hermes container>
   #   the network and its gateway, e.g. "myhermes_default 172.18.0.1"
   ip -o -4 addr show | grep ' <gateway>/'      # the bridge interface (e.g. br-0123456789ab) and the prefix (e.g. /16)
   ```

   `<gateway>` is that address, `<bridge interface>` that interface, and `<bridge subnet>` the
   network with that prefix (e.g. `172.18.0.0/16` for `172.18.0.1/16`).
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
   Hermes' data, as in step 4b of the installation); it prints `401`, as no agent key is sent.
   Without `python3` in the container, `hermes intentrivet check` in step 4b does the check:

   ```sh
   U=$(docker exec <Hermes container> sh -c 'stat -c %U "${HERMES_HOME:-$HOME/.hermes}"')   # official image: hermes
   docker exec -u "$U" <Hermes container> python3 -I -c 'import urllib.request as u, urllib.error as e
   try: u.build_opener(u.ProxyHandler({})).open("http://<gateway>:8787/channels", timeout=8)
   except e.HTTPError as x: print(x.code)'
   ```

**Done when** that check prints `401`. Go on with
[step 4b](../README.md#4b-the-plugin-in-your-existing-profile), with `http://<gateway>:8787` as
`INTENTRIVET_URL`.

The bridge's name comes from the network's id: if the network is recreated (e.g.
`docker compose down`, then `up`), its name and perhaps its subnet change, and the rule must
follow. If the subnet changed, so did the gateway: put the new one into the unit's `-L` option (the
`sed` of step 2, with the old gateway in place of `127.0.0.1`), reload and restart the tunnel,
update the firewall rule, and set the new `INTENTRIVET_URL` in the profile's `.env`
([docs/operations.md](operations.md#in-hermes)). Anything that can reach the gateway address (the
Hermes host's own processes, other containers let in) reaches the tunnel too; every request still
needs the agent key.
