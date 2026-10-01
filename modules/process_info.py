"""Uso CPU e RAM dei processi, raggruppati per nome, in sola lettura (usato da list_processes)."""

import ctypes
import decimal
import math
import struct
import sys
import time
from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple

# Unico backend 0.7.2d: Win32 documentato su Windows a 64 bit, con sola
# stdlib ctypes (kernel32 da System32): OpenProcess con
# PROCESS_QUERY_LIMITED_INFORMATION, GetProcessTimes, GetExitCodeProcess,
# K32GetProcessMemoryInfo, CloseHandle. Nessun PROCESS_VM_READ, nessuna
# API non documentata, subprocess, PowerShell o WMI.
# Il modulo si importa su ogni sistema: kernel32 viene caricata solo
# dentro misura_gruppi / crea_lettore_creazione, dopo i controlli di
# piattaforma. Nessuno stato fra chiamate: ogni misura apre e chiude i
# propri handle.
#
# Identità di un processo = (PID, creation time FILETIME). Il chiamante
# legge il creation time con crea_lettore_creazione prima e dopo il nome
# e lo passa solo se coincide; la prima passata confronta lo stesso
# FILETIME (interi esatti, nessuna conversione float) e la seconda lo
# riconfronta: un PID riusato prima o durante la misura non riceve mai
# le metriche del processo nuovo.

# Unica attesa globale fra le due passate (mai una per processo).
CPU_SAMPLE_INTERVAL_SECONDS = 0.1

# Esito di ogni gruppo: metriche complete o marcatore testuale.
LEGGIBILE = "readable"
PROCESSO_PROTETTO = "protected_process"
NON_DISPONIBILE = "not_available"

# Esito interno di una singola istanza terminata o con PID riusato
# durante la finestra: esclusa dal risultato, mai trasformata in zero.
_USCITO = "exited"

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
# OpenProcess su un PID che non esiste più (stessa convenzione di psutil).
_ERROR_INVALID_PARAMETER = 87

# kernel32 cercata solo in System32: nessun path da config, cwd o PATH.
_LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800

_TICK_PER_SECONDO = 10_000_000
_MAX_PID = 0xFFFFFFFF

_PRECISIONE_DECIMALE = 40
_TICK_PER_SECONDO_DECIMALE = Decimal(_TICK_PER_SECONDO)
_ZERO = Decimal(0)
_UNO = Decimal(1)
_CENTO = Decimal(100)

# Layout attesi su Windows a 64 bit: se ctypes non coincide, nessuna
# chiamata (un layout errato è un crash, non un'eccezione intercettabile).
_DIMENSIONE_FILETIME_ATTESA = 8
_DIMENSIONE_PMC_EX2_ATTESA = 96

# PROCESS_MEMORY_COUNTERS_EX2 (PrivateWorkingSetSize) è documentata solo
# da Windows 10/11 22H2 con l'aggiornamento cumulativo di settembre 2023:
# nessuna tabella di build, la capacità si verifica a ogni lettura. Il
# sistema riscrive cb con la dimensione della struttura che ha compilato
# (verificato: cb maggiore -> 96, cb 72/80 -> campi EX2 intatti); in più
# un sistema che non compila il campo lascia questa sentinella. In
# entrambi i casi: non disponibile, mai un fallback ad altri campi.
_SENTINELLA_MEMORIA = (1 << 64) - 1


class GruppoProcessi(NamedTuple):
    """Processi con lo stesso nome completo: metriche sommate o marcatore."""

    nome: str
    istanze: int
    metriche: str
    cpu_percent: int | None
    memory_bytes: int | None


class _Campione(NamedTuple):
    """Esito di una singola istanza al termine delle due passate."""

    esito: str
    tick_cpu: int | None = None
    memoria: int | None = None


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    ]


class _PROCESS_MEMORY_COUNTERS_EX2(ctypes.Structure):
    # Layout completo richiesto dall'ABI; si legge solo PrivateWorkingSetSize.
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
        ("PrivateWorkingSetSize", ctypes.c_size_t),
        ("SharedCommitUsage", ctypes.c_uint64),
    ]


def _valore_filetime(filetime: _FILETIME) -> int:
    return (filetime.dwHighDateTime << 32) | filetime.dwLowDateTime


