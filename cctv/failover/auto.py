"""Автопереключение по приоритету: сердцебиение, кворум, Telegram как арбитр.

Включается ключом ``auto = true`` раздела [failover] (по умолчанию выключено) и
требует не меньше трёх узлов. Агенты одной роли раз в heartbeat_sec спрашивают
друг у друга статус; каждый помнит, сколько секунд назад слышал каждого узла
(«возраст»), и сообщает эти возрасты в своём статусе. Часы узлов не сравниваются:
возраст каждый считает по своим монотонным часам.

Кворум — большинство узлов списка (из трёх — два), считая себя. Правила:

* держатель роли работает, только пока слышит кворум; не слышит его fence_sec
  (половина dead_after_sec) — замирает сам. Отрезанный от остальных держатель
  поэтому останавливает роль раньше, чем её заберёт другой узел;
* роль забирает следующий по приоритету узел, когда держатель молчит
  dead_after_sec для всех узлов, которые слышат друг друга, и их — кворум.
  Кандидат один: первый по приоритету из слышимых и пригодных узлов, кроме
  держателя; остальные узлы видят тот же список и ждут. Новое назначение —
  эпоха +1, без передачи state (держатель недоступен): новый узел стартует на
  копии state, которую держатель раздавал запасным (snapshot);
* без кворума роль не переходит никуда: два узла, не слышащие третьего и друг
  друга, не делят роль, а ждут;
* обратно сама роль не возвращается: вернувшийся узел узнаёт новую эпоху и
  встаёт наготове; вернуть роль главному узлу — кнопкой в /nodes.

Бот: Telegram сам не даёт двум процессам одного бота опрашивать getUpdates —
перебитый опрос получает конфликт (409). С auto процесс бота, получивший конфликт,
сразу выходит с кодом CONFLICT_EXIT — без повторного и без прощального getUpdates,
которые перебили бы опрос соседа. Вернётся он к опросу, только если кворум
подтверждает, что роль держит его узел (тогда перехватил устаревший процесс,
и его узел замрёт сам); иначе роль на этом узле замирает.
"""
from __future__ import annotations

import math

# Код выхода процесса бота «опрос Telegram перехватил другой процесс» (EX_TEMPFAIL).
CONFLICT_EXIT = 75


def ages(heard: dict[str, float], started: float, now: float, nodes, me: str) -> dict[str, float]:
    """Сколько секунд назад этот агент слышал каждый узел; не слышал ни разу — с запуска агента.

    Только что запущенный агент не считает соседа давно молчащим: иначе узлы,
    поднятые вместе, забирали бы роль раньше, чем успели услышать держателя.
    """
    result = {}
    for name in nodes:
        result[name] = 0.0 if name == me else max(0.0, now - heard.get(name, started))
    return result


def reachable(own: dict[str, float], window: float) -> list[str]:
    return [name for name, age in own.items() if age <= window]


def has_quorum(own: dict[str, float], window: float, quorum: int) -> bool:
    return len(reachable(own, window)) >= quorum


def takeover(cfg, holder: str, own: dict[str, float], views: dict[str, dict | None], fit: dict[str, bool],
             running_elsewhere: bool) -> bool:
    """Забрать ли роль этому узлу (cfg.me) у молчащего держателя holder.

    own — возрасты по своим часам; views — последние статусы агентов той же роли
    (их поле ``heard`` — возрасты по часам того узла); fit — пригодность узлов.
    """
    me = cfg.me
    if holder == me:
        return False
    window = cfg.fence_sec
    alive = reachable(own, window)
    if len(alive) < cfg.quorum or holder in alive:
        return False
    for name in alive:
        if name == me:
            age = own.get(holder, math.inf)
        else:
            heard = (views.get(name) or {}).get("heard") or {}
            try:
                age = float(heard.get(holder, 0.0))
            except (TypeError, ValueError):
                age = 0.0
        if age < cfg.dead_after_sec:
            return False   # кто-то из кворума слышал держателя недавно — он жив
    if running_elsewhere:
        return False
    candidates = [node.name for node in cfg.ordered
                  if node.name in alive and node.name != holder and fit.get(node.name, True)]
    return bool(candidates) and candidates[0] == me

