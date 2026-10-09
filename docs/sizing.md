# Will it run on my box? — hardware sizing

Russian: [sizing.ru.md](sizing.ru.md). The calculator: [website](https://egorchenkov.github.io/dozorcam/#sizing).
All documents: [README.md](README.md).

- [Short answer](#short-answer)
- [Where the resources go](#where-the-resources-go)
- [Measured numbers](#measured-numbers)
- [How it was measured](#how-it-was-measured)
- [What will not run](#what-will-not-run)
- [Measure your own machine](#measure-your-own-machine)

## Short answer

| Cameras (2 MP, ~4 Mbit/s) | RAM | CPU | Example |
|---|---|---|---|
| 1–2 | 2 GB | 2 cores | Raspberry Pi 4/5 (2 GB+), Orange Pi 5, any mini PC |
| 3–4 | 4 GB | 4 cores | Raspberry Pi 5 (4–8 GB), Intel N100 mini PC |
| 5–8 | 8 GB | 4 cores | Intel N100/N305 mini PC, a home server |
| 9–16 | 16 GB | 4+ cores | a home server |

Disk: about 2 GB free for the image (~0.8 GB unpacked) and one update next to it; state is
kilobytes, logs are capped by Docker (10 MB × 3 per container). Video is never written to disk.

## Where the resources go

- **Video buffer (RAM).** Each camera is recorded into a rolling buffer in tmpfs: 10 minutes
  of the main stream and of the detector stream, but not more than `CCTV_BUFFER_MAX_BYTES` per
  stream. The installer sets 200 MB per stream (7 minutes at 4 Mbit/s, 3.5 at 8 Mbit/s) and
  gives the buffer 25 % of RAM within 512 MB–4 GB, so a camera takes up to ~240 MB and the
  buffer holds RAM/4 ÷ 240 MB cameras (2 GB — 2, 4 GB — 4, 8 GB — 8, 16 GB — 17). The engine
  limits every stream on its own and has **no common limit**: cameras that do not fit into
  `CCTV_BUFFER_TMPFS` fill it up and recording stops ("No space left on device"). Adding cameras
  beyond that — raise `CCTV_BUFFER_TMPFS` (and RAM) first.
- **Engine processes (RAM).** About 85–95 MB plus 77–90 MB per camera right after start; on a
  live installation after hours of work — about 150 MB plus 140 MB per camera. The calculator
  uses the latter.
- **Bot:** about 66 MB. **System and Docker:** keep about 0.4 GB.
- **CPU, quiet scene.** Each camera's detector stream is decoded all the time and passes the
  motion gate: 0.03–0.05 core per camera.
- **CPU, people or moving trees.** The person detector (YOLOX-Tiny, 2 frames per second per
  camera) runs only on frames that passed the motion gate: 0.14 core per busy camera on a
  modern x86 core, 0.61 on an ARM Neoverse-N1 core. At most two frames are scored at once
  (`CCTV_PERSON_INFER_SLOTS=2`), so the detector never takes more than ~2 cores: with more busy
  cameras it queues and events arrive later instead of starving the recording and the bot.

## Measured numbers

**Live installation** (4 Hikvision cameras, an ARM server with 4 Neoverse-N1 cores,
release 0.1.3, 6 hours of normal day and evening): the engine averaged 0.53 core (`cpu.stat`
of its cgroup over the uptime), memory 1.6 GB — 0.7 GB processes and 0.85 GB of video buffer
in a 2 GB tmpfs; the bot 0.015 core and 66 MB; the image 800 MB.

**Bench** — `scripts/sizing-bench.sh`, release image 0.1.3 (the engine of 0.3.0 differs only in
camera discovery), synthetic cameras 1920x1080 15 fps 4 Mbit/s + 640x360 25 fps 0.5 Mbit/s,
60 s warm-up, 120 s window. *Quiet* — a still scene, *busy* — motion over the whole frame
without pauses (the worst case: YOLO on every sampled frame).

| Machine | Cameras | Scene | CPU limit | Cores used | Engine processes, MB | Buffer, MB/min per camera |
|---|---|---|---|---|---|---|
| arm64, Neoverse-N1 | 1 | quiet | 2 | 0.057 | 159 | 33.8 |
| arm64, Neoverse-N1 | 4 | quiet | 2 | 0.187 | 382 | 33.6 |
| arm64, Neoverse-N1 | 8 | quiet | 2 | 0.361 | 696 | 33.7 |
| arm64, Neoverse-N1 | 1 | busy | 1 | 0.665 | 162 | 33.7 |
| arm64, Neoverse-N1 | 4 | busy | 2 | 1.846 ¹ | 395 | 33.6 |
| arm64, Neoverse-N1 | 4 | busy | 1 | 0.993 ¹ | 377 | 33.6 |
| amd64, AMD EPYC 9V45 | 1 | quiet | 2 | 0.034 | 180 | 33.6 |
| amd64, AMD EPYC 9V45 | 4 | quiet | 2 | 0.110 | 463 | 33.6 |
| amd64, AMD EPYC 9V45 | 8 | quiet | 2 | 0.237 | 814 | 33.6 |
| amd64, AMD EPYC 9V45 | 1 | busy | 2 | 0.175 | 186 | 34.0 |
| amd64, AMD EPYC 9V45 | 4 | busy | 2 | 1.059 | 464 | 33.9 |
| amd64, AMD EPYC 9V45 | 4 | busy | 1 | 1.000 ² | 461 | 34.0 |

¹ The detector could not keep up: 589 and 314 scored frames instead of ~888 — it queues, the
recording is unaffected. ² The whole core is busy, 857 of ~888 frames scored.

No run was killed by the memory limit, no recorder failed. A separate run with a 96 MB buffer
and one camera filled the tmpfs in three minutes: 53 recorder failures "No space left on
device" in four minutes — that is why the installer limits every stream.

## How it was measured

- One engine container with the same flags as `compose.yml` (host network, read-only root,
  `cap_drop: ALL`, tmpfs buffer) plus `--cpus` and `--memory`; the bot is not needed for the
  engine's numbers.
- Cameras: `bluenviron/mediamtx` on loopback serves N pairs of streams from two looped H.264
  files (`-c copy`, CBR) with a camera-like digest login, so the engine goes through its own
  RTSP credential proxy as with real cameras.
- CPU — the `cpu.stat` usage of the container's cgroup over the window, divided by its length;
  memory — `memory.stat` of the cgroup: `anon` for the processes, `shmem` for the tmpfs buffer;
  scored frames and lag — from the engine's own `gate_stats` and `detector_skipped_segments`.
- arm64 — on the Neoverse-N1 server next to the live installation (2 of its 4 cores at most);
  amd64 — on a GitHub-hosted `ubuntu-24.04` runner (2 vCPU of an AMD EPYC 9V45, 8 GB), the
  workflow `.github/workflows/sizing.yml`. Raw results are kept with the project.
- Not measured: Raspberry Pi 4/5, Intel N100. The calculator counts an N100 like the measured
  ARM (on the safe side) and a Pi 4 as twice slower per core — both marked as estimates.

## What will not run

- **1 GB RAM** (Raspberry Pi 3, Zero 2 W, small VPS): the system and Docker take about
  0.4 GB, the engine 0.16–0.3 GB with one camera, the buffer of one camera up to 0.24 GB,
  plus the bot — nothing is left for page cache and peaks; the installer warns below 2 GB.
- **32-bit ARM** (armv7: Raspberry Pi 2, many older NAS): there is no image — Dozorcam is built
  and tested for arm64 and amd64 only.
- **Many busy cameras on a slow CPU:** the recording keeps going, but person events arrive late
  while the detector queue is full. Use the camera's own person detection to gate the detector
  (`human_gate_mode`) or split the cameras between two installations.

## Measure your own machine

```bash
scripts/sizing-bench.sh -n 4 -s busy -c 2 -m 4g -i ghcr.io/egorchenkov/dozorcam:latest
```

`-n` cameras, `-s quiet|busy`, `-c` CPU limit, `-m` memory limit, `-w`/`-d` warm-up and window
in seconds, `-o` append the JSON line to a file. It needs Docker and about 5 minutes; ports
18890, 18891, 38654 and 38680 must be free. On GitHub: Actions → sizing → Run workflow.
