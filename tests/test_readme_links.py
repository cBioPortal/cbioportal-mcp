from pathlib import Path


def test_readme_links_current_system_prompt_file():
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "src/cbioportal_mcp/resources/system-prompt.md" in readme
    assert "src/cbioportal_mcp/prompts/cbioportal_prompt.py" not in readme
