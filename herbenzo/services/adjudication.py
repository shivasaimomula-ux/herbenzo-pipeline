"""Citation Adjudication Service — the keystone shared service.

Answers the question that "is this PMID real?" does not:

    Does this study actually support THIS claim about THIS subject?

Every check is deterministic and inspectable. There is no model call in the
default path, and the verdict always carries the reason and the matched terms,
so a reviewer can see exactly why a citation passed or failed.

The failure mode this exists to prevent is real and was observed in production
output: a zinc-oxide-nanoparticle crop-fertilizer study cited as oral safety
evidence for a botanical. The PMID was genuine, the DOI matched, and the claim
was false. ``SUBJECT_ABSENT`` is the check that catches it.
"""

from __future__ import annotations

import dataclasses
import re

from herbenzo.clients.pubmed import Article
from herbenzo.services.evidence import EvidenceStore

__all__ = ["Verdict", "ReasonCode", "Adjudication", "AdjudicationService", "SAFETY_TERMS"]


class Verdict:
    SUPPORT = "support"
    PARTIAL = "partial"
    REJECT = "reject"


class ReasonCode:
    RECORD_NOT_FOUND = "record_not_found"
    RETRACTED_SOURCE = "retracted_source"
    SUBJECT_ABSENT = "subject_absent"
    CLAIM_TOPIC_ABSENT = "claim_topic_absent"
    WRONG_CLAIM_DOMAIN = "wrong_claim_domain"
    SAFETY_SIGNAL_WEAK = "safety_signal_weak"
    TIER_BELOW_CLAIM = "tier_below_claim"
    NO_ABSTRACT = "no_abstract"
    SUPPORTED = "supported"
    PARTIAL_SUBJECT_ONLY = "partial_subject_only"


#: Terms that indicate a record actually reports tolerability/safety data.
SAFETY_TERMS = {
    "safety", "toxicity", "tolerability", "adverse", "irritation",
    "sensitization", "sensitisation", "contraindication", "interaction",
    "side effect", "no observed adverse", "noael", "ld50", "genotoxic",
    "mutagenic", "phototoxic", "dermatitis", "allergy", "allergic",
}

_MODEL_TERMS = {
    "in_vitro": {"in vitro", "cell line", "cultured", "hacat", "assay",
                 "keratinocyte", "fibroblast", "cells were"},
    "animal":   {"mice", "mouse", "rat", "rats", "rabbit", "murine",
                 "in vivo animal", "wistar", "sprague"},
    "human":    {"patients", "volunteers", "participants", "subjects",
                 "randomized", "randomised", "double-blind", "clinical trial"},
}

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "have", "has", "been",
    "may", "can", "are", "was", "were", "its", "their", "which", "into", "than",
    "reported", "suggest", "suggests", "shown", "show", "study", "studies",
    "effect", "effects", "activity", "use", "used", "using", "data", "evidence",
}


@dataclasses.dataclass(frozen=True)
class Adjudication:
    pmid: str
    claim: str
    subject: str
    verdict: str
    reason_code: str
    evidence_tier: str
    model_system: str
    subject_match: bool
    matched_terms: tuple[str, ...]
    note: str
    title: str = ""

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


