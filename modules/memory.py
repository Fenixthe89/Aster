"""Gestione dello storage della memoria persistente di Aster."""

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


SCHEMA_VERSION = 2


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
            "next_memory_id": 1,
        },
        "memories": [],
    }

def crea_cestino_vuoto() -> dict:
    """
    Restituisce la struttura iniziale di un archivio
    dei ricordi eliminati.
    """

    return {
        "meta": {
            "schema_version": SCHEMA_VERSION,
        },
        "deleted_memories": [],
    }

def migra_archivio_v1_a_v2(memoria: dict) -> dict:
    """
    Converte un archivio memoria dallo schema 1 allo schema 2.

    La migrazione è consentita solo se l'archivio v1 non contiene
    ancora ricordi, perché lo schema dei singoli ricordi non era
    definito nella versione 1.
    """

    meta = memoria.get("meta")
    memories = memoria.get("memories")

    if not isinstance(meta, dict):
        raise ValueError(
            "Impossibile migrare: campo 'meta' non valido."
        )

    if meta.get("schema_version") != 1:
        raise ValueError(
            "Impossibile migrare: l'archivio non usa lo schema 1."
        )

    if not isinstance(memories, list):
        raise ValueError(
            "Impossibile migrare: campo 'memories' non valido."
        )

    if memories:
        raise ValueError(
            "Impossibile migrare automaticamente un archivio v1 "
            "che contiene già dei ricordi."
        )

    return {
        "meta": {
            "schema_version": 2,
            "next_memory_id": 1,
        },
        "memories": [],
    }

def timestamp_corrente() -> str:
    """
    Restituisce data e ora locale in formato ISO 8601,
    includendo il fuso orario.
    """

    return datetime.now().astimezone().isoformat(timespec="seconds")

def crea_ricordo(memoria: dict, content: str) -> dict:
    """
    Crea un nuovo ricordo usando il prossimo ID disponibile.
    Aggiorna next_memory_id nell'archivio passato.
    """

    if not isinstance(content, str) or not content.strip():
        raise ValueError(
            "Il contenuto del ricordo deve essere una stringa non vuota."
        )

    valida_archivio(memoria)

    memory_id = memoria["meta"]["next_memory_id"]
    timestamp = timestamp_corrente()

    ricordo = {
        "id": memory_id,
        "content": content.strip(),
        "created_at": timestamp,
        "updated_at": timestamp,
    }

    valida_ricordo(ricordo)

    memoria["memories"].append(ricordo)
    memoria["meta"]["next_memory_id"] += 1

    return ricordo.copy()

def modifica_ricordo(memoria: dict, memory_id: int, nuovo_content: str) -> dict:
    """
    Modifica il contenuto di un ricordo esistente.
    Mantiene created_at e aggiorna updated_at.
    """

    valida_archivio(memoria)

    if type(memory_id) is not int or memory_id < 1:
        raise ValueError(
            "L'ID del ricordo deve essere un numero intero maggiore o uguale a 1."
        )

    if not isinstance(nuovo_content, str) or not nuovo_content.strip():
        raise ValueError(
            "Il nuovo contenuto del ricordo deve essere una stringa non vuota."
        )

    for ricordo in memoria["memories"]:
        if ricordo["id"] == memory_id:
            ricordo["content"] = nuovo_content.strip()
            ricordo["updated_at"] = timestamp_corrente()

            valida_ricordo(ricordo)

            return ricordo.copy()

    raise ValueError(
        f"Nessun ricordo trovato con ID {memory_id}."
    )

def cerca_memoria(
    memoria: dict,
    query: str,
    limite: int = 5,
) -> dict:
    """
    Cerca ricordi nella memoria attiva tramite confronto testuale semplice.

    La ricerca ignora maiuscole/minuscole e normalizza gli spazi.
    Restituisce al massimo il numero di risultati indicato da limite.
    """

    valida_archivio(memoria)

    if not isinstance(query, str) or not query.strip():
        raise ValueError(
            "La query di ricerca deve essere una stringa non vuota."
        )

    if type(limite) is not int or limite < 1:
        raise ValueError(
            "Il limite dei risultati deve essere un numero intero "
            "maggiore o uguale a 1."
        )

    query_normalizzata = " ".join(
        query.casefold().split()
    )

    corrispondenze = []

    for ricordo in memoria["memories"]:
        content_normalizzato = " ".join(
            ricordo["content"].casefold().split()
        )

        if query_normalizzata in content_normalizzato:
            corrispondenze.append(ricordo.copy())

    risultati = corrispondenze[:limite]

    return {
        "results": risultati,
        "returned": len(risultati),
        "total_matches": len(corrispondenze),
        "has_more": len(corrispondenze) > len(risultati),
    }

