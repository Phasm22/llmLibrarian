# llmli CLI for agents

Short recipes for shell/automation clients. Prefer this over scraping human tables.

Human text output remains the default for legacy commands. Agent-oriented commands (`query`, `multi-query`, `read`, `schema`) emit JSON by default.

## Always start here

```bash
llmli --version          # package + git commit + cli.py path (detect stale installs)
llmli schema             # self-describing commands/args/outputs (schema_version)
llmli --json ls          # roster with private + host
llmli status --json      # write-in-progress + Chroma lock holders
```

If `--version` shows an unexpected `cli_path` or old commit, you are not running this checkout.

## Common recipes

### List silos

```bash
llmli --json ls
```

Private silos have `"private": true`. Unscoped retrieval skips them; pass the **exact slug** via `--in`.

### Query with frontmatter filters (MCP `query_personal_knowledge`)

```bash
llmli query --in recipes-xxxx --main-protein chicken --max-total-time 45 "weeknight dinner"
llmli query --in recipes-xxxx --tags vegetarian,quick --category sides "what goes with salmon"
```

Filters: `--category`, `--main-protein`, `--course`, `--era`, `--tags`, `--min-servings`, `--max-servings`, `--max-total-time`, `--section`, `--doc-type`, `--source`.

### Multi-angle retrieve (MCP `multi_query_knowledge`)

```bash
llmli multi-query --in recipes-xxxx --q "sourdough" --q "starter feeding schedule"
```

### Find files by name/date (MCP `find_files`)

```bash
llmli find --name "*.pdf" --date 2024-01-01:2024-12-31 --json
llmli find --in my-silo --name "README*" --json
```

### Read an indexed file (MCP `read_document`)

```bash
llmli read README.md --in my-silo
# page with --start-chunk from next_start_chunk
```

### Verify exclude violations

```bash
llmli --json verify my-silo
llmli verify my-silo --purge --dry-run
llmli verify my-silo --purge --yes
```

### Add / reindex safely

```bash
llmli add /path/to/folder --dry-run
llmli add /path/to/folder --exclude "*.tmp" --exclude "node_modules/"
llmli add /path/to/folder --full --dry-run
llmli add /path/to/folder --full --yes
```

### Destructive ops (always `--yes` or `--dry-run`)

```bash
llmli rm my-silo --dry-run
llmli rm my-silo --yes
llmli repair my-silo --dry-run
llmli repair my-silo --yes
llmli private my-silo --yes
llmli private my-silo --off --yes   # widens unscoped exposure — confirm with the user
```

## Conventions

| Topic | Rule |
|-------|------|
| JSON | Global `--json` or per-command. Errors: `{ok:false, error:{code,message,retryable,busy}, exit_code}`. |
| Exit codes | `0` ok · `1` error · `2` usage · `3` busy/retryable · `4` confirmation required |
| Busy | Lock timeout → exit `3`, `error.code=busy`, `retryable=true`. Check `llmli status --json`. |
| Private silos | Same as MCP: unscoped skips them; open only with exact `--in <slug>`. |
| Silo addressing | Slug, display name, or prefix — except private reads need the exact slug. |
| Discoverability | Unknown subcommand prints `Did you mean: …`. Prefer `llmli schema` over guessing. |
| Watchers | `pal pull … --watch` is unchanged; do not add `--yes` there. |

## MCP parity map

| MCP tool | CLI |
|----------|-----|
| `list_silos` | `llmli --json ls` |
| `query_personal_knowledge` | `llmli query …` |
| `multi_query_knowledge` | `llmli multi-query …` |
| `find_files` | `llmli find … --json` |
| `read_document` | `llmli read …` |
| `inspect_silo` | `llmli inspect … --json` |
| `set_silo_privacy` | `llmli private … --yes` |
| `repair_silo` | `llmli repair … --yes` |
| `add_silo` | `llmli add …` |
| Ollama answer | `llmli ask` / `pal ask` (not MCP) |

`pal query`, `pal status`, `pal find`, and `pal private` delegate to the same llmli surface.
