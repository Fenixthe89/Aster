"""
Percorsi runtime di Aster: radice applicazione, risorse e dati.

Distingue tre radici:
- app_root: directory d'installazione (radice del repository da
  sorgente, directory dell'eseguibile da frozen);
- resource_root: asset in sola lettura distribuiti con Aster
  (= app_root da sorgente, sys._MEIPASS da frozen se disponibile);
- data_root: directory scrivibile dei dati persistenti dell'utente.

Il modulo è diviso in due parti nettamente separate:
- risolvi_percorsi: logica pura di scelta dei path, su PureWindowsPath/
  PurePosixPath in base al sistema indicato, così che i test possano
  simulare Windows/Linux/macOS da qualsiasi host;
- percorsi_runtime: unico punto che legge sys/os e converte il
  risultato in veri Path del sistema operativo corrente.

Nessuna delle due crea, scrive o migra qualcosa: l'unica osservazione
del filesystem ammessa è l'esistenza del marker portable.txt, tramite
una funzione iniettata. I path qui calcolati sono fidati (derivati da
sys e dall'ambiente), non provengono mai dal modello e non devono mai
finire nei risultati dei tool, nella cronologia o nel prompt.
"""

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

MODALITA_SORGENTE = "source"
MODALITA_PORTABLE = "portable"
MODALITA_NORMALE = "normal"

NOME_MARKER_PORTABLE = "portable.txt"


class ErrorePercorsiRuntime(RuntimeError):
    """Impossibile determinare in modo sicuro una radice runtime."""


@dataclass(frozen=True)
class PercorsiRuntime:
    """
    Radici runtime di Aster.

    percorsi_runtime() restituisce sempre veri Path del sistema
    corrente; risolvi_percorsi() restituisce PurePath del sistema
    simulato.
    """

    modalita: str
    app_root: PurePath
    resource_root: PurePath
    data_root: PurePath

    @property
    def config_file(self) -> PurePath:
        return self.app_root / "config.json"

    @property
    def prompt_file(self) -> PurePath:
        return self.resource_root / "prompt.txt"

    @property
    def memory_file(self) -> PurePath:
        return self.data_root / "memory.json"


# =====================================================================
# Parte pura: nessuna I/O, nessun accesso a sys/os
# =====================================================================


def _classe_path(sistema: str) -> type[PurePath]:
    """PureWindowsPath per Windows ("win32"), PurePosixPath altrimenti."""

    return PureWindowsPath if sistema == "win32" else PurePosixPath


def _path_assoluto_o_none(valore, classe_path: type[PurePath]) -> PurePath | None:
    """
    Converte valore in path solo se è una stringa non vuota, senza NUL
    e assoluta secondo la convenzione del sistema indicato. Qualsiasi
    altro valore (assente, vuoto, relativo) restituisce None: mai
    Path("") né altre forme che finirebbero risolte rispetto alla cwd.
    """

    if not isinstance(valore, str) or not valore or "\x00" in valore:
        return None

    candidato = classe_path(valore)

    if not candidato.is_absolute():
        return None

    return candidato


def _data_root_normale(
    sistema: str,
    environ: Mapping[str, str],
    home: str | None,
) -> PurePath:
    """Directory dati utente OS-specifica della modalità normale."""

    classe_path = _classe_path(sistema)
    home_valida = _path_assoluto_o_none(home, classe_path)

    if sistema == "win32":
        local_app_data = _path_assoluto_o_none(
            environ.get("LOCALAPPDATA"),
            classe_path,
        )
        if local_app_data is not None:
            return local_app_data / "Aster"
        if home_valida is not None:
            return home_valida / "AppData" / "Local" / "Aster"

    elif sistema == "darwin":
        # Su macOS XDG_DATA_HOME viene ignorata intenzionalmente.
        if home_valida is not None:
            return home_valida / "Library" / "Application Support" / "Aster"

    else:
        # Linux e altri sistemi POSIX: convenzione XDG. Un
        # XDG_DATA_HOME relativo va ignorato (specifica XDG).
        xdg_data_home = _path_assoluto_o_none(
            environ.get("XDG_DATA_HOME"),
            classe_path,
        )
        if xdg_data_home is not None:
            return xdg_data_home / "aster"
        if home_valida is not None:
            return home_valida / ".local" / "share" / "aster"

    raise ErrorePercorsiRuntime(
        "Impossibile determinare la directory dati utente: "
        "nessuna home valida disponibile."
    )


