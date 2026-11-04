# Failover

Dozorcam can run on several machines at once so that a spare one takes over the engine or
the bot. This is a separate layer on top of the container supervisor: the engine and the bot
themselves know nothing about other nodes. Russian: [failover.ru.md](failover.ru.md).

Status: **step 3 of 5** — standby mode, manual switching from the bot, one configuration
for all nodes (the active node hands it to the armed ones) and several engines behind one bot
([sites](#sites-several-engines-behind-one-bot)). Automatic takeover when a node dies comes in
later steps.

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

Everything about nodes lives in the configuration directory, and the active node hands that
directory to the others (see [One configuration for all nodes](#one-configuration-for-all-nodes)),
so every node runs on the same files. The only per-node setting is its name.

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
node's own files stay where they are: its role assignment (`failover.json`), its copy of the
handed-out configuration (`failover-config/`, it has its own revisions) and the detector's
daily diagnostics (`diag/`). The configuration and the settings made from the chat are on the
new node already — the active node handed them out while it was armed.

## One configuration for all nodes

The node that holds a role hands its configuration to the armed nodes, so a spare node is
always ready to take over with the same cameras and settings. Two things are handed out,
both over the agents' link (mutual TLS with the installation's CA between machines, every
request signed with `secret`), without the engine or the bot taking part:

- **the configuration directory** — `config.toml`, `secrets.toml`, `cameras.json`. Every
  agent keeps a copy in its state directory (`failover-config/`) with a revision number and
  runs its role on that copy, not on the node's own directory;
- **settings made from the chat** — files in the engine's state that the bot changes through
  the bridge: `cameras.json` (the camera registry), `overrides.json` (paused and retired
  cameras), `activation-vault.json` (passwords of activated cameras), `person_model.json`
  (the chosen model), `person_threshold.override.json` (a manual threshold). An armed node
  writes them only while the role is not running there.

A change reaches the armed nodes within seconds (the holder checks its files every 2 s and
sends the change at once); a node that was down catches up at the next check (`check_sec`).
`/nodes` keeps showing which node holds each role.

**Where to edit.** Edit the configuration directory on the node that holds the role. An
edit on an armed node is not handed out (the log says so). A new node needs only the
`[failover]` section in `config.toml` and the `secret` in `secrets.toml` — everything else
arrives from the active node. After a switch the new active node keeps running on the
received configuration; its own directory is handed out only if it matched the current
revision before the edit — an outdated or incomplete directory of a former spare node never
overwrites the real one. To edit there, first take the current files from the copy:

```sh
docker compose exec engine cat /var/lib/cctv/state/failover-config/config.toml > config/config.toml
docker compose exec engine cat /var/lib/cctv/state/failover-config/secrets.toml > config/secrets.toml
docker compose exec engine cat /var/lib/cctv/state/failover-config/cameras.json > config/cameras.json
chmod 600 config/secrets.toml
```

A received revision on which the node would not start (broken TOML, unknown section, this
node missing from `[failover]`) is rejected — the node keeps the previous one and stays
reachable to receive a fix. The certificate files (`tls_*`) and `secret` are not something
the link can change for itself: the link needs them to work, so change them on every node.

**Node environment.** With a `[failover]` section the handed-out configuration is stronger
than the node's own environment (`.env`): an outdated value in a spare node's `.env` does
not override what the active node handed out. Only node-local keys keep the node's own
value:

- node name and paths inside the container: `CCTV_NODE`, `CCTV_CONFIG_DIR`, `CCTV_STATE_DIR`, `CCTV_BUFFER_DIR`, `CCTV_STORAGE_ROOT`, `CCTV_RUNTIME_DIR`, `CCTV_SUPERVISOR_STATUS`, `CCTV_CAMERA_CONFIG`, `CCTV_PROXY_CAMERA_CONFIG`, `CCTV_RUNTIME_CAMERA_CONFIG`, `CCTV_REGISTRY_FILE`, `CCTV_REGISTRY_SEED`, `CCTV_PROVISION_SOCKET`, `CCTV_PROVISION_BACKUPS`, `CCTV_PROVISION_UNITS`, `CCTV_RETENTION_GUARD`, `CCTV_MODEL_DIRS`, `CCTV_NOTIFY_STATE`
- addresses and ports of this machine: `CCTV_BIND`, `CCTV_PORT`, `CCTV_PUBLIC_URL`, `CCTV_EVENTS_HOST`, `CCTV_EVENTS_PORT`, `CCTV_EVENTS_URL`, `CCTV_BRIDGE_URL`, `CCTV_RTSP_PROXY_PORT`, `CCTV_NODES_URL`, `CCTV_NODES_TOKEN`
- its own engine↔bot certificates: `CCTV_BRIDGE_CA`, `CCTV_BRIDGE_CLIENT_CERT`, `CCTV_BRIDGE_CLIENT_KEY`, `CCTV_CLIENT_CA`, `CCTV_SERVER_CERT`, `CCTV_SERVER_KEY`, `CCTV_EVENTS_CA`, `CCTV_EVENTS_CERT`, `CCTV_EVENTS_CLIENT_CA`, `CCTV_EVENTS_KEY`, `CCTV_BRIDGE_USER`, `CCTV_BRIDGE_GROUP`
- its network and Bot API server: `CCTV_DISCOVERY_NETWORKS`, `CCTV_TELEGRAM_API`
- its memory, disk and CPU: `CCTV_BUFFER_MAX_BYTES`, `CCTV_BUFFER_SEGMENTS`, `CCTV_MAX_TOTAL_BYTES`, `CCTV_STORAGE_BUDGET_BYTES`, `CCTV_MIN_FREE_BYTES`, `CCTV_CV_THREADS`, `CCTV_DETECT_NICE`, `CCTV_PERSON_INFER_SLOTS`

Everything else is a setting of the installation: put it into `config.toml` or
`secrets.toml`, not into `.env`. A new setting that would not travel with the handed-out
configuration fails `tests/test_failover_config.py`.

## Sites: several engines behind one bot

A **site** is a set of cameras with its own engine — usually a place with its own network
(a country house, a city flat). One bot serves several sites at once: every engine sends its
events to the bot's receiver, and the bot puts them into the same group and the same topics —
a topic belongs to a camera (`camera_id`), not to an engine. A camera that moves from one
engine to another keeps its topic. The engines know nothing about each other or about sites:
each one keeps its own camera registry and talks to the bot over the usual engine ↔ bot link.

Sites are a list in the `[bot]` section of `config.toml`:

```toml
[bot]
sites = ["dacha", "city=https://192.0.2.20:8780"]
```

- `name=address` — the site and the address of its engine's bridge; `name` alone — the bridge
  of the bot itself (`bridge_url`; with `[failover]` it follows the engine role, so this is the
  site whose engine moves between nodes). The name is lowercase latin, digits and dashes, up to
  20 characters; two sites with one name or one bridge do not start.
- No `sites` — one implicit site, exactly as before: a single-engine install is unchanged, and
  so is a list of one site.
- Between machines the link needs `internal_tls = true` in `[common]`: every bridge has a
  certificate from the installation's CA, the bot uses its own client certificate for all of
  them ([reference: TLS](reference.md#engine--bot-link-and-tls)). Without TLS only loopback
  addresses are allowed (two engines on one machine for a test).
- `sites` is a setting of the installation, not of a node: with `[failover]` it travels with
  the handed-out `config.toml`, so the bot finds the same sites on whichever node it runs.

What the bot does with several sites:

- **the camera registry** is the cameras of all sites; a camera without its own location tag
  is shown on the map under its site's name. A camera id that exists on two sites belongs to
  the first one in the list (the log says so) — give cameras unique ids;
- **a camera's buttons** (frame, clip, pause, rename, setup, threshold) go to the bridge of
  its site, media is taken only from the bridge the event came from;
- **`/add`** asks which site the camera is at: the network search, the activation and the
  password check go through that site's engine, because only it can reach its cameras.
  `/add <site>` and `/add <site> <address>` skip the question;
- **the detector model** is one for the installation: `/model` shows the first site's catalog
  and switches the model on every site; thresholds of all sites are in one list;
- **a silent site** does not stop the others: its cameras stay with the "no frames" state
  (nothing is retired), the other sites keep posting events. Only when every site is silent
  does the bot report the bridge as down.

With `[failover]` one engine — the site without an address in `sites` — is the engine role of
the layer: it has an armed copy on another node and moves with `/nodes`. The engines of the
other sites run on their own machines outside the layer, with their own configuration
directory. So that their events find the bot on whichever node it runs, list the bot's
receiver of every such node in their `events_url` — the engine sends each event to the first
receiver that accepts it (an armed bot does not listen):

```toml
[engine]
events_url = ["https://192.0.2.10:8781/v1/events", "https://192.0.2.20:8781/v1/events"]
```

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
- `tests/test_sites.py` — two real engine bridges with different cameras and one bot, all
  over mutual TLS: the install starts with one engine and the bot creates the topics; then the
  cameras move to two sites with their own engines, the bot restarts on the same database with
  `sites`, creates no new topics, and events sent by both engines at once land in the old
  topics of their cameras; a camera's pause reaches only its site's engine; `/add` asks for the
  site; a silent site keeps its cameras and does not stop the other one; an engine with two
  receivers in `events_url` delivers to the one that runs when the first is closed. Compatibility: no
  `sites` gives the single bridge client exactly as before, a list of one site behaves the same
  way; invalid lists refuse to start.
- `tests/test_failover_config.py` — two nodes talking mutual TLS, each with its own
  configuration directory (the spare one has only `[failover]` and outdated values): the
  first revision, an edit of the directory on the active node and settings made from the
  chat reach the armed node within seconds without manual steps; a client without the
  installation's certificate gets no answer; after the engine moves, the new active node
  runs on the received configuration and does not hand out its outdated directory, while an
  edit on top of the current revision is handed back. Guards: a new file in the configuration
  directory, a new settings file in a role's state, a setting read outside `CCTV_*`, a
  setting that would not follow the handed-out configuration or an undocumented node-local
  key fails the test; a received revision on which the node would not start is rejected.
  The list of sites travels the same way: added on the active node, it reaches the armed one
  and wins over an outdated `CCTV_SITES` in its `.env`.
