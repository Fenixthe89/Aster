"""Gestione dello storage della memoria persistente di Aster."""

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


SCHEMA_VERSION = 1


MODALITA_NORMALE = "normale"
MODALITA_SOLA_LETTURA = "sola_lettura"
MODALITA_DISABILITATA = "disabilitata"


@dataclass(frozen=True)
class StatoMemoria:
    """Descrive lo stato della memoria persistente nella sessione corrente."""

    memoria: dict | None
    modalita: str
    messaggio: str | None = None


def crea_archivio_vuoto() -> dict:
    """Restituisce la struttura iniziale di un archivio memoria valido."""

    return {
        "meta": {
            "schema_version": SCHEMA_VERSION,
        },
        "memories": [],
    }


def valida_archivio(memoria: object) -> None:
    """
    Verifica che la struttura generale dell'archivio memoria sia valida.

    La struttura dei singoli ricordi non viene ancora controllata:
    sarà definita nelle versioni successive.
    """

    if not isinstance(memoria, dict):
        raise ValueError(
            "La memoria deve contenere un oggetto JSON."
        )

    meta = memoria.get("meta")

    if not isinstance(meta, dict):
        raise ValueError(
            "La memoria non contiene un campo 'meta' valido."
        )

    versione_schema = meta.get("schema_version")

    if type(versione_schema) is not int:
        raise ValueError(
            "Il campo 'meta.schema_version' deve essere un numero intero."
        )

    if versione_schema != SCHEMA_VERSION:
        raise ValueError(
            f"Schema memoria non supportato: {versione_schema}. "
            f"Versione supportata: {SCHEMA_VERSION}."
        )

    memories = memoria.get("memories")

    if not isinstance(memories, list):
        raise ValueError(
            "La memoria non contiene un campo 'memories' valido."
        )

def carica_archivio(percorso: Path) -> dict:
    """
    Carica un archivio memoria dal disco e ne valida la struttura.

    Le eccezioni vengono lasciate propagare al chiamante, che potrà
    distinguere tra file mancante, JSON non valido e struttura non valida.
    """

    with percorso.open("r", encoding="utf-8") as file:
        memoria = json.load(file)

    valida_archivio(memoria)

    return memoria

def crea_archivio_su_disco(percorso: Path) -> dict:
    """
    Crea un nuovo archivio memoria sul disco in modo sicuro.

    Il contenuto viene prima scritto e validato in un file temporaneo.
    Solo dopo la validazione il file temporaneo sostituisce atomicamente
    il percorso della memoria principale.
    """

    memoria = crea_archivio_vuoto()

    percorso.parent.mkdir(parents=True, exist_ok=True)

    percorso_temporaneo = percorso.with_suffix(".tmp")

    with percorso_temporaneo.open("w", encoding="utf-8") as file:
        json.dump(
            memoria,
            file,
            ensure_ascii=False,
            indent=4,
        )
        file.write("\n")

    carica_archivio(percorso_temporaneo)

    os.replace(percorso_temporaneo, percorso)

    return memoria

def salva_archivio(percorso: Path, memoria: dict) -> None:
    """
    Salva un archivio memoria in modo sicuro.

    La nuova memoria viene prima scritta e validata in un file temporaneo.
    Se esiste una memoria principale valida, viene conservata come backup
    prima della sostituzione atomica del file principale.
    """

    valida_archivio(memoria)

    percorso.parent.mkdir(parents=True, exist_ok=True)

    percorso_temporaneo = percorso.with_suffix(".tmp")

    percorso_backup = percorso.with_name(
        f"{percorso.stem}.backup{percorso.suffix}"
    )

    percorso_backup_temporaneo = percorso.with_name(
        f"{percorso.stem}.backup.tmp"
    )

    # Se il main manca ma esiste già un backup, non si tratta
    # di una prima inizializzazione: il salvataggio viene bloccato.
    if not percorso.exists() and percorso_backup.exists():
        raise RuntimeError(
            "Salvataggio bloccato: la memoria principale non esiste, "
            "ma è presente un backup. La memoria persistente deve "
            "restare in sola lettura fino al ripristino esplicito."
        )


    # Se esiste una memoria principale precedente, deve essere valida.
    # Se è corrotta, il salvataggio viene bloccato senza modificarla.
    if percorso.exists():
        carica_archivio(percorso)

    # Scrive la nuova memoria nel file temporaneo.
    with percorso_temporaneo.open("w", encoding="utf-8") as file:
        json.dump(
            memoria,
            file,
            ensure_ascii=False,
            indent=4,
        )
        file.write("\n")

    # Rilegge il temporaneo per assicurarsi che sia valido.
    carica_archivio(percorso_temporaneo)

    # Se esiste una memoria precedente valida, prepara il backup
    # senza sovrascrivere direttamente l'eventuale backup esistente.
    if percorso.exists():
        shutil.copy2(percorso, percorso_backup_temporaneo)

        carica_archivio(percorso_backup_temporaneo)

        os.replace(
            percorso_backup_temporaneo,
            percorso_backup,
        )

    # La nuova memoria diventa quella principale solo alla fine.
    os.replace(
        percorso_temporaneo,
        percorso,
    )