class _ApiKernel32:
    """Strato ctypes minimo su kernel32: chiamate grezze, nessuna decisione."""

    def __init__(self):
        kernel32 = ctypes.WinDLL(
            "kernel32",
            use_last_error=True,
            winmode=_LOAD_LIBRARY_SEARCH_SYSTEM32,
        )

        self._open_process = kernel32.OpenProcess
        self._open_process.restype = ctypes.c_void_p
        self._open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]

        self._close_handle = kernel32.CloseHandle
        self._close_handle.restype = ctypes.c_int
        self._close_handle.argtypes = [ctypes.c_void_p]

        self._get_process_times = kernel32.GetProcessTimes
        self._get_process_times.restype = ctypes.c_int
        self._get_process_times.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
        ]

        self._get_exit_code_process = kernel32.GetExitCodeProcess
        self._get_exit_code_process.restype = ctypes.c_int
        self._get_exit_code_process.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]

        self._get_process_memory_info = kernel32.K32GetProcessMemoryInfo
        self._get_process_memory_info.restype = ctypes.c_int
        self._get_process_memory_info.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_PROCESS_MEMORY_COUNTERS_EX2),
            ctypes.c_uint32,
        ]

    def apri(self, pid: int) -> tuple[int | None, int]:
        """(handle, codice errore): il codice conta solo se l'handle è None."""

        ctypes.set_last_error(0)
        handle = self._open_process(_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not handle:
            return None, ctypes.get_last_error()
        return handle, 0

    def chiudi(self, handle: int) -> None:
        self._close_handle(handle)

    def tempi(self, handle: int) -> tuple[int, int] | None:
        """(creazione, kernel + user) in tick da 100 ns, o None se la lettura fallisce."""

        creazione = _FILETIME()
        uscita = _FILETIME()
        kernel = _FILETIME()
        utente = _FILETIME()
        if not self._get_process_times(
            handle,
            ctypes.byref(creazione),
            ctypes.byref(uscita),
            ctypes.byref(kernel),
            ctypes.byref(utente),
        ):
            return None
        return _valore_filetime(creazione), _valore_filetime(kernel) + _valore_filetime(utente)

    def codice_uscita(self, handle: int) -> int | None:
        codice = ctypes.c_uint32()
        if not self._get_exit_code_process(handle, ctypes.byref(codice)):
            return None
        return codice.value

    def memoria_privata(self, handle: int) -> int | None:
        """
        PrivateWorkingSetSize, o None se la lettura fallisce o il sistema
        non ha compilato la struttura EX2 (cb restituito diverso o campo
        lasciato alla sentinella).
        """

        dimensione = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS_EX2)
        contatori = _PROCESS_MEMORY_COUNTERS_EX2()
        contatori.cb = dimensione
        contatori.PrivateWorkingSetSize = _SENTINELLA_MEMORIA
        if not self._get_process_memory_info(
            handle,
            ctypes.byref(contatori),
            dimensione,
        ):
            return None
        if contatori.cb != dimensione:
            return None
        if contatori.PrivateWorkingSetSize == _SENTINELLA_MEMORIA:
            return None
        return contatori.PrivateWorkingSetSize


def _intero_non_negativo(valore) -> bool:
    return type(valore) is int and valore >= 0


def _leggi_creazione(api, pid) -> int | None:
    """Creation time FILETIME del processo con questo PID, o None se non leggibile."""

    if type(pid) is not int or not 0 < pid <= _MAX_PID:
        return None

    try:
        handle, _errore = api.apri(pid)
        if handle is None:
            return None
        try:
            tempi = api.tempi(handle)
        finally:
            api.chiudi(handle)
    except Exception:
        return None

    if tempi is None or not _intero_non_negativo(tempi[0]):
        return None
    return tempi[0]


def _prima_lettura(api, pid: int, creazione_enumerata):
    """
    Prima passata su un PID: (creazione, tick CPU) oppure l'esito finale.

    OpenProcess negato -> processo protetto; PID inesistente -> uscito;
    qualunque altro errore -> non disponibile. Il processo aperto deve
    essere quello enumerato: creation time diverso -> PID riusato dopo
    l'enumerazione (escluso); creation time enumerato assente -> identità
    non verificabile, nessuna metrica attribuita al nome. Mai un valore
    inventato.
    """

    handle, errore = api.apri(pid)
    if handle is None:
        if errore == _ERROR_ACCESS_DENIED:
            return PROCESSO_PROTETTO
        if errore == _ERROR_INVALID_PARAMETER:
            return _USCITO
        return NON_DISPONIBILE

    try:
        tempi = api.tempi(handle)
    finally:
        api.chiudi(handle)

    if tempi is None:
        return NON_DISPONIBILE

    creazione, tick = tempi
    if not (_intero_non_negativo(creazione) and _intero_non_negativo(tick)):
        return NON_DISPONIBILE
    if creazione_enumerata is None:
        return NON_DISPONIBILE
    if creazione != creazione_enumerata:
        return _USCITO
    return creazione, tick


