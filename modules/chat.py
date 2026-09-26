"""Ciclo conversazionale e gestione della cronologia."""
import json
import re
from pathlib import Path

from modules.config import carica_config
from modules.file_tools import (
    fallback_deterministico_file,
    prepara_contesto_filesystem,
    registra_tool_filesystem,
)
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
from modules.runtime_paths import percorsi_runtime
from modules.system_tools import (
    fallback_deterministico_sistema,
    registra_tool_sistema,
)
from modules.tool_registry import ContestoMemoria, crea_registro_memoria
from modules.tool_response import (
    genera_risposta_post_tool,
    pulisci_testo_modello,
    raccogli_risposta_finale,
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
    num_ctx: int = 8192,
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
            num_ctx,
        )

        return raccogli_risposta_finale(
            stream
        )

    except Exception:
        return genera_risposta_deterministica_memoria(
            risultato_tool
        )

def _fallback_minimo_dominio_sconosciuto(risultato_tool: dict) -> str:
    """
    Fallback minimo per un tool il cui dominio non e' riconosciuto.

    Oggi l'unico caso raggiungibile e' un nome tool non registrato nel
    registry (nessun dominio reale diverso da "memory" esiste ancora):
    non tenta di interpretare il contenuto del risultato, restituisce
    solo l'errore se presente o un messaggio neutro.
    """

    error = risultato_tool.get("error")

    if error:
        return error

    return "Non riesco a gestire questa richiesta."

# ---------------------------------------------------------------------
# Budget della cronologia (0.6.8)
# ---------------------------------------------------------------------
# Rete di sicurezza pratica, NON una garanzia token-safe: i caratteri
# non sono token (es. le cifre valgono circa un token ciascuna).
SOGLIA_COMPATTAZIONE_TOOL = 1000
SOGLIA_COMPATTAZIONE_RISPOSTA = 2000
MAX_CARATTERI_CRONOLOGIA = 6000

# Placeholder fisso per un risultato tool non JSON: nessun dato originale.
PLACEHOLDER_TOOL_OMESSO = '{"omitted_from_history": true}'
PLACEHOLDER_RISPOSTA_OMESSA = (
    "[Risposta precedente basata su un risultato tool voluminoso. "
    "Richiamare lo strumento se servono di nuovo i dettagli.]"
)

_CAMPI_TOOL_CONSERVATI = ("ok", "operation", "status")


def _campo(messaggio, nome: str):
    """Legge un campo sia da un dict sia da un messaggio ollama (oggetto)."""

    if isinstance(messaggio, dict):
        return messaggio.get(nome)
    return getattr(messaggio, nome, None)


def _ha_tool_calls(messaggio) -> bool:
    return _campo(messaggio, "role") == "assistant" and bool(
        _campo(messaggio, "tool_calls")
    )


def _dimensione_messaggio(messaggio) -> int:
    """Caratteri di un messaggio: contenuto più nome/argomenti delle tool call."""

    dimensione = len(_campo(messaggio, "content") or "")

    for chiamata in _campo(messaggio, "tool_calls") or []:
        funzione = _campo(chiamata, "function")
        dimensione += len(str(_campo(funzione, "name") or ""))
        dimensione += len(str(_campo(funzione, "arguments") or ""))

    return dimensione


def _tool_gia_compattato(contenuto: str) -> bool:
    try:
        dati = json.loads(contenuto)
    except (TypeError, ValueError):
        return False
    return isinstance(dati, dict) and dati.get("omitted_from_history") is True


def _compatta_contenuto_tool(contenuto: str) -> str:
    """
    Rappresentazione minima di un risultato tool voluminoso.

    Conserva solo ok/operation/status (se presenti) più il marcatore
    omitted_from_history: mai data, content, processes, entries, path
    o testo di errore.
    """

    try:
        dati = json.loads(contenuto)
    except (TypeError, ValueError):
        return PLACEHOLDER_TOOL_OMESSO

    if not isinstance(dati, dict):
        return PLACEHOLDER_TOOL_OMESSO

    minimo = {
        campo: dati[campo]
        for campo in _CAMPI_TOOL_CONSERVATI
        if campo in dati and isinstance(dati[campo], (bool, str))
    }
    minimo["omitted_from_history"] = True

    return json.dumps(minimo, ensure_ascii=False)


def _con_contenuto(messaggio: dict, contenuto: str) -> dict:
    """Copia del messaggio con nuovo contenuto (mai mutazione in place)."""

    nuovo = dict(messaggio)
    nuovo["content"] = contenuto
    return nuovo


