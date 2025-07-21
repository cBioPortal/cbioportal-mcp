"""Genomic data queries ported from ClickhouseGenomicDataMapper.xml."""

from typing import Any, Dict, List, Optional
import logging

logger = logging.getLogger(__name__)


class GenomicDataQueries:
    """Genomic data database queries ported from MyBatis mapper."""
    
    @staticmethod
    def get_molecular_profile_sample_counts() -> str:
        """
        Get molecular profile sample counts.
        
        Ported from ClickhouseGenomicDataMapper.xml:getMolecularProfileSampleCounts
        Note: StudyViewFilter logic simplified for this implementation.
        """
        
        query = """
        SELECT 
            replaceOne(genetic_profile.stable_id,
                concat(sample_derived.cancer_study_identifier,'_'), '') AS value,
            genetic_profile.name AS label,
            count(sample_profile.genetic_profile_id) AS count
        FROM sample_profile
        LEFT JOIN sample_derived ON sample_profile.sample_id = sample_derived.internal_id
        LEFT JOIN genetic_profile ON sample_profile.genetic_profile_id = genetic_profile.genetic_profile_id
        GROUP BY genetic_profile.stable_id, genetic_profile.name, sample_derived.cancer_study_identifier
        """
        
        return query.strip()
    
    @staticmethod
    def get_genomic_data_bin_counts(
        profile_type: str,
        hugo_gene_symbols: List[str]
    ) -> str:
        """
        Get genomic data bin counts for genes.
        
        Ported from ClickhouseGenomicDataMapper.xml:getGenomicDataBinCounts
        """
        
        gene_filter = ""
        if hugo_gene_symbols:
            genes_str = "', '".join(hugo_gene_symbols)
            gene_filter = f"AND hugo_gene_symbol IN ('{genes_str}')"
        
        query = f"""
        WITH genomic_numerical_query AS (
            SELECT
                concat(hugo_gene_symbol, profile_type) AS attributeId,
                multiIf(
                    alteration_value = '' OR upperUTF8(alteration_value) = 'NA' OR 
                    upperUTF8(alteration_value) = 'NAN' OR upperUTF8(alteration_value) = 'N/A',
                    'NA',
                    alteration_value
                ) AS value,
                cast(count(value) as INTEGER) AS count
            FROM genetic_alteration_derived
            WHERE 
                multiIf(
                    alteration_value = '' OR upperUTF8(alteration_value) = 'NA' OR 
                    upperUTF8(alteration_value) = 'NAN' OR upperUTF8(alteration_value) = 'N/A',
                    'NA',
                    alteration_value
                ) != 'NA'
                AND profile_type = '{profile_type}'
                {gene_filter}
            GROUP BY hugo_gene_symbol, profile_type, value
        ),
        genomic_numerical_sum AS (
            SELECT
                attributeId,
                sum(count) as genomic_numerical_count
            FROM genomic_numerical_query
            GROUP BY attributeId
        )
        SELECT * FROM genomic_numerical_query
        UNION ALL
        SELECT
            coalesce((SELECT attributeId FROM genomic_numerical_sum LIMIT 1),
                concat('{hugo_gene_symbols[0] if hugo_gene_symbols else "GENE"}', '{profile_type}')) as attributeId,
            'NA' as value,
            cast(0 as INTEGER) as count  -- Simplified NA count calculation
        """
        
        return query.strip()
    
    @staticmethod
    def get_cna_counts(
        profile_type: str,
        hugo_gene_symbols: List[str]
    ) -> str:
        """
        Get copy number alteration counts.
        
        Ported from ClickhouseGenomicDataMapper.xml:getCNACounts
        """
        
        gene_filter = ""
        if hugo_gene_symbols:
            genes_str = "', '".join(hugo_gene_symbols)
            gene_filter = f"AND hugo_gene_symbol IN ('{genes_str}')"
        
        query = f"""
        WITH cna_count_query as (
            SELECT
                hugo_gene_symbol as hugoGeneSymbol,
                '{profile_type}' as profileType,
                multiIf(
                    alteration_value = '2', 'Amplified', 
                    alteration_value = '1', 'Gained', 
                    alteration_value = '0', 'Diploid', 
                    alteration_value = '-1', 'Heterozygously deleted', 
                    alteration_value = '-2', 'Homozygously deleted', 
                    'NA'
                ) as label,
                toString(alteration_value) as value,
                cast(count(*) as INTEGER) as count
            FROM genetic_alteration_derived
            WHERE 
                multiIf(
                    alteration_value = '' OR upperUTF8(alteration_value) = 'NA' OR 
                    upperUTF8(alteration_value) = 'NAN' OR upperUTF8(alteration_value) = 'N/A',
                    'NA',
                    alteration_value
                ) != 'NA'
                AND profile_type = '{profile_type}'
                {gene_filter}
            GROUP BY hugo_gene_symbol, alteration_value
        ),
        cna_sum AS (
            SELECT
                hugoGeneSymbol,
                sum(count) as cna_count
            FROM cna_count_query
            GROUP BY hugoGeneSymbol
        )
        SELECT * FROM cna_count_query
        UNION ALL
        SELECT
            coalesce((SELECT hugoGeneSymbol FROM cna_sum LIMIT 1), '{hugo_gene_symbols[0] if hugo_gene_symbols else "GENE"}') as hugoGeneSymbol,
            '{profile_type}',
            'NA' as label,
            'NA' as value,
            cast(0 as INTEGER) as count  -- Simplified NA count calculation
        """
        
        return query.strip()
    
    @staticmethod
    def get_mutation_counts_by_type(
        hugo_gene_symbols: List[str]
    ) -> str:
        """
        Get mutation counts by type.
        
        Ported from ClickhouseGenomicDataMapper.xml:getMutationCountsByType
        """
        
        gene_filter = ""
        if hugo_gene_symbols:
            genes_str = "', '".join(hugo_gene_symbols)
            gene_filter = f"AND hugo_gene_symbol IN ('{genes_str}')"
        
        query = f"""
        SELECT
            hugo_gene_symbol as hugoGeneSymbol,
            'mutations' as profileType,
            replace(mutation_type, '_', ' ') as label,
            mutation_type as value,
            count(*) as count,
            count(distinct(sample_unique_id)) as uniqueCount
        FROM genomic_event_derived
        WHERE variant_type = 'mutation'
            {gene_filter}
        GROUP BY mutation_type, hugo_gene_symbol
        """
        
        return query.strip()
    
    @staticmethod
    def get_mutation_counts(
        hugo_gene_symbol: str
    ) -> str:
        """
        Get mutation profiling and mutation counts for a gene.
        
        Ported from ClickhouseGenomicDataMapper.xml:getMutationCounts
        """
        
        query = f"""
        WITH profiled_count as (
            SELECT count(distinct sgp.sample_unique_id)
            FROM sample_to_gene_panel_derived sgp
            JOIN gene_panel_to_gene_derived gpg ON sgp.gene_panel_id = gpg.gene_panel_id
            WHERE gpg.gene = '{hugo_gene_symbol}'
                AND sgp.alteration_type = 'MUTATION_EXTENDED'
        ),
        mutated_count as (
            SELECT count(distinct sample_unique_id)
            FROM genomic_event_derived
            WHERE hugo_gene_symbol = '{hugo_gene_symbol}'
                AND variant_type = 'mutation'
        )
        SELECT
            cast((SELECT * FROM mutated_count) as INTEGER) as mutatedCount,
            cast(((SELECT * FROM profiled_count) - (SELECT * FROM mutated_count)) as INTEGER) as notMutatedCount,
            cast((SELECT * FROM profiled_count) as INTEGER) as profiledCount
        """
        
        return query.strip()
    
    @staticmethod
    def get_gene_mutations(
        hugo_gene_symbol: str,
        study_ids: Optional[List[str]] = None
    ) -> str:
        """
        Get detailed mutation information for a gene.
        
        Custom convenience function for gene mutation lookup.
        """
        
        study_filter = ""
        if study_ids:
            study_str = "', '".join(study_ids)
            study_filter = f"AND cancer_study_identifier IN ('{study_str}')"
        
        query = f"""
        SELECT
            cancer_study_identifier as studyId,
            sample_unique_id as sampleId,
            patient_unique_id as patientId,
            hugo_gene_symbol as geneSymbol,
            entrez_gene_id as entrezGeneId,
            mutation_type as mutationType,
            mutation_status as mutationStatus,
            variant_type as variantType,
            protein_change as proteinChange,
            amino_acid_change as aminoAcidChange
        FROM genomic_event_derived
        WHERE hugo_gene_symbol = '{hugo_gene_symbol}'
            AND variant_type = 'mutation'
            {study_filter}
        ORDER BY cancer_study_identifier, sample_unique_id
        """
        
        return query.strip()


