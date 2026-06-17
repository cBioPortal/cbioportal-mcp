# cBioPortal FAQ Guide

Curated answers to frequently asked general questions about cBioPortal. Source: [cBioPortal FAQ](https://docs.cbioportal.org/user-guide/faq/).

## What is cBioPortal?

cBioPortal for Cancer Genomics is an open-access, open-source resource for interactive exploration of multidimensional cancer genomics data sets. It was originally developed at Memorial Sloan Kettering Cancer Center (MSK) and is now maintained by a multi-institutional team.

## History

- **2008**: cBioPortal first became available online.
- **2012**: First major publication — Cerami et al., *Cancer Discovery*.
- **2013**: Second major publication — Gao et al., *Science Signaling*.
- **2023**: Third major publication — de Bruijn et al., *Cancer Research*.

## How to Cite cBioPortal

When using cBioPortal in publications, cite these three papers:

1. Cerami et al. "The cBio Cancer Genomics Portal: An Open Platform for Exploring Multidimensional Cancer Genomics Data." *Cancer Discovery* 2, 401–404 (2012). doi:10.1158/2159-8290.CD-12-0095
2. Gao et al. "Integrative Analysis of Complex Cancer Genomics and Clinical Profiles Using the cBioPortal." *Science Signaling* 6, pl1 (2013). doi:10.1126/scisignal.2004088
3. de Bruijn et al. "Analysis and Visualization of Longitudinal Genomic and Clinical Data from the AACR Project GENIE Biopharma Collaborative in cBioPortal." *Cancer Research* 83, 3861–3867 (2023). doi:10.1158/0008-5472.CAN-23-0816

Also cite the specific study publication(s) whose data you used.

## What Data Types Does cBioPortal Contain?

- **Mutations** (non-synonymous somatic mutations)
- **DNA copy-number alterations** (CNA) from GISTIC or other methods
- **mRNA expression** data (RNA-seq, microarray)
- **Protein/phosphoprotein** levels (RPPA, mass spectrometry)
- **DNA methylation** data
- **Structural variants** (gene fusions, rearrangements)
- **Clinical data** (demographics, diagnosis, treatment, outcomes)

Note: Synonymous mutations are not included in cBioPortal.

## Data Types Usually Not Stored Directly

cBioPortal generally does not store:

- raw CT, MRI, pathology-slide, or histology image files
- raw sequencing files such as BAM, CRAM, or FASTQ
- polygenic risk scores as a standard data type
- full external clinical-trial databases

However, this deployment may contain links to external viewers or portals through `resource_*` tables. For imaging, pathology, Minerva, HTAN, or viewer questions, read `cbioportal://external-resources-guide` and check those tables before saying the data is absent.

## What the MCP Agent Can and Cannot Produce

The MCP agent can:

- query cBioPortal data and return tables in text/JSON/Markdown
- summarize counts, frequencies, and available attributes
- provide cBioPortal study links and DataHub download links when applicable
- provide SQL snippets or handoff instructions for R/Python/cBioPortal tools

The MCP agent should not promise to:

- render Kaplan-Meier plots or other visual figures directly
- export large CSV files or create downloadable files
- run external notebooks, Databricks jobs, PySpark pipelines, or web apps

If the user asks for plots or downloads, provide the underlying data in a compact table when feasible and redirect them to cBioPortal's built-in visualization/download features or an appropriate external workflow.

## Clinical Actionability and OncoKB

cBioPortal data alone is not enough to answer clinical actionability questions such as "Are TP53 mutations clinically actionable?"

Answer actionability questions only if actionability/driver annotation data is available and has been queried. Otherwise:

- state that cBioPortal alone does not establish clinical actionability
- suggest checking OncoKB or the cBioPortal web interface for curated annotations
- do not provide therapeutic recommendations from general model knowledge

## Germline Variant Studies

cBioPortal can represent germline variant studies when germline variants and associated clinical data are loaded as a study. Many visualization and correlation features used for somatic variants can also be useful for germline studies, provided the data is modeled correctly.

For germline-study setup questions, explain:

- a study can be built around germline variants plus clinical attributes
- clinical data can be used for stratification and plots just as in other studies
- mutation status values may vary by study and should be matched case-insensitively in queries
- the user should follow cBioPortal datahub/import formats for study submission

## Adding Data to cBioPortal

When asked how to add clinical or genomic data to cBioPortal, first ask whether the user wants to:

- submit data to the public cBioPortal
- run or populate a private cBioPortal instance
- visualize their own data without full submission

For public cBioPortal, point users to the cBioPortal data curation process and datahub formats. For private instances, explain that they need a valid study package with clinical, sample, molecular, and metadata files. For quick visualization without submission, mention cBioPortal standalone tools such as Mutation Mapper and OncoPrint when applicable.

## Reference Genome

The public cBioPortal largely uses **hg19/GRCh37**. However, some studies use **hg38/GRCh38**, including datasets sourced from the GDC (Genomic Data Commons).

## Common Abbreviations

| Abbreviation | Meaning |
|---|---|
| VUS | Variant of Unknown Significance |
| CNA | Copy Number Alteration |
| AMP | Amplification (GISTIC value +2) |
| HOMDEL | Deep/Homozygous Deletion (GISTIC value -2) |
| GAIN | Low-level gain (GISTIC value +1) |
| HETLOSS | Shallow/Heterozygous deletion (GISTIC value -1) |
| TMB | Tumor Mutational Burden (mutations per megabase) |
| MSI | Microsatellite Instability |
| OQL | Onco Query Language |
| SV | Structural Variant |
| RPPA | Reverse Phase Protein Array |
| CH | Clonal Hematopoiesis |

## Study Naming Conventions

Study identifiers typically combine disease, institution, and year, but the order varies. Common patterns include `{disease}_{institution}_{year}` (e.g., `brca_tcga_pan_can_atlas_2018`) and `{institution}_{disease}_{year}` (e.g., `msk_ch_2020`). Common abbreviations in study identifiers:

| Pattern | Meaning | Example studies |
|---|---|---|
| `_tcga_` | The Cancer Genome Atlas | `luad_tcga_pan_can_atlas_2018` |
| `_msk_` or `msk_` | Memorial Sloan Kettering | `msk_chord_2024` |
| `_ch_` | Clonal Hematopoiesis | `msk_ch_2020`, `msk_ch_2023`, `msk_ch_ped_2021` |
| `_target_` | Therapeutically Applicable Research to Generate Effective Treatments | `nbl_target_2018_pub` |
| `_genie_` | AACR Project GENIE | `genie_public` |
| `_pan_can_` | Pan-Cancer Atlas | `brca_tcga_pan_can_atlas_2018` |

When searching for studies on a specific topic, use `list_studies(search=...)` with the disease name or abbreviation. For example, `list_studies(search="clonal hematopoiesis")` finds CH studies.

### Study Links

- **View a study:** `https://www.cbioportal.org/study?id={study_id}` (e.g., `https://www.cbioportal.org/study?id=msk_ch_2020`)
- **Download study data:** `https://datahub.assets.cbioportal.org/{study_id}.tar.gz`

## Copy Number (GISTIC) Thresholds

| Value | Label | Meaning |
|---|---|---|
| -2 | Deep Deletion (HOMDEL) | Deep loss, possibly homozygous deletion |
| -1 | Shallow Deletion (HETLOSS) | Shallow loss, possibly heterozygous deletion |
| 0 | Diploid | Normal copy number |
| +1 | Gain | Low-level gain (few extra copies) |
| +2 | Amplification (AMP) | High-level amplification |

## cBioPortal vs. GDC (Genomic Data Commons)

- **cBioPortal** is an exploratory analysis tool for interactive visualization and querying of processed cancer genomics data.
- **GDC** is a data repository for full download and access to all data, including raw files (BAM, FASTQ).

cBioPortal imports some GDC data and presents it in a user-friendly interface for exploration.

## API Access

cBioPortal provides a REST API (Swagger-documented), as well as R and MATLAB interfaces for programmatic access. The public API is available at https://www.cbioportal.org/api.

## Combined and Virtual Studies

- **Virtual Study**: A custom study comprised of samples from one or more existing studies, with permanent shareable links.
- **Combined Study**: Also known as a combined cohort — a custom study comprised of samples from multiple studies for cross-study analysis.

## Mutation Annotation

Mutations are annotated using **Genome Nexus**, which utilizes **VEP** (Variant Effect Predictor) with the canonical MSKCC transcript. This provides standardized variant classification and functional impact predictions (e.g., OncoKB, SIFT, PolyPhen-2).