def _dividi_in_turni(conversazione: list) -> list[list]:
    """
    Divide la conversazione in blocchi-turno, ognuno aperto da un
    messaggio user. I messaggi prima del primo user (pezzi di un
    turno già tagliato) vengono scartati.
    """

    turni = []

    for messaggio in conversazione:
        if _campo(messaggio, "role") == "user":
            turni.append([messaggio])
        elif turni:
            turni[-1].append(messaggio)

    return turni


def _turno_coerente(turno: list) -> bool:
    """
    Un turno è coerente se: inizia con user; ogni role=tool segue una
    tool call assistant (o un altro tool); ogni tool call assistant è
    seguita da almeno un role=tool; non contiene altri ruoli.
    """

    if not turno or _campo(turno[0], "role") != "user":
        return False

    for indice in range(1, len(turno)):
        messaggio = turno[indice]
        ruolo = _campo(messaggio, "role")
        precedente = turno[indice - 1]

        if ruolo == "tool":
            if not (
                _ha_tool_calls(precedente)
                or _campo(precedente, "role") == "tool"
            ):
                return False
        elif ruolo == "assistant":
            if _ha_tool_calls(messaggio):
                successivo = turno[indice + 1] if indice + 1 < len(turno) else None
                if successivo is None or _campo(successivo, "role") != "tool":
                    return False
        else:
            return False

    return True


def _compatta_turno(turno: list) -> list:
    """
    Compatta i role=tool voluminosi e, nei soli turni con un tool
    compattato, anche la risposta finale assistant troppo lunga (che
    potrebbe ripetere il contenuto del tool, es. read_file).
    """

    risultato = []
    turno_pesante = False

    for messaggio in turno:
        if (
            isinstance(messaggio, dict)
            and messaggio.get("role") == "tool"
            and isinstance(messaggio.get("content"), str)
        ):
            contenuto = messaggio["content"]
            if _tool_gia_compattato(contenuto):
                turno_pesante = True
            elif len(contenuto) > SOGLIA_COMPATTAZIONE_TOOL:
                messaggio = _con_contenuto(
                    messaggio,
                    _compatta_contenuto_tool(contenuto),
                )
                turno_pesante = True

        risultato.append(messaggio)

    if not turno_pesante:
        return risultato

    for indice, messaggio in enumerate(risultato):
        if (
            isinstance(messaggio, dict)
            and messaggio.get("role") == "assistant"
            and not messaggio.get("tool_calls")
            and len(messaggio.get("content") or "") > SOGLIA_COMPATTAZIONE_RISPOSTA
        ):
            risultato[indice] = _con_contenuto(
                messaggio,
                PLACEHOLDER_RISPOSTA_OMESSA,
            )

    return risultato


def limita_cronologia(
    messaggi: list,
    max_messaggi: int,
) -> None:
    """
    Mantiene sempre il messaggio di sistema e una cronologia limitata.

    Ordine: divisione in turni interi (scarta pezzi iniziali orfani e
    turni incoerenti) -> compattazione dei risultati tool voluminosi ->
    limite per numero di messaggi -> limite per caratteri. I tagli
    avvengono sempre per turni interi, mai a metà turno. L'ultimo
    turno (che contiene il messaggio user corrente) resta sempre.
    """

    messaggio_sistema = messaggi[0]
    turni = _dividi_in_turni(messaggi[1:])

    if not turni:
        messaggi[:] = [messaggio_sistema]
        return

    ultimo = turni[-1]
    if not _turno_coerente(ultimo):
        ultimo = [ultimo[0]]

    turni = [turno for turno in turni[:-1] if _turno_coerente(turno)]
    turni.append(ultimo)

    turni = [_compatta_turno(turno) for turno in turni]

    def totale_messaggi():
        return sum(len(turno) for turno in turni)

    def totale_caratteri():
        return sum(
            _dimensione_messaggio(messaggio)
            for turno in turni
            for messaggio in turno
        )

    while len(turni) > 1 and totale_messaggi() > max_messaggi:
        turni.pop(0)

    while len(turni) > 1 and totale_caratteri() > MAX_CARATTERI_CRONOLOGIA:
        turni.pop(0)

    messaggi[:] = [
        messaggio_sistema,
        *(messaggio for turno in turni for messaggio in turno),
    ]


