"""Tool non-memory di Aster: informazioni di sistema reali (dominio "system")."""

import math
import platform
import shutil
import string
import time
import unicodedata
from pathlib import Path

from modules import gpu_info, process_info, volume_info
from modules.tool_registry import RegistroStrumenti, ToolSpec

# Import protetto: se psutil manca, Aster parte comunque; list_processes
# risponde con un tool_error fisso e get_system_info con metriche None.
try:
    import psutil
except ImportError:
    psutil = None

# winreg esiste solo su Windows: altrove il modello CPU usa il fallback.
try:
    import winreg
except ImportError:
    winreg = None

# Limite di GRUPPI (processi aggregati per nome) restituiti da
# list_processes: il risultato entra in role="tool" e resta in
# cronologia, quindi va tenuto contenuto.
MAX_PROCESS_ENTRIES = 20

# Finestra di campionamento di cpu_percent: con interval=None le
# chiamate di un processo appena avviato o ravvicinate restituiscono
# 0.0 privo di significato; 0.1 s è la latenza accettata per un dato reale.
CPU_USAGE_INTERVAL_SECONDS = 0.1

_CHIAVE_REGISTRO_CPU = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
_VALORE_REGISTRO_CPU = "ProcessorNameString"

_MAX_LUNGHEZZA_FILTRO = 64
_MAX_LUNGHEZZA_NOME_PROCESSO = 255

# Valori esatti (dopo strip + casefold) che il modello usa a volte per
# dire "nessun filtro": mai un confronto per sottostringa.
_SENTINELLE_NESSUN_FILTRO = frozenset({"nil", "none", "null", "<nil>"})

_ORDINAMENTI = ("cpu", "memory")
_ORDINAMENTO_PREDEFINITO = "memory"

# Nome mostrato al modello: al massimo MAX_PROCESS_NAME_UNITS unità
# pesate per avvicinare il costo reale in token (una cifra ASCII o un
# segno di punteggiatura valgono circa un token, un carattere non ASCII
# anche più di uno, un carattere astrale fino a quattro). Filtro,
# aggregazione, ordinamento e scelta dei top usano sempre il nome completo.
MAX_PROCESS_NAME_UNITS = 40
_CIFRE_ASCII = frozenset("0123456789")
# Esattamente gli ASCII stampabili non alfanumerici e non spazio
# (string.punctuation è una costante, indipendente da locale e Unicode).
_PUNTEGGIATURA_ASCII = frozenset(string.punctuation)
_PESO_CIFRA_NOME = 3
_PESO_PUNTEGGIATURA_NOME = 2
_PESO_ASCII_NOME = 1
_PESO_BMP_NOME = 4
_PESO_ASTRALE_NOME = 8
_MAX_CODICE_BMP = 0xFFFF
# Il suffisso pesa come qualunque altro testo mostrato ("." è
# punteggiatura: 6 unità): il nome troncato resta sempre nel limite.
_SUFFISSO_NOME_TRONCATO = "..."

_ERRORE_PSUTIL_ASSENTE = "Elenco processi non disponibile su questa installazione."
_ERRORE_ENUMERAZIONE = "Non sono riuscito a leggere l'elenco dei processi."
_ERRORE_FILTRO_NON_VALIDO = "Filtro nome non valido."
_ERRORE_ORDINAMENTO_NON_VALIDO = "Ordinamento non valido: usa cpu oppure memory."

_ERRORE_VOLUMI_NON_SUPPORTATI = "L'elenco delle unità locali è disponibile solo su Windows."
_ERRORE_VOLUMI = "Non sono riuscito a elencare le unità locali."

