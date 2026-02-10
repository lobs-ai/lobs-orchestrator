# TOOLS.md - Programmer

## Available Tools

You have access to:
- **read/write/edit** — File operations
- **exec** — Run shell commands (build, test, lint, etc.)
- **browser** — For checking documentation or APIs if needed

## Common Patterns

### Running Tests
```bash
# Python
python -m pytest path/to/tests -v

# Node
npm test

# Swift
swift test
```

### Checking Build
```bash
# Python (type check)
python -m mypy path/

# Node
npm run build

# Swift
swift build
```

### Linting
```bash
# Python
python -m ruff check path/

# Node
npm run lint
```

## Notes

- Prefer project-specific test/build commands (check package.json, Makefile, etc.)
- Run tests before finishing to verify your changes work
- If tests fail, fix them (unless the task explicitly says to skip tests)
