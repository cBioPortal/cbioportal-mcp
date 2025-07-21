"""Cancer study queries ported from CancerStudyMapper.xml."""

from typing import Any, Dict, List, Optional
import logging

logger = logging.getLogger(__name__)


class CancerStudyQueries:
    """Cancer study database queries ported from MyBatis mapper."""
    
    @staticmethod
    def get_cancer_studies_metadata(
        study_ids: Optional[List[str]] = None,
        sort_field: Optional[str] = None,
        sort_order: Optional[str] = "ASC"
    ) -> str:
        """
        Get detailed cancer study metadata with sample counts.
        
        Ported from CancerStudyMapper.xml:getCancerStudiesMetadata
        """
        
        # Build WHERE clause for study filtering
        where_clause = ""
        if study_ids:
            study_ids_str = "', '".join(study_ids)
            where_clause = f"WHERE cs.cancer_study_identifier IN ('{study_ids_str}')"
        
        # Build ORDER BY clause
        order_clause = ""
        if sort_field:
            order_clause = f"ORDER BY {sort_field} {sort_order}"
        
        query = f"""
        WITH sample_counts as (
            SELECT
                sample_list.cancer_study_id,
                countIf(stable_id LIKE '%_all') as allSampleCount,
                countIf(stable_id LIKE '%_sequenced') as sequencedSampleCount,
                countIf(stable_id LIKE '%_cna') as cnaSampleCount,
                countIf(stable_id LIKE '%_rna_seq_v2_mrna') as mrnaRnaSeqV2SampleCount,
                countIf(stable_id LIKE '%_microrna') as miRnaSampleCount,
                countIf(stable_id LIKE '%_mrna' AND stable_id NOT LIKE '%_rna_seq_v2_mrna') as mrnaMicroarraySampleCount,
                countIf(stable_id LIKE '%_methylation_hm27') as methylationHm27SampleCount,
                countIf(stable_id LIKE '%_rppa') as rppaSampleCount,
                countIf(stable_id LIKE '%_protein_quantification') as massSpectrometrySampleCount,
                countIf(stable_id LIKE '%_3way_complete') as completeSampleCount,
                countIf(stable_id LIKE '%_rna_seq_mrna') as mrnaRnaSeqSampleCount
            FROM sample_list_list
            INNER JOIN sample_list ON sample_list_list.list_id = sample_list.list_id
            GROUP BY sample_list.cancer_study_id
        ),
        treatment AS (
            SELECT
                COUNT(DISTINCT patient_unique_id) as count,
                cancer_study_identifier
            FROM clinical_event_derived
            WHERE event_type IN ('Treatment', 'TREATMENT')
            GROUP BY cancer_study_identifier 
        ),
        sv AS (
            SELECT
                COUNT(DISTINCT sample_unique_id) as count,
                cancer_study_identifier
            FROM genomic_event_derived
            WHERE variant_type = 'structural_variant'
            GROUP BY cancer_study_identifier 
        )
        SELECT
            cs.cancer_study_id AS cancerStudyId,
            cs.cancer_study_identifier AS cancerStudyIdentifier,
            cs.type_of_cancer_id AS typeOfCancerId,
            cs.name AS name,
            cs.description AS description,
            cs.public AS publicStudy,
            cs.pmid AS pmid,
            cs.citation AS citation,
            cs.groups AS groups,
            cs.status AS status,
            cs.import_date AS importDate,
            reference_genome.name AS referenceGenome,
            allSampleCount,
            sequencedSampleCount,
            cnaSampleCount,
            mrnaRnaSeqV2SampleCount,
            miRnaSampleCount,
            mrnaMicroarraySampleCount,
            methylationHm27SampleCount,
            rppaSampleCount,
            massSpectrometrySampleCount,
            completeSampleCount,
            mrnaRnaSeqSampleCount,
            IFNULL(treatment.count, 0) AS treatmentCount,
            IFNULL(sv.count, 0) AS structuralVariantCount,
            type_of_cancer.name AS type_of_cancer_name,
            type_of_cancer.dedicated_color AS type_of_cancer_dedicated_color,
            type_of_cancer.short_name AS type_of_cancer_short_name,
            type_of_cancer.parent AS type_of_cancer_parent
            
        FROM cancer_study AS cs
        INNER JOIN reference_genome ON reference_genome.reference_genome_id = cs.reference_genome_id
        INNER JOIN type_of_cancer ON cs.type_of_cancer_id = type_of_cancer.type_of_cancer_id
        LEFT JOIN treatment ON cs.cancer_study_identifier = treatment.cancer_study_identifier
        LEFT JOIN sv ON sv.cancer_study_identifier = cs.cancer_study_identifier
        INNER JOIN sample_counts ON sample_counts.cancer_study_id = cs.cancer_study_id
        {where_clause}
        GROUP BY cs.cancer_study_id, cs.cancer_study_identifier, cs.type_of_cancer_id, cs.name,
        cs.description, cs.public, cs.pmid, cs.citation, cs.groups, cs.status, cs.import_date, 
        reference_genome.name, treatment.count, sv.count, allSampleCount,
        sequencedSampleCount, cnaSampleCount, mrnaRnaSeqV2SampleCount,
        miRnaSampleCount, mrnaMicroarraySampleCount, methylationHm27SampleCount,
        rppaSampleCount, massSpectrometrySampleCount, completeSampleCount, 
        mrnaRnaSeqSampleCount, type_of_cancer.name, type_of_cancer.dedicated_color,
        type_of_cancer.short_name, type_of_cancer.parent
        {order_clause}
        """
        
        return query.strip()
    
    @staticmethod
    def get_cancer_studies_metadata_summary(
        study_ids: Optional[List[str]] = None
    ) -> str:
        """
        Get cancer study metadata summary (without sample counts).
        
        Ported from CancerStudyMapper.xml:getCancerStudiesMetadataSummary
        """
        
        # Build WHERE clause for study filtering
        where_clause = ""
        if study_ids:
            study_ids_str = "', '".join(study_ids)
            where_clause = f"WHERE cs.cancer_study_identifier IN ('{study_ids_str}')"
        
        query = f"""
        SELECT
            cs.cancer_study_id AS cancerStudyId,
            cs.cancer_study_identifier AS cancerStudyIdentifier,
            cs.type_of_cancer_id AS typeOfCancerId,
            cs.name AS name,
            cs.description AS description,
            cs.public AS publicStudy,
            cs.pmid AS pmid,
            cs.citation AS citation,
            cs.groups AS groups,
            cs.status AS status,
            cs.import_date AS importDate,
            reference_genome.name AS referenceGenome,
            type_of_cancer.name AS type_of_cancer_name,
            type_of_cancer.dedicated_color AS type_of_cancer_dedicated_color,
            type_of_cancer.short_name AS type_of_cancer_short_name,
            type_of_cancer.parent AS type_of_cancer_parent

        FROM cancer_study AS cs
        INNER JOIN reference_genome ON reference_genome.reference_genome_id = cs.reference_genome_id
        INNER JOIN type_of_cancer ON cs.type_of_cancer_id = type_of_cancer.type_of_cancer_id
        {where_clause}
        GROUP BY cs.cancer_study_id, cs.cancer_study_identifier, cs.type_of_cancer_id, cs.name,
        cs.description, cs.public, cs.pmid, cs.citation, cs.groups, cs.status, cs.import_date,
        reference_genome.name, type_of_cancer.name, type_of_cancer.dedicated_color,
        type_of_cancer.short_name, type_of_cancer.parent
        """
        
        return query.strip()
    
    @staticmethod
    def get_filtered_study_ids() -> str:
        """
        Get study IDs from filtered samples.
        
        Ported from CancerStudyMapper.xml:getFilteredStudyIds
        Note: This would typically be used with StudyViewFilter, but simplified here.
        """
        
        query = """
        SELECT DISTINCT cancer_study_identifier
        FROM sample_derived
        """
        
        return query.strip()
    
    @staticmethod
    def search_studies_by_keyword(keyword: str) -> str:
        """
        Search studies by keyword in name, description, or identifier.
        
        This is a custom convenience function not in the original mapper.
        """
        
        query = f"""
        SELECT
            cs.cancer_study_id AS cancerStudyId,
            cs.cancer_study_identifier AS cancerStudyIdentifier,
            cs.type_of_cancer_id AS typeOfCancerId,
            cs.name AS name,
            cs.description AS description,
            cs.public AS publicStudy,
            cs.pmid AS pmid,
            cs.citation AS citation,
            type_of_cancer.name AS type_of_cancer_name,
            type_of_cancer.short_name AS type_of_cancer_short_name
            
        FROM cancer_study AS cs
        INNER JOIN type_of_cancer ON cs.type_of_cancer_id = type_of_cancer.type_of_cancer_id
        WHERE 
            cs.name ILIKE '%{keyword}%' 
            OR cs.description ILIKE '%{keyword}%'
            OR cs.cancer_study_identifier ILIKE '%{keyword}%'
            OR type_of_cancer.name ILIKE '%{keyword}%'
        ORDER BY cs.name
        """
        
        return query.strip()


