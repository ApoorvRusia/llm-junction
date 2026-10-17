# LLM Junction

One command to talk to other LLMs through a single [OpenRouter](https://openrouter.ai)
API key. Ask a question, keep a multi-turn thread going, or run the same prompt
past several models and compare answers — from your terminal.

Free-tier-first: it defaults to free models and refuses paid ones unless you
explicitly allow them.

## Setup

No dependencies — Python 3.10+ and the standard library only.

```bash
export OPENROUTER_API_KEY="sk-or-v1-..."   # get one at openrouter.ai/keys
chmod +x junction.py
./junction.py models --free -q gemma
```

## Usage

```bash
# list models (live from OpenRouter)
junction models --free -q gemma

# ask one model
junction ask google/gemma-3-27b-it:free "Explain covered calls like I'm five"

# let OpenRouter pick an available free model
junction ask openrouter/free "Give me a second opinion on this idea"

# keep a thread going
junction ask openrouter/free "Second opinion please: ..." --thread research
junction thread research "What about the downside risk?"

# ask several models the same thing, compare answers
junction roundtable "Is this a good deal?" \
  --models google/gemma-3-27b-it:free,meta-llama/llama-3.3-70b-instruct:free

# threads and usage
junction threads
junction show research
junction usage
```

## Free tier

OpenRouter's free tier needs no credit card. Models whose IDs end in `:free`
cost nothing: **20 requests/min, 50/day** (rises to 1,000/day after a one-time
$10 credit purchase — you never have to spend it). `openrouter/free` routes to
whichever free model is currently available.

The free bench is open-weight models (Gemma, Llama, Qwen, Mistral, …) and the
lineup churns — list live with `junction models --free` rather than pinning
one forever. Flagship models (Grok, Gemini Pro, Claude, GPT) are paid-only on
OpenRouter; pass `--allow-paid` to call them (bills per token to your balance).

## Guardrails

- Paid models are refused by default. `--allow-paid` opts in per call.
- `usage` shows today's request count against the daily cap (default 50,
  configurable in `~/.config/llm-junction/config.json`).
- Threads persist under `~/.config/llm-junction/threads/`.
- The key is read from `OPENROUTER_API_KEY` at runtime and never written to
  disk, logs, or chat.

## Why it exists

Built to give an AI assistant a "phone a friend" line to other models —
second opinions in chat, multi-model eval harnesses, and cost-vs-quality
comparisons across the free bench before spending on flagships.
