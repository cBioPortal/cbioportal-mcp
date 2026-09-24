# Deployment-specific general guides

Drop `.md` files in this directory to make them discoverable by the MCP
agent as deployment-specific guides. Each `<name>.md` becomes
accessible via:

- `get_general_guide("<name>")` — returns the file contents.
- `list_guides()` — lists every name found here under
  `cbioportal://general-guide/<name>` URIs.

Files starting with `_` (like this README) are skipped by discovery.
