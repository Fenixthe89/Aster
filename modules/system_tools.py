"""Tool non-memory di Aster: informazioni di sistema reali (dominio "system")."""

import os
import platform
import shutil
from pathlib import Path

from modules.tool_registry import RegistroStrumenti, ToolSpec

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
