-- Daily-clone ID lookups. No existing query/view is changed.
-- Local CLICKHOUSE TABLE sources default to the dictionary's database, so
-- blue/green clones do not read one another. No remote credentials are embedded.
-- LIFETIME(0) disables timed refresh: CREATE OR REPLACE invalidates the previous
-- snapshot on every clone/prep run; the first lookup loads the new snapshot.

-- A symbol may map to several Entrez IDs. Preserve every distinct match.
CREATE OR REPLACE VIEW dictionary_gene_symbol_source AS
SELECT g.hugo_gene_symbol AS hugo_gene_symbol,
       arraySort(groupUniqArray(g.entrez_gene_id)) AS entrez_gene_ids
FROM gene AS g
GROUP BY g.hugo_gene_symbol;

-- Match script 6's standard-list counts, using only base-schema columns.
-- This also works with apply_sql.sh's lexicographic order (10 before 6).
CREATE OR REPLACE VIEW dictionary_study_source AS
SELECT assumeNotNull(cs.cancer_study_identifier) AS cancer_study_identifier,
       cs.cancer_study_id AS cancer_study_id,
       cs.name AS name,
       cs.type_of_cancer_id AS type_of_cancer_id,
       ifNull(counts.sample_count, 0) AS sample_count,
       ifNull(counts.mutation_sample_count, 0) AS mutation_sample_count,
       ifNull(counts.cna_sample_count, 0) AS cna_sample_count
FROM cancer_study AS cs
LEFT JOIN (
    SELECT study.cancer_study_id AS cancer_study_id,
           countIf(sl.stable_id = concat(study.cancer_study_identifier, '_all')) AS sample_count,
           countIf(sl.stable_id = concat(study.cancer_study_identifier, '_sequenced')) AS mutation_sample_count,
           countIf(sl.stable_id = concat(study.cancer_study_identifier, '_cna')) AS cna_sample_count
    FROM sample_list_list AS sll
    INNER JOIN sample_list AS sl ON sll.list_id = sl.list_id
    INNER JOIN cancer_study AS study ON sl.cancer_study_id = study.cancer_study_id
    GROUP BY study.cancer_study_id
) AS counts ON cs.cancer_study_id = counts.cancer_study_id
WHERE cs.cancer_study_identifier IS NOT NULL;

-- Complex keys preserve the upstream signed Int64 IDs (including negative IDs).
CREATE OR REPLACE DICTIONARY gene_by_entrez_dict
(
    entrez_gene_id Int64,
    hugo_gene_symbol String
)
PRIMARY KEY entrez_gene_id
SOURCE(CLICKHOUSE(TABLE 'gene'))
LAYOUT(COMPLEX_KEY_HASHED())
LIFETIME(0);

CREATE OR REPLACE DICTIONARY gene_by_symbol_dict
(
    hugo_gene_symbol String,
    entrez_gene_ids Array(Int64)
)
PRIMARY KEY hugo_gene_symbol
SOURCE(CLICKHOUSE(TABLE 'dictionary_gene_symbol_source'))
LAYOUT(COMPLEX_KEY_HASHED())
LIFETIME(0);

CREATE OR REPLACE DICTIONARY study_by_identifier_dict
(
    cancer_study_identifier String,
    cancer_study_id Int64,
    name String,
    type_of_cancer_id String,
    sample_count UInt64,
    mutation_sample_count UInt64,
    cna_sample_count UInt64
)
PRIMARY KEY cancer_study_identifier
SOURCE(CLICKHOUSE(TABLE 'dictionary_study_source'))
LAYOUT(COMPLEX_KEY_HASHED())
LIFETIME(0);

CREATE OR REPLACE DICTIONARY genetic_profile_by_stable_id_dict
(
    stable_id String,
    genetic_profile_id Int64,
    cancer_study_id Int64,
    genetic_alteration_type String
)
PRIMARY KEY stable_id
SOURCE(CLICKHOUSE(TABLE 'genetic_profile'))
LAYOUT(COMPLEX_KEY_HASHED())
LIFETIME(0);
