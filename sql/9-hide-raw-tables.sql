-- ============================================================================
-- Hide the raw cBioPortal tables from the agent
-- ============================================================================
-- Everything the agent needs is in the *_derived tables, the views and small
-- tables built by the files above (treatment_event_agents, sample_list_members,
-- cancer_study counts). The raw tables are dropped from the LLM copy last, so
-- the earlier files can still read them while they build. Kept on purpose:
-- cancer_study, type_of_cancer, genetic_profile, clinical_attribute_meta,
-- gene, resource, resource_definition (small, one hop from cancer_study).
--
-- No view in sql/ reads a table dropped here (tests/test_hidden_raw_tables.py).
-- ============================================================================

DROP TABLE IF EXISTS allele_specific_copy_number SYNC;
DROP TABLE IF EXISTS alteration_driver_annotation SYNC;
DROP TABLE IF EXISTS cbioportal_sequence_state SYNC;
DROP TABLE IF EXISTS clinical_event SYNC;
DROP TABLE IF EXISTS clinical_event_data SYNC;
DROP TABLE IF EXISTS clinical_patient SYNC;
DROP TABLE IF EXISTS clinical_sample SYNC;
DROP TABLE IF EXISTS cna_event SYNC;
DROP TABLE IF EXISTS copy_number_seg SYNC;
DROP TABLE IF EXISTS copy_number_seg_file SYNC;
DROP TABLE IF EXISTS gene_alias SYNC;
DROP TABLE IF EXISTS gene_panel SYNC;
DROP TABLE IF EXISTS gene_panel_list SYNC;
DROP TABLE IF EXISTS genetic_alteration SYNC;
DROP TABLE IF EXISTS genetic_entity SYNC;
DROP TABLE IF EXISTS generic_entity_properties SYNC;
DROP TABLE IF EXISTS genetic_profile_link SYNC;
DROP TABLE IF EXISTS genetic_profile_samples SYNC;
DROP TABLE IF EXISTS geneset SYNC;
DROP TABLE IF EXISTS geneset_gene SYNC;
DROP TABLE IF EXISTS geneset_hierarchy_leaf SYNC;
DROP TABLE IF EXISTS geneset_hierarchy_node SYNC;
DROP TABLE IF EXISTS gistic SYNC;
DROP TABLE IF EXISTS gistic_to_gene SYNC;
DROP TABLE IF EXISTS info SYNC;
DROP TABLE IF EXISTS mut_sig SYNC;
DROP TABLE IF EXISTS mutation SYNC;
DROP TABLE IF EXISTS mutation_count_by_keyword SYNC;
DROP TABLE IF EXISTS mutation_event SYNC;
DROP TABLE IF EXISTS patient SYNC;
DROP TABLE IF EXISTS reference_genome SYNC;
DROP TABLE IF EXISTS reference_genome_gene SYNC;
DROP TABLE IF EXISTS sample SYNC;
DROP TABLE IF EXISTS sample_cna_event SYNC;
DROP TABLE IF EXISTS sample_list SYNC;
DROP TABLE IF EXISTS sample_list_list SYNC;
DROP TABLE IF EXISTS sample_profile SYNC;
DROP TABLE IF EXISTS structural_variant SYNC;
