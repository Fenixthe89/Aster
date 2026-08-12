"""Ciclo conversazionale e gestione della cronologia."""

from modules.ollama_manager import avvia_stream


def limita_cronologia(
    messaggi: list[dict[str, str]],
    max_messaggi: int,
) -> None:
    """
    Mantiene sempre il messaggio di sistema e soltanto
    gli ultimi messaggi della conversazione.
    """

    messaggio_sistema = messaggi[0]
    conversazione = messaggi[1:]

    if len(conversazione) > max_messaggi:
        conversazione = conversazione[-max_messaggi:]

    messaggi[:] = [messaggio_sistema, *conversazione]


def avvia_chat(prompt: str, modello: str, max_messaggi: int) -> None:
    """Avvia la conversazione interattiva con Aster."""

    messaggi = [
        {
            "role": "system",
            "content": prompt,
        }
    ]

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
        limita_cronologia(messaggi, max_messaggi)

        print("\nAster: ", end="", flush=True)

        risposta_completa = ""

        try:
            stream = avvia_stream(modello, messaggi)

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

            limita_cronologia(messaggi, max_messaggi)

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