def _seconda_lettura(api, pid: int, creazione: int, tick_iniziali: int) -> _Campione:
    """
    Seconda passata: stesso processo ancora attivo, delta CPU e RAM.

    Terminato (codice di uscita diverso da STILL_ACTIVE o PID non più
    esistente) o PID riusato (creation time diverso) -> escluso: le
    metriche di un processo non vengono mai attribuite a un altro.
    """

    handle, errore = api.apri(pid)
    if handle is None:
        if errore == _ERROR_INVALID_PARAMETER:
            return _Campione(_USCITO)
        # Leggibile un attimo prima: identità non più verificabile.
        return _Campione(NON_DISPONIBILE)

    try:
        codice = api.codice_uscita(handle)
        if codice is None:
            return _Campione(NON_DISPONIBILE)
        if codice != _STILL_ACTIVE:
            return _Campione(_USCITO)

        tempi = api.tempi(handle)
        if tempi is None:
            return _Campione(NON_DISPONIBILE)
        nuova_creazione, tick_finali = tempi
        if nuova_creazione != creazione:
            return _Campione(_USCITO)
        if not _intero_non_negativo(tick_finali) or tick_finali < tick_iniziali:
            return _Campione(NON_DISPONIBILE)

        memoria = api.memoria_privata(handle)
    finally:
        api.chiudi(handle)

    if not _intero_non_negativo(memoria):
        return _Campione(NON_DISPONIBILE)
    return _Campione(LEGGIBILE, tick_finali - tick_iniziali, memoria)


def _campiona(api, identita: dict, orologio, attendi) -> tuple[dict, float]:
    """
    Due passate su tutti i PID con un'unica attesa globale.

    identita: PID -> creation time letto all'enumerazione (o None). La
    finestra è la distanza tra i punti medi delle due passate: ogni
    processo viene letto due volte nello stesso ordine, quindi il suo
    intervallo reale è circa l'attesa più la durata di una passata.
    """

    esiti = {}
    iniziali = {}

    inizio_prima = orologio()
    for pid, creazione_enumerata in identita.items():
        lettura = _prima_lettura(api, pid, creazione_enumerata)
        if isinstance(lettura, tuple):
            iniziali[pid] = lettura
        else:
            esiti[pid] = _Campione(lettura)
    fine_prima = orologio()

    if iniziali:
        attendi(CPU_SAMPLE_INTERVAL_SECONDS)

    inizio_seconda = orologio()
    for pid, (creazione, tick) in iniziali.items():
        esiti[pid] = _seconda_lettura(api, pid, creazione, tick)
    fine_seconda = orologio()

    finestra = (inizio_seconda + fine_seconda) / 2 - (inizio_prima + fine_prima) / 2
    return esiti, finestra


def normalizza_cpu(tick: int, secondi_finestra, processori_logici) -> int | None:
    """
    Percentuale CPU sul totale dei processori logici (come Gestione attività).

    cpu_seconds = tick / 10_000_000; percentuale = cpu_seconds /
    secondi_finestra / processori_logici * 100, limitata a [0, 100] e
    arrotondata all'intero con .5 per eccesso. Calcolo in Decimal sul
    valore decimale della finestra (Decimal(repr(float)), tick e
    processori interi esatti) e ROUND_HALF_UP: niente errori binari alle
    soglie .5 (35_000 tick in 0.1 s su 1 processore = 3.5% -> 4, mentre
    int(x + 0.5) sul float darebbe 3). None se finestra o numero di
    processori non sono validi.
    """

    if (
        isinstance(secondi_finestra, bool)
        or not isinstance(secondi_finestra, (int, float))
        or not math.isfinite(secondi_finestra)
        or secondi_finestra <= 0
    ):
        return None
    if type(processori_logici) is not int or processori_logici <= 0:
        return None
    if type(tick) is not int:
        return None

    # Contesto locale: il contesto decimal globale non viene toccato.
    with decimal.localcontext() as contesto:
        contesto.prec = _PRECISIONE_DECIMALE
        percentuale = (Decimal(tick) * _CENTO) / (
            _TICK_PER_SECONDO_DECIMALE
            * Decimal(repr(secondi_finestra))
            * processori_logici
        )
        percentuale = min(max(percentuale, _ZERO), _CENTO)
        return int(percentuale.quantize(_UNO, rounding=ROUND_HALF_UP))


