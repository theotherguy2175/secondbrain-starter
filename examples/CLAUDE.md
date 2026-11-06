# Second brain protocol (paste into your global CLAUDE.md / agent instructions)

You have a `brain` MCP server: the user's personal knowledge base (notes,
decisions, home lab and side-project history, past Claude sessions).

- **Before** answering questions about the user's home lab, projects, past
  decisions or preferences, call `brain_search` with specific keywords (hostnames,
  error text, tool names). Try 2-3 phrasings. Prefer newer results when they
  conflict (check `age_days`); `brain_read` the promising ones.
- **After** you learn something durable, call `brain_write`:
  a clear title, the exact error/command/value in the body, and *why* (the
  decision and its reason). Use `kind`: decision, fix, howto, reference,
  preference, project. Use `brain_append` to extend an existing note instead of
  creating duplicates.
- Pin (`pinned: true`) only evergreen facts (IP plan, naming conventions).
- **Never** store secrets (passwords, tokens, private keys, recovery codes).
- Keep `scope` accurate (`personal`, `homelab`, a project name). Do not copy
  anything from work systems into this brain.
- If results look stale or empty unexpectedly, call `brain_status` and tell the
  user which connector is failing.
