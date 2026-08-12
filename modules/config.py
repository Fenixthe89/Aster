"""Caricamento e validazione della configurazione di Aster."""

import json
from pathlib import Path


def carica_config(config_file: Path) -> dict:
    """
    Legge la configurazione di Aster dal file indicato.

    Il percorso arriva come parametro perché il modulo non conosce
    la posizione del progetto: quella la determina aster.py.
    """

    if not config_file.exists():
        raise FileNotFoundError(
            "Il file config.json non è stato trovato.\n"
            f"Percorso previsto: {config_file}"
        )

    with open(config_file, "r", encoding="utf-8") as file:
        config = json.load(file)

    # Validazione introdotta nella v0.3.1: un history_limit di tipo
    # errato non causerebbe un errore all'avvio, ma solo durante la
    # conversazione, dentro limita_cronologia().
    if not isinstance(config["chat"]["history_limit"], int):
        raise TypeError(
            "Il campo 'chat.history_limit' in config.json deve essere "
            "un numero intero."
        )

    return config