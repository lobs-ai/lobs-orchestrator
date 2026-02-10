# TOOLS.md - Reviewer

## Available Tools

You have access to:
- **read** — Read files and diffs
- **exec** — Run commands (tests, linters, type checkers)

Note: You can read and verify, but you don't write code. That's Programmer's job.

## Review Workflow

### 1. Get the Diff
```bash
# If reviewing uncommitted changes
git diff

# If reviewing specific commit
git show <commit>

# If reviewing branch
git diff main..branch
```

### 2. Run Automated Checks
```bash
# Run tests
python -m pytest -v

# Run linter
python -m ruff check .

# Run type checker
python -m mypy .
```

### 3. Read Changed Files
Use `read` to examine files in detail.

### 4. Check Test Coverage
```bash
# See what's tested
python -m pytest --cov=path --cov-report=term-missing
```

## Common Review Checks

### Logic Errors
- Off-by-one errors
- Null/undefined access
- Wrong comparison operators
- Missing error handling

### Security
- SQL injection (string concatenation in queries)
- XSS (unescaped user input in HTML)
- Auth/authz bypass
- Secrets in code

### Performance
- N+1 queries (loop with DB call inside)
- Unbounded operations (no limits/pagination)
- Missing indexes
- Blocking operations

### Maintainability
- Unclear naming
- Missing comments on complex logic
- Copy-pasted code
- Magic numbers/strings
