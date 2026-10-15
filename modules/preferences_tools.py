"""Tool del dominio "preferences": preferenze applicative strutturate di Aster."""

from dataclasses import dataclass
from pathlib import Path

from modules import app_preferences
from modules.tool_registry import RegistroStrumenti, ToolSpec

_ERRORE_CATEGORIA = "category deve essere browser, editor o media_player."
_ERRORE_VALUE_ASSENTE = (
    "value è obbligatorio: usa null esplicito per cancellare una preferenza."
)
_ERRORE_VALUE_NON_VALIDO = (
    "value non valido: usa un nome applicativo semplice, "
    "non un percorso, un comando o testo con caratteri non sicuri."
)
_ERRORE_NON_AFFIDABILE = (
    "Le preferenze applicative non sono leggibili in modo affidabile al momento."
)
_ERRORE_GENERICO = "Non sono riuscito a leggere o salvare le preferenze applicative."

_ETICHETTE_CATEGORIA = {
    "browser": "browser",
    "editor": "editor",
    "media_player": "lettore multimediale",
}

# Normalizzazione: solo spazi esterni e maiuscole/minuscole (nessun'altra
# tolleranza, es. punteggiatura finale): risposte ambigue devono annullare,
# mai essere interpretate per sottostringa o somiglianza.
_RISPOSTE_AFFERMATIVE = frozenset({"si", "sì", "yes"})


@dataclass
class ContestoPreferenze:
    """Stato minimo richiesto dagli handler dei tool preferenze."""

    percorso_preferenze: Path


TOOLS_PREFERENZE = [
    {
        "type": "function",
        "function": {
            "name": "get_app_preferences",
            "description": (
                "Preferenze applicative salvate in Aster per browser, editor "
                "e lettore multimediale. Non sono le app predefinite di "
                "Windows: solo ciò che l'utente ha detto di preferire ad "
                "Aster."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_app_preference",
            "description": (
                "Imposta, sostituisce o cancella (value=null) la preferenza "
                "Aster per una categoria. Salva solo un'etichetta "
                "descrittiva: non avvia programmi e non cambia le app "
                "predefinite di Windows."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": list(app_preferences.CATEGORIE_VALIDE),
                        "description": "Categoria della preferenza.",
                    },
                    "value": {
                        "type": ["string", "null"],
                        "description": (
                            "Nome dell'applicazione preferita (es. Firefox, "
                            "Visual Studio Code), oppure null esplicito per "
                            "cancellare la preferenza. Sempre obbligatorio: "
                            "omettere il parametro non cancella nulla."
                        ),
                    },
                },
                "required": ["category", "value"],
            },
        },
    },
]


def _errore_preferenze(operazione: str, status: str, messaggio: str) -> dict:
    return {
        "ok": False,
        "operation": operazione,
        "status": status,
        "error": messaggio,
    }


def get_app_preferences(argomenti: dict, contesto: ContestoPreferenze) -> dict:
    """
    Handler del tool get_app_preferences.

    Sola lettura di DATA_ROOT/app_preferences.json. File assente:
    tutte e tre le categorie null (prima esecuzione, non un errore).
    File presente ma non valido: status="unreliable_state", mai una
    sostituzione silenziosa con preferenze vuote. Errore imprevisto:
    tool_error con messaggio fisso. Argomenti ignorati: il tool non
    accetta parametri.
    """

    try:
        preferenze = app_preferences.leggi_preferenze_o_vuote(
            contesto.percorso_preferenze
        )
    except app_preferences.ErrorePreferenzeNonAffidabili:
        return _errore_preferenze(
            "get_app_preferences", "unreliable_state", _ERRORE_NON_AFFIDABILE
        )
    except Exception:
        return _errore_preferenze(
            "get_app_preferences", "tool_error", _ERRORE_GENERICO
        )

    return {
        "ok": True,
        "operation": "get_app_preferences",
        "status": "success",
        "data": dict(preferenze["preferences"]),
    }


def _valida_categoria_e_valore(argomenti: dict) -> tuple[bool, str | None, str | None, dict | None]:
    """
    Valida category/value secondo lo stesso contratto per la proposta
    e per l'applicazione confermata (riuso diretto, non una copia):
    restituisce (valido, categoria, valore_normalizzato, errore). Se
    valido è False, errore è il dict da restituire al chiamante.
    """

    argomenti = argomenti if isinstance(argomenti, dict) else {}

    categoria = argomenti.get("category")
    if categoria not in app_preferences.CATEGORIE_VALIDE:
        return False, None, None, _errore_preferenze(
            "set_app_preference", "validation_error", _ERRORE_CATEGORIA
        )

    if "value" not in argomenti:
        return False, None, None, _errore_preferenze(
            "set_app_preference", "validation_error", _ERRORE_VALUE_ASSENTE
        )

    valore_grezzo = argomenti["value"]

    if valore_grezzo is None:
        return True, categoria, None, None

    if isinstance(valore_grezzo, str):
        valore_ripulito = valore_grezzo.strip()
        if not app_preferences.valore_preferenza_valido(valore_ripulito):
            return False, None, None, _errore_preferenze(
                "set_app_preference", "validation_error", _ERRORE_VALUE_NON_VALIDO
            )
        return True, categoria, valore_ripulito, None

    return False, None, None, _errore_preferenze(
        "set_app_preference", "validation_error", _ERRORE_VALUE_NON_VALIDO
    )


