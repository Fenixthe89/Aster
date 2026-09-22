"""Contratto tra il modello LLM e il sistema di memoria di Aster."""

import re
from pathlib import Path

from modules.memory_query import estrai_query_da_testo

from modules.memory import (
    MODALITA_DISABILITATA,
    MODALITA_NORMALE,
    MODALITA_SOLA_LETTURA,
    StatoMemoria,
    aggiungi_ricordo,
    aggiorna_ricordo,
    carica_archivio,
    carica_cestino_con_recovery,
    cerca_memoria,
    elimina_ricordo,
    ripristina_ricordo,
)

from modules.memory_session import (
    MemorySessionState,
    PendingAction,
    annulla_pending,
    imposta_pending,
    normalizza_testo,
    registra_proposta_rifiutata,
)

# Status restituiti dal layer Python al modello.
STATUS_MEMORIA = frozenset({
    "created",
    "updated",
    "deleted",
    "restored",
    "searched",
    "cancelled",
    "pending_confirmation",
    "pending_selection",
    "blocked_readonly",
    "blocked_disabled",
    "blocked_sensitive",
    "not_found",
    "duplicate_detected",
    "conflict",
    "validation_error",
    "tool_error",
})

# Categorie neutre restituite dal rilevatore di contenuto sensibile.
CATEGORIE_SENSIBILI = frozenset({
    "password",
    "api_key_or_token",
    "pin",
    "cvv",
    "otp",
    "payment_card",
    "private_key",
    "recovery_secret",
})

def crea_risultato_tool(
    *,
    ok: bool,
    operation: str,
    status: str,
    memory_id: int | None = None,
    content: str | None = None,
    error: str | None = None,
    results: list[dict] | None = None,
    returned: int | None = None,
    total_matches: int | None = None,
    has_more: bool | None = None,
    before: str | None = None,
    after: str | None = None,
    candidates: list[dict] | None = None,
    source: str | None = None,
    sensitive_category: str | None = None,
) -> dict:
    """
    Costruisce il risultato strutturato restituito da Python al modello.
    """

    if type(ok) is not bool:
        raise TypeError("ok deve essere un valore booleano.")

    if not isinstance(operation, str) or not operation.strip():
        raise ValueError(
            "operation deve essere una stringa non vuota."
        )

    if status not in STATUS_MEMORIA:
        raise ValueError(
            f"Status memoria non supportato: {status}."
        )

    risultato = {
        "ok": ok,
        "operation": operation,
        "status": status,
    }

    if memory_id is not None:
        if type(memory_id) is not int or memory_id < 1:
            raise ValueError(
                "memory_id deve essere un intero maggiore o uguale a 1."
            )
        risultato["memory_id"] = memory_id

    if content is not None:
        risultato["content"] = content

    if error is not None:
        risultato["error"] = error

    if results is not None:
        risultato["results"] = results

    if returned is not None:
        risultato["returned"] = returned

    if total_matches is not None:
        risultato["total_matches"] = total_matches

    if has_more is not None:
        risultato["has_more"] = has_more

    if before is not None:
        risultato["before"] = before

    if after is not None:
        risultato["after"] = after

    if candidates is not None:
        risultato["candidates"] = candidates

    if source is not None:

        if source != "persistent_memory":
            raise ValueError(
                f"Source memoria non supportata: {source}."
            )

        risultato["source"] = source

    if sensitive_category is not None:

        if sensitive_category not in CATEGORIE_SENSIBILI:
            raise ValueError(
                f"Categoria sensibile non supportata: {sensitive_category}."
            )

        risultato["sensitive_category"] = sensitive_category

    return risultato