def inizializza_memoria(percorso: Path) -> StatoMemoria:
    """
    Inizializza la memoria persistente di Aster.

    Gestisce la prima creazione, il caricamento normale e il recupero
    tramite backup quando la memoria principale non è utilizzabile.
    """

    percorso_backup = percorso.with_name(
        f"{percorso.stem}.backup{percorso.suffix}"
    )

    # ---------------------------------------------------------
    # MEMORIA PRINCIPALE MANCANTE
    # ---------------------------------------------------------

    if not percorso.exists():

        # Se esiste già un backup, non creare una memoria vuota:
        # preserva i dati disponibili e usa il backup in sola lettura.
        if percorso_backup.exists():
            try:
                memoria = carica_archivio(percorso_backup)

            except (OSError, json.JSONDecodeError, ValueError) as errore:
                return StatoMemoria(
                    memoria=None,
                    modalita=MODALITA_DISABILITATA,
                    messaggio=(
                        "La memoria principale non esiste e il backup "
                        f"non è utilizzabile: {errore}"
                    ),
                )

            return StatoMemoria(
                memoria=memoria,
                modalita=MODALITA_SOLA_LETTURA,
                messaggio=(
                    "La memoria principale non esiste. "
                    "È stato caricato il backup in sola lettura."
                ),
            )

        # Prima esecuzione reale: non esiste né main né backup.
        try:
            memoria = crea_archivio_su_disco(percorso)

        except (OSError, json.JSONDecodeError, ValueError) as errore:
            return StatoMemoria(
                memoria=None,
                modalita=MODALITA_DISABILITATA,
                messaggio=(
                    "Impossibile creare la memoria persistente: "
                    f"{errore}"
                ),
            )

        return StatoMemoria(
            memoria=memoria,
            modalita=MODALITA_NORMALE,
        )

    # ---------------------------------------------------------
    # MEMORIA PRINCIPALE PRESENTE
    # ---------------------------------------------------------

    errore_principale = None

    try:
        memoria = carica_archivio(percorso)

        return StatoMemoria(
            memoria=memoria,
            modalita=MODALITA_NORMALE,
        )

    except (OSError, json.JSONDecodeError, ValueError) as errore:
        errore_principale = str(errore)

    # ---------------------------------------------------------
    # PRINCIPALE NON UTILIZZABILE: PROVA IL BACKUP
    # ---------------------------------------------------------

    if not percorso_backup.exists():
        return StatoMemoria(
            memoria=None,
            modalita=MODALITA_DISABILITATA,
            messaggio=(
                "La memoria principale non è utilizzabile "
                f"({errore_principale}) e non esiste un backup valido. "
                "La memoria persistente è disabilitata per questa sessione."
            ),
        )

    try:
        memoria_backup = carica_archivio(percorso_backup)

    except (OSError, json.JSONDecodeError, ValueError) as errore_backup:
        return StatoMemoria(
            memoria=None,
            modalita=MODALITA_DISABILITATA,
            messaggio=(
                "La memoria principale non è utilizzabile "
                f"({errore_principale}) e anche il backup non è utilizzabile "
                f"({errore_backup}). "
                "La memoria persistente è disabilitata per questa sessione."
            ),
        )

    return StatoMemoria(
        memoria=memoria_backup,
        modalita=MODALITA_SOLA_LETTURA,
        messaggio=(
            "La memoria principale non è utilizzabile "
            f"({errore_principale}). "
            "È stato caricato il backup in sola lettura."
        ),
    )

