"""Clinical data queries ported from ClickhouseClinicalDataMapper.xml."""

from typing import Any, Dict, List, Optional
import logging

logger = logging.getLogger(__name__)


class ClinicalDataQueries:
    """Clinical data database queries ported from MyBatis mapper."""
    
    @staticmethod
    def get_sample_clinical_data_from_study_view_filter(
        attribute_ids: Optional[List[str]] = None
    ) -> str:
        """
        Get sample-level clinical data with study view filtering.
        
        Ported from ClickhouseClinicalDataMapper.xml:getSampleClinicalDataFromStudyViewFilter
        Note: StudyViewFilter logic simplified for this implementation.
        """
        
        # Build attribute filter
        attribute_filter = ""
        if attribute_ids:
            attr_str = "', '".join(attribute_ids)
            attribute_filter = f"AND attribute_name IN ('{attr_str}')"
        
        query = f"""
        SELECT
            internal_id as internalId,
            replaceOne(sample_unique_id, concat(cancer_study_identifier, '_'), '') as sampleId,
            replaceOne(patient_unique_id, concat(cancer_study_identifier, '_'), '') as patientId,
            attribute_name as attrId,
            attribute_value as attrValue,
            cancer_study_identifier as studyId
        FROM clinical_data_derived
        WHERE type = 'sample'
        {attribute_filter}
        """
        
        return query.strip()
    
    @staticmethod
    def get_patient_clinical_data_from_study_view_filter(
        attribute_ids: Optional[List[str]] = None
    ) -> str:
        """
        Get patient-level clinical data with study view filtering.
        
        Ported from ClickhouseClinicalDataMapper.xml:getPatientClinicalDataFromStudyViewFilter
        """
        
        # Build attribute filter
        attribute_filter = ""
        if attribute_ids:
            attr_str = "', '".join(attribute_ids)
            attribute_filter = f"AND attribute_name IN ('{attr_str}')"
        
        query = f"""
        SELECT
            internal_id as internalId,
            replaceOne(patient_unique_id, concat(cancer_study_identifier, '_'), '') as patientId,
            attribute_name as attrId,
            attribute_value as attrValue,
            cancer_study_identifier as studyId
        FROM clinical_data_derived
        WHERE type = 'patient'
        {attribute_filter}
        """
        
        return query.strip()
    
    @staticmethod
    def get_clinical_data_counts(
        sample_attribute_ids: Optional[List[str]] = None,
        patient_attribute_ids: Optional[List[str]] = None,
        study_ids: Optional[List[str]] = None
    ) -> str:
        """
        Get clinical data value counts for specified attributes.
        
        Ported from ClickhouseClinicalDataMapper.xml:getClinicalDataCounts
        This is a simplified version of the complex original query.
        """
        
        queries = []
        
        # Sample attribute counts
        if sample_attribute_ids:
            attr_str = "', '".join(sample_attribute_ids)
            study_filter = ""
            if study_ids:
                study_str = "', '".join(study_ids)
                study_filter = f"AND cancer_study_identifier IN ('{study_str}')"
            
            sample_query = f"""
            (
                WITH clinical_data_query AS (
                    SELECT
                        attribute_name AS attributeId,
                        attribute_value AS value,
                        cast(count(*) AS INTEGER) as count
                    FROM clinical_data_derived cdd
                    WHERE attribute_name IN ('{attr_str}')
                        AND type = 'sample'
                        AND attribute_value != 'NA'
                        AND attribute_value != ''
                        {study_filter}
                    GROUP BY attribute_name, attribute_value 
                ),
                clinical_data_sum AS (
                    SELECT attributeId, sum(count) AS sum 
                    FROM clinical_data_query 
                    GROUP BY attributeId
                )
                SELECT * FROM clinical_data_query
                UNION ALL
                SELECT 
                    attributeId,
                    'NA' AS value,
                    cast(0 as INTEGER) AS count  -- Simplified NA count
                FROM clinical_data_sum
                WHERE 0 > 0  -- Disabled for now
            )
            """
            queries.append(sample_query)
        
        # Patient attribute counts  
        if patient_attribute_ids:
            attr_str = "', '".join(patient_attribute_ids)
            study_filter = ""
            if study_ids:
                study_str = "', '".join(study_ids)
                study_filter = f"AND cancer_study_identifier IN ('{study_str}')"
            
            patient_query = f"""
            (
                WITH clinical_data_query AS (
                    SELECT
                        attribute_name AS attributeId,
                        attribute_value AS value,
                        cast(count(*) AS INTEGER) as count
                    FROM clinical_data_derived cdd
                    WHERE attribute_name IN ('{attr_str}')
                        AND type = 'patient'
                        AND attribute_value != 'NA'
                        AND attribute_value != ''
                        {study_filter}
                    GROUP BY attribute_name, attribute_value
                ),
                clinical_data_sum AS (
                    SELECT attributeId, sum(count) AS sum 
                    FROM clinical_data_query 
                    GROUP BY attributeId
                )
                SELECT * FROM clinical_data_query
                UNION ALL
                SELECT 
                    attributeId,
                    'NA' AS value,
                    cast(0 as INTEGER) AS count  -- Simplified NA count
                FROM clinical_data_sum
                WHERE 0 > 0  -- Disabled for now
            )
            """
            queries.append(patient_query)
        
        if not queries:
            return "SELECT 'No attributes specified' as error"
        
        return " UNION ALL ".join(queries)
    
    @staticmethod
    def get_clinical_data_by_attribute(
        attribute_name: str,
        study_ids: Optional[List[str]] = None,
        data_type: str = "sample"
    ) -> str:
        """
        Get clinical data for a specific attribute.
        
        Custom convenience function for easy attribute lookup.
        """
        
        study_filter = ""
        if study_ids:
            study_str = "', '".join(study_ids)
            study_filter = f"AND cancer_study_identifier IN ('{study_str}')"
        
        query = f"""
        SELECT
            cancer_study_identifier as studyId,
            {'sample_unique_id' if data_type == 'sample' else 'patient_unique_id'} as uniqueId,
            attribute_name as attributeName,
            attribute_value as attributeValue,
            type
        FROM clinical_data_derived
        WHERE attribute_name = '{attribute_name}'
            AND type = '{data_type}'
            {study_filter}
        ORDER BY cancer_study_identifier, uniqueId
        """
        
        return query.strip()
    
    @staticmethod
    def get_clinical_attributes_for_studies(
        study_ids: Optional[List[str]] = None
    ) -> str:
        """
        Get available clinical attributes for studies.
        
        Custom function to discover available clinical attributes.
        """
        
        study_filter = ""
        if study_ids:
            study_str = "', '".join(study_ids)
            study_filter = f"WHERE cancer_study_identifier IN ('{study_str}')"
        
        query = f"""
        SELECT
            cancer_study_identifier as studyId,
            attribute_name as attributeName,
            type,
            count(*) as valueCount,
            count(DISTINCT attribute_value) as distinctValueCount
        FROM clinical_data_derived
        {study_filter}
        GROUP BY cancer_study_identifier, attribute_name, type
        ORDER BY cancer_study_identifier, type, attribute_name
        """
        
        return query.strip()