async def execute_cancer_study_query(
    clickhouse_client, 
    query_type: str, 
    **kwargs
) -> Dict[str, Any]:
    """
    Execute cancer study queries with error handling.
    
    Args:
        clickhouse_client: ClickHouse client instance
        query_type: Type of query to execute
        **kwargs: Query parameters
    
    Returns:
        Query results as dictionary
    """
    
    try:
        queries = CancerStudyQueries()
        
        if query_type == "metadata":
            sql = queries.get_cancer_studies_metadata(
                study_ids=kwargs.get("study_ids"),
                sort_field=kwargs.get("sort_field"),
                sort_order=kwargs.get("sort_order")
            )
        elif query_type == "summary": 
            sql = queries.get_cancer_studies_metadata_summary(
                study_ids=kwargs.get("study_ids")
            )
        elif query_type == "search":
            keyword = kwargs.get("keyword", "")
            if not keyword:
                raise ValueError("Keyword is required for search")
            sql = queries.search_studies_by_keyword(keyword)
        elif query_type == "study_ids":
            sql = queries.get_filtered_study_ids()
        else:
            raise ValueError(f"Unknown query type: {query_type}")
        
        logger.info(f"Executing cancer study query: {query_type}")
        logger.debug(f"SQL: {sql}")
        
        result = await clickhouse_client.execute_query(sql)
        
        return {
            "success": True,
            "query_type": query_type,
            "data": result,
            "row_count": len(result) if isinstance(result, list) else 1
        }
        
    except Exception as e:
        logger.error(f"Error executing cancer study query {query_type}: {e}")
        return {
            "success": False,
            "query_type": query_type,
            "error": str(e),
            "data": None
        }