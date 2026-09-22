"""Ciclo conversazionale e gestione della cronologia."""
import json
import re
from pathlib import Path

from modules.memory import StatoMemoria
from modules.memory_query import estrai_query_da_testo
from modules.memory_session import MemorySessionState
from modules.memory_tools import (
    TOOLS_MEMORIA,
    esegui_tool_memoria,
)
from modules.ollama_manager import (
    esegui_risposta_finale,
    esegui_turno_con_tools,
)

TOOLS_RICERCA_MEMORIA = [
    tool
    for tool in TOOLS_MEMORIA
    if tool["function"]["name"] == "cerca_memoria"
]


def sembra_domanda_memoria(domanda: str) -> bool:
    """
    Riconosce in modo conservativo domande che potrebbero
    riguardare informazioni personali già memorizzate.
    """

    testo = " ".join(
        domanda.casefold().split()
    )

    indicatori_forti = (
        "ti ricordi",
        "ricordi che",
        "memoria",
        "quel ricordo",
        "che id ha",
        "quale id",
        "cosa avevo deciso",
        "cosa avevamo deciso",
        "avevo scelto",
        "avevamo scelto",
    )

    if any(
        indicatore in testo
        for indicatore in indicatori_forti
    ):
        return True

    interrogativi = (
        "che ",
        "quale ",
        "qual ",
        "cosa ",
    )

    indicatori_personali = (
        " mio ",
        " mia ",
        " miei ",
        " mie ",
        " preferisco ",
        " progetto aster",
        " ho deciso ",
        " abbiamo deciso ",
    )

    testo_spaziato = f" {testo} "

    return (
        testo.startswith(interrogativi)
        and any(
            indicatore in testo_spaziato
            for indicatore in indicatori_personali
        )
    )

def sembra_eliminazione_memoria_senza_id(domanda: str) -> bool:
    """
    Riconosce una richiesta esplicita di eliminazione
    di un ricordo quando l'utente non fornisce un ID.
    """

    testo = " ".join(
        domanda.casefold().split()
    )

    verbi_eliminazione = (
        "elimina",
        "eliminare",
        "cancella",
        "cancellare",
    )

    riferimenti_memoria = (
        "ricordo",
        "ricordi",
        "memoria",
    )

    ha_verbo = any(
        verbo in testo
        for verbo in verbi_eliminazione
    )

    parla_di_memoria = any(
        riferimento in testo
        for riferimento in riferimenti_memoria
    )

    ha_id_esplicito = re.search(
        r"\bid\s*[:#]?\s*\d+\b",
        testo,
    ) is not None

    return (
        ha_verbo
        and parla_di_memoria
        and not ha_id_esplicito
    )

STOPWORD_RECALL_MEMORIA = {
    "che",
    "chi",
    "cosa",
    "come",
    "dove",
    "quando",
    "quale",
    "quali",
    "qual",
    "uso",
    "usi",
    "usa",
    "usare",
    "per",
    "con",
    "del",
    "della",
    "dei",
    "delle",
    "nel",
    "nella",
    "nei",
    "nelle",
    "il",
    "lo",
    "la",
    "i",
    "gli",
    "le",
    "un",
    "uno",
    "una",
    "mio",
    "mia",
    "miei",
    "mie",
}

def genera_query_memoria(domanda: str) -> list[str]:
    """
    Estrae query semplici e conservative dalla domanda
    per il fallback della ricerca memoria.
    """

    return estrai_query_da_testo(domanda, STOPWORD_RECALL_MEMORIA)

def cerca_memoria_fallback(
    domanda: str,
    stato_memoria: StatoMemoria,
    stato_sessione: MemorySessionState,
    percorso_memoria: Path,
    limite_ricerca: int,
) -> dict | None:
    """
    Prova query progressivamente più semplici finché
    trova almeno un ricordo pertinente.
    """

    for query in genera_query_memoria(domanda):
        risultato = esegui_tool_memoria(
            nome_tool="cerca_memoria",
            argomenti={"query": query},
            stato_memoria=stato_memoria,
            stato_sessione=stato_sessione,
            percorso_memoria=percorso_memoria,
            limite_ricerca=limite_ricerca,
        )

        if (
            risultato.get("status") == "searched"
            and risultato.get("returned", 0) > 0
        ):
            return risultato

    return None

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

