"""Stato temporaneo della memoria durante una sessione di chat."""

from copy import deepcopy
from dataclasses import dataclass, field


OPERAZIONI_PENDING = frozenset({
    "create",
    "update",
    "delete",
    "restore",
})

FASI_PENDING = frozenset({
    "selection",
    "confirmation",
})

MAX_TURNI_PENDING_INATTIVI = 3


@dataclass
class PendingAction:
    """Descrive un'operazione memoria in attesa."""

    operation: str
    phase: str

    target_id: int | None = None
    candidates: list[dict] = field(default_factory=list)

    before: str | None = None
    after: str | None = None

    proposed_content: str | None = None


@dataclass
class MemorySessionState:
    """Stato memoria valido soltanto per la sessione corrente."""

    pending_action: PendingAction | None = None
    rejected_proposals: set[str] = field(default_factory=set)
    pending_idle_turns: int = 0


def normalizza_testo(testo: str) -> str:
    """Normalizza testo per confronti interni di sessione."""

    if not isinstance(testo, str) or not testo.strip():
        raise ValueError(
            "Il testo da normalizzare deve essere una stringa non vuota."
        )

    return " ".join(testo.casefold().split())


def valida_pending_action(azione: PendingAction) -> None:
    """Verifica la struttura generale di un'azione pending."""

    if not isinstance(azione, PendingAction):
        raise TypeError(
            "L'azione pending deve essere un oggetto PendingAction."
        )

    if azione.operation not in OPERAZIONI_PENDING:
        raise ValueError(
            f"Operazione pending non supportata: {azione.operation}."
        )

    if azione.phase not in FASI_PENDING:
        raise ValueError(
            f"Fase pending non supportata: {azione.phase}."
        )

    if azione.target_id is not None:
        if type(azione.target_id) is not int or azione.target_id < 1:
            raise ValueError(
                "target_id deve essere un intero maggiore o uguale a 1."
            )

    if not isinstance(azione.candidates, list):
        raise TypeError(
            "candidates deve essere una lista."
        )

    for candidato in azione.candidates:
        if not isinstance(candidato, dict):
            raise TypeError(
                "Ogni candidato deve essere un dizionario."
            )


def imposta_pending(
    stato: MemorySessionState,
    azione: PendingAction,
) -> None:
    """Imposta una nuova azione pending nella sessione."""

    valida_pending_action(azione)

    stato.pending_action = deepcopy(azione)
    stato.pending_idle_turns = 0


def annulla_pending(stato: MemorySessionState) -> None:
    """Annulla l'eventuale azione pending."""

    stato.pending_action = None
    stato.pending_idle_turns = 0


def pending_attivo(stato: MemorySessionState) -> bool:
    """Restituisce True se esiste un'azione pending."""

    return stato.pending_action is not None


def registra_turno_pertinente(
    stato: MemorySessionState,
) -> None:
    """Azzera il contatore di inattività del pending."""

    if pending_attivo(stato):
        stato.pending_idle_turns = 0


def registra_turno_non_pertinente(
    stato: MemorySessionState,
    limite: int = MAX_TURNI_PENDING_INATTIVI,
) -> bool:
    """
    Incrementa i turni inattivi.

    Restituisce True se il pending è scaduto e viene annullato.
    """

    if type(limite) is not int or limite < 1:
        raise ValueError(
            "Il limite dei turni deve essere un intero maggiore "
            "o uguale a 1."
        )

    if not pending_attivo(stato):
        return False

    stato.pending_idle_turns += 1

    if stato.pending_idle_turns >= limite:
        annulla_pending(stato)
        return True

    return False


def registra_proposta_rifiutata(
    stato: MemorySessionState,
    contenuto: str,
) -> None:
    """Registra un rifiuto valido soltanto nella sessione corrente."""

    stato.rejected_proposals.add(
        normalizza_testo(contenuto)
    )


def proposta_gia_rifiutata(
    stato: MemorySessionState,
    contenuto: str,
) -> bool:
    """Controlla se la stessa proposta è già stata rifiutata."""

    return (
        normalizza_testo(contenuto)
        in stato.rejected_proposals
    )