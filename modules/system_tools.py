"""Primo tool non-memory di Aster: informazioni di sistema reali (dominio "system")."""

import os
import platform

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


def fallback_deterministico_sistema(risultato_tool: dict) -> str:
    """
    Fallback deterministico per il dominio "system" quando il secondo
    giro Ollama viene saltato o fallisce.

    Non fa json.dumps(data): costruisce una risposta leggibile solo dai
    campi noti di get_system_info, senza inventare valori mancanti.
    """

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


def registra_tool_sistema(registro: RegistroStrumenti) -> None:
    """Registra get_system_info nel RegistroStrumenti esistente (dominio "system")."""

    registro.registra(
        ToolSpec(
            nome="get_system_info",
            schema=TOOLS_SISTEMA[0],
            handler=get_system_info,
            livello="READ_ONLY",
            dominio="system",
        )
    )
