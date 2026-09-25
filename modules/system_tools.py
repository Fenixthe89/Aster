"""Tool non-memory di Aster: informazioni di sistema reali (dominio "system")."""

import os
import platform
import shutil
import unicodedata
from pathlib import Path

from modules.tool_registry import RegistroStrumenti, ToolSpec

# Import protetto: se psutil manca, Aster parte comunque e solo
# list_processes risponde con un tool_error fisso.
try:
    import psutil
except ImportError:
    psutil = None

# Limite di processi restituiti da list_processes: il risultato entra
# in role="tool" e resta in cronologia, quindi va tenuto contenuto.
MAX_PROCESS_ENTRIES = 50

_MAX_LUNGHEZZA_FILTRO = 64
_MAX_LUNGHEZZA_NOME_PROCESSO = 255

_ERRORE_PSUTIL_ASSENTE = "Elenco processi non disponibile su questa installazione."
_ERRORE_ENUMERAZIONE = "Non sono riuscito a leggere l'elenco dei processi."
_ERRORE_FILTRO_NON_VALIDO = "Filtro nome non valido."

TOOLS_SISTEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_system_info",
            "description": (
                "Restituisce informazioni tecniche reali sul computer locale "
                "su cui Aster sta girando: sistema operativo, release, "
                "architettura, numero di CPU logiche, versione di Python e "
                "processore. Non richiede parametri."
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
                "Restituisce lo spazio totale, usato e libero (in byte) del "
                "filesystem che contiene realmente l'installazione di Aster. "
                "Non riguarda tutti i dischi del sistema, né una cartella "
                "specifica, né un drive scelto dall'utente. Non richiede "
                "parametri."
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
                "Elenca i processi attualmente in esecuzione sul computer "
                "locale (solo nome e PID). Sola osservazione: non avvia, "
                "chiude né modifica processi. Un processo non corrisponde "
                "necessariamente a una finestra visibile."
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
                },
                "required": [],
            },
        },
    },
]


def get_system_info(argomenti: dict, contesto) -> dict:
    """
    Handler del tool get_system_info.

    Legge informazioni di sistema reali usando solo platform/os della
    standard library. Non richiede parametri e non usa alcuno stato
    esterno: argomenti e contesto vengono ignorati esplicitamente,
    nessun side effect. cpu_count=None e processor="" sono esiti validi
    della piattaforma (non errori) e vengono restituiti cosi' come sono.
    """

    try:
        data = {
            "os_name": platform.system(),
            "os_release": platform.release(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "processor": platform.processor(),
        }
    except Exception as errore:
        return {
            "ok": False,
            "operation": "get_system_info",
            "status": "tool_error",
            "error": str(errore),
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
    except Exception as errore:
        return {
            "ok": False,
            "operation": "get_disk_usage",
            "status": "tool_error",
            "error": str(errore),
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
    nessun filtro (filtro=None). Tipo non stringa, caratteri di
    controllo o lunghezza oltre _MAX_LUNGHEZZA_FILTRO dopo strip()
    rendono il filtro non valido. Il filtro e' solo testo per un
    confronto Python per sottostringa: nessuna regex, wildcard o shell.
    """

    if valore is None:
        return True, None

    if not isinstance(valore, str):
        return False, None

    if _contiene_caratteri_controllo(valore):
        return False, None

    filtro = valore.strip()

    if not filtro:
        return True, None

    if len(filtro) > _MAX_LUNGHEZZA_FILTRO:
        return False, None

    return True, filtro


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
    Ordina per (name.casefold(), pid) PRIMA di troncare a
    MAX_PROCESS_ENTRIES; total conta tutti i processi validi che
    corrispondono al filtro. Nessuna deduplicazione per nome.
    """

    argomenti = argomenti if isinstance(argomenti, dict) else {}

    valido, filtro = _normalizza_filtro_nome(argomenti.get("name"))
    if not valido:
        return _errore_list_processes(_ERRORE_FILTRO_NON_VALIDO)

    if psutil is None:
        return _errore_list_processes(_ERRORE_PSUTIL_ASSENTE)

    filtro_casefold = filtro.casefold() if filtro is not None else None
    processi = []

    try:
        for processo in psutil.process_iter(["pid", "name"]):
            try:
                info = processo.info
            except (
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                psutil.ZombieProcess,
            ):
                continue

            entry = _entry_processo_valida(info)
            if entry is None:
                continue

            if (
                filtro_casefold is not None
                and filtro_casefold not in entry["name"].casefold()
            ):
                continue

            processi.append(entry)
    except Exception:
        return _errore_list_processes(_ERRORE_ENUMERAZIONE)

    processi.sort(key=lambda entry: (entry["name"].casefold(), entry["pid"]))
    totale = len(processi)

    return {
        "ok": True,
        "operation": "list_processes",
        "status": "success",
        "data": {
            "processes": processi[:MAX_PROCESS_ENTRIES],
            "name_filter": filtro,
            "total": totale,
            "truncated": totale > MAX_PROCESS_ENTRIES,
        },
    }


def _fallback_get_system_info(risultato_tool: dict) -> str:
    """Fallback deterministico dedicato a get_system_info (comportamento invariato)."""

    if risultato_tool.get("status") != "success":
        return (
            risultato_tool.get("error")
            or "Non sono riuscito a leggere le informazioni di sistema."
        )

    data = risultato_tool.get("data", {})

    cpu_count = data.get("cpu_count")
    cpu_testo = str(cpu_count) if cpu_count is not None else "non disponibile"

    processor = data.get("processor")
    processor_testo = processor if processor else "non disponibile"

    return (
        f"Sistema operativo: {data.get('os_name')} {data.get('os_release')}\n"
        f"Architettura: {data.get('machine')}\n"
        f"CPU logiche: {cpu_testo}\n"
        f"Processore: {processor_testo}\n"
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

    data = risultato_tool.get("data", {})
    processi = data.get("processes") or []
    filtro = data.get("name_filter")
    totale = data.get("total", len(processi))

    if not processi:
        if filtro:
            return (
                f'Nessun processo corrispondente al filtro "{filtro}" '
                "risulta visibile."
            )
        return "Nessun processo attivo risulta visibile."

    if filtro:
        intestazione = (
            f'Processi attivi corrispondenti al filtro "{filtro}": {totale}'
        )
    else:
        intestazione = (
            f"Processi attivi osservabili: {totale} "
            "(non tutti corrispondono a finestre o app visibili)"
        )

    righe = [intestazione]
    righe.extend(
        f"- {processo.get('name')} (PID {processo.get('pid')})"
        for processo in processi
    )

    if data.get("truncated"):
        righe.append(
            f"Elenco parziale: mostrati {len(processi)} processi su {totale}."
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
