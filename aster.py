from pathlib import Path

import json

import ollama

# ---------------------------------------------------------
# PERCORSI DEL PROGETTO
# ---------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"

# ------------------------------------------------------------
# CARICAMENTO DELLA CONFIGURAZIONE
# ------------------------------------------------------------

def carica_config() -> dict:
    """Legge la configurazione di Aster dal file config.json."""

    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            "Il file config.json non è stato trovato.\n"
            f"Percorso previsto: {CONFIG_FILE}"
        )

    with open(CONFIG_FILE, "r", encoding="utf-8") as file:
        return json.load(file)

# ---------------------------------------------------------
# CARICAMENTO DEL PROMPT
# ---------------------------------------------------------

def carica_prompt() -> str:
    """Legge e controlla il prompt di sistema di Aster."""

    if not PROMPT_FILE.exists():
        raise FileNotFoundError(
            f"Il file prompt.txt non è stato trovato.\n"
            f"Percorso previsto: {PROMPT_FILE}"
        )

    prompt = PROMPT_FILE.read_text(encoding="utf-8").strip()

    if not prompt:
        raise ValueError(
            "Il file prompt.txt esiste, ma è vuoto."
        )

    return prompt


# ---------------------------------------------------------
# CONTROLLO DI OLLAMA
# ---------------------------------------------------------

def controlla_ollama() -> None:
    """
    Verifica che Ollama sia raggiungibile e che il modello
    configurato sia disponibile localmente.
    """

    try:
        risposta = ollama.list()

        # Normalizza il risultato di ollama.list():
        # alcune versioni restituiscono un oggetto,
        # altre possono restituire un dizionario.
        lista_modelli = getattr(risposta, "models", None)

        if lista_modelli is None and isinstance(risposta, dict):
            lista_modelli = risposta.get("models", [])

        if lista_modelli is None:
            lista_modelli = []

    except Exception as errore:
        raise ConnectionError(
            "Impossibile contattare Ollama.\n"
            "Controlla che Ollama sia aperto e in esecuzione."
        ) from errore

    modelli_installati = []

    for modello in lista_modelli:
        nome = getattr(modello, "model", None)

        if nome is None and isinstance(modello, dict):
            nome = modello.get("model") or modello.get("name")

        if nome:
            modelli_installati.append(nome)

    if MODELLO not in modelli_installati:
        raise RuntimeError(
            f"Il modello '{MODELLO}' non risulta installato.\n"
            f"Esegui: ollama pull {MODELLO}"
        )

# ---------------------------------------------------------
# GESTIONE DELLA CRONOLOGIA
# ---------------------------------------------------------

def limita_cronologia(messaggi: list[dict[str, str]]) -> None:
    """
    Mantiene sempre il messaggio di sistema e soltanto
    gli ultimi messaggi della conversazione.
    """

    messaggio_sistema = messaggi[0]
    conversazione = messaggi[1:]

    if len(conversazione) > MAX_MESSAGGI:
        conversazione = conversazione[-MAX_MESSAGGI:]

    messaggi[:] = [messaggio_sistema, *conversazione]


# ---------------------------------------------------------
# INTERFACCIA
# ---------------------------------------------------------

def stampa_banner() -> None:
    """Mostra le informazioni iniziali di Aster."""

    print("=" * 55)
    print(f"{NOME_ASSISTENTE} CYBER AGENT v{VERSIONE}")
    print(f"Modello: {MODELLO}")
    print("Bentornato, Sem.")
    print("Scrivi 'esci' per terminare.")
    print("=" * 55)


# ---------------------------------------------------------
# CHAT
# ---------------------------------------------------------

def avvia_chat(prompt: str) -> None:
    """Avvia la conversazione interattiva con Aster."""

    messaggi = [
        {
            "role": "system",
            "content": prompt,
        }
    ]

    stampa_banner()

    while True:
        try:
            domanda = input("\nTu: ").strip()

        except KeyboardInterrupt:
            print("\n\nAster: Sessione interrotta. A presto, Sem.")
            break

        if not domanda:
            continue

        if domanda.lower() in {"esci", "exit", "quit"}:
            print("\nAster: Sessione terminata.")
            break

        messaggio_utente = {
            "role": "user",
            "content": domanda,
        }

        messaggi.append(messaggio_utente)
        limita_cronologia(messaggi)

        print("\nAster: ", end="", flush=True)

        risposta_completa = ""

        try:
            stream = ollama.chat(
                model=MODELLO,
                messages=messaggi,
                stream=True,
            )

            for parte in stream:
                testo = parte["message"]["content"]
                risposta_completa += testo
                print(testo, end="", flush=True)

            print()

            messaggi.append(
                {
                    "role": "assistant",
                    "content": risposta_completa,
                }
            )

            limita_cronologia(messaggi)

        except KeyboardInterrupt:
            print("\n\nGenerazione interrotta.")

            # La domanda non ha ricevuto una risposta completa:
            # la rimuoviamo per mantenere coerente la cronologia.
            if messaggi and messaggi[-1] is messaggio_utente:
                messaggi.pop()

        except Exception as errore:
            print(f"\nErrore nella comunicazione con Ollama: {errore}")

            # Evita di lasciare nella cronologia un messaggio user
            # senza la relativa risposta assistant.
            if messaggi and messaggi[-1] is messaggio_utente:
                messaggi.pop()


# ---------------------------------------------------------
# AVVIO DEL PROGRAMMA
# ---------------------------------------------------------

def main() -> None:
    """Prepara Aster ed entra nella chat."""

    global NOME_ASSISTENTE
    global VERSIONE
    global MODELLO
    global MAX_MESSAGGI
    global PROMPT_FILE

    try:
        config = carica_config()

        NOME_ASSISTENTE = config["assistant"]["name"]
        VERSIONE = config["assistant"]["version"]

        MODELLO = config["ollama"]["model"]
        
	MAX_MESSAGGI = config["chat"]["history_limit"]
	if not isinstance(MAX_MESSAGGI, int):
	raise TypeError(
        "Il campo 'chat.history_limit' in config.json deve essere un numero intero."
    )
     
   PROMPT_FILE = BASE_DIR / config["files"]["prompt"]

        prompt = carica_prompt()

        print("Controllo connessione con Ollama...")
        controlla_ollama()

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

    avvia_chat(prompt)

if __name__ == "__main__":
    main()