async def execute_genomic_data_query(
    clickhouse_client,
    query_type: str,
    **kwargs
) -> Dict[str, Any]:
    """
    Execute genomic data queries with error handling.
    
    Args:
        clickhouse_client: ClickHouse client instance
        query_type: Type of query to execute
        **kwargs: Query parameters
    
    Returns:
        Query results as dictionary
    """
    
    try:
        queries = GenomicDataQueries()
        
        if query_type == "molecular_profile_sample_counts":
            sql = queries.get_molecular_profile_sample_counts()
            
        elif query_type == "genomic_data_bin_counts":
            profile_type = kwargs.get("profile_type")
            hugo_gene_symbols = kwargs.get("hugo_gene_symbols", [])
            if not profile_type:
                raise ValueError("profile_type is required")
            sql = queries.get_genomic_data_bin_counts(profile_type, hugo_gene_symbols)
            
        elif query_type == "cna_counts":
            profile_type = kwargs.get("profile_type", "cna")
            hugo_gene_symbols = kwargs.get("hugo_gene_symbols", [])
            sql = queries.get_cna_counts(profile_type, hugo_gene_symbols)
            
        elif query_type == "mutation_counts_by_type":
            hugo_gene_symbols = kwargs.get("hugo_gene_symbols", [])
            sql = queries.get_mutation_counts_by_type(hugo_gene_symbols)
            
        elif query_type == "mutation_counts":
            hugo_gene_symbol = kwargs.get("hugo_gene_symbol")
            if not hugo_gene_symbol:
                raise ValueError("hugo_gene_symbol is required")
            sql = queries.get_mutation_counts(hugo_gene_symbol)
            
        elif query_type == "gene_mutations":
            hugo_gene_symbol = kwargs.get("hugo_gene_symbol")
            if not hugo_gene_symbol:
                raise ValueError("hugo_gene_symbol is required")
            sql = queries.get_gene_mutations(
                hugo_gene_symbol=hugo_gene_symbol,
                study_ids=kwargs.get("study_ids")
            )
            
        else:
            raise ValueError(f"Unknown query type: {query_type}")
        
        logger.info(f"Executing genomic data query: {query_type}")
        logger.debug(f"SQL: {sql}")
        
        result = await clickhouse_client.execute_query(sql)
        
        return {
            "success": True,
            "query_type": query_type,
            "data": result,
            "row_count": len(result) if isinstance(result, list) else 1
        }
        
    except Exception as e:
        logger.error(f"Error executing genomic data query {query_type}: {e}")
        return {
            "success": False,
            "query_type": query_type,
            "error": str(e),
            "data": None
        }