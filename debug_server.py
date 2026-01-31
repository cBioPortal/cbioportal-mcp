
import sys
import traceback

with open("full_error.txt", "w") as f:
    try:
        from cbioportal_mcp.server import main
        main()
    except Exception:
        traceback.print_exc(file=f)
