"""Comunicazione e controlli relativi a Ollama."""

import ollama


def controlla_ollama(modello: str) -> None:
    """
    Verifica che Ollama sia raggiungibile e che il modello
    indicato sia disponibile localmente.
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

    for modello_trovato in lista_modelli:
        nome = getattr(modello_trovato, "model", None)

        if nome is None and isinstance(modello_trovato, dict):
            nome = modello_trovato.get("model") or modello_trovato.get("name")

        if nome:
            modelli_installati.append(nome)

    if modello not in modelli_installati:
        raise RuntimeError(
            f"Il modello '{modello}' non risulta installato.\n"
            f"Esegui: ollama pull {modello}"
        )
def avvia_stream(modello: str, messaggi: list[dict[str, str]]):
    """Avvia lo streaming della risposta tramite Ollama."""
    return ollama.chat(
        model=modello,
        messages=messaggi,
        stream=True,
    )