def genera_risposta_deterministica_memoria(
    risultato_tool: dict,
) -> str:
    """
    Genera una risposta locale quando il tool memoria è già stato
    eseguito ma Ollama non riesce a produrre la risposta finale.
    """

    status = risultato_tool.get("status")
    memory_id = risultato_tool.get("memory_id")
    content = risultato_tool.get("content")
    error = risultato_tool.get("error")

    if status == "created":
        return (
            f"Ho salvato il ricordo con ID {memory_id}: "
            f'"{content}"'
        )

    if status == "updated":
        return (
            f"Ho aggiornato il ricordo con ID {memory_id}: "
            f'"{content}"'
        )

    if status == "deleted":
        return (
            f"Il ricordo con ID {memory_id} è stato "
            "spostato nel cestino."
        )

    if status == "restored":
        return (
            f"Il ricordo con ID {memory_id} è stato "
            "ripristinato."
        )

    if status == "pending_confirmation":
        before = risultato_tool.get("before")
        after = risultato_tool.get("after")

        if before is not None and after is not None:
            return (
                "Modifica in attesa di conferma:\n\n"
                f"PRIMA: {before}\n"
                f"DOPO: {after}\n\n"
                "Confermi?"
            )

        if content is not None:
            return (
                f'Operazione in attesa sul ricordo '
                f'ID {memory_id}: "{content}"\n\n'
                "Confermi?"
            )

        return "L'operazione è in attesa di conferma. Confermi?"

    if status == "pending_selection":
        candidati = risultato_tool.get("candidates", [])

        righe = [
            "Ho trovato più ricordi compatibili:",
            "",
        ]

        for candidato in candidati:
            righe.append(
                f'ID {candidato.get("id")}: '
                f'"{candidato.get("content")}"'
            )

        righe.extend([
            "",
            "Quale ID vuoi selezionare?",
        ])

        return "\n".join(righe)

    if status == "blocked_readonly":
        return "La memoria è disponibile solo in lettura."

    if status == "blocked_disabled":
        return "La memoria persistente è disabilitata."

    if status == "blocked_sensitive":
        return (
            "Non ho salvato questo contenuto perché sembra "
            "includere un dato sensibile."
        )

    if status == "not_found":
        return error or "Non ho trovato il ricordo richiesto."

    if status == "duplicate_detected":
        return (
            f"Esiste già un ricordo equivalente"
            + (
                f" con ID {memory_id}."
                if memory_id is not None
                else "."
            )
        )

    if status == "conflict":
        return (
            "Esiste già un'operazione di memoria in attesa. "
            "Confermala o annullala prima di iniziarne un'altra."
        )

    if status == "cancelled":
        return "Operazione di memoria annullata."

    if status in {"validation_error", "tool_error"}:
        return error or "Si è verificato un errore nella memoria."

    if status == "searched":
        risultati = risultato_tool.get("results", [])

        if not risultati:
            return "Non ho trovato ricordi pertinenti."

        righe = ["Ho recuperato questi ricordi:", ""]

        for ricordo in risultati:
            righe.append(
                f'ID {ricordo.get("id")}: '
                f'"{ricordo.get("content")}"'
            )

        return "\n".join(righe)

    return (
        "L'operazione di memoria è stata elaborata, "
        "ma non riesco a generare la risposta finale."
    )

def genera_risposta_finale_memoria(
    *,
    modello: str,
    messaggi: list,
    host_ollama: str,
    timeout_ollama: float,
    risultato_tool: dict,
) -> str:
    """
    Prova il secondo passaggio LLM.
    Se Ollama fallisce, usa il risultato Python già ottenuto.
    """

    try:
        stream = esegui_risposta_finale(
            modello,
            messaggi,
            host_ollama,
            timeout_ollama,
        )

        return raccogli_risposta_finale(
            stream
        )

    except Exception:
        return genera_risposta_deterministica_memoria(
            risultato_tool
        )

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