TOOLS_MEMORIA = [
    {
        "type": "function",
        "function": {
            "name": "cerca_memoria",
            "description": (
                "Cerca informazioni nella memoria persistente di Aster "
                "quando servono ricordi precedenti dell'utente."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Testo semplice da cercare nei ricordi.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crea_memoria",
            "description": (
                "Richiede la creazione di un nuovo ricordo permanente. "
                "Usare mode='explicit' quando l'utente ha chiesto "
                "direttamente di memorizzare il fatto. "
                "Usare mode='proposal' quando Aster vuole soltanto "
                "proporre il ricordo e attendere conferma."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Contenuto preciso del ricordo da salvare.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["explicit", "proposal"],
                        "description": (
                            "Usa 'explicit' quando l'utente ha autorizzato "
                            "direttamente il salvataggio. Usa 'proposal' "
                            "quando Aster sta proponendo spontaneamente il ricordo."
                        ),
                    },
                },
                "required": ["content", "mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "modifica_memoria",
            "description": (
                "Richiede la modifica di un ricordo esistente. "
                "La modifica richiede sempre conferma dell'utente "
                "prima dell'esecuzione reale."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "integer", "minimum": 1},
                    "new_content": {"type": "string"},
                },
                "required": ["memory_id", "new_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "elimina_memoria",
            "description": (
                "Richiede lo spostamento di un ricordo nel cestino. "
                "L'eliminazione richiede sempre conferma dell'utente."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "integer", "minimum": 1}
                },
                "required": ["memory_id"],
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "elimina_memoria_per_query",
            "description": (
                "Cerca il ricordo da eliminare quando l'utente "
                "non ha fornito un ID esplicito. "
                "Se esiste un solo candidato prepara la conferma; "
                "se ne esistono più di uno richiede la selezione."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Testo da cercare nei ricordi per identificare "
                            "il ricordo che l'utente vuole eliminare."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "ripristina_memoria",
            "description": (
                "Richiede il ripristino di un ricordo dal cestino. "
                "Il ripristino richiede sempre conferma dell'utente."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "integer", "minimum": 1}
                },
                "required": ["memory_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gestisci_pending_memoria",
            "description": (
                "Gestisce la risposta dell'utente a un'operazione "
                "di memoria già in attesa. Usare solo quando esiste un pending."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "decision": {
                        "type": "string",
                        "enum": ["confirm", "cancel", "select"],
                        "description": (
                            "Conferma, annulla oppure seleziona "
                            "un candidato dell'operazione pending."
                        ),
                    },
                    "memory_id": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "ID scelto quando decision='select'.",
                    },
                },
                "required": ["decision"],
            },
        },
    },
]


def _memoria_per_lettura(
    stato_memoria: StatoMemoria,
    percorso_memoria: Path,
) -> dict | None:
    """Restituisce l'archivio da usare per le letture."""

    if stato_memoria.modalita == MODALITA_DISABILITATA:
        return None

    if stato_memoria.modalita == MODALITA_NORMALE:
        return carica_archivio(percorso_memoria)

    if stato_memoria.modalita == MODALITA_SOLA_LETTURA:
        return stato_memoria.memoria

    raise ValueError(
        f"Modalità memoria sconosciuta: {stato_memoria.modalita}."
    )


def _trova_duplicato_esatto(
    memoria: dict,
    content: str,
) -> dict | None:
    """Trova un ricordo con contenuto testualmente equivalente."""

    content_normalizzato = normalizza_testo(content)

    for ricordo in memoria["memories"]:
        if normalizza_testo(ricordo["content"]) == content_normalizzato:
            return ricordo.copy()

    return None


def _trova_ricordo_per_id(
    memoria: dict,
    memory_id: int,
) -> dict | None:
    """Trova un ricordo attivo tramite ID."""

    for ricordo in memoria["memories"]:
        if ricordo["id"] == memory_id:
            return ricordo.copy()

    return None


def _trova_ricordo_eliminato_per_id(
    cestino: dict,
    memory_id: int,
) -> dict | None:
    """Trova un ricordo nel cestino tramite ID."""

    for ricordo in cestino["deleted_memories"]:
        if ricordo["id"] == memory_id:
            return ricordo.copy()

    return None

def prepara_selezione_memoria(
    *,
    operation: str,
    candidates: list[dict],
    stato_sessione: MemorySessionState,
) -> dict:
    """
    Crea un pending di selezione quando più ricordi
    possono essere il target della stessa operazione.
    """

    if operation != "delete":
        return crea_risultato_tool(
            ok=False,
            operation=operation,
            status="validation_error",
            error=(
                "La selezione senza ID è supportata "
                "solo per l'eliminazione in questa fase."
            ),
        )

    if stato_sessione.pending_action is not None:
        pending_corrente = stato_sessione.pending_action

        return crea_risultato_tool(
            ok=False,
            operation=pending_corrente.operation,
            status="conflict",
            memory_id=pending_corrente.target_id,
            error=(
                "Esiste già un'operazione di memoria in attesa. "
                "Confermare o annullare il pending corrente "
                "prima di iniziarne uno nuovo."
            ),
        )

    if not isinstance(candidates, list) or len(candidates) < 2:
        return crea_risultato_tool(
            ok=False,
            operation=operation,
            status="validation_error",
            error=(
                "Servono almeno due candidati validi "
                "per creare un pending di selezione."
            ),
        )

    candidati_validi = []
    id_visti = set()

    for candidato in candidates:
        if not isinstance(candidato, dict):
            return crea_risultato_tool(
                ok=False,
                operation=operation,
                status="validation_error",
                error="Formato candidato non valido.",
            )

        memory_id = candidato.get("id")
        content = candidato.get("content")

        if (
            type(memory_id) is not int
            or memory_id < 1
            or not isinstance(content, str)
            or not content.strip()
        ):
            return crea_risultato_tool(
                ok=False,
                operation=operation,
                status="validation_error",
                error="Candidato memoria non valido.",
            )

        if memory_id in id_visti:
            return crea_risultato_tool(
                ok=False,
                operation=operation,
                status="validation_error",
                error="ID candidato duplicato.",
            )

        id_visti.add(memory_id)

        candidati_validi.append(
            {
                "id": memory_id,
                "content": content,
            }
        )

    imposta_pending(
        stato_sessione,
        PendingAction(
            operation=operation,
            phase="selection",
            candidates=candidati_validi,
        ),
    )

    return crea_risultato_tool(
        ok=False,
        operation=operation,
        status="pending_selection",
        candidates=candidati_validi,
        source="persistent_memory",
    )