def set_app_preference(argomenti: dict, contesto: ContestoPreferenze) -> dict:
    """
    Handler REGISTRATO del tool set_app_preference: valida e propone,
    non scrive mai su disco.

    category deve essere una delle tre categorie note; value deve
    essere presente come chiave (un parametro mancante non è mai
    interpretato come cancellazione): null esplicito propone la
    cancellazione, una stringa valida propone l'impostazione/
    sostituzione. Su validazione riuscita restituisce
    status="pending_confirmation" (ok=False, come le proposte di
    memoria in modules/memory_tools.py) con category/value proposti in
    data: NON è una scrittura riuscita, solo una proposta.

    NOTA ARCHITETTURALE SULLA CONFERMA: questo handler (l'unico
    raggiungibile da registro_strumenti.dispatch, quindi anche da una
    chiamata diretta al registry che aggirasse avvia_chat) non importa
    né invoca mai app_preferences.imposta_preferenza: strutturalmente
    non può scrivere. L'unica funzione capace di scrivere è
    applica_preferenza_confermata, più sotto in questo modulo: NON è
    registrata come tool, il modello non può chiamarla. È avvia_chat
    (modules/chat.py) a riconoscere status="pending_confirmation" per
    questo tool, presentare una domanda sì/no con input() e invocare
    applica_preferenza_confermata solo dopo una risposta affermativa
    esplicita e inequivocabile. Questo evita di generalizzare il
    sistema di pending della memoria (modules/memory_session.py,
    costruito su concetti specifici: target_id intero, candidates da
    una ricerca, before/after testuali) a un dominio con una semantica
    diversa: qui non serve selezione tra candidati, solo un sì/no
    immediato e mai residuo tra un turno e l'altro.
    """

    valido, categoria, valore_normalizzato, errore = _valida_categoria_e_valore(argomenti)
    if not valido:
        return errore

    return {
        "ok": False,
        "operation": "set_app_preference",
        "status": "pending_confirmation",
        "data": {"category": categoria, "value": valore_normalizzato},
    }


def applica_preferenza_confermata(
    categoria: str, valore: str | None, contesto: ContestoPreferenze
) -> dict:
    """
    Scrive una preferenza GIÀ confermata dall'utente tramite
    avvia_chat. Non è un tool: non compare in TOOLS_PREFERENZE, non è
    registrata nel RegistroStrumenti, il modello non può invocarla.

    Rivalida categoria e valore (mai fidarsi del solo controllo fatto
    al momento della proposta, anche se in pratica i valori arrivano
    da una proposta già validata da questo stesso modulo) e poi usa
    la persistenza esistente (app_preferences.imposta_preferenza),
    identica a quella che avrebbe usato una scrittura diretta. Questa
    è l'unica funzione dell'intero modulo che può raggiungere quella
    scrittura.
    """

    if categoria not in app_preferences.CATEGORIE_VALIDE:
        return _errore_preferenze(
            "set_app_preference", "validation_error", _ERRORE_CATEGORIA
        )

    if valore is not None and (
        not isinstance(valore, str) or not app_preferences.valore_preferenza_valido(valore)
    ):
        return _errore_preferenze(
            "set_app_preference", "validation_error", _ERRORE_VALUE_NON_VALIDO
        )

    try:
        app_preferences.imposta_preferenza(
            contesto.percorso_preferenze, categoria, valore
        )
    except app_preferences.ErrorePreferenzeNonAffidabili:
        return _errore_preferenze(
            "set_app_preference", "unreliable_state", _ERRORE_NON_AFFIDABILE
        )
    except Exception:
        return _errore_preferenze(
            "set_app_preference", "tool_error", _ERRORE_GENERICO
        )

    return {
        "ok": True,
        "operation": "set_app_preference",
        "status": "success",
        "data": {"category": categoria, "value": valore},
    }


