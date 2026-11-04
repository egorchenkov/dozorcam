"""Панель узлов для бота: готовый текст и кнопки, бот их только показывает.

Бот не знает ни узлов, ни ролей: по /nodes он берёт у агента своей роли
``{"text", "buttons", "toast"}`` и пересылает нажатия обратно строкой кнопки.
Строки — из каталогов i18n на языке, который назвал бот.
"""
from __future__ import annotations

from .. import i18n
from .config import ROLES

REFRESH = "rf"


def _t(lang: str, key: str, **params) -> str:
    return i18n.t(key, lang, **params)


def _button(text: str, data: str) -> dict:
    return {"text": text[:64], "data": data}


def _fitness(lang: str, role: str, health: dict) -> str:
    if health.get("fit") is None or not health.get("checked_at"):
        return _t(lang, "nodes.not_checked")
    if role == "engine":
        if not health.get("total"):
            return _t(lang, "nodes.fit.no_cameras")
        return _t(lang, "nodes.fit.cameras", ok=health.get("ok", 0), total=health.get("total", 0))
    if health.get("fit"):
        return _t(lang, "nodes.fit.telegram_ok")
    reason = str(health.get("reason") or "")
    key = f"nodes.fit.telegram_{reason}"
    return _t(lang, key) if i18n.has(key) else _t(lang, "nodes.fit.telegram_unreachable")


def _role_line(lang: str, agent, node: str, role: str, status: dict | None) -> str:
    label = _t(lang, f"nodes.role.{role}")
    if status is None:
        return _t(lang, "nodes.line_down", role=label)
    holder = agent.holder(role)
    if node == holder and status.get("fenced"):
        state = _t(lang, "nodes.state.fenced")
    elif node == holder:
        state = _t(lang, "nodes.state.active" if status.get("running") else "nodes.state.starting")
    else:
        state = _t(lang, "nodes.state.armed")
    health = status.get("health") or {}
    parts = [state, _fitness(lang, role, health)]
    checked = health.get("checked_at")
    if checked and status.get("now"):
        parts.append(_t(lang, "nodes.checked", age=max(0, int(status["now"] - checked))))
    return _t(lang, "nodes.line", role=label, state=" · ".join(parts))


def _outcome(lang: str, last: dict) -> str:
    role = _t(lang, f"nodes.role.{last.get('role')}")
    if last.get("ok") and last.get("auto"):
        return _t(lang, "nodes.last_auto", role=role, source=last.get("from", "?"), target=last.get("to", "?"))
    if last.get("ok"):
        return _t(lang, "nodes.last_ok", role=role, source=last.get("from", "?"), target=last.get("to", "?"))
    code = str(last.get("error") or "")
    key = f"nodes.error.{code}"
    reason = (_t(lang, key, source=last.get("from", "?"), target=last.get("to", "?")) if i18n.has(key)
              else _t(lang, "nodes.error.other", code=code))
    return _t(lang, "nodes.last_failed", role=role, target=last.get("to", "?"), reason=reason)


def _last(agent, last: dict) -> dict:
    """Итог переключения, которое вёл этот агент; роль с тех пор перевёл другой узел —
    итог по назначению (иначе панель этого узла показывала бы устаревшее)."""
    with agent.lock:
        current = dict(agent.assign[last.get("role")])
    if last.get("epoch") == current["epoch"] or not current.get("carry"):
        return last
    return {"role": last.get("role"), "ok": True, "from": current["prev"], "to": current["holder"]}