def avvia_chat(
    prompt: str,
    modello: str,
    max_messaggi: int,
    host_ollama: str,
    timeout_ollama: float,
    num_ctx: int,
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

    registro_strumenti = crea_registro_memoria()
    registra_tool_sistema(registro_strumenti)
    registra_tool_filesystem(registro_strumenti)

    contesto_strumenti = ContestoMemoria(
        stato_memoria=stato_memoria,
        stato_sessione=stato_sessione,
        percorso_memoria=percorso_memoria,
        limite_ricerca=limite_ricerca,
    )

    # Le root filesystem autorizzate vivono in config.json
    # (tools.filesystem.allowed_roots); avvia_chat non riceve ancora il
    # config grezzo dal chiamante, quindi lo rilegge qui una volta sola
    # all'avvio della sessione, in modo self-contained. Le root relative
    # (es. "./workspace") si risolvono rispetto ad app_root, mai rispetto
    # a cwd, data_root o resource_root.
    percorsi = percorsi_runtime()
    config_filesystem = carica_config(percorsi.config_file)
    contesto_filesystem = prepara_contesto_filesystem(
        config_filesystem,
        percorsi.app_root,
    )

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

        # Stato pre-turno: se il turno fallisce in qualunque punto
        # (dispatch, post-tool, secondo giro, interruzione), la
        # cronologia torna esattamente a questo stato, senza user,
        # tool call o role=tool orfani. Copia superficiale sufficiente:
        # limita_cronologia non modifica mai i messaggi in place.
        messaggi_pre_turno = list(messaggi)

        messaggi.append(messaggio_utente)
        limita_cronologia(messaggi, max_messaggi)

        try:
            risposta = esegui_turno_con_tools(
                modello,
                messaggi,
                registro_strumenti.elenco_schema(),
                host_ollama,
                timeout_ollama,
                num_ctx,
            )

            tool_calls = risposta.message.tool_calls or []

            if len(tool_calls) > 1:
                risposta_completa = (
                    "Posso gestire una sola operazione "
                    "per volta. "
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
                        num_ctx=num_ctx,
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
                            num_ctx=num_ctx,
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

            # Dominio determinato esplicitamente dal registry (nessuna
            # euristica su prefissi/substring del nome del tool).
            tool_spec = registro_strumenti.trova(nome_tool)
            dominio_tool = tool_spec.dominio if tool_spec is not None else None

            # Conserviamo il messaggio assistant contenente
            # la tool call nella cronologia.
            messaggi.append(
                risposta.message
            )

            # Il registry inoltra un contesto opaco all'handler: per il
            # dominio filesystem serve quello con le root autorizzate,
            # non lo stato memoria (che gli handler filesystem non
            # userebbero comunque).
            if dominio_tool == "filesystem":
                contesto_dispatch = contesto_filesystem
            else:
                contesto_dispatch = contesto_strumenti

            risultato_tool = registro_strumenti.dispatch(
                nome_tool,
                argomenti,
                contesto_dispatch,
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

            if dominio_tool == "memory":
                # Contenuto potenzialmente sensibile: nessun secondo
                # giro Ollama, per non fargli mai vedere/ripetere il
                # valore rilevato. Risposta locale deterministica.
                salta_secondo_giro = (
                    risultato_tool.get("status") == "blocked_sensitive"
                )
                fallback_deterministico = genera_risposta_deterministica_memoria
            elif dominio_tool == "system":
                salta_secondo_giro = False
                fallback_deterministico = fallback_deterministico_sistema
            elif dominio_tool == "filesystem":
                # File classificato sensibile (per nome o per contenuto):
                # read_file non mette mai il contenuto in risultato_tool
                # in questo caso, quindi role="tool" e' già sicuro; qui
                # evitiamo comunque il secondo giro Ollama, per non
                # fargli mai ragionare o commentare su un blocco di
                # sicurezza. Risposta locale deterministica.
                salta_secondo_giro = (
                    risultato_tool.get("status") == "sensitive_file"
                )
                fallback_deterministico = fallback_deterministico_file
            else:
                salta_secondo_giro = False
                fallback_deterministico = _fallback_minimo_dominio_sconosciuto

            risposta_completa = genera_risposta_post_tool(
                modello=modello,
                messaggi=messaggi,
                host_ollama=host_ollama,
                timeout_ollama=timeout_ollama,
                num_ctx=num_ctx,
                risultato_tool=risultato_tool,
                salta_secondo_giro=salta_secondo_giro,
                fallback_deterministico=fallback_deterministico,
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

            messaggi[:] = messaggi_pre_turno

        except Exception as errore:
            print(
                "\nErrore nella comunicazione "
                f"con Ollama: {errore}"
            )

            messaggi[:] = messaggi_pre_turno