def risolvi_percorsi(
    *,
    frozen: bool,
    eseguibile: str | None,
    meipass: str | None,
    file_modulo: str | None,
    sistema: str,
    environ: Mapping[str, str],
    home: str | None,
    marker_presente: Callable[[PurePath], bool],
) -> PercorsiRuntime:
    """
    Sceglie le tre radici runtime a partire da input espliciti.

    - eseguibile / file_modulo: path assoluti già risolti dal chiamante;
    - meipass: sys._MEIPASS, None se assente;
    - sistema: valore in stile sys.platform ("win32", "darwin", ...);
    - marker_presente: unica osservazione del filesystem ammessa, deve
      restituire True solo per un file reale.

    Non crea cartelle, non scrive e non migra nulla. Qualsiasi input
    mancante o relativo fa fallire la risoluzione (fail closed): la
    cwd non viene mai usata come fallback.
    """

    classe_path = _classe_path(sistema)

    if not frozen:
        percorso_modulo = _path_assoluto_o_none(file_modulo, classe_path)
        if percorso_modulo is None:
            raise ErrorePercorsiRuntime(
                "Impossibile determinare la directory di Aster."
            )

        # modules/runtime_paths.py -> radice del repository.
        app_root = percorso_modulo.parent.parent

        return PercorsiRuntime(
            modalita=MODALITA_SORGENTE,
            app_root=app_root,
            resource_root=app_root,
            data_root=app_root / "data",
        )

    percorso_eseguibile = _path_assoluto_o_none(eseguibile, classe_path)
    if percorso_eseguibile is None:
        raise ErrorePercorsiRuntime(
            "Impossibile determinare la directory dell'eseguibile di Aster."
        )

    app_root = percorso_eseguibile.parent

    if meipass is None or meipass == "":
        resource_root = app_root
    else:
        resource_root = _path_assoluto_o_none(meipass, classe_path)
        if resource_root is None:
            raise ErrorePercorsiRuntime(
                "Impossibile determinare la directory delle risorse di Aster."
            )

    if marker_presente(app_root / NOME_MARKER_PORTABLE):
        return PercorsiRuntime(
            modalita=MODALITA_PORTABLE,
            app_root=app_root,
            resource_root=resource_root,
            data_root=app_root / "data",
        )

    return PercorsiRuntime(
        modalita=MODALITA_NORMALE,
        app_root=app_root,
        resource_root=resource_root,
        data_root=_data_root_normale(sistema, environ, home),
    )


# =====================================================================
# Parte runtime: legge sys/os e produce veri Path del sistema corrente
# =====================================================================


def _file_marker_esiste(percorso: PurePath) -> bool:
    """True solo per un file reale: una directory omonima non vale."""

    return Path(percorso).is_file()


def _home_corrente() -> str | None:
    """Home dell'utente corrente, None se non determinabile."""

    try:
        return str(Path.home())
    except (RuntimeError, KeyError, OSError):
        return None


def percorsi_runtime() -> PercorsiRuntime:
    """Radici runtime reali del processo corrente, come veri Path."""

    frozen = bool(getattr(sys, "frozen", False))

    if frozen:
        # Il valore grezzo di sys.executable va validato PRIMA di
        # resolve(): su un path vuoto o relativo resolve() userebbe la
        # cwd e produrrebbe un path assoluto che il resolver
        # accetterebbe. Solo un valore già assoluto viene risolto
        # (symlink inclusi).
        eseguibile_grezzo = _path_assoluto_o_none(
            sys.executable,
            _classe_path(sys.platform),
        )
        if eseguibile_grezzo is None:
            raise ErrorePercorsiRuntime(
                "Impossibile determinare la directory dell'eseguibile di Aster."
            )

        eseguibile = str(Path(eseguibile_grezzo).resolve())
        meipass = getattr(sys, "_MEIPASS", None)
        file_modulo = None
        home = _home_corrente()
    else:
        eseguibile = None
        meipass = None
        file_modulo = str(Path(__file__).resolve())
        home = None

    percorsi = risolvi_percorsi(
        frozen=frozen,
        eseguibile=eseguibile,
        meipass=meipass,
        file_modulo=file_modulo,
        sistema=sys.platform,
        environ=os.environ,
        home=home,
        marker_presente=_file_marker_esiste,
    )

    return PercorsiRuntime(
        modalita=percorsi.modalita,
        app_root=Path(percorsi.app_root),
        resource_root=Path(percorsi.resource_root),
        data_root=Path(percorsi.data_root),
    )