TOOLS_SISTEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_system_info",
            "description": (
                "Dati reali del computer locale: sistema operativo, CPU "
                "(modello, core, thread, uso), RAM, uptime, GPU e VRAM "
                "dedicata totale."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_disk_usage",
            "description": (
                "Spazio totale, usato e libero (byte) del solo filesystem "
                "che contiene realmente Aster: non di tutte le unità né di "
                "una cartella."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_processes",
            "description": (
                "Elenca i processi in esecuzione sul computer locale, "
                "raggruppati per nome programma, con uso attuale di CPU e "
                "RAM. Sola osservazione: non avvia, chiude né modifica "
                "processi. Un processo non corrisponde necessariamente a "
                "una finestra visibile."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Opzionale: breve nome o frammento del nome del "
                            "programma da cercare (es. steam, discord, "
                            "ollama). Confronto senza distinzione "
                            "maiuscole/minuscole. Ometti per l'elenco "
                            "generale."
                        ),
                    },
                    "sort": {
                        "type": "string",
                        "enum": ["cpu", "memory"],
                        "description": (
                            "Opzionale: ordina per uso di CPU o di RAM "
                            "(memory), per trovare chi consuma di più."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_local_volumes",
            "description": (
                "Spazio (byte e % usata) delle unità locali fisse con lettera "
                "(C:, D:, ...). Esclude le unità che Windows classifica come "
                "rete, rimovibili, ottiche o RAM disk."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]


def _intero_positivo(valore) -> int | None:
    """valore se è un int vero (bool escluso) > 0, altrimenti None."""

    if type(valore) is int and valore > 0:
        return valore
    return None


def _intero_non_negativo(valore) -> int | None:
    """valore se è un int vero (bool escluso) >= 0, altrimenti None."""

    if type(valore) is int and valore >= 0:
        return valore
    return None


def _percentuale(valore) -> float | None:
    """float in [0, 100] se valore è un numero finito (bool escluso), altrimenti None."""

    if isinstance(valore, bool) or not isinstance(valore, (int, float)):
        return None

    # Range prima di isfinite: NaN fallisce il confronto e un int enorme
    # viene scartato senza OverflowError nella conversione a float.
    if not 0 <= valore <= 100 or not math.isfinite(valore):
        return None

    return float(valore)


def _percentuale_intera(valore) -> int | None:
    """Percentuale validata, arrotondata all'intero con .5 per eccesso (no banker's rounding)."""

    # Il modello legge male i decimali sotto 1 (0.6 -> "60%"): all'LLM solo interi.
    percentuale = _percentuale(valore)
    if percentuale is None:
        return None
    return int(percentuale + 0.5)


def _raccogli(collettore):
    """Esegue un singolo collector: qualunque errore locale diventa None."""

    try:
        return collettore()
    except Exception:
        return None


def _modello_cpu_registro() -> str | None:
    """ProcessorNameString dal registro Windows (sola lettura), o None."""

    if winreg is None:
        return None

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _CHIAVE_REGISTRO_CPU) as chiave:
            valore, _tipo = winreg.QueryValueEx(chiave, _VALORE_REGISTRO_CPU)
    except OSError:
        return None

    if not isinstance(valore, str):
        return None

    return valore.strip() or None


def _modello_cpu() -> str | None:
    modello = _modello_cpu_registro()
    if modello is not None:
        return modello

    fallback = platform.processor()
    if not isinstance(fallback, str):
        return None

    return fallback.strip() or None


def _core_fisici() -> int | None:
    if psutil is None:
        return None
    return _intero_positivo(psutil.cpu_count(logical=False))


def _processori_logici() -> int | None:
    if psutil is None:
        return None
    return _intero_positivo(psutil.cpu_count(logical=True))


def _uso_cpu() -> int | None:
    if psutil is None:
        return None
    return _percentuale_intera(psutil.cpu_percent(interval=CPU_USAGE_INTERVAL_SECONDS))


def _info_cpu() -> dict:
    """Ogni metrica CPU è raccolta separatamente: un errore ne azzera solo una."""

    return {
        "model": _raccogli(_modello_cpu),
        "physical_cores": _raccogli(_core_fisici),
        "logical_processors": _raccogli(_processori_logici),
        "usage_percent": _raccogli(_uso_cpu),
    }


def _info_memoria() -> dict:
    """RAM da un'unica virtual_memory(): se fallisce, tutti i campi None."""

    memoria = {
        "total_bytes": None,
        "available_bytes": None,
        "used_bytes": None,
        "usage_percent": None,
    }

    if psutil is None:
        return memoria

    try:
        dati = psutil.virtual_memory()
        memoria["total_bytes"] = _intero_non_negativo(getattr(dati, "total", None))
        memoria["available_bytes"] = _intero_non_negativo(getattr(dati, "available", None))
        memoria["used_bytes"] = _intero_non_negativo(getattr(dati, "used", None))
        memoria["usage_percent"] = _percentuale(getattr(dati, "percent", None))
    except Exception:
        return {campo: None for campo in memoria}

    return memoria


def _nome_adattatore(valore) -> str | None:
    """Nome dell'adapter ripulito; None se non stringa, vuoto o con caratteri Cc."""

    # Testo fornito dal driver: è un DATO, mai un'istruzione.
    if not isinstance(valore, str):
        return None

    nome = valore.strip()
    if not nome or _contiene_caratteri_controllo(nome):
        return None

    return nome


def _info_gpu() -> dict:
    """
    Blocco GPU: tutto o niente sull'enumerazione, None sul singolo campo.

    info_available distingue "lettura riuscita" (anche con zero adapter
    hardware) da "dati non letti" (sistema non supportato o errore):
    l'assenza di dati non significa assenza di GPU. Nessun testo di
    eccezione esce da qui.
    """

    try:
        adattatori = [
            {
                "name": _nome_adattatore(adattatore.name),
                "dedicated_memory_bytes": _intero_non_negativo(
                    adattatore.dedicated_memory_bytes
                ),
            }
            for adattatore in gpu_info.leggi_adattatori_grafici()
        ]
    except Exception:
        return {"info_available": False, "adapters": []}

    return {"info_available": True, "adapters": adattatori}


def _numero_finito(valore) -> bool:
    return (
        not isinstance(valore, bool)
        and isinstance(valore, (int, float))
        and math.isfinite(valore)
    )


def _uptime_secondi() -> int | None:
    """Secondi interi dall'avvio; None (mai 0) se boot_time/orologio sono anomali."""

    if psutil is None:
        return None

    avvio = psutil.boot_time()
    adesso = time.time()

    if not _numero_finito(avvio) or not _numero_finito(adesso):
        return None

    delta = adesso - avvio
    if not math.isfinite(delta) or delta < 0:
        return None

    return int(delta)


def _scomposizione_uptime(secondi: int | None) -> dict:
    """Giorni/ore/minuti calcolati da Python sullo stesso uptime_seconds normalizzato."""

    if secondi is None:
        return {"days": None, "hours": None, "minutes": None}

    resto = secondi % 86400
    return {
        "days": secondi // 86400,
        "hours": resto // 3600,
        "minutes": (resto % 3600) // 60,
    }


def get_system_info(argomenti: dict, contesto) -> dict:
    """
    Handler del tool get_system_info.

    Snapshot in sola lettura di sistema operativo, CPU, RAM, GPU e
    uptime: valori raw (byte, secondi, percentuali), nessuna conversione
    in unità leggibili; l'uptime è anche scomposto da Python in
    giorni/ore/minuti interi, perché il modello sbaglia la divisione
    dei secondi. Ogni metrica CPU, la memoria, la GPU e l'uptime
    falliscono in modo indipendente (None, o gpu.info_available=false),
    senza perdere il resto dello snapshot. Solo un errore imprevisto
    fuori dai collector (es. nei campi del sistema operativo) produce
    tool_error, con messaggio fisso.
    Argomenti e contesto vengono ignorati; nessun side effect. Blocca
    per circa CPU_USAGE_INTERVAL_SECONDS per misurare l'uso CPU.
    """

    try:
        data = {
            "os_name": platform.system(),
            "os_release": platform.release(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
            "cpu": _info_cpu(),
            "memory": _info_memoria(),
            "gpu": _info_gpu(),
            "uptime_seconds": _raccogli(_uptime_secondi),
        }
        data["uptime_breakdown"] = _scomposizione_uptime(data["uptime_seconds"])
    except Exception:
        # Messaggio fisso: il testo dell'eccezione potrebbe contenere
        # path o dettagli OS e non deve mai arrivare in role="tool".
        return {
            "ok": False,
            "operation": "get_system_info",
            "status": "tool_error",
            "error": "Non sono riuscito a leggere le informazioni di sistema.",
        }

    return {
        "ok": True,
        "operation": "get_system_info",
        "status": "success",
        "data": data,
    }


def get_disk_usage(argomenti: dict, contesto) -> dict:
    """
    Handler del tool get_disk_usage.

    Determina il filesystem che contiene realmente l'installazione di
    Aster passando a shutil.disk_usage la directory del modulo stesso,
    non l'anchor/drive root: su Linux l'anchor sarebbe sempre "/" anche
    se Aster si trovasse su un mount separato (es. /home), mentre la
    directory reale del modulo risolve sempre al filesystem corretto
    sia su Windows sia su Linux. Non richiede parametri e non usa
    alcuno stato esterno: argomenti e contesto vengono ignorati
    esplicitamente, nessun side effect. Non espone il path reale, solo
    l'etichetta neutra "aster_filesystem" e tre interi in byte.
    """

    try:
        percorso_aster = Path(__file__).resolve().parent
        uso = shutil.disk_usage(percorso_aster)
    except Exception:
        # Messaggio fisso: un OSError includerebbe il path reale di Aster.
        return {
            "ok": False,
            "operation": "get_disk_usage",
            "status": "tool_error",
            "error": "Non sono riuscito a leggere lo spazio disco.",
        }

    return {
        "ok": True,
        "operation": "get_disk_usage",
        "status": "success",
        "data": {
            "scope": "aster_filesystem",
            "total_bytes": uso.total,
            "used_bytes": uso.used,
            "free_bytes": uso.free,
        },
    }


def _contiene_caratteri_controllo(testo: str) -> bool:
    """True se il testo contiene caratteri Unicode di categoria Cc."""

    return any(unicodedata.category(ch) == "Cc" for ch in testo)


def _errore_list_processes(messaggio: str) -> dict:
    """tool_error di list_processes con messaggio fisso (mai testo di eccezioni)."""

    return {
        "ok": False,
        "operation": "list_processes",
        "status": "tool_error",
        "error": messaggio,
    }


def _normalizza_filtro_nome(valore) -> tuple[bool, str | None]:
    """
    Valida il parametro opzionale name di list_processes.

    Restituisce (valido, filtro). None, "" o soli spazi significano
    nessun filtro (filtro=None), così come le sole sentinelle esatte
    nil/none/null/<nil> (dopo strip e casefold). Tipo non stringa,
    caratteri di controllo o lunghezza oltre _MAX_LUNGHEZZA_FILTRO dopo
    strip() rendono il filtro non valido. Il filtro e' solo testo per un
    confronto Python per sottostringa: nessuna regex, wildcard o shell.
    """

    if valore is None:
        return True, None

    if not isinstance(valore, str):
        return False, None

    if _contiene_caratteri_controllo(valore):
        return False, None

    filtro = valore.strip()

    if not filtro or filtro.casefold() in _SENTINELLE_NESSUN_FILTRO:
        return True, None

    if len(filtro) > _MAX_LUNGHEZZA_FILTRO:
        return False, None

    return True, filtro


def _normalizza_ordinamento(valore) -> str | None:
    """sort di list_processes: assente/null -> memory; solo "cpu" o "memory" esatti, altrimenti None."""

    if valore is None:
        return _ORDINAMENTO_PREDEFINITO
    if isinstance(valore, str) and valore in _ORDINAMENTI:
        return valore
    return None


def _unita_carattere_nome(carattere: str) -> int:
    """
    Peso di un carattere del nome: cifra ASCII 3, punteggiatura ASCII 2,
    altro ASCII (lettere, spazio) 1, non ASCII BMP 4, astrale 8.
    """

    if carattere in _CIFRE_ASCII:
        return _PESO_CIFRA_NOME
    if carattere in _PUNTEGGIATURA_ASCII:
        return _PESO_PUNTEGGIATURA_NOME
    if carattere.isascii():
        return _PESO_ASCII_NOME
    if ord(carattere) <= _MAX_CODICE_BMP:
        return _PESO_BMP_NOME
    return _PESO_ASTRALE_NOME


def _unita_nome(testo: str) -> int:
    return sum(_unita_carattere_nome(carattere) for carattere in testo)


def _nome_mostrato(nome: str) -> str:
    """
    Nome limitato a MAX_PROCESS_NAME_UNITS unità pesate.

    Si itera per caratteri Python (mai byte): nessun carattere viene
    spezzato. Se va tagliato, il suffisso "..." sta dentro lo stesso
    budget, pesato come il resto: il risultato non supera mai il limite.
    """

    if _unita_nome(nome) <= MAX_PROCESS_NAME_UNITS:
        return nome

    budget = MAX_PROCESS_NAME_UNITS - _unita_nome(_SUFFISSO_NOME_TRONCATO)
    tenuti = []
    for carattere in nome:
        costo = _unita_carattere_nome(carattere)
        if costo > budget:
            break
        tenuti.append(carattere)
        budget -= costo

    return "".join(tenuti) + _SUFFISSO_NOME_TRONCATO


def _gruppo_misurabile(gruppo) -> bool:
    return gruppo.metriche == process_info.LEGGIBILE


def _chiave_ordinamento(ordinamento: str):
    """Misurabili per metrica decrescente, poi marcatori; parità per nome casefold e nome."""

    def chiave(gruppo):
        if not _gruppo_misurabile(gruppo):
            return (1, 0, gruppo.nome.casefold(), gruppo.nome)
        valore = gruppo.cpu_percent if ordinamento == "cpu" else gruppo.memory_bytes
        return (0, -valore, gruppo.nome.casefold(), gruppo.nome)

    return chiave


def _nome_massimo(gruppi: list, campo: str, solo_positivi: bool = False) -> str | None:
    """
    Nome (limitato) del gruppo misurabile col valore più alto.

    None se nessun gruppo è misurabile oppure, con solo_positivi, se
    tutti i valori sono 0: nessun vincitore scelto per ordine alfabetico.
    """

    misurabili = [
        gruppo for gruppo in gruppi
        if _gruppo_misurabile(gruppo) and (not solo_positivi or getattr(gruppo, campo) > 0)
    ]
    if not misurabili:
        return None

    vincitore = min(
        misurabili,
        key=lambda gruppo: (-getattr(gruppo, campo), gruppo.nome.casefold(), gruppo.nome),
    )
    return _nome_mostrato(vincitore.nome)


def _entry_gruppo(gruppo) -> dict:
    """Entry di output: metriche complete oppure marcatore testuale, mai null né PID."""

    entry = {"name": _nome_mostrato(gruppo.nome), "instances": gruppo.istanze}
    if _gruppo_misurabile(gruppo):
        entry["cpu_percent"] = gruppo.cpu_percent
        entry["memory_bytes"] = gruppo.memory_bytes
    else:
        entry["metrics"] = gruppo.metriche
    return entry


def _entry_processo_valida(info) -> dict | None:
    """Restituisce {"pid", "name"} se l'info del processo è valida, altrimenti None."""

    if not isinstance(info, dict):
        return None

    pid = info.get("pid")
    nome = info.get("name")

    if not isinstance(pid, int) or isinstance(pid, bool):
        return None

    if not isinstance(nome, str) or not nome.strip():
        return None

    if len(nome) > _MAX_LUNGHEZZA_NOME_PROCESSO:
        return None

    if _contiene_caratteri_controllo(nome):
        return None

    return {"pid": pid, "name": nome}


def _identita_attorno_al_nome(prima, dopo) -> tuple[bool, int | None]:
    """
    (candidato valido, creation time atteso) dalle due letture attorno al nome.

    Stesso FILETIME prima e dopo -> metriche ammesse con quell'identità.
    Entrambe illeggibili (processo protetto o non apribile) -> solo
    marcatore, mai metriche. Identità cambiata, o leggibile una sola
    volta -> PID ambiguo durante la lettura del nome: processo escluso.
    """

    if prima is None and dopo is None:
        return True, None
    if prima is not None and prima == dopo:
        return True, prima
    return False, None


def list_processes(argomenti: dict, contesto) -> dict:
    """
    Handler del tool list_processes.

    Sola osservazione: enumera i processi con psutil.process_iter
    leggendo esclusivamente pid e name (nessun username, cmdline, exe,
    cwd, environment o altro attributo). Un processo che scompare o
    non è accessibile durante l'enumerazione viene saltato senza far
    fallire il tool; solo un errore dell'enumerazione globale produce
    tool_error, sempre con messaggio fisso (mai il testo
    dell'eccezione, che potrebbe contenere path o dettagli OS).

    Identità = creation time FILETIME, letto prima e dopo il nome di
    ogni processo (il nome non viene mai letto prima): le metriche vanno
    a quel nome solo se l'identità resta la stessa attorno al nome e
    poi nelle due passate di process_info. Il creation time non entra
    mai nel risultato.

    Il filtro si applica al nome completo PRIMA dell'aggregazione;
    process_info raggruppa per nome completo esatto e misura CPU e RAM
    (o marca il gruppo protected_process / not_available, mai null né
    somme parziali). I gruppi si ordinano per sort (default memory),
    misurabili prima dei marcatori, PRIMA di troncare a
    MAX_PROCESS_ENTRIES. total, top_cpu, top_memory e
    metrics_unavailable si calcolano su tutti i gruppi corrispondenti,
    prima del troncamento. Nessun PID in output.
    """

    argomenti = argomenti if isinstance(argomenti, dict) else {}

    valido, filtro = _normalizza_filtro_nome(argomenti.get("name"))
    if not valido:
        return _errore_list_processes(_ERRORE_FILTRO_NON_VALIDO)

    ordinamento = _normalizza_ordinamento(argomenti.get("sort"))
    if ordinamento is None:
        return _errore_list_processes(_ERRORE_ORDINAMENTO_NON_VALIDO)

    if psutil is None:
        return _errore_list_processes(_ERRORE_PSUTIL_ASSENTE)

    filtro_casefold = filtro.casefold() if filtro is not None else None
    processi = []

    try:
        leggi_creazione = process_info.crea_lettore_creazione()
        # psutil 7 riusa tra una chiamata e l'altra gli oggetti Process in
        # cache, nome compreso, senza riverificarne l'identità: un PID
        # riusato manterrebbe il nome vecchio. Oggetti nuovi a ogni chiamata.
        psutil.process_iter.cache_clear()

        # Solo il PID: il nome non viene letto prima dell'identità.
        for processo in psutil.process_iter(["pid"]):
            try:
                info = processo.info
            except (
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                psutil.ZombieProcess,
            ):
                continue

            pid = info.get("pid") if isinstance(info, dict) else None
            if not isinstance(pid, int) or isinstance(pid, bool):
                continue

            # Identità prima e dopo la lettura del nome: il nome appartiene
            # al processo misurato solo se in mezzo l'identità non cambia.
            prima = leggi_creazione(pid)
            try:
                nome = processo.name()
            except (
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                psutil.ZombieProcess,
            ):
                continue
            dopo = leggi_creazione(pid)

            stabile, creazione = _identita_attorno_al_nome(prima, dopo)
            if not stabile:
                continue

            entry = _entry_processo_valida({"pid": pid, "name": nome})
            if entry is None:
                continue

            if (
                filtro_casefold is not None
                and filtro_casefold not in entry["name"].casefold()
            ):
                continue

            processi.append((entry["pid"], entry["name"], creazione))

        gruppi = process_info.misura_gruppi(processi, _raccogli(_processori_logici))
    except Exception:
        return _errore_list_processes(_ERRORE_ENUMERAZIONE)

    gruppi.sort(key=_chiave_ordinamento(ordinamento))
    totale = len(gruppi)

    return {
        "ok": True,
        "operation": "list_processes",
        "status": "success",
        "data": {
            "processes": [_entry_gruppo(gruppo) for gruppo in gruppi[:MAX_PROCESS_ENTRIES]],
            "name_filter": filtro,
            "sort": ordinamento,
            "total": totale,
            "truncated": totale > MAX_PROCESS_ENTRIES,
            # Tutte le CPU visibili a 0%: nessun "processo che usa più CPU".
            "top_cpu": _nome_massimo(gruppi, "cpu_percent", solo_positivi=True),
            "top_memory": _nome_massimo(gruppi, "memory_bytes"),
            "metrics_unavailable": sum(
                1 for gruppo in gruppi if not _gruppo_misurabile(gruppo)
            ),
        },
    }


def _errore_list_local_volumes(status: str, messaggio: str) -> dict:
    """Errore di list_local_volumes con messaggio fisso (mai testo di eccezioni)."""

    return {
        "ok": False,
        "operation": "list_local_volumes",
        "status": status,
        "error": messaggio,
    }


def list_local_volumes(argomenti: dict, contesto) -> dict:
    """
    Handler del tool list_local_volumes.

    Sola osservazione dello spazio delle unità che Windows classifica
    DRIVE_FIXED (unità locale fissa secondo Windows, non necessariamente
    un disco interno): nessun path dal modello, nessuna lettura di
    file o directory, nessuna autorizzazione filesystem concessa.
    Byte interi e percentuale usata troncata, già normalizzati da
    volume_info; al massimo MAX_LOCAL_VOLUMES elementi, total conta
    tutte le unità fisse. Una singola unità illeggibile resta in elenco
    con info_available=False. fullest_drive (used_percent più alto,
    calcolato da Python) è null se l'elenco è troncato, vuoto o senza
    volumi leggibili. Piattaforma non Windows ->
    unsupported_platform; enumerazione fallita -> tool_error; sempre
    con messaggi fissi. Argomenti e contesto vengono ignorati.
    """

    try:
        risultato = volume_info.leggi_volumi_locali()
    except volume_info.PiattaformaNonSupportata:
        return _errore_list_local_volumes("unsupported_platform", _ERRORE_VOLUMI_NON_SUPPORTATI)
    except Exception:
        return _errore_list_local_volumes("tool_error", _ERRORE_VOLUMI)

    return {
        "ok": True,
        "operation": "list_local_volumes",
        "status": "success",
        "data": {
            "scope": "local_fixed_volumes",
            "volumes": risultato.volumi,
            "total": risultato.totale,
            "truncated": risultato.troncato,
            "fullest_drive": volume_info.unita_piu_piena(risultato.volumi, risultato.troncato),
        },
    }


_NON_DISPONIBILE = "non disponibile"


def _testo_o_nd(valore) -> str:
    return str(valore) if valore is not None and valore != "" else _NON_DISPONIBILE


def _percentuale_o_nd(valore) -> str:
    if isinstance(valore, int):
        return f"{valore}%"
    return f"{valore:.1f}%" if isinstance(valore, float) else _NON_DISPONIBILE


def _bytes_o_nd(valore) -> str:
    return _formatta_bytes(valore) if isinstance(valore, int) else _NON_DISPONIBILE


def _formatta_durata(secondi: int) -> str:
    """Durata leggibile per il solo testo del fallback (il dato resta in secondi)."""

    giorni, resto = divmod(secondi, 86400)
    ore, resto = divmod(resto, 3600)
    minuti = resto // 60
    return f"{giorni} g, {ore} h, {minuti} min"


def _righe_gpu(gpu) -> list[str]:
    """Righe "Scheda video" del fallback (VRAM leggibile solo qui, mai nel dato)."""

    if not isinstance(gpu, dict) or gpu.get("info_available") is not True:
        return ["Scheda video: non disponibile"]

    adattatori = gpu.get("adapters")
    if not isinstance(adattatori, list):
        return ["Scheda video: non disponibile"]

    if not adattatori:
        return ["Scheda video: nessun adattatore grafico hardware segnalato dal sistema"]

    righe = []
    for adattatore in adattatori:
        if not isinstance(adattatore, dict):
            adattatore = {}
        righe.append(
            f"Scheda video: {_testo_o_nd(adattatore.get('name'))} "
            f"(VRAM dedicata: {_bytes_o_nd(adattatore.get('dedicated_memory_bytes'))})"
        )
    return righe


def _fallback_get_system_info(risultato_tool: dict) -> str:
    """Fallback deterministico dedicato a get_system_info."""

    if risultato_tool.get("status") != "success":
        return (
            risultato_tool.get("error")
            or "Non sono riuscito a leggere le informazioni di sistema."
        )

    data = risultato_tool.get("data", {})
    cpu = data.get("cpu") if isinstance(data.get("cpu"), dict) else {}
    memoria = data.get("memory") if isinstance(data.get("memory"), dict) else {}

    uptime = data.get("uptime_seconds")
    uptime_testo = (
        _formatta_durata(uptime) if isinstance(uptime, int) else _NON_DISPONIBILE
    )
    righe_gpu = "\n".join(_righe_gpu(data.get("gpu")))

    return (
        f"Sistema operativo: {data.get('os_name')} {data.get('os_release')}\n"
        f"Architettura: {data.get('machine')}\n"
        f"Processore: {_testo_o_nd(cpu.get('model'))}\n"
        f"Core fisici: {_testo_o_nd(cpu.get('physical_cores'))}\n"
        f"Processori logici (thread): {_testo_o_nd(cpu.get('logical_processors'))}\n"
        f"Uso CPU: {_percentuale_o_nd(cpu.get('usage_percent'))}\n"
        f"RAM totale: {_bytes_o_nd(memoria.get('total_bytes'))}\n"
        f"RAM disponibile: {_bytes_o_nd(memoria.get('available_bytes'))}\n"
        f"RAM in uso: {_bytes_o_nd(memoria.get('used_bytes'))} "
        f"({_percentuale_o_nd(memoria.get('usage_percent'))})\n"
        f"{righe_gpu}\n"
        f"Acceso da: {uptime_testo}\n"
        f"Python: {data.get('python_version')}"
    )


def _formatta_bytes(valore_bytes: int) -> str:
    """
    Converte un numero di byte in una stringa leggibile (GiB o TiB).

    Solo per il testo del fallback: il contratto dati strutturato
    resta sempre in byte interi.
    """

    tib = valore_bytes / (1024 ** 4)
    if tib >= 1:
        return f"{tib:.2f} TiB"

    gib = valore_bytes / (1024 ** 3)
    return f"{gib:.2f} GiB"


def _fallback_get_disk_usage(risultato_tool: dict) -> str:
    """Fallback deterministico dedicato a get_disk_usage."""

    if risultato_tool.get("status") != "success":
        return (
            risultato_tool.get("error")
            or "Non sono riuscito a leggere lo spazio disco."
        )

    data = risultato_tool.get("data", {})
    total = data.get("total_bytes")
    used = data.get("used_bytes")
    free = data.get("free_bytes")

    if total is None or used is None or free is None:
        return "Non sono riuscito a leggere lo spazio disco."

    return (
        f"Spazio totale: {_formatta_bytes(total)}\n"
        f"Spazio usato: {_formatta_bytes(used)}\n"
        f"Spazio libero: {_formatta_bytes(free)}"
    )


def _fallback_list_processes(risultato_tool: dict) -> str:
    """Fallback deterministico dedicato a list_processes."""

    if risultato_tool.get("status") != "success":
        return (
            risultato_tool.get("error")
            or "Non sono riuscito a leggere l'elenco dei processi."
        )

    data = risultato_tool.get("data")
    data = data if isinstance(data, dict) else {}
    processi = data.get("processes")
    processi = processi if isinstance(processi, list) else []
    filtro = data.get("name_filter")
    totale = _intero_non_negativo(data.get("total"))
    if totale is None:
        totale = len(processi)

    if not processi:
        if filtro:
            return (
                f'Nessun processo corrispondente al filtro "{filtro}" '
                "risulta visibile."
            )
        return "Nessun processo attivo risulta visibile."

    if filtro:
        intestazione = (
            f'Programmi corrispondenti al filtro "{filtro}": {totale}'
        )
    else:
        intestazione = (
            f"Programmi in esecuzione (processi raggruppati per nome): {totale} "
            "(non tutti corrispondono a finestre o app visibili)"
        )

    righe = [intestazione]
    righe.extend(_riga_gruppo_processi(gruppo) for gruppo in processi)

    for etichetta, chiave in (("CPU", "top_cpu"), ("RAM", "top_memory")):
        nome = data.get(chiave)
        if isinstance(nome, str) and nome:
            righe.append(f"Uso di {etichetta} più alto: {nome}")

    if data.get("truncated") is True:
        righe.append(
            f"Elenco parziale: mostrati {len(processi)} programmi su {totale}."
        )

    return "\n".join(righe)


def _formatta_bytes_processo(valore_bytes: int) -> str:
    """RAM di un processo per il solo testo del fallback: MiB sotto 1 GiB, poi GiB/TiB."""

    if valore_bytes < 1024 ** 3:
        return f"{valore_bytes / (1024 ** 2):.0f} MiB"
    return _formatta_bytes(valore_bytes)


def _riga_gruppo_processi(gruppo) -> str:
    """Riga del fallback per un gruppo: metriche solo se valide, mai PID né valori inventati."""

    if not isinstance(gruppo, dict):
        gruppo = {}

    nome = gruppo.get("name")
    if not isinstance(nome, str) or not nome:
        nome = "Processo senza nome"

    istanze = _intero_positivo(gruppo.get("instances"))
    if istanze is None:
        testo_istanze = "istanze non note"
    else:
        testo_istanze = "1 processo" if istanze == 1 else f"{istanze} processi"

    cpu = gruppo.get("cpu_percent")
    memoria = gruppo.get("memory_bytes")
    if (
        _intero_non_negativo(cpu) is not None
        and cpu <= 100
        and _intero_non_negativo(memoria) is not None
    ):
        metriche = f"CPU {cpu}%, RAM {_formatta_bytes_processo(memoria)}"
    elif gruppo.get("metrics") == process_info.PROCESSO_PROTETTO:
        metriche = "CPU e RAM non leggibili (processo protetto dal sistema)"
    else:
        metriche = "CPU e RAM non disponibili"

    return f"- {nome} ({testo_istanze}): {metriche}"


def _riga_volume(volume) -> str:
    """Riga del fallback per una singola unità (GiB/TiB solo nel testo, mai nel dato)."""

    if not isinstance(volume, dict):
        volume = {}

    drive = volume.get("drive")
    if not isinstance(drive, str) or not drive:
        drive = "Unità sconosciuta"

    totale = volume.get("total_bytes")
    libero = volume.get("free_bytes")
    percentuale = volume.get("used_percent")

    leggibile = (
        volume.get("info_available") is True
        and _intero_positivo(totale) is not None
        and _intero_non_negativo(libero) is not None
        and _intero_non_negativo(percentuale) is not None
        and percentuale <= 100
    )
    if not leggibile:
        return f"- {drive} spazio non leggibile"

    return (
        f"- {drive} {_formatta_bytes(totale)} totali, "
        f"{_formatta_bytes(libero)} liberi ({percentuale}% usato)"
    )


def _fallback_list_local_volumes(risultato_tool: dict) -> str:
    """Fallback deterministico dedicato a list_local_volumes."""

    status = risultato_tool.get("status")
    if status == "unsupported_platform":
        return risultato_tool.get("error") or _ERRORE_VOLUMI_NON_SUPPORTATI
    if status != "success":
        return risultato_tool.get("error") or _ERRORE_VOLUMI

    data = risultato_tool.get("data")
    data = data if isinstance(data, dict) else {}
    volumi = data.get("volumes")
    volumi = volumi if isinstance(volumi, list) else []
    totale = _intero_non_negativo(data.get("total"))
    if totale is None:
        totale = len(volumi)

    if not volumi:
        return "Windows non segnala unità locali fisse con lettera."

    righe = [f"Unità locali fisse secondo Windows: {totale}"]
    righe.extend(_riga_volume(volume) for volume in volumi)

    if data.get("truncated") is True:
        righe.append(
            f"Elenco parziale: mostrate {len(volumi)} unità su {totale}."
        )

    return "\n".join(righe)


def fallback_deterministico_sistema(risultato_tool: dict) -> str:
    """
    Router del fallback deterministico per il dominio "system".

    Sceglie il rendering in base a risultato_tool["operation"], senza
    mai fare un dump generico dei dati: ogni tool ha il proprio
    fallback scritto a mano sui propri campi noti.
    """

    operation = risultato_tool.get("operation")

    if operation == "get_system_info":
        return _fallback_get_system_info(risultato_tool)

    if operation == "get_disk_usage":
        return _fallback_get_disk_usage(risultato_tool)

    if operation == "list_processes":
        return _fallback_list_processes(risultato_tool)

    if operation == "list_local_volumes":
        return _fallback_list_local_volumes(risultato_tool)

    if risultato_tool.get("status") != "success":
        return (
            risultato_tool.get("error")
            or "Non sono riuscito a leggere le informazioni di sistema."
        )

    return "Non sono riuscito a generare una risposta per questa operazione di sistema."


def registra_tool_sistema(registro: RegistroStrumenti) -> None:
    """Registra i tool del dominio "system" nel RegistroStrumenti esistente."""

    registro.registra(
        ToolSpec(
            nome="get_system_info",
            schema=TOOLS_SISTEMA[0],
            handler=get_system_info,
            livello="READ_ONLY",
            dominio="system",
        )
    )

    registro.registra(
        ToolSpec(
            nome="get_disk_usage",
            schema=TOOLS_SISTEMA[1],
            handler=get_disk_usage,
            livello="READ_ONLY",
            dominio="system",
        )
    )

    registro.registra(
        ToolSpec(
            nome="list_processes",
            schema=TOOLS_SISTEMA[2],
            handler=list_processes,
            livello="READ_ONLY",
            dominio="system",
        )
    )

    registro.registra(
        ToolSpec(
            nome="list_local_volumes",
            schema=TOOLS_SISTEMA[3],
            handler=list_local_volumes,
            livello="READ_ONLY",
            dominio="system",
        )
    )
