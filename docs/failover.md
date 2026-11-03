# Failover

Dozorcam can run on several machines at once so that a spare one takes over the engine or
the bot. This is a separate layer on top of the container supervisor: the engine and the bot
themselves know nothing about other nodes. Russian: [failover.ru.md](failover.ru.md).

Status: **step 1 of 5** — standby mode and manual switching from the bot. Automatic takeover
when a node dies comes in later steps.

## Node and roles

A **node** is any machine with Docker that runs the same pair of containers (engine + bot)
from the same image, with the same configuration directory. Nodes differ only by name
(`CCTV_NODE` in the node's `.env`) and by priority in the `[failover]` section of
`config.toml`.

There are two **roles**: `engine` (cameras, recording, detection) and `bot` (Telegram).
Exactly one node is active for each role; the roles may sit on different nodes. Every other
node is **armed**:

- its containers are running and its configuration is the same;
- the engine does not open RTSP or ONVIF to the cameras (camera session limits);
- the bot does not poll Telegram (a second poller would steal updates and duplicate replies);
- the healthcheck is green and says `armed: the … role is held by node …`.

Once a minute (`check_sec`) every node checks whether it is **fit** for its roles: a bare TCP
connect to the main stream port of every enabled camera (no RTSP request, nothing sent) and
`getMe` to the Bot API. The result is shown in `/nodes`.

Without a `[failover]` section there is no layer: a single node, both roles always active.

## Configuration

Everything about nodes lives in the shared configuration directory, so every node reads the
same file. The only per-node setting is its name.

```toml
# config.toml
[failover]
check_sec = 60                 # fitness check period, 5…3600 s

[[failover.nodes]]
name = "dacha"                 # lowercase latin, digits, dashes, up to 20
priority = 1                   # lower is more important; active from the first start
address = "192.0.2.10"         # agents at :8782 (engine) and :8783 (bot)

[[failover.nodes]]
name = "city"
priority = 2
address = "192.0.2.20"
# engine_agent = "https://192.0.2.20:8782"          # or agent addresses explicitly
# bot_agent = "https://192.0.2.20:8783"
# bridge_url = "https://192.0.2.20:8780"            # this node's bridge, for a bot elsewhere
# events_url = "https://192.0.2.20:8781/v1/events"  # this node's bot receiver, for an engine elsewhere

# Between machines the agents talk mutual TLS with the installation's CA:
# tls_cert = "/etc/cctv/tls/node.crt"
# tls_key = "/etc/cctv/tls/node.key"
# tls_ca = "/etc/cctv/tls/ca.crt"
```

```toml
# secrets.toml (mode 0600)
[failover]
secret = "…"                   # shared key that signs agent requests, at least 16 characters
```

```sh
# .env of each node
CCTV_NODE=dacha
```

| Key | Where | Meaning |
|---|---|---|
| `check_sec` | `[failover]` | fitness check period, default 60 s |
| `nodes` | `[failover]` | the list of nodes, `[[failover.nodes]]` |
| `tls_cert`, `tls_key`, `tls_ca` | `[failover]` | mutual TLS between agents; all three or none |
| `name` | node | node name, the same as `CCTV_NODE` of that machine |
| `priority` | node | lower wins; default — the order in the list |
| `address` | node | host of the node; agents at ports 8782/8783 |
| `engine_agent`, `bot_agent` | node | agent addresses explicitly (two instances on one machine) |
| `bridge_url`, `events_url` | node | how a role on another node reaches this node's engine bridge / bot receiver |
| `secret` | `secrets.toml` `[failover]` | shared signing key |

Rules the configuration enforces (the role does not start otherwise, the reason is in the log
and the healthcheck): plain `http` agents only on loopback (two instances on one machine);
between machines — `https` with `tls_cert`, `tls_key`, `tls_ca`; unknown keys are rejected;
`CCTV_NODE` must name one of the nodes. Every request between agents is signed with
`secret`, has a time window and cannot be replayed.

A role on another machine also needs the engine↔bot link over the network: set
`internal_tls = true` in `[common]` (see [reference: TLS](reference.md#engine--bot-link-and-tls))
and the node's `bridge_url` / `events_url`.

## Switching a role from the bot

`/nodes` (owner only) shows every node, which role is active where, which nodes are armed,
whether each node is fit and how long ago it was checked, and the result of the last
switch. The buttons "engine → node" / "bot → node" move a role; a node that does not answer
or is not fit gets no button. After the confirmation:

1. the current node freezes the role and confirms its processes are stopped;
2. the new node takes the role's state directory from it and starts the role;
3. the other nodes learn the new assignment (they also catch up by themselves every
   `check_sec`).

There are never two active nodes for a role, not even for a second. If the new node does
not start the role, the role goes back to the old one. While the engine moves, recording
and detection pause for about a minute; while the bot moves, it is silent for a few
seconds and then answers from the new node — send `/nodes` again there.

### What moves with a role

Only the role's **state directory on disk**: the camera registry and calibrations of the
engine, the bot's database (topics, owner, people). The **memory of the processes is lost**
on every switch — the new node starts the role from scratch, as after a restart: the
recording buffer, an unfinished `/add` dialog, pending events are not carried over. The
node's own files stay where they are: its role assignment (`failover.json`) and the
detector's daily diagnostics (`diag/`).

## Two instances on one machine

For a test without a second machine: two copies of the compose project with different
`COMPOSE_PROJECT_NAME`, ports (`CCTV_BRIDGE_PORT`, `CCTV_EVENTS_PORT`,
`CCTV_RTSP_PROXY_PORT`) and `CCTV_NODE`, the same configuration directory, and explicit
loopback agents:

```toml
[[failover.nodes]]
name = "dacha"
engine_agent = "http://127.0.0.1:8782"
bot_agent = "http://127.0.0.1:8783"
bridge_url = "http://127.0.0.1:8780"
events_url = "http://127.0.0.1:8781/v1/events"

[[failover.nodes]]
name = "city"
engine_agent = "http://127.0.0.1:8792"
bot_agent = "http://127.0.0.1:8793"
bridge_url = "http://127.0.0.1:8790"
events_url = "http://127.0.0.1:8791/v1/events"
```

## How it is checked

- `tests/test_failover_armed.py` — an armed engine starts no processes and sends the camera
  0 bytes (TCP connect only), never touches the snapshot/ONVIF port or the detector stream;
  an armed bot only calls `getMe`, never `getUpdates`.
- `tests/test_failover_switch.py` — two real supervisor processes with a real bot against a
  Bot API mock: `/nodes` and the buttons move the bot there and back; Telegram is polled by
  one node at a time, every action gets exactly one reply, the fitness check is visible in
  the panel. The engine moves there and back through the bot's panel client; its processes
  never live on two nodes at once; the state directory moves, process memory does not.
- `tests/test_failover_boundary.py` — the engine, the bot and the shared code do not import
  the layer; the supervisor uses only "be active / freeze"; layer settings come only from
  the shared configuration directory (from the node's environment only `CCTV_NODE`), and
  every key is documented here.
