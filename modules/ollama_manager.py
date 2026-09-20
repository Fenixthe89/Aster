"""Comunicazione e controlli relativi a Ollama."""

import ollama


def crea_client(host: str, timeout: float = 60) -> ollama.Client:
    """Crea un client Ollama usando l'endpoint configurato."""

    if not isinstance(host, str) or not host.strip():
        raise ValueError(
            "L'host di Ollama deve essere una stringa non vuota."
        )

    return ollama.Client(
        host=host.strip(),
        timeout=timeout,
    )


def controlla_ollama(
    modello: str,
    host: str,
    timeout: float = 60,
) -> None:
    """
    Verifica che Ollama sia raggiungibile e che il modello
    indicato sia disponibile.
    """

    client = crea_client(host, timeout)

    try:
        risposta = client.list()

        lista_modelli = getattr(
            risposta,
            "models",
            None,
        )

        if lista_modelli is None and isinstance(risposta, dict):
            lista_modelli = risposta.get(
                "models",
                [],
            )

        if lista_modelli is None:
            lista_modelli = []

    except Exception as errore:
        raise ConnectionError(
            "Impossibile contattare Ollama.\n"
            "Controlla che il server Ollama sia raggiungibile."
        ) from errore

    modelli_installati = []

    for modello_trovato in lista_modelli:
        nome = getattr(
            modello_trovato,
            "model",
            None,
        )

        if nome is None and isinstance(
            modello_trovato,
            dict,
        ):
            nome = (
                modello_trovato.get("model")
                or modello_trovato.get("name")
            )

        if nome:
            modelli_installati.append(nome)

    if modello not in modelli_installati:
        raise RuntimeError(
            f"Il modello '{modello}' non risulta disponibile.\n"
            f"Esegui sul nodo Ollama: ollama pull {modello}"
        )

def avvia_stream(
    modello: str,
    messaggi: list[dict],
    host: str = "http://localhost:11434",
    timeout: float = 60,
):
    """Avvia lo streaming normale della risposta."""

    client = crea_client(host, timeout)

    return client.chat(
        model=modello,
        messages=messaggi,
        stream=True,
        think=False,
    )


def esegui_turno_con_tools(
    modello: str,
    messaggi: list[dict],
    tools: list[dict],
    host: str,
    timeout: float = 60,
):
    """
    Esegue il primo giro LLM con tool disponibili.

    Il turno non usa streaming:
    Python riceve prima l'intera risposta e soltanto dopo
    decide se gestire testo normale oppure tool call.
    """

    client = crea_client(host, timeout)

    return client.chat(
        model=modello,
        messages=messaggi,
        tools=tools,
        stream=False,
        think=False,
    )


def esegui_risposta_finale(
    modello: str,
    messaggi: list[dict],
    host: str,
    timeout: float = 60,
):
    """
    Genera la risposta finale dopo l'esecuzione di un tool.

    In questa fase non vengono esposti nuovi tool.
    """

    client = crea_client(host, timeout)

    return client.chat(
        model=modello,
        messages=messaggi,
        stream=True,
        think=False,
    )