def risultato_set_app_preference_annullato(categoria: str, valore: str | None) -> dict:
    """
    Risultato deterministico di una proposta rifiutata o non
    confermata in modo inequivocabile: nessuna scrittura è avvenuta
    (né verrà più tentata per questa proposta, che non sopravvive
    oltre questo turno). ok=True perché l'operazione richiesta
    dall'utente (non applicare la modifica) è stata eseguita
    correttamente, non è un errore.
    """

    return {
        "ok": True,
        "operation": "set_app_preference",
        "status": "cancelled",
        "data": {"category": categoria, "value": valore},
    }


def risposta_affermativa(testo: object) -> bool:
    """
    True solo per una risposta esplicita e inequivocabile ("si", "sì",
    "yes", case-insensitive, spazi esterni ignorati). Qualunque altra
    cosa (vuota, "no", ambigua, non stringa) è False: mai un confronto
    per sottostringa o somiglianza.
    """

    if not isinstance(testo, str):
        return False
    return testo.strip().casefold() in _RISPOSTE_AFFERMATIVE


def domanda_conferma_set_app_preference(categoria: str, valore: str | None) -> str:
    """Domanda deterministica sì/no presentata dall'utente prima di scrivere."""

    etichetta = _ETICHETTE_CATEGORIA.get(categoria, categoria)
    if valore is None:
        return (
            f"Confermi di cancellare la preferenza salvata per {etichetta}? (sì/no)"
        )
    return f'Confermi di impostare "{valore}" come {etichetta} preferito? (sì/no)'


def _riga_preferenza(categoria: str, valore) -> str:
    etichette = {
        "browser": "Browser",
        "editor": "Editor",
        "media_player": "Lettore multimediale",
    }
    etichetta = etichette.get(categoria, categoria)

    if isinstance(valore, str) and valore:
        return f"- {etichetta}: {valore}"
    return f"- {etichetta}: nessuna preferenza salvata"


def _fallback_get_app_preferences(risultato_tool: dict) -> str:
    status = risultato_tool.get("status")
    if status == "unreliable_state":
        return risultato_tool.get("error") or _ERRORE_NON_AFFIDABILE
    if status != "success":
        return risultato_tool.get("error") or _ERRORE_GENERICO

    data = risultato_tool.get("data")
    data = data if isinstance(data, dict) else {}

    righe = ["Preferenze applicative salvate in Aster:"]
    righe.extend(
        _riga_preferenza(categoria, data.get(categoria))
        for categoria in app_preferences.CATEGORIE_VALIDE
    )
    return "\n".join(righe)


def _fallback_set_app_preference(risultato_tool: dict) -> str:
    status = risultato_tool.get("status")

    if status == "unreliable_state":
        return risultato_tool.get("error") or _ERRORE_NON_AFFIDABILE

    data = risultato_tool.get("data")
    data = data if isinstance(data, dict) else {}
    categoria = data.get("category")
    valore = data.get("value")
    etichetta = _ETICHETTE_CATEGORIA.get(categoria, categoria or "preferenza")

    if status == "pending_confirmation":
        # Non dovrebbe arrivare qui in condizioni normali (avvia_chat
        # gestisce questo stato prima di chiamare il router di
        # fallback), ma non deve mai essere presentato come una
        # scrittura riuscita se succede.
        return domanda_conferma_set_app_preference(categoria, valore)

    if status == "cancelled":
        return f"Operazione annullata: nessuna modifica salvata per {etichetta}."

    if status != "success":
        return risultato_tool.get("error") or _ERRORE_GENERICO

    if isinstance(valore, str) and valore:
        return f"Preferenza salvata: {etichetta} -> {valore}."
    return f"Preferenza cancellata: {etichetta}."


def fallback_deterministico_preferenze(risultato_tool: dict) -> str:
    """
    Router del fallback deterministico per il dominio "preferences".

    Sceglie il rendering in base a risultato_tool["operation"], senza
    mai fare un dump generico dei dati.
    """

    operation = risultato_tool.get("operation")

    if operation == "get_app_preferences":
        return _fallback_get_app_preferences(risultato_tool)

    if operation == "set_app_preference":
        return _fallback_set_app_preference(risultato_tool)

    if risultato_tool.get("status") != "success":
        return risultato_tool.get("error") or _ERRORE_GENERICO

    return "Non sono riuscito a generare una risposta per questa preferenza applicativa."


def registra_tool_preferenze(registro: RegistroStrumenti) -> None:
    """Registra i tool del dominio "preferences" nel RegistroStrumenti esistente."""

    registro.registra(
        ToolSpec(
            nome="get_app_preferences",
            schema=TOOLS_PREFERENZE[0],
            handler=get_app_preferences,
            livello="READ_ONLY",
            dominio="preferences",
        )
    )

    registro.registra(
        ToolSpec(
            nome="set_app_preference",
            schema=TOOLS_PREFERENZE[1],
            handler=set_app_preference,
            livello="CONFIRM_REQUIRED",
            dominio="preferences",
        )
    )