def _aggrega(processi: list, esiti: dict, secondi_finestra, processori_logici) -> list:
    """
    Gruppi per nome completo esatto, nell'ordine di prima comparsa.

    Istanze uscite escluse (gruppo rimosso se resta vuoto). Precedenza:
    not_available > protected_process > metriche complete. Mai somme
    parziali: i tick grezzi delle istanze si sommano prima, poi la
    percentuale si normalizza e arrotonda una sola volta.
    """

    istanze_per_nome = {}
    for pid, nome in processi:
        esito = esiti.get(pid, _Campione(NON_DISPONIBILE))
        if esito.esito == _USCITO:
            continue
        istanze_per_nome.setdefault(nome, []).append(esito)

    gruppi = []
    for nome, istanze in istanze_per_nome.items():
        stati = {istanza.esito for istanza in istanze}

        if NON_DISPONIBILE in stati:
            metriche = NON_DISPONIBILE
        elif PROCESSO_PROTETTO in stati:
            metriche = PROCESSO_PROTETTO
        else:
            metriche = LEGGIBILE

        cpu = memoria = None
        if metriche == LEGGIBILE:
            cpu = normalizza_cpu(
                sum(istanza.tick_cpu for istanza in istanze),
                secondi_finestra,
                processori_logici,
            )
            if cpu is None:
                metriche = NON_DISPONIBILE
            else:
                memoria = sum(istanza.memoria for istanza in istanze)

        gruppi.append(GruppoProcessi(nome, len(istanze), metriche, cpu, memoria))

    return gruppi


def _misura(processi: list, api, orologio, attendi, processori_logici) -> list:
    """
    Metriche dei processi (pid, nome, creazione) già validati e filtrati
    dal chiamante; creazione è il FILETIME letto all'enumerazione o None.

    PID 0 (System Idle Process) escluso del tutto: il suo tempo CPU è
    inattività, non uso reale. api None (backend non disponibile),
    numero di processori logici non valido o un errore imprevisto del
    campionamento -> ogni gruppo not_available, senza attese: l'elenco
    dei nomi resta comunque disponibile.
    """

    processi = [voce for voce in processi if voce[0] != 0]
    esiti = {}
    finestra = None

    if api is not None and type(processori_logici) is int and processori_logici > 0:
        identita = {}
        for pid, _nome, creazione in processi:
            if type(pid) is int and 0 < pid <= _MAX_PID:
                identita.setdefault(pid, creazione)
        try:
            esiti, finestra = _campiona(api, identita, orologio, attendi)
        except Exception:
            esiti, finestra = {}, None

    return _aggrega(
        [(pid, nome) for pid, nome, _creazione in processi],
        esiti,
        finestra,
        processori_logici,
    )


def _piattaforma() -> str:
    return sys.platform


def _bit_processo() -> int:
    return struct.calcsize("P") * 8


def _crea_api():
    """Backend Win32 se piattaforma e layout sono quelli attesi, altrimenti None."""

    if _piattaforma() != "win32" or _bit_processo() != 64:
        return None

    if (
        ctypes.sizeof(_FILETIME) != _DIMENSIONE_FILETIME_ATTESA
        or ctypes.sizeof(_PROCESS_MEMORY_COUNTERS_EX2) != _DIMENSIONE_PMC_EX2_ATTESA
    ):
        return None

    try:
        return _ApiKernel32()
    except Exception:
        return None


def crea_lettore_creazione():
    """
    Lettore dell'identità all'enumerazione: pid -> creation time FILETIME o None.

    Da chiamare prima e dopo la lettura del nome del processo. None per
    processi non apribili (protetti, già terminati) e fuori da Windows a
    64 bit: identità non verificabile, quindi nessuna metrica attribuita
    al nome. Legge solo il creation time; non solleva eccezioni.
    """

    api = _crea_api()
    if api is None:
        return lambda pid: None

    def leggi(pid):
        return _leggi_creazione(api, pid)

    return leggi


def misura_gruppi(processi: list, processori_logici) -> list[GruppoProcessi]:
    """
    Raggruppa per nome i processi (pid, nome, creazione) e ne misura CPU e RAM.

    creazione è il valore di crea_lettore_creazione letto all'enumerazione:
    solo il processo con lo stesso creation time riceve metriche.
    processori_logici è il denominatore della percentuale CPU (lo stesso
    valore mostrato da get_system_info). Blocca per circa
    CPU_SAMPLE_INTERVAL_SECONDS (un'unica attesa globale) quando almeno
    un processo è leggibile. Fuori da Windows a 64 bit, o se il backend
    non si carica, ogni gruppo è not_available. Non solleva eccezioni
    per errori di sistema.
    """

    return _misura(
        processi,
        _crea_api(),
        time.monotonic,
        time.sleep,
        processori_logici,
    )