def avvia_chat(
    prompt: str,
    modello: str,
    max_messaggi: int,
    host_ollama: str,
    timeout_ollama: float,
    stato_memoria: StatoMemoria,
    percorso_memoria: Path,
    limite_ricerca: int,
) -> None:
    """Avvia la conversazione interattiva con Aster."""

    messaggi = [
        {
            "role": "system",
            "content": prompt,
        }
    ]

    stato_sessione = MemorySessionState()

    while True:
        try:
            domanda = input("\nTu: ").strip()

        except (KeyboardInterrupt, EOFError):
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

        try:
            risposta = esegui_turno_con_tools(
                modello,
                messaggi,
                TOOLS_MEMORIA,
                host_ollama,
                timeout_ollama,
            )

            tool_calls = risposta.message.tool_calls or []

            if len(tool_calls) > 1:
                risposta_completa = (
                    "Posso gestire una sola operazione "
                    "di memoria per volta. "
                    "Indicami quale vuoi eseguire per prima."
                )

                print(
                    f"\nAster: {risposta_completa}"
                )

                messaggi.append(
                    {
                        "role": "assistant",
                        "content": risposta_completa,
                    }
                )

                limita_cronologia(
                    messaggi,
                    max_messaggi,
                )

                continue

            # -------------------------------------------------
            # NESSUN TOOL RICHIESTO
            # -------------------------------------------------

            if not tool_calls:

                if sembra_eliminazione_memoria_senza_id(domanda):
                    risultato_memoria = esegui_tool_memoria(
                        nome_tool="elimina_memoria_per_query",
                        argomenti={"query": domanda},
                        stato_memoria=stato_memoria,
                        stato_sessione=stato_sessione,
                        percorso_memoria=percorso_memoria,
                        limite_ricerca=limite_ricerca,
                    )

                    messaggi_memoria = list(messaggi)

                    messaggi_memoria.append(
                        {
                            "role": "system",
                            "content": (
                                "RISULTATO OPERAZIONE MEMORIA "
                                "ESEGUITA DA PYTHON:\n"
                                + json.dumps(
                                    risultato_memoria,
                                    ensure_ascii=False,
                                )
                                + "\n"
                                "Python è la fonte di verità. "
                                "Rispondi esclusivamente in base "
                                "a questo risultato."
                            ),
                        }
                    )

                    risposta_completa = genera_risposta_finale_memoria(
                        modello=modello,
                        messaggi=messaggi_memoria,
                        host_ollama=host_ollama,
                        timeout_ollama=timeout_ollama,
                        risultato_tool=risultato_memoria,
                    )

                    print(
                        f"\nAster: {risposta_completa}"
                    )

                    messaggi.append(
                        {
                            "role": "assistant",
                            "content": risposta_completa,
                        }
                    )

                    limita_cronologia(
                        messaggi,
                        max_messaggi,
                    )

                    continue

                if sembra_domanda_memoria(domanda):
                    risultato_memoria = cerca_memoria_fallback(
                        domanda,
                        stato_memoria,
                        stato_sessione,
                        percorso_memoria,
                        limite_ricerca,
                    )

                    if risultato_memoria is not None:
                        messaggi_memoria = list(messaggi)

                        messaggi_memoria.append(
                            {
                                "role": "system",
                                "content": (
                                    "RISULTATO MEMORIA PERSISTENTE "
                                    "RECUPERATO DA PYTHON:\n"
                                    + json.dumps(
                                        risultato_memoria,
                                        ensure_ascii=False,
                                    )
                                    + "\n"
                                    "Usa esclusivamente questi dati "
                                    "per rispondere alla domanda corrente. "
                                    "Python è la fonte di verità. "
                                    "Non inventare informazioni mancanti."
                                ),
                            }
                        )

                        risposta_completa = genera_risposta_finale_memoria(
                            modello=modello,
                            messaggi=messaggi_memoria,
                            host_ollama=host_ollama,
                            timeout_ollama=timeout_ollama,
                            risultato_tool=risultato_memoria,
                        )

                        print(
                            f"\nAster: {risposta_completa}"
                        )

                        messaggi.append(
                            {
                                "role": "assistant",
                                "content": risposta_completa,
                            }
                        )

                        limita_cronologia(
                            messaggi,
                            max_messaggi,
                        )

                        continue
                risposta_completa = pulisci_testo_modello(
                    risposta.message.content or ""
                )

                print(
                        f"\nAster: {risposta_completa}"
                    )

                messaggi.append(
                        {
                            "role": "assistant",
                            "content": risposta_completa,
                        }
                    )

                limita_cronologia(
                        messaggi,
                        max_messaggi,
                    )

                continue

            # -------------------------------------------------
            # TOOL RICHIESTO DAL MODELLO
            # -------------------------------------------------

            chiamata = tool_calls[0]

            nome_tool = chiamata.function.name
            argomenti = chiamata.function.arguments

            if isinstance(argomenti, str):
                argomenti = json.loads(argomenti)

            # Conserviamo il messaggio assistant contenente
            # la tool call nella cronologia.
            messaggi.append(
                risposta.message
            )

            risultato_tool = esegui_tool_memoria(
                nome_tool=nome_tool,
                argomenti=argomenti,
                stato_memoria=stato_memoria,
                stato_sessione=stato_sessione,
                percorso_memoria=percorso_memoria,
                limite_ricerca=limite_ricerca,
            )
            if (
                nome_tool == "cerca_memoria"
                and risultato_tool.get("status") == "searched"
                and risultato_tool.get("returned", 0) == 0
                and sembra_domanda_memoria(domanda)
            ):
                risultato_fallback = cerca_memoria_fallback(
                    domanda,
                    stato_memoria,
                    stato_sessione,
                    percorso_memoria,
                    limite_ricerca,
                )

                if risultato_fallback is not None:
                    risultato_tool = risultato_fallback

            # Python è la fonte di verità.
            # Il modello riceve esattamente il risultato
            # dell'operazione realmente eseguita.
            messaggi.append(
                {
                    "role": "tool",
                    "content": json.dumps(
                        risultato_tool,
                        ensure_ascii=False,
                    ),
                }
            )

            # -------------------------------------------------
            # RISPOSTA FINALE DOPO IL TOOL
            # -------------------------------------------------

            if risultato_tool.get("status") == "blocked_sensitive":
                # Contenuto potenzialmente sensibile: nessun secondo
                # giro Ollama, per non fargli mai vedere/ripetere il
                # valore rilevato. Risposta locale deterministica.
                risposta_completa = genera_risposta_deterministica_memoria(
                    risultato_tool
                )
            else:
                try:
                    risposta_completa = genera_risposta_finale_memoria(
                        modello=modello,
                        messaggi=messaggi,
                        host_ollama=host_ollama,
                        timeout_ollama=timeout_ollama,
                        risultato_tool=risultato_tool,
                    )

                except Exception:
                    risposta_completa = (
                        genera_risposta_deterministica_memoria(
                            risultato_tool
                        )
                    )

            print(
                f"\nAster: {risposta_completa}"
            )

            messaggi.append(
                {
                    "role": "assistant",
                    "content": risposta_completa,
                }
            )

            limita_cronologia(
                messaggi,
                max_messaggi,
            )

        except KeyboardInterrupt:
            print(
                "\n\nGenerazione interrotta."
            )

            if (
                messaggi
                and messaggi[-1]
                is messaggio_utente
            ):
                messaggi.pop()

        except Exception as errore:
            print(
                "\nErrore nella comunicazione "
                f"con Ollama: {errore}"
            )

            if (
                messaggi
                and messaggi[-1]
                is messaggio_utente
            ):
                messaggi.pop()