"""
Robustes Lesen und Schreiben von JSON-Konfigurationsdateien.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger(__name__)


def read_json(path: Path, default: Any = None) -> Any:
    """
    Liest eine JSON-Datei.

    Args:
        path: Pfad zur Datei
        default: Rückgabewert wenn die Datei fehlt oder leer ist

    Returns:
        Geparster Inhalt oder default
    """
    if not path.is_file():
        return default

    try:
        content = path.read_text(encoding='utf-8').strip()
    except OSError as e:
        _logger.error(f"Konnte {path} nicht lesen: {e}")
        return default

    return json.loads(content) if content else default


def write_json(path: Path, data: Any) -> bool:
    """
    Schreibt JSON möglichst atomar.

    Fällt auf direktes Überschreiben zurück, wenn das atomare Umbenennen nicht
    erlaubt ist - etwa bei einer als einzelne Datei eingebundenen Docker-Volume.

    Args:
        path: Zieldatei
        data: Zu serialisierende Daten

    Returns:
        True bei Erfolg
    """
    payload = json.dumps(data, indent=2, ensure_ascii=False)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _logger.error(f"Konnte Verzeichnis für {path} nicht anlegen: {e}")
        return False

    return _write_atomic(path, payload) or _write_in_place(path, payload)


def _write_atomic(path: Path, payload: str) -> bool:
    """Schreibt über eine Temp-Datei und benennt sie um."""
    temp_path: Optional[Path] = None

    try:
        fd, temp_name = tempfile.mkstemp(
            suffix='.tmp', prefix=f'{path.stem}_', dir=str(path.parent)
        )
        temp_path = Path(temp_name)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_path, path)
        return True
    except OSError as e:
        _logger.debug(f"Atomares Schreiben von {path} nicht möglich: {e}")
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        return False


def _write_in_place(path: Path, payload: str) -> bool:
    """Überschreibt die Datei direkt (Fallback für Bind-Mounts)."""
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        return True
    except OSError as e:
        _logger.error(f"Konnte {path} nicht schreiben: {e}")
        return False
