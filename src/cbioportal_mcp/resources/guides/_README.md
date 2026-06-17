# Deployment-specific general guides

Drop `.md` files here (or overlay via a ConfigMap mount) to make them
discoverable by the MCP agent as deployment-specific guides.

Each `<name>.md` becomes accessible via:

- `get_general_guide("<name>")` — returns the file contents
- `list_guides()` — lists every name found here under
  `cbioportal://general-guide/<name>` URIs

Files starting with `_` are skipped (template / private). `README.md` is
not loaded as a guide because the dispatch is by stem-name, not glob.
