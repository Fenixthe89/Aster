from pathlib import Path

import json

from modules.config import carica_config

from modules.prompt import carica_prompt

from modules.ollama_manager import controlla_ollama

from modules.ui import stampa_banner

from modules.chat import avvia_chat

from modules.memory import inizializza_memoria

# ---------------------------------------------------------
# PERCORSI DEL PROGETTO
# ---------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"

# ---------------------------------------------------------
# AVVIO DEL PROGRAMMA
# ---------------------------------------------------------

def main() -> None:
    """Prepara Aster ed entra nella chat."""

    try:
        config = carica_config(CONFIG_FILE)

        nome_assistente = config["assistant"]["name"]
        versione = config["assistant"]["version"]

        modello = config["ollama"]["model"]
        max_messaggi = config["chat"]["history_limit"]

        host_ollama = config["ollama"]["host"]
        limite_ricerca = config["memory"]["search_max_results"]

        percorso_prompt = BASE_DIR / config["files"]["prompt"]
        prompt = carica_prompt(percorso_prompt)

        percorso_memoria = BASE_DIR / config["files"]["memory"]
        stato_memoria = inizializza_memoria(percorso_memoria)

        print("Controllo connessione con Ollama...")
        controlla_ollama(
        modello,
        host_ollama,
    )
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        ConnectionError,
        RuntimeError,
    ) as errore:
        print("\nImpossibile avviare Aster.")
        print(errore)
        return

    stampa_banner(nome_assistente, versione, modello)

    if stato_memoria.messaggio:
        print(f"\nMemoria: {stato_memoria.messaggio}")

    avvia_chat(
        prompt,
        modello,
        max_messaggi,
        host_ollama,
        stato_memoria,
        percorso_memoria,
        limite_ricerca,
)

if __name__ == "__main__":
    main()