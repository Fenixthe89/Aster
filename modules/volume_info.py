"""Unità locali fisse e loro spazio, in sola lettura (usata da list_local_volumes)."""

import contextlib
import ctypes
import shutil
import sys
from typing import NamedTuple

# Unico backend 0.7.2c: Windows, stdlib ctypes + shutil.disk_usage.
# Nessun subprocess, PowerShell o WMI; nessuna lettura di directory,
# file, etichette, filesystem, seriali, GUID, UNC o device path.
# Il modulo si importa su ogni sistema: kernel32 viene caricata solo
# dentro leggi_volumi_locali, dopo il controllo di piattaforma.

# Volumi restituiti al massimo; "totale" conta comunque tutte le unità fisse.
MAX_LOCAL_VOLUMES = 8

# Tipi restituiti da GetDriveTypeW. Passa SOLO DRIVE_FIXED: è la
# classificazione di Windows, non la garanzia di un disco interno
# (alcuni dispositivi USB risultano DRIVE_FIXED).
DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6

# kernel32 cercata solo in System32: nessun path da config, cwd o PATH.
_LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800

# Nessuna finestra "inserire un disco" durante la lettura dello spazio.
_SEM_FAILCRITICALERRORS = 0x0001


class PiattaformaNonSupportata(Exception):
    """Backend unità locali non disponibile su questo sistema."""


class EnumerazioneNonRiuscita(Exception):
    """Elenco delle lettere di unità non leggibile."""


class _ModalitaErroriNonGarantita(Exception):
    """Error mode del thread non impostato o non ripristinato: lettura non affidabile."""


class RisultatoVolumi(NamedTuple):
    """Volumi già normalizzati (al massimo MAX_LOCAL_VOLUMES) e conteggio totale."""

    volumi: list
    totale: int
    troncato: bool


class _ApiKernel32:
    """Strato ctypes minimo su kernel32: chiamate grezze, nessuna decisione."""

    def __init__(self):
        kernel32 = ctypes.WinDLL(
            "kernel32",
            use_last_error=True,
            winmode=_LOAD_LIBRARY_SEARCH_SYSTEM32,
        )

        self._get_logical_drives = kernel32.GetLogicalDrives
        self._get_logical_drives.restype = ctypes.c_uint32
        self._get_logical_drives.argtypes = []

        self._get_drive_type = kernel32.GetDriveTypeW
        self._get_drive_type.restype = ctypes.c_uint
        self._get_drive_type.argtypes = [ctypes.c_wchar_p]

        self._set_thread_error_mode = kernel32.SetThreadErrorMode
        self._set_thread_error_mode.restype = ctypes.c_int
        self._set_thread_error_mode.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]

    def lettere_logiche(self) -> tuple[int, int]:
        """(bitmask, codice errore): il codice conta solo se la bitmask è 0."""

        ctypes.set_last_error(0)
        maschera = self._get_logical_drives()
        errore = ctypes.get_last_error() if maschera == 0 else 0
        return maschera, errore

    def tipo_unita(self, radice: str) -> int:
        return self._get_drive_type(radice)

    def imposta_modalita_errori(self, modalita: int) -> int | None:
        """Imposta l'error mode del thread; restituisce il precedente, None se fallisce."""

        precedente = ctypes.c_uint32()
        if not self._set_thread_error_mode(modalita, ctypes.byref(precedente)):
            return None
        return precedente.value

    def spazio(self, radice: str):
        # Stessa primitiva di get_disk_usage, per valori coerenti tra i tool.
        return shutil.disk_usage(radice)


@contextlib.contextmanager
def _errori_critici_silenziati(api):
    """
    SEM_FAILCRITICALERRORS sul solo thread corrente, fail-closed.

    Se l'error mode non si può impostare, la lettura non avviene. Il
    valore precedente viene sempre ripristinato; se il ripristino
    fallisce, la lettura non è considerata riuscita. In entrambi i casi
    esce solo un'eccezione interna fissa, che il chiamante trasforma in
    volume illeggibile. Mai SetErrorMode a livello di processo.
    """

    precedente = api.imposta_modalita_errori(_SEM_FAILCRITICALERRORS)
    if precedente is None:
        raise _ModalitaErroriNonGarantita("Error mode del thread non impostato.")

    try:
        yield
    finally:
        if api.imposta_modalita_errori(precedente) is None:
            raise _ModalitaErroriNonGarantita("Error mode del thread non ripristinato.")