def view(agent, lang: str | None, *, statuses=None) -> dict:
    lang = i18n.resolve(lang)
    found = agent.statuses() if statuses is None else statuses
    cfg = agent.cfg
    # Панель отдаёт агент бота своего узла — этот узел и отвечает в Telegram.
    lines = [_t(lang, "nodes.title"), _t(lang, "nodes.answering", name=cfg.me),
             _t(lang, "nodes.check_every", sec=cfg.check_sec)]
    if cfg.auto:
        lines.append(_t(lang, "nodes.auto_on", sec=cfg.dead_after_sec, quorum=cfg.quorum, total=len(cfg.nodes)))
    else:
        lines.append(_t(lang, "nodes.auto_off"))
    lines.append("")
    for node in cfg.ordered:
        head = _t(lang, "nodes.node", name=node.name, priority=node.priority)
        if node.name == cfg.me:
            head += _t(lang, "nodes.this")
        lines.append(head)
        lines += [_role_line(lang, agent, node.name, role, found.get((node.name, role))) for role in ROLES]
    if agent.switching:
        switching = agent.switching
        lines += ["", _t(lang, "nodes.switching", role=_t(lang, f"nodes.role.{switching['role']}"),
                         source=switching["from"], target=switching["to"])]
    else:
        outcomes = [_outcome(lang, _last(agent, agent.last_switch))] if agent.last_switch else []
        # Роль забрал другой узел сам (держатель замолчал) — итог по назначению.
        for role in ROLES:
            with agent.lock:
                current = dict(agent.assign.get(role) or {})
            last = agent.last_switch or {}
            if current.get("auto") and not (last.get("role") == role and last.get("epoch") == current["epoch"]):
                outcomes.append(_outcome(lang, {"role": role, "ok": True, "auto": True,
                                                "from": current["prev"], "to": current["holder"]}))
        if outcomes:
            lines += ["", *outcomes]
    rows = []
    for role in ROLES:
        holder = agent.holder(role)
        row = [_button(_t(lang, "nodes.button.move", role=_t(lang, f"nodes.role.{role}"), node=node.name),
                       f"go:{role}:{node.name}")
               for node in cfg.ordered
               if node.name != holder and found.get((node.name, role)) is not None
               and (found[(node.name, role)].get("health") or {}).get("fit") is not False]
        if row:
            rows.append(row)
    rows.append([_button(_t(lang, "nodes.button.refresh"), REFRESH)])
    return {"text": "\n".join(lines), "buttons": rows, "toast": ""}


def press(agent, data: str, lang: str | None) -> dict:
    lang = i18n.resolve(lang)
    parts = data.split(":")
    if data == REFRESH:
        return {**view(agent, lang), "toast": _t(lang, "nodes.refreshed")}
    if len(parts) == 3 and parts[0] == "go" and parts[1] in ROLES and agent.cfg.node(parts[2]):
        role, target = parts[1], parts[2]
        with agent.lock:
            epoch = agent.assign[role]["epoch"]
        source = agent.holder(role)
        if source == target:
            return {**view(agent, lang), "toast": _t(lang, "nodes.stale")}
        text = _t(lang, f"nodes.confirm.{role}", source=source, target=target)
        return {"text": text, "toast": "", "buttons": [[
            _button(_t(lang, "nodes.button.yes"), f"ok:{role}:{target}:{epoch}"),
            _button(_t(lang, "nodes.button.cancel"), REFRESH)]]}
    if len(parts) == 4 and parts[0] == "ok" and parts[1] in ROLES and agent.cfg.node(parts[2]):
        role, target = parts[1], parts[2]
        with agent.lock:
            epoch = agent.assign[role]["epoch"]
        if parts[3] != str(epoch):
            return {**view(agent, lang), "toast": _t(lang, "nodes.stale")}
        code = agent.request_switch(role, target)
        if code == "busy":
            return {**view(agent, lang), "toast": _t(lang, "nodes.busy")}
        if code != "started":
            return {**view(agent, lang), "toast": _t(lang, "nodes.unknown")}
        own_bot = role == "bot" and agent.holder("bot") == agent.cfg.me
        # Переезд бота: панель называет реальный срок (остановка здесь, передача state, старт
        # там) и объём передаваемых данных, когда он известен, а не «несколько секунд».
        size = agent.state_size() if own_bot else 0
        text = _t(lang, "nodes.started.bot" if own_bot else "nodes.started.engine",
                  role=_t(lang, f"nodes.role.{role}"), target=target,
                  size=_t(lang, "nodes.started.size", mb=f"{size / 1048576:.1f}") if size else "")
        buttons = [] if own_bot else [[_button(_t(lang, "nodes.button.refresh"), REFRESH)]]
        return {"text": text, "buttons": buttons, "toast": _t(lang, "nodes.started_toast")}
    return {**view(agent, lang), "toast": _t(lang, "nodes.unknown")}
