"""Foundation di sicurezza filesystem: decide ALLOW/DENY, non legge alcun contenuto."""

from dataclasses import dataclass
from pathlib import Path

# Qwen propone un path. Questa funzione e' l'unico punto che decide
# ALLOW/DENY, sempre sul path risolto (mai su ricerca testuale di
# ".."). Nessuna apertura o lettura di contenuto, nessuna
# enumerazione di directory in questo modulo: solo operazioni pathlib
# di normalizzazione e containment (resolve, exists, is_dir,
# is_relative_to).

RAGIONI_AMMESSE = frozenset({
    "ok",
    "no_allowed_roots",
    "invalid_allowed_root",
    "invalid_path",
    "outside_allowed_roots",
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


def risolvi_path_autorizzato(path_richiesto, allowed_roots) -> PathDecision:
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

            for root in roots_validi:
                if risolto.is_relative_to(root):
                    return PathDecision(allowed=True, resolved_path=risolto, reason="ok")

            return PathDecision(
                allowed=False, resolved_path=None, reason="outside_allowed_roots"
            )

        if len(roots_validi) > 1:
            return PathDecision(
                allowed=False, resolved_path=None, reason="ambiguous_relative_path"
            )

        root = roots_validi[0]
        risolto = (root / candidato).resolve(strict=False)

        if risolto.is_relative_to(root):
            return PathDecision(allowed=True, resolved_path=risolto, reason="ok")

        return PathDecision(allowed=False, resolved_path=None, reason="outside_allowed_roots")

    except (OSError, RuntimeError, ValueError):
        return PathDecision(allowed=False, resolved_path=None, reason="path_resolution_error")