STOPWORD_ELIMINAZIONE_MEMORIA = {
    "il",
    "lo",
    "la",
    "i",
    "gli",
    "le",
    "un",
    "uno",
    "una",
    "ricordo",
    "ricordi",
    "memoria",
    "elimina",
    "eliminare",
    "cancella",
    "cancellare",
    "su",
    "di",
    "del",
    "della",
    "dei",
    "delle",
    "per",
    "che",
    "quello",
    "quella",
}

def _genera_query_eliminazione(query: str) -> list[str]:
    """
    Genera fallback conservativi per una richiesta
    di eliminazione senza ID.
    """

    return estrai_query_da_testo(query, STOPWORD_ELIMINAZIONE_MEMORIA)


# ---------------------------------------------------------------
# RILEVAMENTO CONTENUTO SENSIBILE (guardia lato Python, 0.5.3.2)
#
# Alta precisione: privilegia i falsi negativi rispetto ai falsi
# positivi. Intercetta solo pattern con parola-chiave pertinente e
# assegnazione esplicita a un valore plausibile (o marcatori/prefissi
# strutturalmente inequivocabili). Non fa detection generica di
# entropia: una stringa casuale senza contesto non viene bloccata.
# ---------------------------------------------------------------

_COPULA = r"(?:è|e'|:|=)"

_PASSWORD_ESCLUSE = (
    "manager", "generator", "dimenticata", "dimenticato",
    "sicura", "sicuro", "complessa", "complesso",
    "debole", "robusta", "robusto",
)

_PASSWORD_PATTERN = re.compile(
    r"\b(?:password|pw|pwd)\b"
    r"(?!\s+(?:" + "|".join(_PASSWORD_ESCLUSE) + r")\b)"
    r"[^.\n]{0,40}?"
    rf"{_COPULA}\s*"
    r"['\"]?([^\s'\".,;!?]{1,64})",
    re.IGNORECASE,
)

_TOKEN_PREFISSI_PATTERN = re.compile(
    r"\bsk-[A-Za-z0-9]{16,}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{16,}\b"
    r"|\bAIza[0-9A-Za-z_\-]{16,}\b"
    r"|\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"
    r"|\bAKIA[0-9A-Z]{12,}\b"
    r"|\bya29\.[0-9A-Za-z_\-]{16,}\b"
)

_TOKEN_KEYWORD_PATTERN = re.compile(
    r"\b(?:api\s*key|token)\b"
    r"[^.\n]{0,40}?"
    rf"{_COPULA}\s*"
    r"['\"]?([A-Za-z0-9_\-]{16,})",
    re.IGNORECASE,
)

_PIN_PATTERN = re.compile(
    rf"\bPIN\b[^.\n]{{0,40}}?{_COPULA}\s*['\"]?(\d{{4,6}})\b",
    re.IGNORECASE,
)

_CVV_PATTERN = re.compile(
    rf"\bCVV\b[^.\n]{{0,40}}?{_COPULA}\s*['\"]?(\d{{3,4}})\b",
    re.IGNORECASE,
)

_OTP_PATTERN = re.compile(
    r"\b(?:2FA|OTP|codice\s+di\s+verifica|codice\s+a\s+due\s+fattori)\b"
    rf"[^.\n]{{0,40}}?{_COPULA}\s*['\"]?(\d{{4,8}})\b",
    re.IGNORECASE,
)

_CARTA_KEYWORD_PATTERN = re.compile(
    r"\b(?:carta(?:\s+di\s+(?:credito|debito))?|credit\s*card)\b",
    re.IGNORECASE,
)

_CARTA_NUMERO_PATTERN = re.compile(r"[\d][\d \-]{11,25}[\d]")

_PEM_PRIVATE_PATTERN = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
)

_SEED_KEYWORD_PATTERN = re.compile(
    r"\b(?:seed\s+phrase|recovery\s+phrase|mnemonic)\b",
    re.IGNORECASE,
)

_SEED_WORDS_PATTERN = re.compile(
    r"(?:\b[a-zà-ÿ]{3,8}\b[\s,]+){7,}\b[a-zà-ÿ]{3,8}\b",
    re.IGNORECASE,
)

_RECOVERY_CODE_KEYWORD_PATTERN = re.compile(
    r"\b(?:recovery\s+code|backup\s+code|codice\s+di\s+(?:recupero|backup))\b",
    re.IGNORECASE,
)

_RECOVERY_CODE_VALUE_PATTERN = re.compile(
    rf"[^.\n]{{0,40}}?{_COPULA}\s*['\"]?([A-Za-z0-9]{{4,6}}(?:-[A-Za-z0-9]{{4,6}})?)\b"
)


