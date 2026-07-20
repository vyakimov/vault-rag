# MCP server & evaluation — reference

## MCP server (tool-calling agents)

If your harness speaks MCP, prefer the server over shelling out — same contracts, same envelopes:

```
<repo>/bin/vault-spider-mcp                                  # stdio (Claude Desktop etc.)
<repo>/bin/vault-spider-mcp --transport streamable-http      # serves /mcp on 127.0.0.1:8000
```

Tools mirror the CLI: `vault_stats`, `sync_index` (defaults to dry-run), `search_vault`,
`answer_from_vault` (both accept the same filters, including `provenance`), `lint_vault`,
`plan_enrichment`, note reads, and the safe mutations (mutating tools default `dry_run: true`).
Every tool returns the CLI's JSON envelope — the same `"ok"` / `error.type` rules apply. The
HTTP transport has **no built-in auth**; keep it on localhost or behind your own proxy.

## Evaluation (development, not vault workflow)

Golden-dataset benchmark for retrieval/synthesis quality. Two datasets ship in the repo:
`eval/` (clean synthetic) and `eval-realistic/` (messy synthetic, styled like a real vault).

```bash
# labels still valid? (run after any corpus/query change; drift fails contract_violation)
VAULT_SPIDER_CONFIG=eval/eval-config.yaml ./bin/vault-spider eval validate --dataset eval

# index the corpus into a DEDICATED chroma dir (never the live-vault index), then score
VAULT_SPIDER_CONFIG=eval/eval-config.yaml ./bin/vault-spider sync \
    --root eval/public_vault --reset --chroma-path /tmp/vs-eval
VAULT_SPIDER_CONFIG=eval/eval-config.yaml ./bin/vault-spider eval run \
    --dataset eval --chroma-path /tmp/vs-eval --out results.json
```

- Default `run` scores retrieval only (deterministic): nDCG@k, per-group evidence recall@k,
  complete@k, MRR — overall and per category/slice. Unanswerable queries are skipped.
- `--stage synthesis` adds abstention scoring and gold/forbidden-fact checks via an LLM judge
  (costs chat calls; inherits judge variance).
- Useful knobs: `--mode`, `--granularity`, `-n`, `--k`, `--only <query-id>` (debug one query),
  `--out <file>`.
- `run` refuses (`config_mismatch`) an index that doesn't exactly match the corpus — rebuild
  with `sync --reset` against the dedicated chroma path.
- Set `VAULT_SPIDER_CONFIG` to the dataset's own config so skip/ignore rules match what was
  indexed.
