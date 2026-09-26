import json

from modules.config import ErroreConfig, carica_config_runtime

from modules.prompt import carica_prompt

from modules.ollama_manager import controlla_ollama

from modules.ui import stampa_banner

from modules.chat import avvia_chat

from modules.file_tools import prepara_contesto_filesystem

from modules.memory import inizializza_memoria

from modules.runtime_paths import percorsi_runtime

# ---------------------------------------------------------
# AVVIO DEL PROGRAMMA
# ---------------------------------------------------------

def main() -> None:
    """Prepara Aster ed entra nella chat."""

    try:
        # Percorsi di config, prompt e memoria: gestiti centralmente da
        # runtime_paths (da sorgente prompt e memoria identici alla v0.6.8).
        percorsi = percorsi_runtime()

        # Unica lettura della configurazione: default app-owned più
        # eventuale override utente, validati e uniti per whitelist.
        config = carica_config_runtime(
            percorsi.default_config_file,
            percorsi.user_config_file,
            percorsi.legacy_config_file,
        )

        nome_assistente = config["assistant"]["name"]
        versione = config["assistant"]["version"]

        modello = config["ollama"]["model"]
        max_messaggi = config["chat"]["history_limit"]

        host_ollama = config["ollama"]["host"]
        timeout_ollama = config["ollama"]["timeout"]
        num_ctx = config["ollama"]["num_ctx"]
        limite_ricerca = config["memory"]["search_max_results"]

        # Le root filesystem relative (es. "./workspace") si risolvono
        # rispetto ad app_root, mai rispetto a cwd, data_root o
        # resource_root.
        contesto_filesystem = prepara_contesto_filesystem(
            config,
            percorsi.app_root,
        )

        prompt = carica_prompt(percorsi.prompt_file)

        percorso_memoria = percorsi.memory_file
        stato_memoria = inizializza_memoria(percorso_memoria)

        print("Controllo connessione con Ollama...")
        controlla_ollama(
        modello,
        host_ollama,
        timeout_ollama,
    )
    except (
        ErroreConfig,
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
        timeout_ollama,
        num_ctx,
        stato_memoria,
        percorso_memoria,
        limite_ricerca,
        contesto_filesystem,
)

if __name__ == "__main__":
    main()