"""Foundation di sicurezza filesystem: decide ALLOW/DENY, non legge alcun contenuto."""

import os
from dataclasses import dataclass
from pathlib import Path

# Qwen propone un path. Questa funzione e' l'unico punto che decide
# ALLOW/DENY, sempre sul path risolto (mai su ricerca testuale di
# ".."). Nessuna apertura o lettura di contenuto, nessuna
# enumerazione di directory in questo modulo: solo operazioni pathlib
# di normalizzazione e containment (resolve, exists, is_dir,
# is_relative_to) e os.stat sui soli metadati, per l'identita'
# (st_dev, st_ino) delle reserved roots.
#
# Reserved roots (es. DATA_ROOT): imposte dal runtime, mai dal config.
# Hanno precedenza sulle allowed roots: un target che cade in una
# reserved root e' negato anche se una allowed root lo contiene.

RAGIONI_AMMESSE = frozenset({
    "ok",
    "no_allowed_roots",
    "invalid_allowed_root",
    "invalid_reserved_root",
    "invalid_path",
    "outside_allowed_roots",
    "reserved_root",
    "ambiguous_relative_path",
    "path_resolution_error",
})


@dataclass(frozen=True)
class PathDecision:
    """Esito della validazione: mai un path negato con un resolved_path valorizzato."""

    allowed: bool
    resolved_path: Path | None
    reason: str

    def __post_init__(self):
        if type(self.allowed) is not bool:
            raise TypeError("allowed deve essere un valore booleano.")

        if self.reason not in RAGIONI_AMMESSE:
            raise ValueError(f"reason non ammesso: {self.reason}.")

        if self.resolved_path is not None and not isinstance(self.resolved_path, Path):
            raise TypeError("resolved_path deve essere un Path o None.")

        if not self.allowed and self.resolved_path is not None:
            raise ValueError(
                "Una PathDecision negata non puo' contenere un resolved_path: "
                "un path esterno rifiutato non deve mai essere esposto."
            )

        if self.allowed and self.resolved_path is None:
            raise ValueError(
                "Una PathDecision autorizzata deve contenere un resolved_path."
            )


def _normalizza_allowed_roots(allowed_roots) -> list[Path]:
    """
    Filtra allowed_roots alle sole root realmente autorizzabili:
    assolute, esistenti, directory. Le root vengono poi risolte e
    deduplicate. Non risolve mai una root relativa rispetto a
    os.getcwd(): una root non assoluta viene semplicemente scartata.
    """

    try:
        candidate = list(allowed_roots)
    except TypeError:
        return []

    roots_validi: list[Path] = []

    for candidata in candidate:
        try:
            candidata_path = Path(candidata)
        except (TypeError, ValueError):
            continue

        if not candidata_path.is_absolute():
            continue

        try:
            if not candidata_path.exists() or not candidata_path.is_dir():
                continue
        except OSError:
            continue

        try:
            risolta = candidata_path.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue

        if risolta not in roots_validi:
            roots_validi.append(risolta)

    return roots_validi


class _IdentitaNonVerificabile(Exception):
    """Un oggetto esistente non ha un'identita' filesystem affidabile."""


# Errori di stat che significano "a questo percorso non esiste nulla":
# NotADirectoryError indica un componente intermedio che e' un file.
_ERRORI_INESISTENTE = (FileNotFoundError, NotADirectoryError)


def _identita(percorso: Path) -> tuple[int, int] | None:
    """
    Identita' (st_dev, st_ino) dell'oggetto reale a percorso, seguendo
    symlink e junction. None solo se il percorso non esiste. Qualsiasi
    altro errore, o un st_ino nullo (identita' non significativa),
    solleva _IdentitaNonVerificabile: non si puo' dimostrare che
    l'oggetto non sia una reserved root.
    """

    try:
        informazioni = os.stat(percorso)
    except _ERRORI_INESISTENTE:
        return None
    except (OSError, ValueError):
        raise _IdentitaNonVerificabile from None

    if informazioni.st_ino == 0:
        raise _IdentitaNonVerificabile

    return informazioni.st_dev, informazioni.st_ino


@dataclass(frozen=True)
class _ReservedNormalizzata:
    originale: Path
    risolta: Path
    identita: tuple[int, int] | None


def _normalizza_reserved_roots(reserved_roots) -> list[_ReservedNormalizzata] | None:
    """
    A differenza delle allowed roots, una reserved root non valida non
    viene mai scartata: restituisce None (il chiamante nega tutto) se
    una qualsiasi reserved e' di tipo errato, relativa, non risolvibile
    o esistente ma senza identita' affidabile. Una reserved che non
    esiste resta protetta dal solo confronto dei path.

    Ricalcolata a ogni decisione: una reserved creata, sostituita o
    trasformata in symlink dopo la preparazione del contesto resta
    protetta.
    """

    if isinstance(reserved_roots, (str, bytes, os.PathLike)):
        return None

    try:
        candidate = list(reserved_roots)
    except TypeError:
        return None

    normalizzate = []

    for candidata in candidate:
        if not isinstance(candidata, (str, Path)):
            return None

        originale = Path(candidata)

        if not originale.is_absolute():
            return None

        try:
            risolta = originale.resolve(strict=False)
            identita = _identita(risolta)
        except (OSError, RuntimeError, ValueError, _IdentitaNonVerificabile):
            return None

        normalizzate.append(_ReservedNormalizzata(originale, risolta, identita))

    return normalizzate


