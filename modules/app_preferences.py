"""
Persistenza delle preferenze applicative strutturate di Aster (browser,
editor, lettore multimediale preferiti).

Sono dati descrittivi scelti dall'utente per Aster: non sono percorsi
eseguibili, comandi o argomenti di avvio, e non cambiano le
applicazioni predefinite di Windows. Il file vive in
DATA_ROOT/app_preferences.json (percorso fornito dal chiamante, mai
calcolato qui): la stessa Reserved Data Root che già protegge
memory.json lo rende inaccessibile ai tool filesystem generici, senza
bisogno di alcuna modifica a modules/filesystem_policy.py.

Persistenza sicura, con lo stesso principio di modules/memory.py ma
senza cestino, migrazioni multi-schema o promozione di backup: una
preferenza persa è banalmente ri-impostabile, non serve quella
complessità. Scrittura sempre su file temporaneo nella stessa
directory, validata prima della sostituzione atomica
(os.replace); il file precedente non viene mai toccato prima che il
temporaneo sia stato scritto e validato con successo.
"""

import json
import os
import re
import unicodedata
from pathlib import Path

SCHEMA_VERSION = 1

CATEGORIE_VALIDE = ("browser", "editor", "media_player")
_CATEGORIE_VALIDE_SET = frozenset(CATEGORIE_VALIDE)

_MAX_LUNGHEZZA_VALORE = 100

# Stesse categorie Unicode vietate usate altrove nel progetto per dati
# testuali provenienti dall'utente o dal registro (app_info.py):
# controllo (Cc), surrogati isolati (Cs), marcatori di formattazione
# invisibili/bidirezionali (Cf).
_CATEGORIE_UNICODE_VIETATE = frozenset({"Cc", "Cs", "Cf"})

_SEPARATORI_PERCORSO = ("\\", "/")
_METACARATTERI_COMANDO = frozenset(";|&<>`$")
_ESTENSIONI_ESEGUIBILI = (
    ".exe", ".bat", ".cmd", ".com", ".ps1", ".sh", ".msi", ".vbs", ".scr",
)
_PATTERN_LETTERA_UNITA = re.compile(r"^[A-Za-z]:")


class ErrorePreferenzeNonAffidabili(Exception):
    """
    Il file delle preferenze esiste ma non è utilizzabile in modo
    affidabile (JSON non valido, struttura o schema incompatibili, o
    errore di I/O nella lettura). Non viene mai confuso con "nessuna
    preferenza impostata": un file assente è un caso normale (prima
    esecuzione), un file presente ma corrotto non lo è, e non va mai
    sovrascritto automaticamente con preferenze vuote.
    """


def crea_preferenze_vuote() -> dict:
    """Struttura iniziale valida: tutte e tre le categorie a null."""

    return {
        "schema_version": SCHEMA_VERSION,
        "preferences": {categoria: None for categoria in CATEGORIE_VALIDE},
    }


def _contiene_categorie_vietate(testo: str) -> bool:
    return any(unicodedata.category(ch) in _CATEGORIE_UNICODE_VIETATE for ch in testo)


def valore_preferenza_valido(testo: str) -> bool:
    """
    True se testo è un'etichetta descrittiva accettabile: non vuota,
    non oltre il limite, senza caratteri Unicode pericolosi, senza
    separatori di percorso, lettera di unità, metacaratteri di shell o
    estensione eseguibile. Mai un'interpretazione di percorso o
    comando: solo un filtro sulla forma del testo.

    Validazione pura, centralizzata qui: la stessa regola si applica
    sia al value proposto da set_app_preference (modules/
    preferences_tools.py, che chiama questa funzione) sia ai valori
    già persistiti sul disco, tramite valida_preferenze qui sotto. Un
    solo validatore, non due copie indipendenti che potrebbero
    divergere nel tempo.
    """

    if not isinstance(testo, str):
        return False

    # .strip() qui (non solo "not testo"): un valore persistito potrebbe
    # non essere mai passato per lo strip applicato lato tool input: una
    # stringa di soli spazi non è un'etichetta valida in nessuno dei due
    # casi.
    if not testo.strip() or len(testo) > _MAX_LUNGHEZZA_VALORE:
        return False

    if _contiene_categorie_vietate(testo):
        return False

    if any(separatore in testo for separatore in _SEPARATORI_PERCORSO):
        return False

    if any(carattere in _METACARATTERI_COMANDO for carattere in testo):
        return False

    if _PATTERN_LETTERA_UNITA.match(testo):
        return False

    if testo.startswith("-"):
        return False

    minuscolo = testo.casefold()
    if any(minuscolo.endswith(estensione) for estensione in _ESTENSIONI_ESEGUIBILI):
        return False

    return True


