# Laptop control through reverse SSH

The laptop initiates SSH to `live4s.arman-babaei.ir:22`. A reverse forward exposes
the laptop's loopback-only RPC on the host's loopback port 18766. No NAT changes,
public HTTP listener, root laptop process, or additional public ports are needed.

**Security boundary:** the service grants arbitrary command execution as the
laptop user to anyone possessing the token and tunnel access. File RPC paths are
restricted to `--root`, but commands are NOT sandboxed and can access everything
the laptop user can. Use a dedicated account if stronger isolation is needed.
Do not point it at untrusted jobs, publish the token, or run it with sudo. The
host's administrators can access the credential. Stop the service/tunnel to
revoke access; rotate the token before restarting to invalidate copies.

## Laptop setup

Substitute your actual SSH username for `HOST_USER`. No password or private key
should be pasted into chat. Keep existing local repository changes intact.

```sh
cd /path/to/imquic-l4s-tests
git fetch origin perf/render-vmaf-20s
git switch perf/render-vmaf-20s
git pull --ff-only origin perf/render-vmaf-20s

mkdir -p "$HOME/.config/live4s"
chmod 700 "$HOME/.config/live4s"
python3 tools/l4s/laptop_rpc.py \
  --token-file "$HOME/.config/live4s/laptop-rpc.token" init-token
```

Token creation is exclusive; it will not replace an existing credential. Send the
token to the host over SSH, not Git. These commands intentionally fail rather
than overwrite an existing host token:

```sh
ssh HOST_USER@live4s.arman-babaei.ir \
  'umask 077; mkdir -p "$HOME/.config/live4s"; chmod 700 "$HOME/.config/live4s"; set -C; cat > "$HOME/.config/live4s/laptop-rpc.token"' \
  < "$HOME/.config/live4s/laptop-rpc.token"
```

Run the service in a dedicated terminal (or an explicitly started tmux session):

```sh
python3 tools/l4s/laptop_rpc.py \
  --token-file "$HOME/.config/live4s/laptop-rpc.token" serve \
  --root "$PWD" --state "$HOME/.local/state/live4s/laptop-rpc" \
  --port 18765 --allow-exec
```

In another laptop terminal, establish the tunnel:

```sh
ssh -NT -p 22 \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -R 127.0.0.1:18766:127.0.0.1:18765 \
  HOST_USER@live4s.arman-babaei.ir
```

The SSH server must allow remote forwarding and keep `GatewayPorts no` (the
default). Do not enable public binding. Failure to bind 18766 must be resolved
before using the client; do not connect to an unidentified existing listener.

## Host control

Run under the host account that received the token:

```sh
python3 tools/l4s/laptop_rpc.py \
  --token-file "$HOME/.config/live4s/laptop-rpc.token" call info

python3 tools/l4s/laptop_rpc.py \
  --token-file "$HOME/.config/live4s/laptop-rpc.token" call start \
  '{"argv":["nvidia-smi"],"cwd":"."}'
```

`start` returns a job ID. Use it for `call status '{"job":"ID","offset":0}'`
or `call stop '{"job":"ID"}'`. Status returns base64 logs in chunks of at most
1 MiB and a `next_offset`. A null return code means still running. Commands are
argv arrays with no implicit shell; explicitly invoking a shell is permitted.
Only this service instance's owned jobs can be stopped through RPC. Job ownership
does not persist across service restarts; logs and launch metadata do persist.
Keep the service alive while jobs run. Ctrl-C sends SIGTERM to owned process
groups; commands that detach can escape those groups. This is not a process
sandbox or a guarantee of descendant cleanup.

File methods: `list` (`path`), `read` (`path`, `offset`, `size`), and `write`
(`path`, `data_base64`, optional `overwrite:true`). Read/write chunks are limited
to 1 MiB. Parent directories must already exist; writes default to exclusive
creation. Use rsync/scp over the original SSH connection for bulky scene/video
transfers instead of this JSON interface.

There is no automatic startup, network discovery, deletion method, or unattended
installation. No laptop connection exists until the user starts both commands.