def _in_reserved_root(risolto: Path, reserved: list[_ReservedNormalizzata]) -> bool:
    """
    True se il target risolto coincide con una reserved root o vi
    discende. Due controlli indipendenti:

    - path: forma risolta e forma originale della reserved (vale anche
      per target e reserved inesistenti);
    - identita': il target o un suo antenato esistente e' lo stesso
      oggetto filesystem di una reserved esistente, qualunque sia la
      rappresentazione del path (\\\\?\\, UNC loopback, junction).

    Solleva _IdentitaNonVerificabile se un antenato esistente non ha
    un'identita' affidabile.
    """

    for voce in reserved:
        if risolto.is_relative_to(voce.risolta) or risolto.is_relative_to(voce.originale):
            return True

    identita_reserved = {voce.identita for voce in reserved if voce.identita is not None}

    if not identita_reserved:
        return False

    for antenato in (risolto, *risolto.parents):
        if _identita(antenato) in identita_reserved:
            return True

    return False


def risolvi_path_autorizzato(path_richiesto, allowed_roots, *, reserved_roots) -> PathDecision:
    """
    Decide se path_richiesto e' autorizzato rispetto a allowed_roots.

    Root: solo path assoluti, esistenti, directory diventano root
    autorizzate (dedup dopo resolve()); zero root valide -> deny.

    Path assoluto richiesto: resolve(strict=False), poi ALLOW solo se
    il risultato e' contenuto (is_relative_to, dopo resolve su
    entrambi i lati) in almeno una root risolta.

    Path relativo richiesto: ammesso solo con esattamente una root
    valida (altrimenti ambiguous_relative_path, mai "la prima root"),
    risolto come (root / path_richiesto) e poi stesso containment.

    Un target inesistente ma la cui posizione risolta ricadrebbe
    dentro una root valida e' ALLOW: autorizzazione e' indipendente da
    esistenza del target (vale solo per le root, non per il target).

    reserved_roots (keyword-only, obbligatorio: () solo se esplicito):
    zone imposte dal runtime che nessuna allowed root puo' aprire. Il
    veto arriva dopo il containment, sullo stesso path risolto, con
    confronto dei path e dell'identita' filesystem. Una reserved non
    verificabile nega tutto (fail closed).

    Nessuna eccezione si propaga per input non valido: ogni caso
    limite produce una PathDecision negata con una reason del
    vocabolario fisso, e resolved_path resta sempre None in quel caso
    (un path esterno rifiutato non viene mai esposto, nemmeno nel
    reason).
    """

    if not allowed_roots:
        return PathDecision(allowed=False, resolved_path=None, reason="no_allowed_roots")

    roots_validi = _normalizza_allowed_roots(allowed_roots)

    if not roots_validi:
        return PathDecision(allowed=False, resolved_path=None, reason="invalid_allowed_root")

    reserved = _normalizza_reserved_roots(reserved_roots)

    if reserved is None:
        return PathDecision(allowed=False, resolved_path=None, reason="invalid_reserved_root")

    if not isinstance(path_richiesto, (str, Path)) or (
        isinstance(path_richiesto, str) and not path_richiesto.strip()
    ):
        return PathDecision(allowed=False, resolved_path=None, reason="invalid_path")

    try:
        candidato = Path(path_richiesto)
    except (TypeError, ValueError):
        return PathDecision(allowed=False, resolved_path=None, reason="invalid_path")

    try:
        if candidato.is_absolute():
            risolto = candidato.resolve(strict=False)

            if not any(risolto.is_relative_to(root) for root in roots_validi):
                return PathDecision(
                    allowed=False, resolved_path=None, reason="outside_allowed_roots"
                )

        else:
            if len(roots_validi) > 1:
                return PathDecision(
                    allowed=False, resolved_path=None, reason="ambiguous_relative_path"
                )

            root = roots_validi[0]
            risolto = (root / candidato).resolve(strict=False)

            if not risolto.is_relative_to(root):
                return PathDecision(
                    allowed=False, resolved_path=None, reason="outside_allowed_roots"
                )

        # Veto reserved dopo il containment: le reason esistenti restano
        # invariate e reserved_root compare solo quando il carve-out
        # sottrae davvero un path altrimenti autorizzato.
        if _in_reserved_root(risolto, reserved):
            return PathDecision(allowed=False, resolved_path=None, reason="reserved_root")

        return PathDecision(allowed=True, resolved_path=risolto, reason="ok")

    except (OSError, RuntimeError, ValueError, _IdentitaNonVerificabile):
        # Anche un antenato del target senza identita' affidabile: non
        # si puo' escludere che sia una reserved root.
        return PathDecision(allowed=False, resolved_path=None, reason="path_resolution_error")
