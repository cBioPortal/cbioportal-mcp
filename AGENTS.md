# AGENTS.md - cBioPortal MCP Development Guide

## Context Efficiency

MCP interactions should minimize token usage. Every tool call and resource read consumes context.

### Design Principles

1. **Tools should return minimal, structured data**
   - Return only what's needed to answer the question
   - Use LIMIT in queries
   - Avoid returning large text blobs

2. **Resources are for reference, not bulk data**
   - Study guides explain *what data means*, not *what the data is*
   - General guides explain *how to query*, not query results
   - Never put dynamic data (counts, frequencies) in static resources

3. **Let the agent query, don't prefetch**
   - Don't include "top 10 genes" in study guides — agent can query
   - Don't include sample counts — agent can query
   - Include *how to interpret* results, not the results themselves

### Tool Design

**Good tool:**
```python
def get_study_info(study_id: str) -> dict:
    """Returns basic study metadata (name, cancer type, description)."""
    # Small, focused response
```

**Bad tool:**
```python
def get_study_everything(study_id: str) -> dict:
    """Returns all study data including samples, mutations, clinical data..."""
    # Too much data, wastes context
```

### Resource Design

**Good resource content:**
```markdown
## MSI_STATUS Attribute
- **MSI-H**: Microsatellite instability high (>30% unstable loci)
- **MSI-L**: Low instability
- **MSS**: Microsatellite stable

Use MSI status to predict immunotherapy response.
```

**Bad resource content:**
```markdown
## MSI Status Distribution
- MSI-H: 1,234 samples (15%)
- MSI-L: 456 samples (6%)
- MSS: 6,789 samples (79%)
```
(Numbers change; agent should query for current counts)

### When to Use What

| Need | Use |
|------|-----|
| How to calculate mutation frequency | General guide (mutation-frequency-guide.md) |
| What clinical attributes mean in a study | Study guide (study-guides/xxx.md) |
| Actual mutation counts | Tool: clickhouse_run_select_query |
| List of studies | Tool: list_studies |
| Study-specific attribute semantics | Tool: get_study_guide → loads resource |

### Caching Strategy

- Study guides are static files → can be cached
- Query results are dynamic → don't cache in resources
- General guides rarely change → good to preload in agent instructions

## File Structure

```
resources/
├── mutation-frequency-guide.md    # How to calculate frequencies
├── clinical-data-guide.md         # How to query clinical data
├── sample-filtering-guide.md      # How to filter samples
├── common-pitfalls.md             # Common mistakes
└── study-guides/
    ├── _tcga_pancan_template.md   # Shared TCGA semantics
    ├── msk_chord_2024.md          # MSK-CHORD specifics
    └── ...                        # Per-study semantics
```

## Adding New Study Guides

1. Create `resources/study-guides/{study_id}.md`
2. Focus on:
   - What non-harmonized attributes mean
   - Molecular subtypes and their clinical relevance
   - Study-specific caveats
3. Do NOT include:
   - Sample/patient counts
   - Gene frequencies
   - Any data that should be queried

## Testing

Use benchmark questions from cbioportal-mcp-qa to validate:
- Guides help agent construct correct queries
- Agent doesn't hallucinate unavailable attributes
- Context usage is reasonable (not loading entire guides unnecessarily)
