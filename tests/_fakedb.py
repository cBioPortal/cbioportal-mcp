"""An in-memory stand-in for the ClickHouse queries the data apps issue.

The older test modules fake ``run_select_query`` with a few substring checks each.
The multi-study, OQL and stratified features issue many more query shapes, so this
module keeps small in-memory tables and answers each query shape the server emits
by recognising its main table and a distinctive fragment, applying the study scope
parsed from the SQL itself. It is deliberately not a SQL engine: an unrecognised
query raises, so a new query shape cannot silently return nothing.
"""

from __future__ import annotations

import re
from collections import defaultdict

_STUDY_EQ = re.compile(r"(?:\w+\.)?cancer_study_identifier = '([^']+)'")
_STUDY_IN = re.compile(r"(?:\w+\.)?cancer_study_identifier IN \(('[^)]*')\)")
_PREFERENCE = re.compile(r"preference_name = '([^']+)'")
_QUOTED = re.compile(r"'([^']*)'")


def _quoted(body: str) -> list[str]:
    return _QUOTED.findall(body)


class FakeDB:
    def __init__(self):
        self.studies: dict[str, dict] = {}
        self.preferences: dict[str, tuple[list[str], str]] = {}
        self.samples: dict[str, dict] = {}
        self.events: list[dict] = []
        self.clinical: list[dict] = []
        self.wes: set[str] = set()
        self.panel: dict[str, set[str]] = {}
        self.profiles: list[dict] = []
        self.expression: list[dict] = []
        self.mutation_rows: list[dict] = []
        self.executed: list[str] = []

    # --- building the fixture -------------------------------------------------

    def add_study(self, study: str, cancer_type: str = "mixed") -> FakeDB:
        self.studies[study] = {"name": study.upper(), "type_of_cancer_id": cancer_type}
        return self

    def add_preference(self, name: str, studies: list[str], notes: str = "") -> FakeDB:
        self.preferences[name] = (list(studies), notes)
        return self

    def add_sample(
        self,
        study: str,
        sample: str,
        patient: str | None = None,
        cancer_type: str | None = None,
        wes: bool = True,
        panel_genes: set[str] | None = None,
        patient_stable_id: str | None = None,
    ) -> str:
        """Add a sample; ids are study-prefixed like the real *_unique_id columns."""
        if study not in self.studies:
            self.add_study(study)
        sid = f"{study}_{sample}"
        pid = f"{study}_{patient or sample}"
        self.samples[sid] = {
            "sample_unique_id": sid,
            "patient_unique_id": pid,
            "cancer_study_identifier": study,
            "patient_stable_id": patient_stable_id or (patient or sample),
            "internal_id": len(self.samples) + 1,
        }
        if wes:
            self.wes.add(sid)
        elif panel_genes is not None:
            self.panel[sid] = set(panel_genes)
        if cancer_type is not None:
            self.add_clinical(sid, "CANCER_TYPE", cancer_type)
        return sid

    def add_clinical(self, sample_id: str, attribute: str, value: str) -> None:
        s = self.samples[sample_id]
        self.clinical.append(
            {
                "cancer_study_identifier": s["cancer_study_identifier"],
                "sample_unique_id": sample_id,
                "patient_unique_id": s["patient_unique_id"],
                "attribute_name": attribute,
                "attribute_value": value,
                "type": "sample",
            }
        )

    def add_patient_clinical(self, sample_id: str, attribute: str, value: str) -> None:
        s = self.samples[sample_id]
        self.clinical.append(
            {
                "cancer_study_identifier": s["cancer_study_identifier"],
                "sample_unique_id": "",
                "patient_unique_id": s["patient_unique_id"],
                "attribute_name": attribute,
                "attribute_value": value,
                "type": "patient",
            }
        )

    def add_survival(self, sample_id: str, months: float, event: bool, endpoint: str = "OS"):
        self.add_patient_clinical(sample_id, f"{endpoint}_MONTHS", str(months))
        self.add_patient_clinical(
            sample_id, f"{endpoint}_STATUS", "1:DECEASED" if event else "0:LIVING"
        )

    def add_mutation(
        self,
        sample_id: str,
        gene: str,
        change: str,
        mutation_type: str = "Missense_Mutation",
        status: str = "Somatic",
        driver: str = "",
    ) -> None:
        s = self.samples[sample_id]
        self.events.append(
            {
                "sample_unique_id": sample_id,
                "patient_unique_id": s["patient_unique_id"],
                "cancer_study_identifier": s["cancer_study_identifier"],
                "hugo_gene_symbol": gene,
                "variant_type": "mutation",
                "mutation_variant": change,
                "mutation_type": mutation_type,
                "mutation_status": status,
                "driver_filter": driver,
                "cna_alteration": None,
            }
        )

    def add_cna(self, sample_id: str, gene: str, value: int) -> None:
        s = self.samples[sample_id]
        self.events.append(
            {
                "sample_unique_id": sample_id,
                "patient_unique_id": s["patient_unique_id"],
                "cancer_study_identifier": s["cancer_study_identifier"],
                "hugo_gene_symbol": gene,
                "variant_type": "cna",
                "mutation_variant": "NA",
                "mutation_type": "",
                "mutation_status": "",
                "driver_filter": "",
                "cna_alteration": value,
            }
        )

    def add_expression(self, sample_id: str, gene: str, value: float, profile: str) -> None:
        s = self.samples[sample_id]
        self.expression.append(
            {
                "sample_unique_id": sample_id,
                "cancer_study_identifier": s["cancer_study_identifier"],
                "hugo_gene_symbol": gene,
                "profile_type": profile,
                "alteration_value": str(value),
            }
        )

    def add_profile(self, study: str, profile_type: str, alteration_type: str, datatype: str):
        self.profiles.append(
            {
                "study": study,
                "stable_id": f"{study}_{profile_type}",
                "genetic_alteration_type": alteration_type,
                "datatype": datatype,
            }
        )

    # --- scope parsing ----------------------------------------------------------

    def _scope(self, q: str) -> set[str] | None:
        """Studies named by the query's study filters; None when it has none."""
        found: set[str] = set()
        matched = False
        for m in _STUDY_EQ.finditer(q):
            found.add(m.group(1))
            matched = True
        for m in _STUDY_IN.finditer(q):
            found.update(_quoted(m.group(1)))
            matched = True
        for m in _PREFERENCE.finditer(q):
            if "cancer_study_identifier IN (SELECT" in q:
                found.update(self.preferences.get(m.group(1), ([], ""))[0])
                matched = True
        return found if matched else None

    def _in_list(self, q: str, column: str) -> list[str] | None:
        m = re.search(rf"(?:\w+\.)?{column} IN \(([^)]*)\)", q)
        return _quoted(m.group(1)) if m else None

    # --- dispatch ---------------------------------------------------------------

    def __call__(self, q: str) -> list[dict]:
        self.executed.append(q)
        table_match = re.search(r"FROM\s+(\w+)", q)
        table = table_match.group(1) if table_match else ""
        handler = getattr(self, f"_q_{table}", None)
        if handler is None:
            raise AssertionError(f"FakeDB: no handler for table {table!r} in query:\n{q}")
        return handler(q)

    def _q_cancer_study(self, q: str) -> list[dict]:
        scope = self._scope(q) or set()
        return [
            {"cancer_study_identifier": sid, **meta}
            for sid, meta in sorted(self.studies.items())
            if sid in scope
        ]

    def _q_cancer_study_query_preferences(self, q: str) -> list[dict]:
        if "SELECT DISTINCT preference_name" in q:
            return [{"preference_name": n} for n in sorted(self.preferences)]
        names = self._in_list(q, "preference_name")
        if names is None:
            m = _PREFERENCE.search(q)
            names = [m.group(1)] if m else []
        rows = []
        for name in names:
            if name in self.preferences:
                ids, notes = self.preferences[name]
                rows.append({"preference_name": name, "n": len(ids), "notes": notes})
        if "any(notes) AS notes" in q and "GROUP BY" not in q:
            return [{"notes": rows[0]["notes"]}] if rows else [{"notes": ""}]
        return rows

    def _events_in_scope(self, q: str) -> list[dict]:
        scope = self._scope(q)
        genes = self._in_list(q, "hugo_gene_symbol")
        single = re.search(r"hugo_gene_symbol = '([^']+)'", q)
        types = self._in_list(q, "variant_type")
        out = []
        for e in self.events:
            if scope is not None and e["cancer_study_identifier"] not in scope:
                continue
            if genes is not None and e["hugo_gene_symbol"] not in genes:
                continue
            if single and e["hugo_gene_symbol"] != single.group(1):
                continue
            if types is not None and e["variant_type"] not in types:
                continue
            out.append(e)
        return out

    def _q_genomic_event_derived(self, q: str) -> list[dict]:
        if "countIf(driver_filter != '')" in q:
            counts: dict[str, int] = defaultdict(int)
            for e in self._events_in_scope(q):
                counts[e["cancer_study_identifier"]] += 1 if e["driver_filter"] else 0
            return [{"cancer_study_identifier": k, "annotated": v} for k, v in counts.items()]
        if "WHERE driver_filter != ''" in q:
            studies = sorted(
                {e["cancer_study_identifier"] for e in self.events if e["driver_filter"]}
            )
            return [{"cancer_study_identifier": s} for s in studies]
        if "mutation_variant, mutation_type, mutation_status, driver_filter" in q:
            return [dict(e) for e in self._events_in_scope(q)]
        if "AS altered_samples" in q:
            counts = defaultdict(set)
            for e in self._events_in_scope(q):
                counts[e["hugo_gene_symbol"]].add(e["sample_unique_id"])
            ranked = sorted(counts.items(), key=lambda kv: (-len(kv[1]), kv[0]))
            return [{"hugo_gene_symbol": g, "altered_samples": len(s)} for g, s in ranked]
        if "SELECT DISTINCT patient_unique_id" in q:
            rows = self._events_in_scope(q)
            rows = [e for e in rows if self._matches_config(q, e)]
            return [
                {"patient_unique_id": p} for p in sorted({e["patient_unique_id"] for e in rows})
            ]
        if "SELECT sample_unique_id, mutation_variant, mutation_type" in q:
            keys = (
                "sample_unique_id",
                "mutation_variant",
                "mutation_type",
                "cancer_study_identifier",
            )
            return [
                {k: e[k] for k in keys}
                for e in self._events_in_scope(q)
                if e["variant_type"] == "mutation" and e["mutation_status"] != "UNCALLED"
            ]
        if "variant_type, mutation_type, cna_alteration" in q:
            return [dict(e) for e in self._events_in_scope(q) if self._matches_config(q, e)]
        if "countIf(mutation_type = 'Silent')" in q:
            rows = [e for e in self._events_in_scope(q) if e["variant_type"] == "mutation"]
            silent = sum(1 for e in rows if e["mutation_type"] == "Silent")
            return [{"silent": silent, "total": len(rows)}]
        raise AssertionError(f"FakeDB: unhandled genomic_event_derived query:\n{q}")

    def add_maf_row(
        self,
        sample_id: str,
        gene: str,
        change: str,
        mutation_type: str = "Missense_Mutation",
        alt: int | None = 10,
        ref: int | None = 10,
        codon: str = "",
        chrom: str = "7",
        start: int = 100,
        ref_allele: str = "A",
        tumor_allele: str = "T",
        copy_number: int | None = None,
        status: str = "Somatic",
    ) -> None:
        """A mutation + mutation_event row (the MAF-level tables) for one sample."""
        s = self.samples[sample_id]
        self.mutation_rows.append(
            {
                "sample_unique_id": sample_id,
                "patient_unique_id": s["patient_unique_id"],
                "cancer_study_identifier": s["cancer_study_identifier"],
                "hugo_gene_symbol": gene,
                "variant_type": "mutation",
                "mutation_variant": change,
                "mutation_type": mutation_type,
                "mutation_status": status,
                "driver_filter": "",
                "alt": alt,
                "ref": ref,
                "codon_change": codon,
                "chromosome": chrom,
                "start_position": start,
                "end_position": start,
                "reference_allele": ref_allele,
                "tumor_allele": tumor_allele,
                "variant_class": "SNP",
                "genome_build": "GRCh37",
                "refseq_mrna_id": "NM_1",
                "cn_value": copy_number,
            }
        )

    def _q_mutation(self, q: str) -> list[dict]:
        scope = self._scope(q)
        genes = self._in_list(q, "hugo_gene_symbol") or []
        rows = [
            r
            for r in self.mutation_rows
            if (scope is None or r["cancer_study_identifier"] in scope)
            and r["hugo_gene_symbol"] in genes
        ]
        if "tumor_alt_count" in q:  # allele-frequency rows
            return [
                {
                    "sample_unique_id": r["sample_unique_id"],
                    "gene": r["hugo_gene_symbol"],
                    "study": r["cancer_study_identifier"],
                    "protein_change": r["mutation_variant"],
                    "alt": r["alt"],
                    "ref": r["ref"],
                    **({"cn_value": r["cn_value"]} if "cn_value" in q else {}),
                }
                for r in rows
            ]
        if "codon_change" in q:  # nucleotide rows
            return [
                {
                    k: r[k]
                    for k in (
                        "sample_unique_id",
                        "cancer_study_identifier",
                        "mutation_variant",
                        "mutation_type",
                        "variant_class",
                        "chromosome",
                        "start_position",
                        "end_position",
                        "reference_allele",
                        "tumor_allele",
                        "codon_change",
                        "genome_build",
                        "refseq_mrna_id",
                    )
                }
                for r in rows
            ]
        raise AssertionError(f"FakeDB: unhandled mutation query:\n{q}")

    @staticmethod
    def _matches_config(q: str, e: dict) -> bool:
        """Evaluate the ALTERATION_CONFIGS filter fragments present in the query."""
        ok = False
        if "variant_type = 'mutation'" in q and e["variant_type"] == "mutation":
            ok = ok or e["mutation_status"] != "UNCALLED"
        if "cna_alteration = 2" in q and e["variant_type"] == "cna":
            ok = ok or e["cna_alteration"] == 2
        if "cna_alteration = -2" in q and e["variant_type"] == "cna":
            ok = ok or e["cna_alteration"] == -2
        if "variant_type = 'structural_variant'" in q:
            ok = ok or e["variant_type"] == "structural_variant"
        return ok

    def _profiled_in_scope(self, q: str) -> tuple[set[str], dict[str, set[str]]]:
        scope = self._scope(q)
        wes = {
            s
            for s in self.wes
            if scope is None or self.samples[s]["cancer_study_identifier"] in scope
        }
        panel = {
            s: genes
            for s, genes in self.panel.items()
            if scope is None or self.samples[s]["cancer_study_identifier"] in scope
        }
        return wes, panel

    def _q_mutation_panel_gene_coverage(self, q: str) -> list[dict]:
        wes, panel = self._profiled_in_scope(q)
        genes = self._in_list(q, "hugo_gene_symbol") or []
        if "UNION ALL" in q and "patient_unique_id" in q:  # profiled patients
            rows = [
                {"patient_unique_id": self.samples[s]["patient_unique_id"], "gene": g}
                for s, gs in panel.items()
                for g in gs
                if g in genes
            ]
            rows += [
                {"patient_unique_id": self.samples[s]["patient_unique_id"], "gene": "*"}
                for s in wes
            ]
            return rows
        return [
            {"sample_unique_id": s, "hugo_gene_symbol": g}
            for s, gs in panel.items()
            for g in gs
            if g in genes
        ]

    def _q_sample_to_gene_panel_derived(self, q: str) -> list[dict]:
        # Copy-number / SV profiling mirrors mutation profiling in these fixtures.
        return self._q_mutation_panel_gene_coverage(q)

    def _q_mutation_wes_coverage(self, q: str) -> list[dict]:
        wes, _ = self._profiled_in_scope(q)
        return [{"sample_unique_id": s} for s in sorted(wes)]

    def _clinical_in_scope(self, q: str) -> list[dict]:
        scope = self._scope(q)
        return [c for c in self.clinical if scope is None or c["cancer_study_identifier"] in scope]

    def _q_clinical_data_derived(self, q: str) -> list[dict]:
        rows = self._clinical_in_scope(q)
        if "_MONTHS" in q:
            endpoint = re.search(r"'(\w+)_MONTHS'", q).group(1)
            by_patient: dict[str, dict] = {}
            for c in rows:
                if c["attribute_name"] == f"{endpoint}_MONTHS":
                    by_patient.setdefault(c["patient_unique_id"], {})["time"] = c["attribute_value"]
                elif c["attribute_name"] == f"{endpoint}_STATUS":
                    by_patient.setdefault(c["patient_unique_id"], {})["status"] = c[
                        "attribute_value"
                    ]
            return [{"patient_unique_id": p, **v} for p, v in by_patient.items()]
        attr_match = re.search(r"attribute_name = '([^']+)'", q)
        attr = attr_match.group(1) if attr_match else None
        rows = [c for c in rows if attr is None or c["attribute_name"] == attr]
        if "n_cancer_types" in q:
            return [
                {
                    "n_samples": len({c["sample_unique_id"] for c in rows}),
                    "n_cancer_types": len({c["attribute_value"] for c in rows}),
                }
            ]
        if "SELECT DISTINCT sample_unique_id" in q:
            wanted = set(self._in_list(q, r"upper\(attribute_value\)") or [])
            return [
                {"sample_unique_id": s}
                for s in sorted(
                    {c["sample_unique_id"] for c in rows if c["attribute_value"].upper() in wanted}
                )
            ]
        if "SELECT DISTINCT patient_unique_id, attribute_value" in q:
            pairs = {(c["patient_unique_id"], c["attribute_value"]) for c in rows}
            return [{"patient_unique_id": p, "attribute_value": v} for p, v in sorted(pairs)]
        if "SELECT sample_unique_id, patient_unique_id, attribute_value, type" in q:
            return [dict(c) for c in rows]
        raise AssertionError(f"FakeDB: unhandled clinical_data_derived query:\n{q}")

    def _q_sample_derived(self, q: str) -> list[dict]:
        scope = self._scope(q)
        rows = [
            s
            for s in self.samples.values()
            if scope is None or s["cancer_study_identifier"] in scope
        ]
        if "HAVING length(studies) > 1" in q:
            by_id: dict[str, set[str]] = defaultdict(set)
            for s in rows:
                by_id[s["patient_stable_id"]].add(s["cancer_study_identifier"])
            shared = [sorted(v) for v in by_id.values() if len(v) > 1]
            return [{"shared_ids": len(shared), "example": shared[0] if shared else []}]
        if "SELECT DISTINCT patient_unique_id, cancer_study_identifier" in q:
            pairs = {(s["patient_unique_id"], s["cancer_study_identifier"]) for s in rows}
            return [
                {"patient_unique_id": p, "cancer_study_identifier": c} for p, c in sorted(pairs)
            ]
        if "SELECT sample_unique_id, cancer_study_identifier" in q:
            return [
                {k: s[k] for k in ("sample_unique_id", "cancer_study_identifier")} for s in rows
            ]
        if "SELECT sample_unique_id, patient_unique_id" in q:
            return [{k: s[k] for k in ("sample_unique_id", "patient_unique_id")} for s in rows]
        raise AssertionError(f"FakeDB: unhandled sample_derived query:\n{q}")

    def _q_genetic_profile(self, q: str) -> list[dict]:
        scope = self._scope(q)
        wanted = re.search(r"genetic_alteration_type = '([^']+)'", q).group(1)
        datatype = re.search(r"datatype = '([^']+)'", q)
        return [
            {"study": p["study"], "stable_id": p["stable_id"], "datatype": p["datatype"]}
            for p in self.profiles
            if (scope is None or p["study"] in scope)
            and p["genetic_alteration_type"] == wanted
            and (datatype is None or p["datatype"] == datatype.group(1))
        ]

    def _q_genetic_alteration_derived(self, q: str) -> list[dict]:
        pairs = set(re.findall(r"\('([^']+)', '([^']+)'\)", q))
        gene = re.search(r"hugo_gene_symbol = '([^']+)'", q).group(1)
        by_patient: dict[tuple[str, str], list[float]] = defaultdict(list)
        for e in self.expression:
            if e["hugo_gene_symbol"] != gene:
                continue
            if (e["cancer_study_identifier"], e["profile_type"]) not in pairs:
                continue
            s = self.samples[e["sample_unique_id"]]
            by_patient[(s["patient_unique_id"], s["cancer_study_identifier"])].append(
                float(e["alteration_value"])
            )
        return [
            {"patient_unique_id": p, "study": st, "value": sum(v) / len(v), "n_samples": len(v)}
            for (p, st), v in by_patient.items()
        ]