class AdjudicationService:
    """Deterministic claim-support adjudication.

    ``claim_domain='safety'`` applies the stricter gate: the record must actually
    report tolerability, toxicity or interaction data. This is the gate whose
    absence let irrelevant citations into a safety section.
    """

    def __init__(self, evidence: EvidenceStore | None = None,
                 min_claim_overlap: int = 1) -> None:
        self.evidence = evidence or EvidenceStore()
        self.min_claim_overlap = min_claim_overlap

    def adjudicate(
        self,
        claim: str,
        subject: str,
        pmid: str,
        subject_aliases: tuple[str, ...] = (),
        claim_domain: str = "general",
    ) -> Adjudication:
        art = self.evidence.record(pmid)
        if art is None:
            return self._verdict(pmid, claim, subject, Verdict.REJECT,
                                 ReasonCode.RECORD_NOT_FOUND, "unclassified",
                                 "unknown", False, (),
                                 "No PubMed record retrieved for this identifier.")

        haystack = _haystack(art)
        headline = _headline(art)  # title + MeSH only — the record's declared topic
        title = art.get("title", "")

        if art.get("retracted"):
            return self._verdict(pmid, claim, subject, Verdict.REJECT,
                                 ReasonCode.RETRACTED_SOURCE, _tier(art, haystack),
                                 _model_system(haystack), False, (),
                                 "Source is retracted and must not be cited.", title)

        # 1. Is the subject actually in the paper?
        names = (subject, *subject_aliases)
        subject_hits = tuple(n for n in names if n and n.lower() in haystack)
        if not subject_hits:
            return self._verdict(
                pmid, claim, subject, Verdict.REJECT, ReasonCode.SUBJECT_ABSENT,
                _tier(art, haystack), _model_system(haystack), False, (),
                f"Neither {subject!r} nor any supplied alias appears in the title, "
                "abstract or MeSH terms. The record is real but is not about this "
                "subject.", title)

        if not art.get("abstract"):
            return self._verdict(
                pmid, claim, subject, Verdict.PARTIAL, ReasonCode.NO_ABSTRACT,
                _tier(art, haystack), _model_system(haystack), True, subject_hits,
                "Subject present but no abstract is indexed, so claim support "
                "cannot be assessed automatically. Manual review required.", title)

        # 2. Safety claims need actual safety content — and a stricter gate.
        #    A safety word buried in an abstract is not a safety study. A record
        #    that genuinely reports tolerability declares it in the title or MeSH.
        #    A safety claim can therefore never be auto-SUPPORTed on abstract-only
        #    mention; the best available verdict in that case is PARTIAL.
        if claim_domain == "safety":
            in_headline = _has_term(headline, SAFETY_TERMS)
            in_abstract = _has_term(haystack, SAFETY_TERMS)
            if not in_abstract:
                return self._verdict(
                    pmid, claim, subject, Verdict.REJECT,
                    ReasonCode.WRONG_CLAIM_DOMAIN, _tier(art, haystack),
                    _model_system(haystack), True, subject_hits,
                    "Cited in a safety context, but the record reports no "
                    "tolerability, toxicity, interaction or adverse-effect data.",
                    title)
            if not in_headline:
                return self._verdict(
                    pmid, claim, subject, Verdict.PARTIAL,
                    ReasonCode.SAFETY_SIGNAL_WEAK, _tier(art, haystack),
                    _model_system(haystack), True, subject_hits,
                    "Safety vocabulary appears only in the body of the abstract, "
                    "not in the title or MeSH terms — this is not a safety study. "
                    "Manual review required; do not state as established safety.",
                    title)

        # 3. Does the claim's own vocabulary appear?
        terms = _claim_terms(claim)
        overlap = tuple(sorted(t for t in terms if t in haystack))
        if len(overlap) < self.min_claim_overlap:
            return self._verdict(
                pmid, claim, subject, Verdict.REJECT, ReasonCode.CLAIM_TOPIC_ABSENT,
                _tier(art, haystack), _model_system(haystack), True, subject_hits,
                "Record concerns the right subject but none of the claim's "
                "substantive terms appear in it.", title)

        model = _model_system(haystack)
        if len(overlap) == 1:
            return self._verdict(
                pmid, claim, subject, Verdict.PARTIAL,
                ReasonCode.PARTIAL_SUBJECT_ONLY, _tier(art, haystack), model, True,
                subject_hits + overlap,
                "Weak topical overlap — supports the claim only loosely. "
                "Downgrade the claim or find a more direct source.", title)

        return self._verdict(
            pmid, claim, subject, Verdict.SUPPORT, ReasonCode.SUPPORTED,
            _tier(art, haystack), model, True, subject_hits + overlap,
            f"Subject and claim terms both present. Evidence is {model}; state "
            "the claim at that tier and no higher.", title)

    # -- batch --------------------------------------------------------------

    def adjudicate_many(self, items: list[dict]) -> list[Adjudication]:
        return [self.adjudicate(**i) for i in items]

    @staticmethod
    def _verdict(pmid, claim, subject, verdict, reason, tier, model,
                 smatch, terms, note, title="") -> Adjudication:
        return Adjudication(
            pmid=str(pmid), claim=claim, subject=subject, verdict=verdict,
            reason_code=reason, evidence_tier=tier, model_system=model,
            subject_match=smatch, matched_terms=tuple(terms), note=note, title=title,
        )


# ---------------------------------------------------------------------------

def _haystack(art: Article) -> str:
    return " ".join([
        art.get("title", ""), art.get("abstract", ""),
        " ".join(art.get("mesh_terms", [])),
    ]).lower()


def _headline(art: Article) -> str:
    """Title and MeSH only — what the record declares itself to be about."""
    return " ".join([art.get("title", ""),
                     " ".join(art.get("mesh_terms", []))]).lower()


def _has_term(text: str, terms) -> bool:
    """Whole-word / phrase containment.

    Substring matching is not safe here: 'demonstrated' contains 'rat', which
    silently mislabelled in vitro studies as animal studies.
    """
    return any(re.search(rf"(?<![a-z]){re.escape(t)}(?![a-z])", text) for t in terms)


def _claim_terms(claim: str) -> set[str]:
    words = re.findall(r"[a-z][a-z\-]{3,}", claim.lower())
    return {w for w in words if w not in _STOPWORDS}


def _model_system(haystack: str) -> str:
    """Most-human model system the record shows evidence of.

    Checked human → animal → in vitro, so a clinical study is not downgraded by
    an incidental in vitro mention.
    """
    for label in ("human", "animal", "in_vitro"):
        if _has_term(haystack, _MODEL_TERMS[label]):
            return label
    return "unknown"


def _tier(art: Article, haystack: str) -> str:
    """Evidence tier: publication type first, model system as the fallback.

    Most primary research is indexed only as 'Journal Article', which carries no
    tier information — in that case the model system detected in the abstract is
    the better estimate, and is never allowed to exceed what the text supports.
    """
    by_pubtype = art.evidence_tier
    if by_pubtype != "unclassified":
        return by_pubtype
    model = _model_system(haystack)
    return {"human": "human_observational", "animal": "animal",
            "in_vitro": "in_vitro"}.get(model, "unclassified")
