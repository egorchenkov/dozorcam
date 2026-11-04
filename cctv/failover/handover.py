"""Передача каталога state роли со старого узла на новый при ручном переключении.

При автопереключении держатель недоступен, поэтому state заранее едет копией:
держатель с работающей ролью раз в check_sec собирает согласованный снимок
(базы SQLite — через backup API, а не копией файла под записью), запасной
раскладывает его у себя, меняя только изменившиеся файлы.

Переезжает только диск роли (реестр и калибровки движка, база бота с темами и
владельцем) — и только с замершего узла, когда процессы роли остановлены и
файлы согласованы. Память процессов не переезжает никогда: новый узел
стартует процессы заново. Свои файлы слоя и диагностика узла остаются на месте.
"""
from __future__ import annotations

import hashlib
import io
import pathlib
import shutil
import sqlite3
import tarfile
import tempfile

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


SQLITE_SUFFIXES = (".sqlite3", ".sqlite", ".db")
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


def _files(root: pathlib.Path):
    """Файлы state роли (кроме KEEP и симлинков) — относительные пути по порядку."""
    if not root.is_dir():
        return
    for entry in sorted(root.iterdir()):
        if entry.name in KEEP or entry.is_symlink():
            continue
        for path in [entry, *sorted(entry.rglob("*"))] if entry.is_dir() else [entry]:
            if path.is_file() and not path.is_symlink():
                yield path.relative_to(root)


def snapshot(state_dir: pathlib.Path, max_bytes: int = MAX_BYTES) -> tuple[bytes, str]:
    """Согласованный снимок state работающей роли: (архив, sha содержимого).

    sha считается по путям и содержимому, а не по байтам архива: неизменившийся
    state даёт тот же sha, и запасной не забирает его заново.
    """
    state_dir = pathlib.Path(state_dir)
    with tempfile.TemporaryDirectory(prefix="dozorcam-snapshot-") as tmp:
        copy = pathlib.Path(tmp)
        for rel in list(_files(state_dir)):
            source, target = state_dir / rel, copy / rel
            if rel.name.endswith(SQLITE_SIDECARS):
                continue   # журнал базы уже внутри копии, сделанной backup API
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                if rel.name.endswith(SQLITE_SUFFIXES):
                    # mode=rw: база должна уже быть (не создавать пустую); ro у базы в WAL
                    # без доступа к её -shm не открывается.
                    src = sqlite3.connect(f"{source.absolute().as_uri()}?mode=rw", uri=True, timeout=10)
                    dst = sqlite3.connect(target)
                    try:
                        src.backup(dst)
                    finally:
                        src.close()
                        dst.close()
                else:
                    shutil.copyfile(source, target)
            except FileNotFoundError:
                continue   # файл удалили, пока собирали — его и нет
            except (OSError, sqlite3.Error) as exc:
                raise HandoverError(f"{rel}: {exc}") from exc
        digest = hashlib.sha256()
        for rel in _files(copy):
            digest.update(rel.as_posix().encode() + b"\0" + hashlib.sha256((copy / rel).read_bytes()).digest())
        return pack(copy, max_bytes), digest.hexdigest()


def apply(data: bytes, state_dir: pathlib.Path) -> int:
    """Разложить снимок в state запасного узла: только изменившиеся файлы; сколько изменилось.

    Неизменившиеся файлы не трогаются (mtime тот же): супервизор запасного узла не
    принимает каждую копию за правку конфига.
    """
    state_dir = pathlib.Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    work = state_dir / ".handover"
    shutil.rmtree(work, ignore_errors=True)
    fresh = work / "new"
    fresh.mkdir(parents=True)
    try:
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                if any(_top(member.name) in KEEP for member in archive.getmembers()):
                    raise HandoverError("archive touches kept files")
                archive.extractall(fresh, filter="data")
        except (tarfile.TarError, OSError) as exc:
            raise HandoverError(str(exc)) from exc
        wanted = set(_files(fresh))
        changed = 0
        for rel in sorted(set(_files(state_dir)) - wanted):
            (state_dir / rel).unlink(missing_ok=True)
            changed += 1
        for rel in sorted(wanted):
            target = state_dir / rel
            if target.is_file() and target.read_bytes() == (fresh / rel).read_bytes():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            (fresh / rel).replace(target)
            changed += 1
        return changed
    finally:
        shutil.rmtree(work, ignore_errors=True)


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