def valida_preferenze(dato: object) -> None:
    """
    Verifica che dato sia uno schema v1 valido: solleva TypeError o
    ValueError altrimenti. Le categorie sono chiuse: né mancanti né
    aggiuntive. Ogni valore è null oppure una stringa che supera
    valore_preferenza_valido (stessa regola di set_app_preference: un
    valore persistito non può essere meno affidabile di uno appena
    proposto dal modello).
    """

    if not isinstance(dato, dict):
        raise TypeError("Le preferenze applicative devono essere un oggetto.")

    if set(dato.keys()) != {"schema_version", "preferences"}:
        raise ValueError(
            "Struttura delle preferenze applicative non valida: chiavi inattese."
        )

    versione = dato.get("schema_version")
    if type(versione) is not int or versione != SCHEMA_VERSION:
        raise ValueError(
            "Versione schema delle preferenze applicative non supportata."
        )

    preferenze = dato.get("preferences")
    if not isinstance(preferenze, dict):
        raise TypeError(
            "Il campo preferences deve essere un oggetto."
        )

    if set(preferenze.keys()) != _CATEGORIE_VALIDE_SET:
        raise ValueError(
            "Il campo preferences deve contenere esattamente "
            "le categorie note, né mancanti né aggiuntive."
        )

    for categoria, valore in preferenze.items():
        if valore is None:
            continue

        if not isinstance(valore, str):
            raise TypeError(
                f"Il valore della categoria {categoria!r} deve essere "
                "una stringa oppure null."
            )

        if not valore_preferenza_valido(valore):
            raise ValueError(
                f"Il valore della categoria {categoria!r} non è un'etichetta valida."
            )


def carica_preferenze(percorso: Path) -> dict:
    """
    Carica e valida le preferenze dal disco.

    Le eccezioni (OSError, json.JSONDecodeError, ValueError, TypeError)
    si propagano al chiamante: qui non viene mai deciso se un errore è
    "normale" o no, perché questa funzione non sa se il file doveva
    esistere.
    """

    with percorso.open("r", encoding="utf-8") as file:
        dato = json.load(file)

    valida_preferenze(dato)

    return dato


def salva_preferenze(percorso: Path, preferenze: dict) -> None:
    """
    Salva le preferenze in modo sicuro.

    Scrittura su file temporaneo nella stessa directory, ricaricamento
    e validazione del temporaneo, poi sostituzione atomica
    (os.replace) del file principale. Se la scrittura o la validazione
    del temporaneo falliscono, il file principale non viene mai
    toccato: nessuna perdita del contenuto precedente.
    """

    valida_preferenze(preferenze)

    percorso.parent.mkdir(parents=True, exist_ok=True)

    percorso_temporaneo = percorso.with_suffix(".tmp")

    try:
        # Scrittura, validazione e sostituzione condividono lo stesso
        # finally: un fallimento in uno qualunque dei tre punti (anche
        # json.dump/file.write, non solo la ricarica o os.replace)
        # pulisce comunque il temporaneo, senza lasciare debris su
        # disco. Il file principale non viene mai toccato prima che il
        # temporaneo sia stato scritto e validato con successo.
        with percorso_temporaneo.open("w", encoding="utf-8") as file:
            json.dump(preferenze, file, ensure_ascii=False, indent=4)
            file.write("\n")

        # Il tmp appena scritto deve essere ricaricabile e valido prima
        # di diventare il file principale: mai fidarsi del solo
        # json.dump.
        carica_preferenze(percorso_temporaneo)
        os.replace(percorso_temporaneo, percorso)
    finally:
        # A sostituzione riuscita il temporaneo non esiste piu' (e'
        # diventato il file principale) e questa pulizia e' un no-op.
        if percorso_temporaneo.exists():
            percorso_temporaneo.unlink()


def leggi_preferenze_o_vuote(percorso: Path) -> dict:
    """
    Preferenze correnti, o la struttura vuota se il file non esiste
    ancora (prima esecuzione: comportamento normale, non un errore).

    Se il file esiste ma non è utilizzabile in modo affidabile
    (JSON non valido, schema incompatibile, errore di I/O), solleva
    ErrorePreferenzeNonAffidabili: non viene mai restituita una
    struttura vuota al posto di dati che potrebbero ancora esistere ma
    non essere al momento leggibili.
    """

    try:
        return carica_preferenze(percorso)
    except FileNotFoundError:
        # Nessuna chiamata separata a Path.exists() prima dell'apertura:
        # eviterebbe comunque la finestra TOCTOU (il file potrebbe
        # sparire/comparire tra le due chiamate) e qui serve comunque
        # provare ad aprire per distinguere "assente" da "presente ma
        # non apribile per un altro motivo" nello stesso identico punto.
        return crea_preferenze_vuote()
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as errore:
        raise ErrorePreferenzeNonAffidabili(
            "Le preferenze applicative non sono leggibili in modo affidabile."
        ) from errore


def imposta_preferenza(percorso: Path, categoria: str, valore: str | None) -> dict:
    """
    Imposta (valore stringa) o cancella (valore None) una categoria e
    salva atomicamente; restituisce la struttura completa aggiornata.

    Se lo stato persistente corrente non è leggibile in modo
    affidabile, l'aggiornamento è bloccato: ErrorePreferenzeNonAffidabili
    si propaga da leggi_preferenze_o_vuote senza che alcuna scrittura
    avvenga. Non si scrive mai sopra un file che non si riesce a
    validare, per non perdere silenziosamente le altre categorie che
    potrebbe contenere.
    """

    if categoria not in _CATEGORIE_VALIDE_SET:
        raise ValueError(f"Categoria non valida: {categoria!r}.")

    if valore is not None and not isinstance(valore, str):
        raise TypeError("Il valore di una preferenza deve essere una stringa o None.")

    preferenze = leggi_preferenze_o_vuote(percorso)
    preferenze["preferences"][categoria] = valore
    salva_preferenze(percorso, preferenze)

    return preferenze
