"""Pipeline post-tool generica: nessuna conoscenza di alcun dominio (memoria inclusa)."""

import re
from typing import Callable

from modules.ollama_manager import esegui_risposta_finale


def pulisci_testo_modello(testo: str) -> str:
    """
    Rimuove eventuale reasoning <think> sfuggito
    dentro message.content.
    """

    if not testo:
        return ""

    pulito = re.sub(
        r"<think\b[^>]*>.*?</think\s*>",
        "",
        testo,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # Caso difensivo osservato con alcuni modelli:
    # reasoning senza tag iniziale ma con </think>.
    minuscolo = pulito.casefold()
    chiusura = "</think>"

    if chiusura in minuscolo:
        posizione = minuscolo.rfind(chiusura)

        pulito = pulito[
            posizione + len(chiusura):
        ]

    # Se rimane un <think> aperto senza chiusura,
    # non mostriamo ciò che segue.
    apertura = re.search(
        r"<think\b[^>]*>",
        pulito,
        flags=re.IGNORECASE,
    )

    if apertura is not None:
        pulito = pulito[:apertura.start()]

    return pulito.strip()


def raccogli_risposta_finale(stream) -> str:
    """
    Bufferizza completamente la risposta finale
    prima di mostrarla.
    """

    parti = []

    for parte in stream:
        contenuto = parte.message.content or ""

        if contenuto:
            parti.append(contenuto)

    return pulisci_testo_modello(
        "".join(parti)
    )


def genera_risposta_post_tool(
    *,
    modello: str,
    messaggi: list,
    host_ollama: str,
    timeout_ollama: float,
    num_ctx: int = 8192,
    risultato_tool: dict,
    salta_secondo_giro: bool,
    fallback_deterministico: Callable[[dict], str],
) -> str:
    """
    Pipeline post-tool generica.

    Non conosce alcun dominio: risultato_tool e' opaco, e le decisioni
    di dominio (saltare il secondo giro, quale fallback usare) sono
    prese dal chiamante. Prova il secondo giro Ollama al massimo una
    volta; se viene saltato o se fallisce, delega interamente al
    fallback ricevuto, senza mai rieseguire il tool.
    """

    if salta_secondo_giro:
        return fallback_deterministico(risultato_tool)

    try:
        stream = esegui_risposta_finale(
            modello,
            messaggi,
            host_ollama,
            timeout_ollama,
            num_ctx,
        )

        return raccogli_risposta_finale(stream)

    except Exception:
        return fallback_deterministico(risultato_tool)
