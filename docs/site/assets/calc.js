/* Калькулятор железа Dozorcam (#sizing). Без внешних ресурсов; тексты — CALC_TEXT страницы.
 *
 * Модель и источники чисел — docs/sizing.md (пересмотр 09.10.2026, T-20261009-23):
 * - CPU. Тишина: декодирование потока детектора и гейт движения, ядер на камеру. Камера,
 *   заведённая из бота, детектирует по основному потоку 1080p (detect_substream не включён) —
 *   это в 3 раза дороже детекторного 640x360, калькулятор считает этот случай. Движение:
 *   YOLO 2 кадра/с на занятую камеру; одновременно не больше 2 прогонов модели
 *   (CCTV_PERSON_INFER_SLOTS) — на детектор уходит не больше ~2 ядер, дальше очередь.
 *   Рекомендация — чтобы средняя нагрузка занимала не больше половины ядер: вторая половина —
 *   пики (все камеры с движением разом), бот и система.
 * - RAM. Процессы движка 170 + 150 МиБ на камеру — по боевой установке (4 камеры, 769 МиБ
 *   через 7,5 ч; бенч ниже: 8 камер — 1035 МиБ). Буфер камеры — пик перед чисткой: основной
 *   поток до лимита установщика 200 МиБ плюс 130 с потока (рекордер чистит буфер раз в цикл
 *   130 с) и детекторный ~0,5 Мбит/с за 12 минут. Всё вместе — не больше 85 % памяти,
 *   которую видит система (~95 % номинала): остальное — кэш страниц. Ту же модель считает
 *   установщик (scripts/install.sh), поэтому его буфер на выбранной RAM вмещает эти камеры.
 * - Буфер на SSD: RAM — только процессы; диск получает основной поток всех камер непрерывно
 *   (камера из бота детекторный поток не пишет): номинал плюс 3 % на MPEG-TS и файловую
 *   систему — 4 камеры по 4 Мбит/с записали 2,06 МБ/с (бенч 09.10.2026, io.stat).
 */
(function () {
  var T = window.CALC_TEXT;
  var CPU = {                                  // ядер на камеру: тишина (детектор на основном потоке) и YOLO при движении
    arm: { quiet: 0.15, yolo: 0.61 },          // Neoverse-N1 (замер 09.10.2026)
    x86: { quiet: 0.185, yolo: 0.2 },         // Intel Xeon Platinum 8370C, vCPU раннера GitHub (замер 09.10.2026)
    n100: { quiet: 0.2, yolo: 0.3 },          // Intel N100: оценка — ядро как vCPU раннера, YOLO без AVX-512
    a72: { quiet: 0.3, yolo: 1.2 }             // Raspberry Pi 4: оценка ×2 к Neoverse-N1
  };
  var SLOTS = 2, SUB_MBIT = 0.5, MAIN_CAP_MIB = 200, BUFFER_SEC = 600, PRUNE_SEC = 130;
  var ENGINE_BASE = 170, ENGINE_PER_CAM = 150, BOT = 100, SYSTEM = 400;   // МиБ
  var SEEN = 0.95, LIMIT = 0.85;                                          // доля номинала, доля видимой
  var TS_OVERHEAD = 1.03;                                                 // запись буфера на SSD к номиналу потока
  var SIZES = [2, 4, 6, 8, 16, 32];                                       // ГБ RAM

  function $(id) { return document.getElementById(id); }
  function fmt(x, d) { return (Math.round(x * Math.pow(10, d)) / Math.pow(10, d)).toString().replace(".", T.dec || "."); }
  function fill(s, v) { return s.replace(/\{(\w+)\}/g, function (_, k) { return v[k]; }); }
  function mib(mbit, sec) { return mbit * 1e6 / 8 * sec / 1048576; }
  function pick(used) {
    for (var i = 0; i < SIZES.length; i++) if (used <= SIZES[i] * 1024 * SEEN * LIMIT) return SIZES[i];
    return 0;
  }

  function update() {
    var n = +$("c-n").value, res = +$("c-res").value, act = +$("c-act").value, arch = $("c-arch").value;
    var ssd = $("c-buf").value === "ssd", cpu = CPU[arch];
    $("c-n-out").textContent = n;

    var main = Math.min(mib(res, BUFFER_SEC), MAIN_CAP_MIB) + mib(res, PRUNE_SEC);
    var camBuf = main + mib(SUB_MBIT, BUFFER_SEC + PRUNE_SEC);
    var minutes = Math.min(10, MAIN_CAP_MIB / mib(res, 60));
    var engine = ENGINE_BASE + ENGINE_PER_CAM * n, buffer = n * camBuf;
    var procs = engine + BOT + SYSTEM, used = procs + (ssd ? 0 : buffer);
    var gb = pick(used);
    $("r-ram").textContent = gb ? gb + T.gb : T.over;
    var d = ssd ? fill(T.ramSsd, { use: fmt(used / 1024, 1), eng: fmt(engine / 1024, 1), sys: fmt((BOT + SYSTEM) / 1024, 1) })
                : fill(T.ramD, { use: fmt(used / 1024, 1), buf: fmt(buffer / 1024, 1), min: fmt(minutes, 0),
                                 eng: fmt(engine / 1024, 1), sys: fmt((BOT + SYSTEM) / 1024, 1) });
    if (!ssd && pick(procs) && (!gb || pick(procs) < gb)) d += ". " + fill(T.ssdHint, { gb: pick(procs) });
    if (!ssd) d += ". " + fill(T.tmpfs, { tmpfs: Math.ceil(buffer / 64) * 64, cap: MAIN_CAP_MIB * 1048576 });
    $("r-ram-d").textContent = d;

    var rate = n * res / 8 * TS_OVERHEAD;                                 // МБ/с на диск при буфере на SSD
    $("r-disk").textContent = ssd ? fmt(Math.max(4, buffer * 1.2 / 1024 + 2), 0) + T.gb : "2" + T.gb;
    $("r-disk-d").textContent = ssd ? fill(T.diskSsd, { buf: fmt(buffer / 1024, 1), rate: fmt(rate, 1),
                                                         tb: fmt(rate * 86400 * 365 / 1e6, 0) }) : T.diskRam;

    var quiet = n * cpu.quiet, demand = n * act * cpu.yolo;
    var avg = quiet + Math.min(demand, SLOTS), peak = quiet + Math.min(n * cpu.yolo, SLOTS);
    var cores = 2;
    while (avg > cores / 2) cores += 2;
    $("r-cpu").textContent = T.cores(cores);
    $("r-cpu-d").textContent = fill(T.cpuD, { avg: fmt(avg, 1), peak: fmt(peak, 1) });

    $("r-pick").textContent = T.example + (T.pick[gb] || T.pick[32]);
    var warn = [];
    if (demand > SLOTS * 0.8) warn.push(T.lag);
    if (arch === "a72") warn.push(T.slow);
    if (arch === "n100") warn.push(T.est);
    if (!gb || gb > 16) warn.push(T.split);
    $("r-warn").hidden = !warn.length;
    $("r-warn").textContent = warn.join(" ");
  }

  if (!T || !$("calc")) return;
  ["c-n", "c-res", "c-act", "c-arch", "c-buf"].forEach(function (id) {
    $(id).addEventListener("input", update);
    $(id).addEventListener("change", update);
  });
  update();
})();