def valida_ricordo(ricordo: object) -> None:
    """
    Verifica che un singolo ricordo rispetti lo schema previsto.
    """

    if not isinstance(ricordo, dict):
        raise ValueError(
            "Ogni ricordo deve essere un oggetto JSON."
        )

    memory_id = ricordo.get("id")

    if type(memory_id) is not int:
        raise ValueError(
            "Il campo 'id' del ricordo deve essere un numero intero."
        )

    if memory_id < 1:
        raise ValueError(
            "Il campo 'id' del ricordo deve essere maggiore o uguale a 1."
        )

    content = ricordo.get("content")

    if not isinstance(content, str) or not content.strip():
        raise ValueError(
            "Il campo 'content' del ricordo deve essere una stringa non vuota."
        )

    created_at = ricordo.get("created_at")

    if not isinstance(created_at, str) or not created_at.strip():
        raise ValueError(
            "Il campo 'created_at' del ricordo deve essere una stringa non vuota."
        )

    updated_at = ricordo.get("updated_at")

    if not isinstance(updated_at, str) or not updated_at.strip():
        raise ValueError(
            "Il campo 'updated_at' del ricordo deve essere una stringa non vuota."
        )

def valida_ricordo_eliminato(ricordo: object) -> None:
    """
    Verifica che un ricordo eliminato sia valido.
    Mantiene tutti i campi del ricordo originale e aggiunge deleted_at.
    """

    valida_ricordo(ricordo)

    deleted_at = ricordo.get("deleted_at")

    if not isinstance(deleted_at, str) or not deleted_at.strip():
        raise ValueError(
            "Il campo 'deleted_at' del ricordo eliminato "
            "deve essere una stringa non vuota."
        )

def valida_cestino(cestino: object) -> None:
    """
    Verifica la struttura dell'archivio dei ricordi eliminati.
    """

    if not isinstance(cestino, dict):
        raise ValueError(
            "Il cestino deve contenere un oggetto JSON."
        )

    meta = cestino.get("meta")

    if not isinstance(meta, dict):
        raise ValueError(
            "Il cestino non contiene un campo 'meta' valido."
        )

    versione_schema = meta.get("schema_version")

    if type(versione_schema) is not int:
        raise ValueError(
            "Il campo 'meta.schema_version' del cestino "
            "deve essere un numero intero."
        )

    if versione_schema != SCHEMA_VERSION:
        raise ValueError(
            f"Schema cestino non supportato: {versione_schema}. "
            f"Versione supportata: {SCHEMA_VERSION}."
        )

    deleted_memories = cestino.get("deleted_memories")

    if not isinstance(deleted_memories, list):
        raise ValueError(
            "Il cestino non contiene un campo 'deleted_memories' valido."
        )

    ids_visti = set()

    for ricordo in deleted_memories:
        valida_ricordo_eliminato(ricordo)

        memory_id = ricordo["id"]

        if memory_id in ids_visti:
            raise ValueError(
                f"ID ricordo duplicato nel cestino: {memory_id}."
            )

        ids_visti.add(memory_id)