def _luhn_valido(numero: str) -> bool:
    """Verifica l'algoritmo di Luhn su una sequenza di sole cifre."""

    cifre = [int(carattere) for carattere in numero]
    cifre.reverse()

    totale = 0

    for indice, cifra in enumerate(cifre):
        if indice % 2 == 1:
            cifra *= 2
            if cifra > 9:
                cifra -= 9

        totale += cifra

    return totale % 10 == 0


def _numeri_carta_candidati(content: str) -> list[str]:
    """Estrae sequenze numeriche di lunghezza plausibile per una carta."""

    candidati = []

    for corrispondenza in _CARTA_NUMERO_PATTERN.finditer(content):
        pulito = re.sub(r"[^\d]", "", corrispondenza.group())

        if 13 <= len(pulito) <= 19:
            candidati.append(pulito)

    return candidati


def rileva_contenuto_sensibile(content: str) -> str | None:
    """
    Rileva in modo conservativo un contenuto molto probabilmente
    sensibile (segreti, credenziali) prima che venga salvato.

    Restituisce una categoria neutra oppure None. Calibrato per
    l'alta precisione: un contenuto senza parola-chiave pertinente
    o senza pattern strutturale inequivocabile non viene bloccato,
    anche se potrebbe in teoria essere un segreto reale.
    """

    if not isinstance(content, str) or not content:
        return None

    if _PEM_PRIVATE_PATTERN.search(content):
        return "private_key"

    if _TOKEN_PREFISSI_PATTERN.search(content):
        return "api_key_or_token"

    if _TOKEN_KEYWORD_PATTERN.search(content):
        return "api_key_or_token"

    if _PASSWORD_PATTERN.search(content):
        return "password"

    if _PIN_PATTERN.search(content):
        return "pin"

    if _CVV_PATTERN.search(content):
        return "cvv"

    if _OTP_PATTERN.search(content):
        return "otp"

    if _CARTA_KEYWORD_PATTERN.search(content):
        for candidato in _numeri_carta_candidati(content):
            if _luhn_valido(candidato):
                return "payment_card"

    if (
        _SEED_KEYWORD_PATTERN.search(content)
        and _SEED_WORDS_PATTERN.search(content)
    ):
        return "recovery_secret"

    if (
        _RECOVERY_CODE_KEYWORD_PATTERN.search(content)
        and _RECOVERY_CODE_VALUE_PATTERN.search(content)
    ):
        return "recovery_secret"

    return None

