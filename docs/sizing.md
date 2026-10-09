# Will it run on my box? — hardware sizing

Russian: [sizing.ru.md](sizing.ru.md). The calculator: [website](https://egorchenkov.github.io/dozorcam/#sizing).
All documents: [README.md](README.md).

- [Short answer](#short-answer)
- [Where the resources go](#where-the-resources-go)
- [Buffer in RAM or on an SSD](#buffer-in-ram-or-on-an-ssd)
- [Measured numbers](#measured-numbers)
- [How it was measured](#how-it-was-measured)
- [What will not run](#what-will-not-run)
- [Measure your own machine](#measure-your-own-machine)

## Short answer

2 MP cameras (~4 Mbit/s) added from the chat, a usual house (people in the frame ~15 % of the
time). The calculator gives the same numbers and counts other cameras and scenes.

| Cameras | RAM, buffer in RAM | RAM, buffer on an SSD | CPU | Example |
|---|---|---|---|---|
| 1–2 | 2 GB | 2 GB | 2 cores | Raspberry Pi 4/5 (2 GB+), any mini PC |
| 3–4 | 4 GB | 2 GB | 2 cores | Raspberry Pi 5 (4 GB), Intel N100 mini PC |
| 5–8 | 4 GB for 5, 6 GB for 6–8 | 2 GB up to 6, 4 GB for 7–8 | 4 cores | Raspberry Pi 5 / Orange Pi 5 (8 GB), Intel N100 mini PC (8 GB) |
| 9–16 | 6 GB for 9, 8 GB up to 13, 16 GB for 14–16 | 4 GB | 6–8 cores | a home server, Intel N305 / Core i3 mini PC |

The buffer: if RAM is enough, it lives in RAM (the default); if not, on an SSD — the installer
parameter `DOZORCAM_BUFFER=disk` ([below](#buffer-in-ram-or-on-an-ssd)). Without the buffer RAM
holds only the processes: ~1.4 GB used for 5 cameras, ~1.8 GB for 8 (engine 170 MB + 150 MB per
camera and bot 0.1 GB — from the live installation, system and Docker 0.4 GB); the SSD then gets
0.5 MB/s per camera nonstop, ~16 TB a year.

Where the reserve is: RAM counts the peak of every camera's buffer and keeps 15 % of memory
free for the page cache; the engine processes are taken from the live installation, which is
above the bench (8 cameras: 1.37 GB in the model, 1.03 GB measured). CPU keeps the average
load at no more than half of the cores: the other half is for peaks — when all cameras see
motion at once the detector takes up to 2 more cores — and for the bot and the system. With
2 cores and 4 busy cameras at once nothing breaks: the recording goes on, person events come
a few seconds later.

Disk: about 2 GB free for the image (~0.8 GB unpacked) and one update next to it; state is
kilobytes, logs are capped by Docker (10 MB × 3 per container). With the buffer in RAM video
is never written to disk.

## Where the resources go

- **Video buffer.** Each camera is recorded into a rolling buffer: 10 minutes of the main
  stream (and of the detector stream, if it has one), but not more than `CCTV_BUFFER_MAX_BYTES`
  per stream. The installer sets 200 MB per stream (7 minutes at 4 Mbit/s, 3.5 at 8 Mbit/s).
  The recorder cleans the buffer once per 130-second cycle, so just before a cleaning a stream
  holds its limit plus up to 130 s more: a 2 MP camera peaks at ~306 MB with a detector stream
  and ~262 MB without (measured: 233–236 MB per camera on the main stream alone, 265 MB with
  the detector stream, a few seconds before the cleaning). The engine limits every stream on its
  own and has **no common limit**: cameras that do not fit into `CCTV_BUFFER_TMPFS` fill it up
  and recording stops ("No space left on device"). The installer sizes the tmpfs by the model
  below — 370 MB per camera that fits into RAM, enough for cameras up to 8 Mbit/s; adding more
  cameras than it said — raise `CCTV_BUFFER_TMPFS` (and RAM) or move the buffer to an SSD.
- **Engine processes (RAM).** The calculator and the installer count 170 MB plus 150 MB per
  camera — the live installation (4 cameras, 0.77 GB after 8 hours, unchanged over 40 minutes
  of watching).
  The bench is lower: 0.51 GB with 4 and 1.03 GB with 8 cameras on arm64, 0.58 and 1.00 GB on
  amd64; a camera whose detector reads the 640x360 detector stream takes 20–45 MB less.
- **Bot:** about 0.1 GB. **System and Docker:** keep about 0.4 GB.
- **Everything together** is kept within 85 % of the memory the system sees (about 95 % of the
  nominal size); the rest is the page cache and short peaks (a clip is cut in the transit
  tmpfs, up to 48 MB).
- **CPU, quiet scene.** The detector decodes its stream all the time and passes it through the
  motion gate. A camera added from the chat is detected on its **main stream** (1080p): 0.15 core
  per camera on an ARM Neoverse-N1 core, 0.185 on a vCPU of an Intel Xeon 8370C. A camera set up
  with the detector on its 640x360 stream (`detect_substream` in `cameras.json`) costs three
  times less: 0.045 and 0.03.
- **CPU, people or moving trees.** The person detector (YOLOX-Tiny, 2 frames per second per
  camera) runs only on frames that passed the motion gate: 0.61 core per busy camera on a
  Neoverse-N1 core, 0.2 on the Xeon vCPU. At most two frames are scored at once
  (`CCTV_PERSON_INFER_SLOTS=2`), so the detector never takes more than ~2 cores: with more busy
  cameras it queues and events arrive later instead of starving the recording and the bot.
- **Cross-check.** For the live installation (2 cameras on the main stream, 2 on the detector
  stream) the model gives 0.76 core at the usual activity; the installation averaged 0.77 core
  over 8 hours.

## Buffer in RAM or on an SSD

The buffer lives in RAM by default, and that is preferred: no disk wear and no dependence on
the disk. When RAM is short, the buffer can live on an SSD — nothing is stored there for long,
it is the same ring of the last minutes — and RAM then holds only the processes:

```bash
curl -fsSL https://egorchenkov.github.io/dozorcam/install.sh | DOZORCAM_BUFFER=disk sh
# a separate SSD instead of the Docker volume:
curl -fsSL https://egorchenkov.github.io/dozorcam/install.sh | DOZORCAM_BUFFER=disk DOZORCAM_BUFFER_PATH=/mnt/ssd/dozorcam sh
```

An existing installation: `CCTV_BUFFER_DIR=/var/lib/cctv/buffer-disk` in `.env` (and optionally
`CCTV_BUFFER_DISK=/mnt/ssd/dozorcam`, a directory owned by uid 10001), then `dozorcam restart`.
The buffer volume `engine-buffer` is not part of `dozorcam backup` and is removed by
`dozorcam uninstall`.

- **Writes.** The disk gets every camera's main stream nonstop, a five-second file at a time,
  and the oldest files are deleted: the stream's bitrate plus ~3 % (measured: 4 cameras at
  4 Mbit/s wrote 2.06 MB/s, `io.stat` of the container). A camera added from the chat does not
  record a detector stream; one set up with `detect_substream` adds ~0.06 MB/s, ~2 TB a year.

  | 2 MP cameras (~4 Mbit/s) | Writes | A year | A 300 TBW SSD lasts |
  |---|---|---|---|
  | 1 | 0.5 MB/s | ~16 TB | ~18 years |
  | 5 | 2.6 MB/s | ~81 TB | ~3.7 years |
  | 8 | 4.1 MB/s | ~130 TB | ~2.3 years |

  300 TBW is the rating of a typical 500 GB drive (1 TB drives — ~600 TBW, twice as long); look
  up the TBW of yours. The SSD's own write amplification for whole files written once and
  deleted whole should be small, but it was not measured.
  **Not an HDD:** writes do not wear it out, but it never rests — a new file every few seconds per
  camera and deletions around the clock — and a cheap SMR drive stalls for seconds under such
  rewriting, while the recorder cannot wait. **Not an SD card or USB stick:** without an SSD's
  wear levelling and reserve they wear out in weeks to months at these volumes, and on a
  Raspberry Pi the system usually lives on the same card. The installer warns about both.
- **Checked on disk:** detection costs the same CPU (0.153 core per camera against 0.150 in
  RAM), a segment is read only after the next one appears — the same rule as in RAM, so a disk
  adds no torn frames; clips of 30 s (450 frames 1080p) and snapshots came from the disk
  buffer intact. A clean machine (LXD, 2 GB) installed with `DOZORCAM_BUFFER=disk`: the buffer
  in the Docker volume, the tmpfs empty, motion events with clips in the chat, all buffer
  segments decode without errors; switching back to RAM by running the installer again works.
  The run found that clips — in RAM as well — could end with a torn last frame (a clip took the
  segment still being written); fixed in 0.3.1, clips are now cut from closed segments only. Recently written segments stay in the page cache — the container's memory
  counter shows them, but the kernel gives this memory back on demand.

## Measured numbers

**Live installation** (4 Hikvision cameras — 2 with the detector on the detector stream, 2 on
the main stream; an ARM server with 4 Neoverse-N1 cores; release 0.3.0; 8 hours of a usual
day): the engine averaged 0.77 core (`cpu.stat` of its cgroup over the uptime), up to 1.6 cores
while YOLO runs; processes 0.77 GB (detector 594 MB, RTSP proxy 66 MB, bridge 41 MB, recorders
~8 MB each); the video buffer 0.9–1.0 GB with the 512 MB per-stream limit of that installation
and camera bitrates from 0.6 to 6.5 Mbit/s; the bot 0.1 GB; the image 800 MB.

**Bench, release 0.3.0** — `scripts/sizing-bench.sh`, the installer's limit of 200 MB per
stream, synthetic cameras 1920x1080 15 fps 4 Mbit/s (+ 640x360 25 fps 0.5 Mbit/s for the
detector stream), 60 s warm-up. *Quiet* — a still scene, *busy* — motion over the whole frame
without pauses (the worst case: YOLO on every sampled frame).

| Machine | Cameras | Detector on | Scene | Buffer | CPU limit | Window | Cores used | Engine processes, MB | Buffer at the end, MB |
|---|---|---|---|---|---|---|---|---|---|
| arm64, Neoverse-N1 | 8 | main | quiet | RAM | 2 | 8 min | 1.199 | 1033 | 1649 (peak 1868) |
| arm64, Neoverse-N1 | 4 | main | quiet | **SSD** | 2 | 7 min | 0.612 | 508 | 935, 1.96 MB/s written |
| arm64, Neoverse-N1 | 1 | main | busy | RAM | 2 | 2 min | 0.626 | 190 | 86 |
| arm64, Neoverse-N1 | 8 | detector stream | quiet | RAM | 1 | 7 min | — ¹ | 654 | 2123 |
| amd64, Xeon 8370C | 1 | main | quiet | RAM | 2 | 2 min | 0.160 | 217 | 86 |
| amd64, Xeon 8370C | 4 | main | quiet | RAM | 2 | 2 min | 0.749 | 582 | 344 |
| amd64, Xeon 8370C | 5 | main | quiet | RAM | 2 | 10 min | 0.934 | 745 | 1006 (peak 1178) |
| amd64, Xeon 8370C | 8 | main | quiet | RAM | 2 | 2 min | 1.473 | 997 | 688 |
| amd64, Xeon 8370C | 8 | detector stream | quiet | RAM | 2 | 2 min | 0.348 | 821 | 787 |
| amd64, Xeon 8370C | 1 | main | busy | RAM | 2 | 2 min | 0.355 | 222 | 87 |
| amd64, Xeon 8370C | 4 | main | busy | RAM | 2 | 2 min | 1.889 ² | 660 | 348 |

¹ A memory run (stopped at minute 7): the processes stayed at 630–654 MB from minute 1 to 7.
² Both vCPUs are busy, 644 of ~960 frames scored — the detector queues; no recorder failed.

**Bench, release 0.1.3** (the detector on the 640x360 stream, 400 MB per stream, 120 s window):

| Machine | Cameras | Scene | CPU limit | Cores used | Engine processes, MB |
|---|---|---|---|---|---|
| arm64, Neoverse-N1 | 1 / 4 / 8 | quiet | 2 | 0.057 / 0.187 / 0.361 | 159 / 382 / 696 |
| arm64, Neoverse-N1 | 1 | busy | 1 | 0.665 | 162 |
| arm64, Neoverse-N1 | 4 | busy | 2 / 1 | 1.846 / 0.993 ³ | 395 / 377 |
| amd64, AMD EPYC 9V45 | 1 / 4 / 8 | quiet | 2 | 0.034 / 0.110 / 0.237 | 180 / 463 / 814 |
| amd64, AMD EPYC 9V45 | 1 / 4 | busy | 2 | 0.175 / 1.059 | 186 / 464 |

³ The detector could not keep up: 589 and 314 scored frames instead of ~888; the recording is
unaffected.

No run was killed by the memory limit, no recorder failed. A separate run with a 96 MB buffer
and one camera filled the tmpfs in three minutes: 53 recorder failures "No space left on
device" in four minutes — that is why the installer limits every stream.

## How it was measured

- One engine container with the same flags as `compose.yml` (host network, read-only root,
  `cap_drop: ALL`, tmpfs buffer or a disk directory) plus `--cpus` and `--memory`; the bot is
  not needed for the engine's numbers.
- Cameras: `bluenviron/mediamtx` on loopback serves N pairs of streams from two looped H.264
  files (`-c copy`, CBR) with a camera-like digest login, so the engine goes through its own
  RTSP credential proxy as with real cameras.
- CPU — the `cpu.stat` usage of the container's cgroup over the window, divided by its length;
  memory — `memory.stat` of the cgroup: `anon` for the processes, `shmem` for the tmpfs buffer,
  minute by minute in the long runs; disk writes — `io.stat` of the cgroup; scored frames and
  lag — from the engine's own `gate_stats` and `detector_skipped_segments`; clips and snapshots
  from the disk buffer — through the bridge API, checked with `ffprobe`.
- arm64 — on the Neoverse-N1 server next to the live installation (at most 2 of its 4 cores);
  amd64 — on a GitHub-hosted `ubuntu-24.04` runner (2 vCPU; the 0.3.0 runs got an Intel Xeon
  Platinum 8370C, the 0.1.3 runs an AMD EPYC 9V45), the workflow `.github/workflows/sizing.yml`.
  Raw results are kept with the project.
- Not measured: Raspberry Pi 4/5, Intel N100. The calculator counts a Pi 5 like the measured
  ARM (its cores are ~20 % slower — covered by the half-load rule), an N100 core like a vCPU of
  the measured Xeon with a slower person model (no AVX-512), a Pi 4 as twice slower than the
  measured ARM — the last two marked as estimates.

## What will not run

- **1 GB RAM** (Raspberry Pi 3, Zero 2 W, small VPS): the system and Docker take about
  0.4 GB, the engine 0.2–0.3 GB with one camera, the bot 0.1 GB, the buffer of one camera up to
  0.3 GB — nothing is left for the page cache and peaks; the installer warns below 2 GB.
- **32-bit ARM** (armv7: Raspberry Pi 2, many older NAS): there is no image — Dozorcam is built
  and tested for arm64 and amd64 only.
- **Many busy cameras on a slow CPU:** the recording keeps going, but person events arrive late
  while the detector queue is full. Use the camera's own person detection to gate the detector
  (`human_gate_mode`), put the detector on the camera's detector stream (`detect_substream`) or
  split the cameras between two installations.

## Measure your own machine

```bash
scripts/sizing-bench.sh -n 4 -s busy -c 2 -m 4g -i ghcr.io/egorchenkov/dozorcam:latest
CCTV_BENCH_SUBSTREAM=0 CCTV_BENCH_STREAM_CAP_MB=200 scripts/sizing-bench.sh -n 4 -s quiet   # as installed from the chat
CCTV_BENCH_BUFFER_DISK=/mnt/ssd/dzbench scripts/sizing-bench.sh -n 4                         # the buffer on that disk
```

`-n` cameras, `-s quiet|busy`, `-c` CPU limit, `-m` memory limit, `-w`/`-d` warm-up and window
in seconds, `-o` append the JSON line to a file; variables — see the head of the script
(`CCTV_BENCH_SERIES` writes memory minute by minute). It needs Docker and about 5 minutes;
ports 18890, 18891, 38654 and 38680 must be free. On GitHub: Actions → sizing → Run workflow.
