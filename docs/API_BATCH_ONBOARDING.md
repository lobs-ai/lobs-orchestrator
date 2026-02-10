# Batch Project Onboarding API

## Overview

The batch onboarding endpoint allows you to set up multiple GitHub repositories sequentially during initial setup, providing better UX and easier debugging.

## Endpoint

**POST** `/projects/create-from-github-batch`

## Request Format

```json
{
  "repos": [
    {
      "url": "https://github.com/owner/repo1",
      "clonePath": "/path/to/clone/repo1",
      "syncIssues": false
    },
    {
      "url": "https://github.com/owner/repo2",
      "clonePath": "/path/to/clone/repo2",
      "syncIssues": true
    }
  ]
}
```

### Fields

- `repos` (required): Array of repository configurations
  - `url` (required): GitHub repository URL or `owner/repo` format
  - `clonePath` (optional): Custom clone location (defaults to `~/repo-name`)
  - `syncIssues` (optional): Enable GitHub issue tracking (default: `false`)

## Response Format

```json
{
  "ok": true,
  "results": [
    {
      "ok": true,
      "project": {
        "id": "repo1",
        "name": "repo1",
        "repoPath": "/path/to/clone/repo1",
        "source": "github",
        "githubRepo": "owner/repo1"
      }
    },
    {
      "ok": false,
      "error": "Clone failed: permission denied",
      "url": "https://github.com/owner/repo2"
    }
  ],
  "summary": {
    "total": 2,
    "succeeded": 1,
    "failed": 1
  }
}
```

## Behavior

- **Sequential Processing**: Repositories are cloned and set up one at a time
- **Fault Tolerance**: If one repository fails, processing continues with the next
- **Progress Tracking**: Each result indicates success or failure with details
- **Detailed Errors**: Failed repositories include error messages for debugging

## Example Usage

```bash
curl -X POST http://localhost:7171/projects/create-from-github-batch \
  -H "Content-Type: application/json" \
  -d '{
    "repos": [
      {"url": "https://github.com/myorg/backend"},
      {"url": "https://github.com/myorg/frontend"},
      {"url": "https://github.com/myorg/docs"}
    ]
  }'
```

## Comparison with Single-Repo Endpoint

| Feature | `/projects/create-from-github` | `/projects/create-from-github-batch` |
|---------|-------------------------------|-------------------------------------|
| Repos per call | 1 | Multiple |
| Processing | Immediate | Sequential |
| Fault handling | Single failure stops | Continues after failures |
| Progress tracking | N/A | Per-repo results + summary |
| Use case | Ad-hoc repo addition | Initial onboarding |

## Notes

- The batch endpoint is designed for onboarding workflows where multiple repos need to be set up
- For adding a single repository, the original `/projects/create-from-github` endpoint is simpler
- Processing is strictly sequential to avoid resource contention and improve debuggability