def esegui_tool_memoria(
    *,
    nome_tool: str,
    argomenti: dict,
    stato_memoria: StatoMemoria,
    stato_sessione: MemorySessionState,
    percorso_memoria: Path,
    limite_ricerca: int,
) -> dict:
    """Valida ed esegue un tool memoria richiesto dal modello."""

    if not isinstance(nome_tool, str) or not nome_tool.strip():
        return crea_risultato_tool(
            ok=False,
            operation="unknown",
            status="validation_error",
            error="Nome tool non valido.",
        )

    if not isinstance(argomenti, dict):
        return crea_risultato_tool(
            ok=False,
            operation=nome_tool,
            status="validation_error",
            error="Gli argomenti del tool devono essere un oggetto.",
        )

    if nome_tool == "cerca_memoria":
        if stato_memoria.modalita == MODALITA_DISABILITATA:
            return crea_risultato_tool(
                ok=False,
                operation="search",
                status="blocked_disabled",
                error="La memoria persistente è disabilitata.",
            )

        query = argomenti.get("query")
        try:
            memoria = _memoria_per_lettura(
                stato_memoria,
                percorso_memoria,
            )
            risultato = cerca_memoria(
                memoria,
                query,
                limite=limite_ricerca,
            )
        except (TypeError, ValueError) as errore:
            return crea_risultato_tool(
                ok=False,
                operation="search",
                status="validation_error",
                error=str(errore),
            )
        except OSError as errore:
            return crea_risultato_tool(
                ok=False,
                operation="search",
                status="tool_error",
                error=str(errore),
            )

        return crea_risultato_tool(
            ok=True,
            operation="search",
            status="searched",
            source="persistent_memory",
            results=risultato["results"],
            returned=risultato["returned"],
            total_matches=risultato["total_matches"],
            has_more=risultato["has_more"],
        )

    if nome_tool == "crea_memoria":
        content = argomenti.get("content")
        mode = argomenti.get("mode")

        if not isinstance(content, str) or not content.strip():
            return crea_risultato_tool(
                ok=False,
                operation="create",
                status="validation_error",
                error="Il contenuto del ricordo non è valido.",
            )

        if mode not in {"explicit", "proposal"}:
            return crea_risultato_tool(
                ok=False,
                operation="create",
                status="validation_error",
                error=(
                    "Il parametro mode deve essere "
                    "'explicit' oppure 'proposal'."
                ),
            )

        categoria_sensibile = rileva_contenuto_sensibile(content)
        if categoria_sensibile is not None:
            return crea_risultato_tool(
                ok=False,
                operation="create",
                status="blocked_sensitive",
                error=(
                    "Non è stato salvato: il contenuto sembra "
                    "includere un dato sensibile."
                ),
                sensitive_category=categoria_sensibile,
            )

        if stato_memoria.modalita == MODALITA_DISABILITATA:
            return crea_risultato_tool(
                ok=False,
                operation="create",
                status="blocked_disabled",
                error="La memoria persistente è disabilitata.",
            )

        if stato_memoria.modalita == MODALITA_SOLA_LETTURA:
            return crea_risultato_tool(
                ok=False,
                operation="create",
                status="blocked_readonly",
                error=(
                    "La memoria persistente è disponibile "
                    "solo in lettura."
                ),
            )

        try:
            memoria = carica_archivio(percorso_memoria)
        except (OSError, ValueError) as errore:
            return crea_risultato_tool(
                ok=False,
                operation="create",
                status="tool_error",
                error=str(errore),
            )

        duplicato = _trova_duplicato_esatto(memoria, content)
        if duplicato is not None:
            return crea_risultato_tool(
                ok=False,
                operation="create",
                status="duplicate_detected",
                memory_id=duplicato["id"],
                content=duplicato["content"],
                error="Esiste già un ricordo equivalente.",
            )

        if mode == "proposal":
           
            if stato_sessione.pending_action is not None:
                pending_corrente = stato_sessione.pending_action

                return crea_risultato_tool(
                    ok=False,
                    operation=pending_corrente.operation,
                    status="conflict",
                    memory_id=pending_corrente.target_id,
                    error=(
                        "Esiste già un'operazione di memoria in attesa. "
                        "Confermare o annullare il pending corrente "
                        "prima di iniziarne uno nuovo."
                    ),
                )

            imposta_pending(
                stato_sessione,
                PendingAction(
                    operation="create",
                    phase="confirmation",
                    proposed_content=content.strip(),
                ),
            )

            return crea_risultato_tool(
                ok=False,
                operation="create",
                status="pending_confirmation",
                content=content.strip(),
            )

        try:
            ricordo = aggiungi_ricordo(
                percorso_memoria,
                content,
            )
        except (OSError, ValueError, RuntimeError) as errore:
            return crea_risultato_tool(
                ok=False,
                operation="create",
                status="tool_error",
                error=str(errore),
            )

        pending = stato_sessione.pending_action
        if (
            pending is not None
            and pending.operation == "create"
            and pending.proposed_content is not None
            and normalizza_testo(pending.proposed_content)
            == normalizza_testo(content)
        ):
            annulla_pending(stato_sessione)

        return crea_risultato_tool(
            ok=True,
            operation="create",
            status="created",
            memory_id=ricordo["id"],
            content=ricordo["content"],
        )

    if nome_tool == "modifica_memoria":
        memory_id = argomenti.get("memory_id")
        new_content = argomenti.get("new_content")

        if type(memory_id) is not int or memory_id < 1:
            return crea_risultato_tool(
                ok=False,
                operation="update",
                status="validation_error",
                error="memory_id non valido.",
            )

        if not isinstance(new_content, str) or not new_content.strip():
            return crea_risultato_tool(
                ok=False,
                operation="update",
                status="validation_error",
                error="Il nuovo contenuto non è valido.",
            )

        categoria_sensibile = rileva_contenuto_sensibile(new_content)
        if categoria_sensibile is not None:
            return crea_risultato_tool(
                ok=False,
                operation="update",
                status="blocked_sensitive",
                error=(
                    "Non è stata applicata: il nuovo contenuto sembra "
                    "includere un dato sensibile."
                ),
                sensitive_category=categoria_sensibile,
            )

        if stato_memoria.modalita == MODALITA_DISABILITATA:
            return crea_risultato_tool(
                ok=False,
                operation="update",
                status="blocked_disabled",
                error="La memoria persistente è disabilitata.",
            )

        if stato_memoria.modalita == MODALITA_SOLA_LETTURA:
            return crea_risultato_tool(
                ok=False,
                operation="update",
                status="blocked_readonly",
                error="La memoria è disponibile solo in lettura.",
            )

        try:
            memoria = carica_archivio(percorso_memoria)
        except (OSError, ValueError) as errore:
            return crea_risultato_tool(
                ok=False,
                operation="update",
                status="tool_error",
                error=str(errore),
            )

        ricordo = _trova_ricordo_per_id(memoria, memory_id)
        if ricordo is None:
            return crea_risultato_tool(
                ok=False,
                operation="update",
                status="not_found",
                memory_id=memory_id,
                error="Ricordo non trovato.",
            )

        if stato_sessione.pending_action is not None:
            pending_corrente = stato_sessione.pending_action

            return crea_risultato_tool(
                ok=False,
                operation=pending_corrente.operation,
                status="conflict",
                memory_id=pending_corrente.target_id,
                error=(
                    "Esiste già un'operazione di memoria in attesa. "
                    "Confermare o annullare il pending corrente "
                    "prima di iniziarne uno nuovo."
                ),
            )

        imposta_pending(
            stato_sessione,
            PendingAction(
                operation="update",
                phase="confirmation",
                target_id=memory_id,
                before=ricordo["content"],
                after=new_content.strip(),
            ),
        )

        return crea_risultato_tool(
            ok=False,
            operation="update",
            status="pending_confirmation",
            memory_id=memory_id,
            before=ricordo["content"],
            after=new_content.strip(),
        )

    if nome_tool == "elimina_memoria_per_query":
        query = argomenti.get("query")

        if stato_memoria.modalita == MODALITA_DISABILITATA:
            return crea_risultato_tool(
                ok=False,
                operation="delete",
                status="blocked_disabled",
                error="La memoria persistente è disabilitata.",
            )

        if stato_memoria.modalita == MODALITA_SOLA_LETTURA:
            return crea_risultato_tool(
                ok=False,
                operation="delete",
                status="blocked_readonly",
                error="La memoria è disponibile solo in lettura.",
            )

        try:
            memoria = carica_archivio(percorso_memoria)

            risultato = cerca_memoria(
                memoria,
                query,
                limite=max(limite_ricerca, 2),
            )

            if risultato["total_matches"] == 0:
                for query_fallback in _genera_query_eliminazione(query):
                    risultato_fallback = cerca_memoria(
                        memoria,
                        query_fallback,
                        limite=max(limite_ricerca, 2),
                    )

                    if risultato_fallback["total_matches"] > 0:
                        risultato = risultato_fallback
                        break

        except (TypeError, ValueError) as errore:
            return crea_risultato_tool(
                ok=False,
                operation="delete",
                status="validation_error",
                error=str(errore),
            )

        except OSError as errore:
            return crea_risultato_tool(
                ok=False,
                operation="delete",
                status="tool_error",
                error=str(errore),
            )

        candidati = risultato["results"]
        total_matches = risultato["total_matches"]
        if total_matches == 0:
            return crea_risultato_tool(
                ok=False,
                operation="delete",
                status="not_found",
                error="Nessun ricordo compatibile trovato.",
            )

        if total_matches == 1:
            return esegui_tool_memoria(
                nome_tool="elimina_memoria",
                argomenti={
                    "memory_id": candidati[0]["id"],
                },
                stato_memoria=stato_memoria,
                stato_sessione=stato_sessione,
                percorso_memoria=percorso_memoria,
                limite_ricerca=limite_ricerca,
            )

        return prepara_selezione_memoria(
            operation="delete",
            candidates=candidati,
            stato_sessione=stato_sessione,
        )

    if nome_tool == "elimina_memoria":
        memory_id = argomenti.get("memory_id")

        if type(memory_id) is not int or memory_id < 1:
            return crea_risultato_tool(
                ok=False,
                operation="delete",
                status="validation_error",
                error="memory_id non valido.",
            )

        if stato_memoria.modalita == MODALITA_DISABILITATA:
            return crea_risultato_tool(
                ok=False,
                operation="delete",
                status="blocked_disabled",
                error="La memoria persistente è disabilitata.",
            )

        if stato_memoria.modalita == MODALITA_SOLA_LETTURA:
            return crea_risultato_tool(
                ok=False,
                operation="delete",
                status="blocked_readonly",
                error="La memoria è disponibile solo in lettura.",
            )

        try:
            memoria = carica_archivio(percorso_memoria)
        except (OSError, ValueError) as errore:
            return crea_risultato_tool(
                ok=False,
                operation="delete",
                status="tool_error",
                error=str(errore),
            )

        ricordo = _trova_ricordo_per_id(memoria, memory_id)
        if ricordo is None:
            return crea_risultato_tool(
                ok=False,
                operation="delete",
                status="not_found",
                memory_id=memory_id,
                error="Ricordo non trovato.",
            )


        if stato_sessione.pending_action is not None:
            pending_corrente = stato_sessione.pending_action

            return crea_risultato_tool(
                ok=False,
                operation=pending_corrente.operation,
                status="conflict",
                memory_id=pending_corrente.target_id,
                error=(
                    "Esiste già un'operazione di memoria in attesa. "
                    "Confermare o annullare il pending corrente "
                    "prima di iniziarne uno nuovo."
                ),
            )

        imposta_pending(
            stato_sessione,
            PendingAction(
                operation="delete",
                phase="confirmation",
                target_id=memory_id,
                before=ricordo["content"],
            ),
        )

        return crea_risultato_tool(
            ok=False,
            operation="delete",
            status="pending_confirmation",
            memory_id=memory_id,
            content=ricordo["content"],
        )

    if nome_tool == "ripristina_memoria":
        memory_id = argomenti.get("memory_id")

        if type(memory_id) is not int or memory_id < 1:
            return crea_risultato_tool(
                ok=False,
                operation="restore",
                status="validation_error",
                error="memory_id non valido.",
            )

        if stato_memoria.modalita == MODALITA_DISABILITATA:
            return crea_risultato_tool(
                ok=False,
                operation="restore",
                status="blocked_disabled",
                error="La memoria persistente è disabilitata.",
            )

        if stato_memoria.modalita == MODALITA_SOLA_LETTURA:
            return crea_risultato_tool(
                ok=False,
                operation="restore",
                status="blocked_readonly",
                error="La memoria è disponibile solo in lettura.",
            )

        percorso_cestino = percorso_memoria.with_name(
            "deleted_memories.json"
        )

        percorso_backup_cestino = percorso_cestino.with_name(
            f"{percorso_cestino.stem}.backup{percorso_cestino.suffix}"
        )

        try:
            cestino = carica_cestino_con_recovery(
                percorso_cestino,
                percorso_backup_cestino,
            )
        except (OSError, ValueError, RuntimeError) as errore:
            return crea_risultato_tool(
                ok=False,
                operation="restore",
                status="tool_error",
                error=str(errore),
            )

        if cestino is None:
            return crea_risultato_tool(
                ok=False,
                operation="restore",
                status="not_found",
                memory_id=memory_id,
                error="Il cestino non esiste.",
            )

        ricordo = _trova_ricordo_eliminato_per_id(cestino, memory_id)
        if ricordo is None:
            return crea_risultato_tool(
                ok=False,
                operation="restore",
                status="not_found",
                memory_id=memory_id,
                error="Ricordo eliminato non trovato.",
            )

        if stato_sessione.pending_action is not None:
            pending_corrente = stato_sessione.pending_action

            return crea_risultato_tool(
                ok=False,
                operation=pending_corrente.operation,
                status="conflict",
                memory_id=pending_corrente.target_id,
                error=(
                    "Esiste già un'operazione di memoria in attesa. "
                    "Confermare o annullare il pending corrente "
                    "prima di iniziarne uno nuovo."
                ),
            )

        imposta_pending(
            stato_sessione,
            PendingAction(
                operation="restore",
                phase="confirmation",
                target_id=memory_id,
                before=ricordo["content"],
            ),
        )

        return crea_risultato_tool(
            ok=False,
            operation="restore",
            status="pending_confirmation",
            memory_id=memory_id,
            content=ricordo["content"],
        )

    if nome_tool == "gestisci_pending_memoria":
        decision = argomenti.get("decision")
        memory_id = argomenti.get("memory_id")

        if decision not in {"confirm", "cancel", "select"}:
            return crea_risultato_tool(
                ok=False,
                operation="pending",
                status="validation_error",
                error=(
                    "decision deve essere 'confirm', "
                    "'cancel' oppure 'select'."
                ),
            )

        pending = stato_sessione.pending_action

        if pending is None:
            return crea_risultato_tool(
                ok=False,
                operation="pending",
                status="not_found",
                error=(
                    "Non esiste alcuna operazione di memoria "
                    "in attesa."
                ),
            )

        if decision == "cancel":
            operation = pending.operation
            content = pending.proposed_content

            if operation == "create" and content is not None:
                registra_proposta_rifiutata(
                    stato_sessione,
                    content,
                )

            annulla_pending(stato_sessione)

            return crea_risultato_tool(
                ok=True,
                operation=operation,
                status="cancelled",
                content=content,
            )

        if decision == "select":
            if pending.phase != "selection":
                return crea_risultato_tool(
                    ok=False,
                    operation=pending.operation,
                    status="validation_error",
                    error=(
                        "Il pending corrente non richiede "
                        "la selezione di un candidato."
                    ),
                )

            if type(memory_id) is not int or memory_id < 1:
                return crea_risultato_tool(
                    ok=False,
                    operation=pending.operation,
                    status="validation_error",
                    error="memory_id non valido.",
                )

            candidato = None
            for elemento in pending.candidates:
                if elemento.get("id") == memory_id:
                    candidato = elemento
                    break

            if candidato is None:
                return crea_risultato_tool(
                    ok=False,
                    operation=pending.operation,
                    status="not_found",
                    memory_id=memory_id,
                    error=(
                        "L'ID scelto non appartiene ai "
                        "candidati disponibili."
                    ),
                )

            nuova_azione = PendingAction(
                operation=pending.operation,
                phase="confirmation",
                target_id=memory_id,
                before=candidato.get("content"),
                after=pending.after,
                proposed_content=pending.proposed_content,
            )

            imposta_pending(
                stato_sessione,
                nuova_azione,
            )

            return crea_risultato_tool(
                ok=False,
                operation=nuova_azione.operation,
                status="pending_confirmation",
                memory_id=memory_id,
                content=candidato.get("content"),
                before=nuova_azione.before,
                after=nuova_azione.after,
            )

        if pending.phase != "confirmation":
            return crea_risultato_tool(
                ok=False,
                operation=pending.operation,
                status="validation_error",
                error=(
                    "Il pending corrente non è ancora "
                    "pronto per la conferma."
                ),
            )

        if stato_memoria.modalita == MODALITA_DISABILITATA:
            return crea_risultato_tool(
                ok=False,
                operation=pending.operation,
                status="blocked_disabled",
                error="La memoria persistente è disabilitata.",
            )

        if stato_memoria.modalita == MODALITA_SOLA_LETTURA:
            return crea_risultato_tool(
                ok=False,
                operation=pending.operation,
                status="blocked_readonly",
                error=(
                    "La memoria persistente è disponibile "
                    "solo in lettura."
                ),
            )

        if pending.operation == "create":
            content = pending.proposed_content

            if not isinstance(content, str) or not content.strip():
                return crea_risultato_tool(
                    ok=False,
                    operation="create",
                    status="validation_error",
                    error=(
                        "Il pending di creazione non contiene "
                        "un contenuto valido."
                    ),
                )

            try:
                memoria = carica_archivio(percorso_memoria)
                duplicato = _trova_duplicato_esatto(
                    memoria,
                    content,
                )

                if duplicato is not None:
                    annulla_pending(stato_sessione)
                    return crea_risultato_tool(
                        ok=False,
                        operation="create",
                        status="duplicate_detected",
                        memory_id=duplicato["id"],
                        content=duplicato["content"],
                        error="Esiste già un ricordo equivalente.",
                    )

                ricordo = aggiungi_ricordo(
                    percorso_memoria,
                    content,
                )
            except (OSError, ValueError, RuntimeError) as errore:
                return crea_risultato_tool(
                    ok=False,
                    operation="create",
                    status="tool_error",
                    error=str(errore),
                )

            annulla_pending(stato_sessione)

            return crea_risultato_tool(
                ok=True,
                operation="create",
                status="created",
                memory_id=ricordo["id"],
                content=ricordo["content"],
            )

        if pending.operation == "update":
            if pending.target_id is None or pending.after is None:
                return crea_risultato_tool(
                    ok=False,
                    operation="update",
                    status="validation_error",
                    error="Il pending di modifica è incompleto.",
                )

            try:
                ricordo = aggiorna_ricordo(
                    percorso_memoria,
                    pending.target_id,
                    pending.after,
                )
            except (OSError, ValueError, RuntimeError) as errore:
                return crea_risultato_tool(
                    ok=False,
                    operation="update",
                    status="tool_error",
                    error=str(errore),
                )

            before = pending.before
            annulla_pending(stato_sessione)

            return crea_risultato_tool(
                ok=True,
                operation="update",
                status="updated",
                memory_id=ricordo["id"],
                content=ricordo["content"],
                before=before,
                after=ricordo["content"],
            )

        if pending.operation == "delete":
            if pending.target_id is None:
                return crea_risultato_tool(
                    ok=False,
                    operation="delete",
                    status="validation_error",
                    error="Il pending di eliminazione non contiene un ID.",
                )

            percorso_cestino = percorso_memoria.with_name(
                "deleted_memories.json"
            )

            try:
                ricordo = elimina_ricordo(
                    percorso_memoria,
                    percorso_cestino,
                    pending.target_id,
                )
            except (OSError, ValueError, RuntimeError) as errore:
                return crea_risultato_tool(
                    ok=False,
                    operation="delete",
                    status="tool_error",
                    error=str(errore),
                )

            annulla_pending(stato_sessione)

            return crea_risultato_tool(
                ok=True,
                operation="delete",
                status="deleted",
                memory_id=ricordo["id"],
                content=ricordo["content"],
            )

        if pending.operation == "restore":
            if pending.target_id is None:
                return crea_risultato_tool(
                    ok=False,
                    operation="restore",
                    status="validation_error",
                    error="Il pending di ripristino non contiene un ID.",
                )

            percorso_cestino = percorso_memoria.with_name(
                "deleted_memories.json"
            )

            try:
                ricordo = ripristina_ricordo(
                    percorso_memoria,
                    percorso_cestino,
                    pending.target_id,
                )
            except (OSError, ValueError, RuntimeError) as errore:
                return crea_risultato_tool(
                    ok=False,
                    operation="restore",
                    status="tool_error",
                    error=str(errore),
                )

            annulla_pending(stato_sessione)

            return crea_risultato_tool(
                ok=True,
                operation="restore",
                status="restored",
                memory_id=ricordo["id"],
                content=ricordo["content"],
            )

        return crea_risultato_tool(
            ok=False,
            operation=pending.operation,
            status="tool_error",
            error="Operazione pending non gestita.",
        )

    return crea_risultato_tool(
        ok=False,
        operation=nome_tool,
        status="tool_error",
        error=f"Tool memoria non gestito: {nome_tool}.",
    )
