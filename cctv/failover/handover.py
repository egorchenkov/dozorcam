"""Передача каталога state роли со старого узла на новый при ручном переключении.

Переезжает только диск роли (реестр и калибровки движка, база бота с темами и
владельцем) — и только с замершего узла, когда процессы роли остановлены и
файлы согласованы. Память процессов не переезжает никогда: новый узел
стартует процессы заново. Свои файлы слоя и диагностика узла остаются на месте.
"""
from __future__ import annotations

import io
import pathlib
import shutil
import tarfile

# Не переезжают: назначение ролей этого узла, его копия раздаваемого конфига (у неё
# своя ревизия, share.py), рабочий каталог передачи, суточная диагностика детектора
# (журнал узла, а не состояние роли).
KEEP = frozenset({"failover.json", "failover-config", ".handover", "diag"})
MAX_BYTES = 64 * 1024 * 1024


class HandoverError(RuntimeError):
    pass


def pack(state_dir: pathlib.Path, max_bytes: int = MAX_BYTES) -> bytes:
    buffer = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        if state_dir.is_dir():
            for entry in sorted(state_dir.iterdir()):
                if entry.name in KEEP or entry.is_symlink():
                    continue
                for path in [entry, *sorted(entry.rglob("*"))] if entry.is_dir() else [entry]:
                    if path.is_symlink():
                        continue
                    if path.is_file():
                        total += path.stat().st_size
                        if total > max_bytes:
                            raise HandoverError(f"state > {max_bytes} bytes")
                    archive.add(path, arcname=path.relative_to(state_dir).as_posix(), recursive=False)
    return buffer.getvalue()


def unpack(data: bytes, state_dir: pathlib.Path) -> int:
    """Заменить каталог state содержимым архива (кроме KEEP); число файлов."""
    state_dir.mkdir(parents=True, exist_ok=True)
    work = state_dir / ".handover"
    shutil.rmtree(work, ignore_errors=True)
    fresh = work / "new"
    fresh.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            if any(_top(member.name) in KEEP for member in archive.getmembers()):
                raise HandoverError("archive touches kept files")
            archive.extractall(fresh, filter="data")
    except (tarfile.TarError, OSError) as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise HandoverError(str(exc)) from exc
    for entry in state_dir.iterdir():
        if entry.name in KEEP:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    count = sum(1 for path in fresh.rglob("*") if path.is_file())
    for entry in fresh.iterdir():
        entry.rename(state_dir / entry.name)
    shutil.rmtree(work, ignore_errors=True)
    return count


def _top(name: str) -> str:
    parts = pathlib.PurePosixPath(name).parts
    return parts[0] if parts else ""