def _intero(valore) -> bool:
    return type(valore) is int


def _volume_illeggibile(drive: str) -> dict:
    return {
        "drive": drive,
        "info_available": False,
        "total_bytes": None,
        "used_bytes": None,
        "free_bytes": None,
        "used_percent": None,
    }


def _normalizza_volume(drive: str, totale, usato, libero) -> dict:
    """Volume leggibile solo se i tre valori sono coerenti; mai corretti né inventati."""

    if not (_intero(totale) and _intero(usato) and _intero(libero)):
        return _volume_illeggibile(drive)

    if (
        totale <= 0
        or not 0 <= libero <= totale
        or not 0 <= usato <= totale
        or usato + libero != totale
    ):
        return _volume_illeggibile(drive)

    return {
        "drive": drive,
        "info_available": True,
        "total_bytes": totale,
        "used_bytes": usato,
        "free_bytes": libero,
        # Troncata: 100 solo se il volume è davvero pieno.
        "used_percent": usato * 100 // totale,
    }


def _leggi_volume(api, lettera: str) -> dict:
    """Spazio di una singola unità fissa: un errore rende illeggibile solo questa."""

    drive = f"{lettera}:"
    try:
        with _errori_critici_silenziati(api):
            uso = api.spazio(f"{lettera}:\\")
        totale, usato, libero = uso.total, uso.used, uso.free
    except Exception:
        return _volume_illeggibile(drive)

    return _normalizza_volume(drive, totale, usato, libero)


def _leggi(api) -> RisultatoVolumi:
    """
    Enumera le lettere A→Z, tiene solo DRIVE_FIXED e ne legge lo spazio.

    Il tipo di ogni unità è letto prima di qualunque accesso allo
    spazio: rete, rimovibili, ottiche, RAM disk, sconosciute e root non
    valide non vengono mai interrogate. Nessun filtro per nome, lettera
    o alias (un eventuale alias SUBST resta un residuo noto).
    """

    maschera, errore = api.lettere_logiche()
    if maschera == 0 and errore:
        raise EnumerazioneNonRiuscita("Elenco delle unità non disponibile.")

    lettere = [chr(ord("A") + bit) for bit in range(26) if maschera & (1 << bit)]
    fisse = [
        lettera for lettera in lettere
        if api.tipo_unita(f"{lettera}:\\") == DRIVE_FIXED
    ]

    totale = len(fisse)
    volumi = [_leggi_volume(api, lettera) for lettera in fisse[:MAX_LOCAL_VOLUMES]]
    return RisultatoVolumi(volumi, totale, totale > MAX_LOCAL_VOLUMES)


def unita_piu_piena(volumi: list, troncato: bool) -> str | None:
    """
    Lettera dell'unità con used_percent più alto, calcolata da Python.

    Evita che il modello confronti i byte assoluti invece della
    percentuale. Solo volumi con info_available True e percentuale
    intera valida; a parità vince la prima lettera A→Z. None se
    l'elenco è troncato (lo spazio delle unità oltre il limite non è
    letto: nessuna "più piena" scelta tra le sole prime), vuoto o
    senza volumi leggibili.
    """

    if troncato:
        return None

    candidati = [
        volume for volume in volumi
        if isinstance(volume, dict)
        and volume.get("info_available") is True
        and isinstance(volume.get("drive"), str)
        and type(volume.get("used_percent")) is int
        and 0 <= volume["used_percent"] <= 100
    ]
    if not candidati:
        return None

    piu_piena = min(candidati, key=lambda volume: (-volume["used_percent"], volume["drive"]))
    return piu_piena["drive"]


def _piattaforma() -> str:
    return sys.platform


def leggi_volumi_locali() -> RisultatoVolumi:
    """
    Unità locali fisse secondo Windows, con spazio normalizzato.

    PiattaformaNonSupportata: sistema diverso da Windows.
    EnumerazioneNonRiuscita: GetLogicalDrives non leggibile.
    Un errore su una singola unità la marca info_available=False.
    """

    if _piattaforma() != "win32":
        raise PiattaformaNonSupportata("Unità locali disponibili solo su Windows.")

    return _leggi(_ApiKernel32())