def valida_archivio(memoria: object) -> None:
    """
    Verifica che la struttura generale dell'archivio memoria sia valida.
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

    next_memory_id = meta.get("next_memory_id")

    if type(next_memory_id) is not int:
        raise ValueError(
            "Il campo 'meta.next_memory_id' deve essere un numero intero."
        )

    if next_memory_id < 1:
        raise ValueError(
            "Il campo 'meta.next_memory_id' deve essere maggiore o uguale a 1."
        )

    memories = memoria.get("memories")

    if not isinstance(memories, list):
        raise ValueError(
            "La memoria non contiene un campo 'memories' valido."
        )
    ids_visti = set()

    for ricordo in memories:
        valida_ricordo(ricordo)

        memory_id = ricordo["id"]

        if memory_id in ids_visti:
            raise ValueError(
                f"ID ricordo duplicato: {memory_id}."
        )

        ids_visti.add(memory_id)
    
    if ids_visti and next_memory_id <= max(ids_visti):
        raise ValueError(
            "Il campo 'meta.next_memory_id' deve essere maggiore "
            "di tutti gli ID dei ricordi esistenti."
        )

def carica_archivio(percorso: Path) -> dict:
    """
    Carica un archivio memoria dal disco e ne valida la struttura.

    Se viene trovato un archivio schema 1 e la versione corrente
    è lo schema 2, esegue la migrazione in memoria prima della validazione.

    Le eccezioni vengono lasciate propagare al chiamante, che potrà
    distinguere tra file mancante, JSON non valido e struttura non valida.
    """

    with percorso.open("r", encoding="utf-8") as file:
        memoria = json.load(file)

    if SCHEMA_VERSION == 2 and isinstance(memoria, dict):
        meta = memoria.get("meta")

        if (
            isinstance(meta, dict)
            and meta.get("schema_version") == 1
        ):
            memoria = migra_archivio_v1_a_v2(memoria)

    valida_archivio(memoria)

    return memoria

def carica_cestino(percorso: Path) -> dict:
    """
    Carica l'archivio dei ricordi eliminati dal disco
    e ne valida la struttura.
    """

    with percorso.open("r", encoding="utf-8") as file:
        cestino = json.load(file)

    valida_cestino(cestino)

    return cestino

def salva_cestino(percorso: Path, cestino: dict) -> None:
    """
    Salva l'archivio dei ricordi eliminati in modo sicuro.

    Il nuovo cestino viene prima scritto e validato in un file temporaneo.
    Se esiste un cestino precedente valido, viene conservato come backup
    prima della sostituzione atomica del file principale.
    """

    valida_cestino(cestino)

    percorso.parent.mkdir(parents=True, exist_ok=True)

    percorso_temporaneo = percorso.with_suffix(".tmp")

    percorso_backup = percorso.with_name(
        f"{percorso.stem}.backup{percorso.suffix}"
    )

    percorso_backup_temporaneo = percorso.with_name(
        f"{percorso.stem}.backup.tmp"
    )

    if not percorso.exists() and percorso_backup.exists():
        raise RuntimeError(
            "Salvataggio bloccato: il cestino principale non esiste, "
            "ma è presente un backup."
        )

    if percorso.exists():
        carica_cestino(percorso)

    with percorso_temporaneo.open("w", encoding="utf-8") as file:
        json.dump(
            cestino,
            file,
            ensure_ascii=False,
            indent=4,
        )
        file.write("\n")

    carica_cestino(percorso_temporaneo)

    if percorso.exists():
        shutil.copy2(
            percorso,
            percorso_backup_temporaneo,
        )

        carica_cestino(percorso_backup_temporaneo)

        os.replace(
            percorso_backup_temporaneo,
            percorso_backup,
        )

    os.replace(
        percorso_temporaneo,
        percorso,
    )

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

def aggiungi_ricordo(percorso: Path, content: str) -> dict:
    """
    Carica l'archivio, crea un nuovo ricordo e lo salva su disco.
    Restituisce il ricordo appena creato.
    """

    memoria = carica_archivio(percorso)

    ricordo = crea_ricordo(memoria, content)

    salva_archivio(percorso, memoria)

    return ricordo

def aggiorna_ricordo(percorso: Path, memory_id: int, nuovo_content: str) -> dict:
    """
    Carica l'archivio, modifica un ricordo esistente e salva le modifiche.
    Restituisce il ricordo aggiornato.
    """

    memoria = carica_archivio(percorso)

    ricordo = modifica_ricordo(
        memoria,
        memory_id,
        nuovo_content,
    )

    salva_archivio(percorso, memoria)

    return ricordo

def elimina_ricordo(
    percorso_memoria: Path,
    percorso_cestino: Path,
    memory_id: int,
) -> dict:
    """
    Sposta un ricordo dalla memoria attiva al cestino.

    Il cestino viene salvato prima della rimozione dalla memoria attiva,
    così un'interruzione può produrre temporaneamente due copie,
    ma non la perdita del ricordo.

    La funzione supporta anche il retry dopo un'interruzione parziale.
    """

    if type(memory_id) is not int or memory_id < 1:
        raise ValueError(
            "L'ID del ricordo deve essere un numero intero "
            "maggiore o uguale a 1."
        )

    memoria = carica_archivio(percorso_memoria)

    ricordo = None

    for elemento in memoria["memories"]:
        if elemento["id"] == memory_id:
            ricordo = elemento
            break

    if ricordo is None:
        raise ValueError(
            f"Nessun ricordo trovato con ID {memory_id}."
        )

    if percorso_cestino.exists():
        cestino = carica_cestino(percorso_cestino)
    else:
        cestino = crea_cestino_vuoto()

    ricordo_gia_nel_cestino = None

    for elemento in cestino["deleted_memories"]:
        if elemento["id"] == memory_id:
            ricordo_gia_nel_cestino = elemento
            break

    if ricordo_gia_nel_cestino is None:
        ricordo_eliminato = ricordo.copy()
        ricordo_eliminato["deleted_at"] = timestamp_corrente()

        valida_ricordo_eliminato(ricordo_eliminato)

        cestino["deleted_memories"].append(ricordo_eliminato)

        # Prima viene messa al sicuro la copia nel cestino.
        salva_cestino(percorso_cestino, cestino)

    else:
        # Possibile retry dopo una cancellazione interrotta.
        if (
            ricordo_gia_nel_cestino["content"] != ricordo["content"]
            or ricordo_gia_nel_cestino["created_at"] != ricordo["created_at"]
            or ricordo_gia_nel_cestino["updated_at"] != ricordo["updated_at"]
        ):
            raise ValueError(
                f"Conflitto: l'ID {memory_id} esiste già nel cestino "
                "ma contiene dati differenti."
            )

        ricordo_eliminato = ricordo_gia_nel_cestino

    # Solo dopo che il cestino è sicuro rimuoviamo il ricordo attivo.
    memoria["memories"].remove(ricordo)

    salva_archivio(percorso_memoria, memoria)

    return ricordo_eliminato

def ripristina_ricordo(
    percorso_memoria: Path,
    percorso_cestino: Path,
    memory_id: int,
) -> dict:
    """
    Ripristina un ricordo dal cestino alla memoria attiva.

    La memoria attiva viene salvata prima della rimozione dal cestino,
    così un'interruzione può produrre temporaneamente due copie,
    ma non la perdita del ricordo.

    La funzione supporta anche il retry dopo un'interruzione parziale.
    """

    if type(memory_id) is not int or memory_id < 1:
        raise ValueError(
            "L'ID del ricordo deve essere un numero intero "
            "maggiore o uguale a 1."
        )

    memoria = carica_archivio(percorso_memoria)
    cestino = carica_cestino(percorso_cestino)

    ricordo_eliminato = None

    for elemento in cestino["deleted_memories"]:
        if elemento["id"] == memory_id:
            ricordo_eliminato = elemento
            break

    if ricordo_eliminato is None:
        raise ValueError(
            f"Nessun ricordo eliminato trovato con ID {memory_id}."
        )

    ricordo_attivo = None

    for elemento in memoria["memories"]:
        if elemento["id"] == memory_id:
            ricordo_attivo = elemento
            break

    ricordo_ripristinato = ricordo_eliminato.copy()
    ricordo_ripristinato.pop("deleted_at", None)

    valida_ricordo(ricordo_ripristinato)

    if ricordo_attivo is None:
        memoria["memories"].append(ricordo_ripristinato)

        if memoria["meta"]["next_memory_id"] <= memory_id:
            memoria["meta"]["next_memory_id"] = memory_id + 1

        # Prima viene messa al sicuro la copia nella memoria attiva.
        salva_archivio(percorso_memoria, memoria)

    else:
        # Possibile retry dopo un ripristino interrotto.
        if (
            ricordo_attivo["content"] != ricordo_ripristinato["content"]
            or ricordo_attivo["created_at"] != ricordo_ripristinato["created_at"]
            or ricordo_attivo["updated_at"] != ricordo_ripristinato["updated_at"]
        ):
            raise ValueError(
                f"Conflitto: l'ID {memory_id} esiste già nella memoria "
                "attiva ma contiene dati differenti."
            )

    # Solo dopo che la memoria attiva è sicura rimuoviamo dal cestino.
    cestino["deleted_memories"].remove(ricordo_eliminato)

    salva_cestino(percorso_cestino, cestino)

    return ricordo_ripristinato

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
        with percorso.open("r", encoding="utf-8") as file:
            archivio_su_disco = json.load(file)

        versione_su_disco = None

        if isinstance(archivio_su_disco, dict):
            meta_su_disco = archivio_su_disco.get("meta")

            if isinstance(meta_su_disco, dict):
                versione_su_disco = meta_su_disco.get("schema_version")

        memoria = carica_archivio(percorso)

        if versione_su_disco == 1:
            salva_archivio(percorso, memoria)

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
