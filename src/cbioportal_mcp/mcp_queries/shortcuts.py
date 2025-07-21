"""MCP convenience functions for common cBioPortal queries."""

from typing import Any, Dict, List, Optional
import logging

logger = logging.getLogger(__name__)


class MCPShortcuts:
    """Convenience functions for common cBioPortal analysis patterns."""
    
    @staticmethod
    def get_study_overview(
        study_id: str
    ) -> str:
        """
        Get comprehensive overview of a study.
        
        Combines study metadata with sample and clinical attribute information.
        """
        
        query = f"""
        WITH study_info AS (
            SELECT
                cs.cancer_study_identifier,
                cs.name,
                cs.description,
                type_of_cancer.name as cancer_type
            FROM cancer_study cs
            INNER JOIN type_of_cancer ON cs.type_of_cancer_id = type_of_cancer.type_of_cancer_id
            WHERE cs.cancer_study_identifier = '{study_id}'
        ),
        sample_count AS (
            SELECT count(*) as total_samples
            FROM sample_derived
            WHERE cancer_study_identifier = '{study_id}'
        ),
        clinical_attrs AS (
            SELECT
                type,
                count(DISTINCT attribute_name) as attr_count
            FROM clinical_data_derived
            WHERE cancer_study_identifier = '{study_id}'
            GROUP BY type
        ),
        mutation_count AS (
            SELECT count(DISTINCT sample_unique_id) as samples_with_mutations
            FROM genomic_event_derived
            WHERE cancer_study_identifier = '{study_id}'
                AND variant_type = 'mutation'
        )
        SELECT
            si.cancer_study_identifier as studyId,
            si.name as studyName,
            si.description,
            si.cancer_type as cancerType,
            sc.total_samples as totalSamples,
            mc.samples_with_mutations as samplesWithMutations,
            ca_sample.attr_count as sampleClinicalAttributes,
            ca_patient.attr_count as patientClinicalAttributes
        FROM study_info si
        CROSS JOIN sample_count sc
        CROSS JOIN mutation_count mc
        LEFT JOIN clinical_attrs ca_sample ON ca_sample.type = 'sample'
        LEFT JOIN clinical_attrs ca_patient ON ca_patient.type = 'patient'
        """
        
        return query.strip()
    
    @staticmethod
    def get_top_mutated_genes(
        study_ids: Optional[List[str]] = None,
        limit: int = 20
    ) -> str:
        """
        Get most frequently mutated genes across studies.
        """
        
        study_filter = ""
        if study_ids:
            study_str = "', '".join(study_ids)
            study_filter = f"AND cancer_study_identifier IN ('{study_str}')"
        
        query = f"""
        SELECT
            hugo_gene_symbol as geneSymbol,
            count(DISTINCT sample_unique_id) as mutatedSamples,
            count(DISTINCT cancer_study_identifier) as studiesWithMutations,
            count(*) as totalMutations
        FROM genomic_event_derived
        WHERE variant_type = 'mutation'
            AND mutation_status != 'UNCALLED'
            {study_filter}
        GROUP BY hugo_gene_symbol
        ORDER BY mutatedSamples DESC
        LIMIT {limit}
        """
        
        return query.strip()
    
    @staticmethod
    def get_clinical_summary(
        attribute_name: str,
        study_ids: Optional[List[str]] = None
    ) -> str:
        """
        Get summary statistics for a clinical attribute.
        """
        
        study_filter = ""
        if study_ids:
            study_str = "', '".join(study_ids)
            study_filter = f"AND cancer_study_identifier IN ('{study_str}')"
        
        query = f"""
        SELECT
            cancer_study_identifier as studyId,
            attribute_name as attributeName,
            type,
            attribute_value as value,
            count(*) as count
        FROM clinical_data_derived
        WHERE attribute_name = '{attribute_name}'
            {study_filter}
        GROUP BY cancer_study_identifier, attribute_name, type, attribute_value
        ORDER BY cancer_study_identifier, count DESC
        """
        
        return query.strip()


async def execute_mcp_shortcut(
    clickhouse_client,
    shortcut_type: str,
    **kwargs
) -> Dict[str, Any]:
    """
    Execute MCP shortcut queries with error handling.
    
    Args:
        clickhouse_client: ClickHouse client instance
        shortcut_type: Type of shortcut to execute
        **kwargs: Query parameters
    
    Returns:
        Query results as dictionary
    """
    
    try:
        shortcuts = MCPShortcuts()
        
        if shortcut_type == "study_overview":
            study_id = kwargs.get("study_id")
            if not study_id:
                raise ValueError("study_id is required")
            sql = shortcuts.get_study_overview(study_id)
            
        elif shortcut_type == "top_mutated_genes":
            sql = shortcuts.get_top_mutated_genes(
                study_ids=kwargs.get("study_ids"),
                limit=kwargs.get("limit", 20)
            )
            
        elif shortcut_type == "clinical_summary":
            attribute_name = kwargs.get("attribute_name")
            if not attribute_name:
                raise ValueError("attribute_name is required")
            sql = shortcuts.get_clinical_summary(
                attribute_name=attribute_name,
                study_ids=kwargs.get("study_ids")
            )
            
        else:
            raise ValueError(f"Unknown shortcut type: {shortcut_type}")
        
        logger.info(f"Executing MCP shortcut: {shortcut_type}")
        logger.debug(f"SQL: {sql}")
        
        result = await clickhouse_client.execute_query(sql)
        
        return {
            "success": True,
            "shortcut_type": shortcut_type,
            "data": result,
            "row_count": len(result) if isinstance(result, list) else 1
        }
        
    except Exception as e:
        logger.error(f"Error executing MCP shortcut {shortcut_type}: {e}")
        return {
            "success": False,
            "shortcut_type": shortcut_type,
            "error": str(e),
            "data": None
        }