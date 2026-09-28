# claude-code-subagent-cache

A small script to measure how much of your Claude Code token spend goes to subagents rereading their context after the prompt cache expires, plus the settings I use to cut it.

## The problem

On a Claude subscription, within the plan's included usage, Claude Code gives the main conversation a 1-hour prompt cache. Once you draw on usage credits, or with an API key, the main conversation gets 5 minutes too. Subagents, workflows and teammates get a 5-minute cache by default ([prompt caching docs](https://code.claude.com/docs/en/prompt-caching)). A subagent that waits more than 5 minutes on a test run, a build or another agent loses its cache, and its next call writes its whole context to the cache again.

On my machine, subagent calls that match the idle-reread rule below came to 28% of my token spend over the 4 days before I changed the settings, priced at API list rates. In the first 11 hours or so after the change, idle rereads fell to 1.3% and every subagent cache write used the 1-hour TTL, though that's a short window. That's one person's measurement with a heavy subagent workload, so run the script to get your own number.

## Measure

```
python3 measure.py --days 7
```

It needs Python 3 and nothing else. It reads the transcripts Claude Code keeps under `~/.claude/projects` (use `--projects-dir` for another location) and prints totals only. No transcript content is printed and nothing leaves your machine.

For the main conversation and for subagents it reports cache reads, 5-minute cache writes, 1-hour cache writes, uncached input and output, as tokens and weighted at API list prices. It also counts idle rereads: a subagent call after its first whose cache write is above 20,000 tokens, larger than its cache read, and more than 300 seconds after that subagent's previous call. Models missing from the price table are counted in tokens and listed as "not priced".

## The fix

Merge both variables into the `env` block of `~/.claude/settings.json`, keeping whatever is already there, then restart Claude Code. `settings.example.json` has the same block:

```json
{
  "env": {
    "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL": "1h",
    "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"
  }
}
```

The two settings do different jobs. `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL=1h` is the cache fix: it gives subagents the 1-hour cache and needs Claude Code v2.1.242 or later. The top-level setting `"subagentPromptCacheTtl": "1h"` does the same, and the environment variable wins if both are set. `FORCE_PROMPT_CACHING_5M=1` overrides both, so leave that variable unset. In my testing the value in `settings.json` won over a shell export of the same variable.

`CLAUDE_CODE_SUBAGENT_MODEL=sonnet` is a cost choice, not a cache fix. It makes Sonnet the default model for subagents that don't name one. From Claude Code v2.1.251, it picks a subagent's model in this order: the model passed on the call, the agent's `model` frontmatter (where `inherit` means the session model), this variable, then the session model ([subagent docs](https://code.claude.com/docs/en/sub-agents)). Before v2.1.251 this variable came first and overrode the other two, so it would also move the Haiku `Explore` below and any agent pinned to Opus onto Sonnet. Use v2.1.251 or later for this setup.

Copy `agents/Explore.md` to `~/.claude/agents/`, unless you already have an `Explore.md` there. A user agent named `Explore` replaces the built-in one. This copy sets `model: haiku` in its frontmatter, so its read-only searches run on Haiku whatever the default is.

One caution for anyone whose session runs on Opus. Once the default is Sonnet, an agent with no `model` in its frontmatter and none passed on the call runs on Sonnet. Any agent that writes or reviews code should set `model: opus` (or `model: inherit`) so it stays on Opus. Check your own agents before you turn this on.

## Check that it worked

After the restart, work as usual, then run `measure.py` with a `--days` value that covers only sessions since the change. The "Subagent cache writes by TTL" section shows the 5m and 1h split. With the default, subagent writes are almost all 5m. With the fix in place they should be almost all 1h. If they stay 5m, check your Claude Code version and look for `FORCE_PROMPT_CACHING_5M`. A window that reaches back before the change will still show the old 5m writes.

## Limits

- This is for Claude Code only. Cowork and claude.ai expose no such setting, and Anthropic doesn't document how their subagents cache.
- A 1-hour cache write costs 2x the input price, against 1.25x for a 5-minute write. It only pays off when subagents sit idle past 5 minutes and then carry on. If yours finish quickly, the fix can cost more.
- Dollar figures use API list prices as a proxy. Anthropic doesn't publish how subscription plan limits weight tokens, so your share of plan usage may differ. Prices are in a table at the top of `measure.py`, taken from the [pricing page](https://platform.claude.com/docs/en/about-claude/pricing) on 2026-09-28.
- The idle-reread rule is a heuristic. A large cache write can also come from a model switch, a changed prompt or a large new tool result. The thresholds are constants at the top of `measure.py`.

## Tests

```
python3 -m unittest -v
```

The tests use small synthetic transcripts, never real ones.

## License

MIT. Copyright (c) 2026 Adam Walker.