async def execute_clinical_data_query(
    clickhouse_client,
    query_type: str,
    **kwargs
) -> Dict[str, Any]:
    """
    Execute clinical data queries with error handling.
    
    Args:
        clickhouse_client: ClickHouse client instance
        query_type: Type of query to execute
        **kwargs: Query parameters
    
    Returns:
        Query results as dictionary
    """
    
    try:
        queries = ClinicalDataQueries()
        
        if query_type == "sample_clinical_data":
            sql = queries.get_sample_clinical_data_from_study_view_filter(
                attribute_ids=kwargs.get("attribute_ids")
            )
        elif query_type == "patient_clinical_data":
            sql = queries.get_patient_clinical_data_from_study_view_filter(
                attribute_ids=kwargs.get("attribute_ids")
            )
        elif query_type == "clinical_data_counts":
            sql = queries.get_clinical_data_counts(
                sample_attribute_ids=kwargs.get("sample_attribute_ids"),
                patient_attribute_ids=kwargs.get("patient_attribute_ids"),
                study_ids=kwargs.get("study_ids")
            )
        elif query_type == "by_attribute":
            attribute_name = kwargs.get("attribute_name")
            if not attribute_name:
                raise ValueError("attribute_name is required")
            sql = queries.get_clinical_data_by_attribute(
                attribute_name=attribute_name,
                study_ids=kwargs.get("study_ids"),
                data_type=kwargs.get("data_type", "sample")
            )
        elif query_type == "attributes_for_studies":
            sql = queries.get_clinical_attributes_for_studies(
                study_ids=kwargs.get("study_ids")
            )
        else:
            raise ValueError(f"Unknown query type: {query_type}")
        
        logger.info(f"Executing clinical data query: {query_type}")
        logger.debug(f"SQL: {sql}")
        
        result = await clickhouse_client.execute_query(sql)
        
        return {
            "success": True,
            "query_type": query_type,
            "data": result,
            "row_count": len(result) if isinstance(result, list) else 1
        }
        
    except Exception as e:
        logger.error(f"Error executing clinical data query {query_type}: {e}")
        return {
            "success": False,
            "query_type": query_type,
            "error": str(e),
            "data": None
        }