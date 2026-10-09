/* Калькулятор железа Dozorcam (#sizing). Без внешних ресурсов; тексты — CALC_TEXT страницы.
 *
 * Цифры — замеры scripts/sizing-bench.sh (docs/sizing.md) и боевой установки на 4 камеры:
 *   ядра на камеру в тишине (гейт движения закрыт) и цена YOLO на камеру при движении без
 *   пауз (2 кадра/с выборки); одновременно идут не больше 2 прогонов модели
 *   (CCTV_PERSON_INFER_SLOTS), поэтому на детектор уходит не больше ~2 ядер — дальше очередь.
 *   Память движка 150 + 140 МиБ на камеру — по проду после часов работы (бенч через 3 минуты
 *   даёт нижнюю границу 85 + 77). Буфер — 10 минут потока, но не больше лимита установщика
 *   200 МиБ на основной поток; детекторный 640x360 ~0.5 Мбит/с. Установщик отдаёт буферу
 *   25 % RAM (512 МБ–4 ГБ) — рекомендация RAM этому правилу не противоречит.
 */
(function () {
  var T = window.CALC_TEXT;
  var CPU = {                                  // ядер: тишина на камеру, YOLO на камеру при движении
    arm: { quiet: 0.045, yolo: 0.61 },         // Neoverse-N1 (замер 09.10.2026)
    x86: { quiet: 0.03, yolo: 0.14 },          // AMD EPYC 9V45, 2 vCPU раннера GitHub (замер 09.10.2026)
    n100: { quiet: 0.045, yolo: 0.61 },        // Intel N100: оценка с запасом — как Neoverse-N1
    a72: { quiet: 0.09, yolo: 1.2 }            // Raspberry Pi 4: оценка ×2 к Neoverse-N1
  };
  var SLOTS = 2, SUB_MBIT = 0.5, MAIN_CAP_MIB = 200, BUFFER_SEC = 600;
  var ENGINE_BASE = 150, ENGINE_PER_CAM = 140, BOT = 70, SYSTEM = 400;   // МиБ
  var SIZES = [2, 4, 8, 16, 32];                                          // ГБ RAM

  function $(id) { return document.getElementById(id); }
  function fmt(x, d) { return (Math.round(x * Math.pow(10, d)) / Math.pow(10, d)).toString().replace(".", T.dec || "."); }
  function fill(s, v) { return s.replace(/\{(\w+)\}/g, function (_, k) { return v[k]; }); }
  function mib(mbit, sec) { return mbit * 1e6 / 8 * sec / 1048576; }
  function installerBuffer(gb) { return Math.min(4096, Math.max(512, gb * 1024 / 4)); }

  function update() {
    var n = +$("c-n").value, res = +$("c-res").value, act = +$("c-act").value, arch = $("c-arch").value;
    var cpu = CPU[arch];
    $("c-n-out").textContent = n;

    var main = Math.min(mib(res, BUFFER_SEC), MAIN_CAP_MIB), sub = mib(SUB_MBIT, BUFFER_SEC);
    var minutes = Math.min(10, MAIN_CAP_MIB / mib(res, 60));
    var buffer = n * (main + sub), engine = ENGINE_BASE + ENGINE_PER_CAM * n;
    var used = buffer + engine + BOT + SYSTEM;
    var gb = SIZES[SIZES.length - 1];
    for (var i = 0; i < SIZES.length; i++) {
      if (used <= SIZES[i] * 1024 * 0.8 && buffer <= installerBuffer(SIZES[i])) { gb = SIZES[i]; break; }
    }
    $("r-ram").textContent = gb + T.gb;
    $("r-ram-d").textContent = fill(T.ramD, { use: fmt(used / 1024, 1), buf: fmt(buffer / 1024, 1),
      min: fmt(minutes, 0), eng: fmt(engine / 1024, 1), sys: fmt((BOT + SYSTEM) / 1024, 1) }) +
      ". " + fill(T.tmpfs, { tmpfs: Math.ceil(buffer * 1.1 / 64) * 64, cap: MAIN_CAP_MIB * 1048576 });

    var quiet = n * cpu.quiet, demand = n * act * cpu.yolo;
    var avg = quiet + Math.min(demand, SLOTS), peak = quiet + Math.min(n * cpu.yolo, SLOTS);
    var cores = 2;
    while (cores < peak + 0.5) cores += 2;
    $("r-cpu").textContent = T.cores(cores);
    $("r-cpu-d").textContent = fill(T.cpuD, { avg: fmt(avg, 1), peak: fmt(peak, 1) });

    $("r-pick").textContent = T.example + (T.pick[gb] || T.pick[32]);
    var warn = [];
    if (demand > SLOTS * 0.8) warn.push(T.lag);
    if (arch === "a72") warn.push(T.slow);
    if (arch === "n100") warn.push(T.est);
    if (gb > 16) warn.push(T.split);
    $("r-warn").hidden = !warn.length;
    $("r-warn").textContent = warn.join(" ");
  }

  if (!T || !$("calc")) return;
  ["c-n", "c-res", "c-act", "c-arch"].forEach(function (id) {
    $(id).addEventListener("input", update);
    $(id).addEventListener("change", update);
  });
  update();
})();
