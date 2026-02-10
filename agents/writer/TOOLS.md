# TOOLS.md - Writer

## Available Tools

You have access to:
- **read** — Read existing docs and project files for context
- **write** — Create or update content files
- **web_fetch** — Research if needed for accuracy

## Writing Patterns

### Understanding a Codebase (for docs)
```bash
# Check existing docs
ls docs/
cat README.md

# Understand structure
tree -L 2 src/

# Check for style guide
cat CONTRIBUTING.md
cat .github/PULL_REQUEST_TEMPLATE.md
```

### Consistent Formatting

**Markdown conventions:**
- `#` for title, `##` for sections, `###` for subsections
- Blank line before and after code blocks
- Use `code` for inline code, filenames, commands
- Use **bold** for emphasis, not ALL CAPS
- Numbered lists for sequences, bullets for sets

**Code blocks:**
```markdown
\`\`\`python
# Specify language for syntax highlighting
def example():
    pass
\`\`\`
```

### Research for Accuracy
If you need to verify technical details:
```
1. Check project source code
2. Check official documentation (use web_fetch)
3. If uncertain, note it: "TODO: verify X"
```

## File Locations

Common locations for different content types:
- `README.md` — project root
- `docs/` — detailed documentation
- `CHANGELOG.md` — version history
- `CONTRIBUTING.md` — contributor guide
- `.github/` — PR templates, issue templates
