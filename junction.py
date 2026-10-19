#!/usr/bin/env python3
"""junction -- a switchboard for talking to other LLMs.

The idea: one agent or system that has no direct access to other LLMs plugs
into the junction, and the junction connects it to whichever models are
plugged in on the other side. Providers are pluggable -- OpenRouter ships as
the first one (300+ models through a single key); any OpenAI-compatible API
(xAI, OpenAI, Gemini's OpenAI endpoint, ...) can be added in providers.json
and is then addressable as provider::model.

Setup:
  export OPENROUTER_API_KEY="sk-or-v1-..."   # from openrouter.ai/keys

Each provider's key is read from its configured environment variable at
runtime. Keys are NEVER in this file.

Commands:
  providers                            list connected LLM providers
  ask MODEL "prompt" [options]         ask one model; --thread NAME keeps history
  thread NAME "follow-up"              continue a saved thread with its model
  threads                             list saved threads
  show THREAD                         print a thread's history
  rm-thread THREAD                    delete a thread
  roundtable "prompt" [--models a,b,c]  ask several models, compare answers
  usage                               today's request count vs the daily cap

Addressing: 'provider::model' (e.g. xai::grok-4) or a bare model id, which
uses the default provider (openrouter).

Free-tier guardrails (default ON):
  - model ids must end in :free (or be openrouter/free); pass --allow-paid
    to call a paid model (bills per token to the OpenRouter balance).
  - every request is logged; `usage` shows today's count vs daily cap.

Examples:
  junction models --free -q gemma
  junction ask google/gemma-3-27b-it:free "Explain covered calls like I'm five"
  junction ask openrouter/free "Second opinion please: ..." --thread research
  junction thread research "What about the risk into earnings?"
  junction roundtable "Is this a good deal?" --models google/gemma-3-27b-it:free,meta-llama/llama-3.3-70b-instruct:free
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://openrouter.ai/api/v1"
ALLOWED_HOSTS = ["openrouter.ai"]

# Provider name -> Secure Vault custom connection for the surrogate fallback.
# Keys live in the vault, never in files; the junction only sees a surrogate.
VAULT_CONNECTIONS = {
    "openrouter": "custom.openrouter",
    "google-aistudio": "custom.google-aistudio",
}

HOME = os.path.expanduser("~/.config/llm-junction")
THREADS_DIR = os.path.join(HOME, "threads")
CONFIG_PATH = os.path.join(HOME, "config.json")
USAGE_PATH = os.path.join(HOME, "usage.jsonl")

FREE_ROUTER = "openrouter/free"

PROVIDERS_PATH = os.path.join(HOME, "providers.json")


def _default_providers() -> dict:
    return {
        "default": "openrouter",
        "providers": {
            "openrouter": {
                "base_url": BASE,
                "key_env": "OPENROUTER_API_KEY",
                "note": "300+ models; :free tier available",
            },
        },
    }


def _providers() -> dict:
    try:
        if os.path.exists(PROVIDERS_PATH):
            with open(PROVIDERS_PATH) as f:
                doc = json.load(f)
            if isinstance(doc.get("providers"), dict):
                return doc
    except (OSError, ValueError):
        pass
    return _default_providers()


def _resolve(model_arg: str) -> tuple:
    """Split 'provider::model' into (provider, config, model id).

    A bare model id uses the default provider (openrouter), so
    'google/gemma-3-27b-it:free' and 'openrouter/free' keep working.
    """
    provs = _providers()
    if "::" in model_arg:
        name, mid = model_arg.split("::", 1)
        if name not in provs["providers"]:
            known = ", ".join(sorted(provs["providers"]))
            raise RuntimeError(
                f"unknown provider '{name}'. Known providers: {known} "
                f"(see: junction providers)")
        if not mid:
            raise RuntimeError(f"missing model id after '{name}::'")
        return name, provs["providers"][name], mid
    default = provs.get("default", "openrouter")
    return default, provs["providers"][default], model_arg


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


def _ensure_allowed_url(url: str) -> None:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    allowed = set(ALLOWED_HOSTS)
    for p in _providers().get("providers", {}).values():
        h = (urllib.parse.urlparse(p.get("base_url", "")).hostname or "").lower()
        if h:
            allowed.add(h)
    if host not in allowed:
        raise RuntimeError(f"refusing request to {host}; allowed: {sorted(allowed)}")


def _read_json_response(response) -> dict:
    chunks = []
    while True:
        chunk = response.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except ValueError as exc:
        raise RuntimeError("provider response was not valid JSON") from exc


def _allowed_hosts_for(prov: dict) -> list:
    """Hosts the surrogate exchange may target: the provider's own host."""
    h = (urllib.parse.urlparse(prov.get("base_url", "")).hostname or "").lower()
    return [h] if h else list(ALLOWED_HOSTS)


def _apply_auth(request: urllib.request.Request, prov_name: str = "openrouter",
                prov: dict | None = None) -> None:
    """Attach a provider's API key: its key_env var first, surrogate fallback."""
    prov = prov or {}
    key_env = prov.get("key_env", "OPENROUTER_API_KEY")
    key = os.environ.get(key_env, "").strip()
    if key:
        request.add_header("Authorization", f"Bearer {key}")
        return
    if prov_name in VAULT_CONNECTIONS:
        # Muse runtime: the key lives in the Secure Vault; use the surrogate.
        try:
            sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
            from dynamic_credentials import add_surrogate_to_request
        except ImportError:
            pass
        else:
            add_surrogate_to_request(request, VAULT_CONNECTIONS[prov_name],
                                     allowed_hosts=_allowed_hosts_for(prov))
            return
    raise RuntimeError(
        f"No API key for provider '{prov_name}'. Set the {key_env} "
        f"environment variable and retry."
    )


def _config() -> dict:
    cfg = {"daily_cap": 50, "default_model": FREE_ROUTER}
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def _save_config(cfg: dict) -> None:
    os.makedirs(HOME, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def _log_usage(model: str, ok: bool, note: str = "") -> None:
    os.makedirs(HOME, exist_ok=True)
    entry = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "model": model,
        "ok": ok,
        "note": note,
    }
    with open(USAGE_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _today_count() -> int:
    today = datetime.date.today().isoformat()
    n = 0
    try:
        with open(USAGE_PATH) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("ok") and e.get("ts", "")[:10] == today:
                    n += 1
    except OSError:
        pass
    return n


def _friendly_http_error(url: str, exc: urllib.error.HTTPError,
                         provider: str = "OpenRouter") -> RuntimeError:
    try:
        body = exc.read().decode("utf-8", errors="replace")[:500]
    except Exception:
        body = ""
    if exc.code == 401:
        return RuntimeError(
            f"{provider} rejected the credential (401). The key may be wrong "
            f"or revoked -- create a fresh one and retry."
        )
    if exc.code == 402:
        return RuntimeError(
            f"{provider} returned 402: no spendable balance / credit limit hit. "
            + body
        )
    if exc.code == 429:
        return RuntimeError(
            f"Rate limited (429) by {provider} or its upstream. "
            f"Wait a bit and retry. " + body
        )
    return RuntimeError(f"{provider} HTTP {exc.code} on {url}: {body}")


def _request(url: str, payload: dict | None = None, authed: bool = True,
             timeout: int = 60) -> dict:
    _ensure_allowed_url(url)
    data = None
    headers = {
        "Content-Type": "application/json",
        "HTTP-Referer": "https://muse.ai",
        "X-Title": "Clarke LLM Junction",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    if authed:
        _apply_auth(req)
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as resp:
            return _read_json_response(resp)
    except urllib.error.HTTPError as exc:
        raise _friendly_http_error(url, exc)


def _request_with_retry(url: str, payload: dict, tries: int = 3) -> dict:
    delay = 2.0
    last: Exception | None = None
    for _ in range(tries):
        try:
            return _request(url, payload, authed=True, timeout=90)
        except RuntimeError as exc:
            last = exc
            if "Rate limited (429)" not in str(exc):
                raise
            time.sleep(delay)
            delay *= 2
    raise last  # type: ignore[misc]


def _guard_free(model: str, allow_paid: bool, prov_name: str = "openrouter") -> None:
    if allow_paid:
        return
    if prov_name != "openrouter":
        # Bring-your-own-key provider: the key owner accepts the cost.
        return
    if model == FREE_ROUTER or model.endswith(":free"):
        return
    raise RuntimeError(
        f"Refusing paid model '{model}' under free-tier guardrails. "
        "Pick a :free model (see `junction models --free`), use openrouter/free, "
        "or pass --allow-paid to bill per-token to your OpenRouter balance."
    )


def _thread_path(name: str) -> str:
    safe = "".join(c for c in name if c.isalnum() or c in "-_").strip("-_")
    if not safe:
        raise RuntimeError("thread name must contain letters/numbers")
    return os.path.join(THREADS_DIR, safe + ".json")


def _load_thread(name: str) -> dict:
    path = _thread_path(name)
    try:
        with open(path) as f:
            return json.load(f)
    except OSError:
        raise RuntimeError(f"no such thread: {name} (see `junction threads`)")
    except ValueError:
        raise RuntimeError(f"thread file corrupt: {path}")


def _save_thread(name: str, data: dict) -> None:
    os.makedirs(THREADS_DIR, exist_ok=True)
    with open(_thread_path(name), "w") as f:
        json.dump(data, f, indent=2)


def cmd_models(args) -> int:
    try:
        doc = _request(BASE + "/models", authed=False, timeout=30)
    except RuntimeError:
        doc = _request(BASE + "/models", authed=True, timeout=30)
    models = doc.get("data", [])
    q = (args.query or "").lower()
    rows = []
    for m in models:
        mid = m.get("id", "")
        if args.free and not mid.endswith(":free"):
            continue
        if q and q not in mid.lower() and q not in str(m.get("name", "")).lower():
            continue
        price = m.get("pricing", {}) or {}
        rows.append((mid, m.get("context_length"), price.get("prompt"),
                     m.get("name", "")))
    rows.sort(key=lambda r: r[0])
    print(f"{len(rows)} models" + (" (:free only)" if args.free else ""))
    for mid, ctx, prompt_price, name in rows[:200]:
        price = "free" if (mid.endswith(":free") or prompt_price in ("0", 0)) else f"${prompt_price}/tok"
        print(f"  {mid}  [{ctx} ctx, {price}]")
    if len(rows) > 200:
        print(f"  ... and {len(rows) - 200} more (refine with -q)")
    return 0


def _post_chat(prov_name: str, prov: dict, model_id: str, messages: list,
               max_tokens: int, timeout: int = 120) -> tuple[str, str]:
    """POST one chat completion to the given provider (OpenAI-compatible)."""
    url = prov["base_url"].rstrip("/") + "/chat/completions"
    _ensure_allowed_url(url)
    payload = {"model": model_id, "messages": messages, "max_tokens": max_tokens}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "HTTP-Referer": "https://muse.ai",
        "X-Title": "Clarke LLM Junction",
    }, method="POST")
    _apply_auth(req, prov_name, prov)
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as resp:
            doc = _read_json_response(resp)
    except urllib.error.HTTPError as exc:
        raise _friendly_http_error(url, exc, prov_name)
    try:
        choice = doc["choices"][0]["message"]
        text = (choice.get("content") or "").strip()
        used_model = doc.get("model", model_id)
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"unexpected completion response: {str(doc)[:300]}")
    if not text:
        # Some free reasoning models return their output in `reasoning`
        # with an empty content field. Surface it, clearly labeled.
        reasoning = choice.get("reasoning") or ""
        if isinstance(reasoning, str) and reasoning.strip():
            text = ("[model returned no final answer; showing its reasoning trace]\n"
                    + reasoning.strip()[:3000])
    return used_model, text.strip()


def _chat(model_arg: str, messages: list, max_tokens: int) -> tuple[str, str]:
    """Chat via 'provider::model' or a bare model id (default provider)."""
    prov_name, prov, model_id = _resolve(model_arg)
    delay = 2.0
    last: Exception | None = None
    for _ in range(3):
        try:
            used, text = _post_chat(prov_name, prov, model_id, messages,
                                    max_tokens)
            if prov_name != "openrouter":
                used = f"{prov_name}::{used}"
            return used, text
        except RuntimeError as exc:
            last = exc
            if "Rate limited (429)" not in str(exc):
                raise
            time.sleep(delay)
            delay *= 2
    raise last  # type: ignore[misc]


def cmd_ask(args) -> int:
    _guard_free(args.model, args.allow_paid,
                args.model.split("::", 1)[0] if "::" in args.model else "openrouter")
    prompt = args.prompt
    if args.file:
        with open(args.file) as f:
            prompt = f.read()
    messages: list = []
    if args.system:
        messages.append({"role": "system", "content": args.system})
    thread_name = args.thread
    if thread_name:
        try:
            t = _load_thread(thread_name)
            if t.get("model") != args.model:
                print(f"(continuing thread '{thread_name}' started with {t.get('model')})",
                      file=sys.stderr)
            messages.extend(t.get("messages", []))
        except RuntimeError as exc:
            if "no such thread" not in str(exc):
                raise
            print(f"(starting new thread '{thread_name}')", file=sys.stderr)
    messages.append({"role": "user", "content": prompt})
    try:
        used_model, text = _chat(args.model, messages, args.max_tokens)
    except RuntimeError as exc:
        _log_usage(args.model, False, str(exc)[:120])
        raise
    _log_usage(used_model, True)
    messages.append({"role": "assistant", "content": text})
    if thread_name:
        _save_thread(thread_name, {"model": args.model, "messages": messages})
    print(f"[via {used_model}]\n")
    print(text)
    return 0


def cmd_thread(args) -> int:
    t = _load_thread(args.name)
    model = t.get("model") or _config()["default_model"]
    _guard_free(model, args.allow_paid,
                model.split("::", 1)[0] if "::" in model else "openrouter")
    messages = list(t.get("messages", []))
    messages.append({"role": "user", "content": args.prompt})
    try:
        used_model, text = _chat(model, messages, args.max_tokens)
    except RuntimeError as exc:
        _log_usage(model, False, str(exc)[:120])
        raise
    _log_usage(used_model, True)
    messages.append({"role": "assistant", "content": text})
    _save_thread(args.name, {"model": model, "messages": messages})
    print(f"[via {used_model} | thread {args.name}]\n")
    print(text)
    return 0


def cmd_threads(_args) -> int:
    os.makedirs(THREADS_DIR, exist_ok=True)
    names = sorted(f[:-5] for f in os.listdir(THREADS_DIR) if f.endswith(".json"))
    if not names:
        print("no threads yet -- start one with: junction ask MODEL \"...\" --thread NAME")
        return 0
    for n in names:
        try:
            t = _load_thread(n)
            nmsg = len(t.get("messages", []))
            print(f"  {n}  ({t.get('model', '?')}, {nmsg} messages)")
        except RuntimeError:
            print(f"  {n}  (unreadable)")
    return 0


def cmd_show(args) -> int:
    t = _load_thread(args.name)
    if t.get("roundtable"):
        print(f"roundtable on: {t.get('prompt', '')!r}\n")
        for model, text in (t.get("answers") or {}).items():
            print(f"--- {model} ---\n{text}\n")
        return 0
    print(f"thread '{args.name}' ({t.get('model', '?')}):\n")
    for m in t.get("messages", []):
        role = m.get("role", "?").upper()
        print(f"[{role}] {m.get('content', '')}\n")
    return 0


def cmd_rm_thread(args) -> int:
    path = _thread_path(args.name)
    try:
        os.remove(path)
        print(f"deleted thread '{args.name}'")
    except OSError:
        raise RuntimeError(f"no such thread: {args.name}")
    return 0


def cmd_roundtable(args) -> int:
    models = [m.strip() for m in args.models.split(",") if m.strip()] or [FREE_ROUTER]
    for m in models:
        _guard_free(m, args.allow_paid,
                    m.split("::", 1)[0] if "::" in m else "openrouter")
    messages = []
    if args.system:
        messages.append({"role": "system", "content": args.system})
    messages.append({"role": "user", "content": args.prompt})
    answers: dict = {}
    for m in models:
        try:
            used, text = _chat(m, messages, args.max_tokens)
            answers[m] = text
            _log_usage(used, True)
            print(f"--- {used} ---\n{text}\n")
        except RuntimeError as exc:
            _log_usage(m, False, str(exc)[:120])
            answers[m] = f"ERROR: {exc}"
            print(f"--- {m} ---\nERROR: {exc}\n")
    name = "roundtable-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    _save_thread(name, {"roundtable": True, "prompt": args.prompt, "answers": answers})
    print(f"(saved as '{name}'; replay with: junction show {name})")
    return 0


def cmd_providers(_args) -> int:
    provs = _providers()
    print(f"default provider: {provs.get('default', 'openrouter')}")
    print(f"config file: {PROVIDERS_PATH}\n")
    for name, p in provs["providers"].items():
        key_env = p.get("key_env", "")
        key_state = "set" if os.environ.get(key_env, "").strip() else "missing"
        print(f"  {name}")
        print(f"    base_url: {p.get('base_url')}")
        print(f"    key: {key_env} [{key_state}]")
        if p.get("note"):
            print(f"    {p['note']}")
    print("\nPlug in another OpenAI-compatible provider by editing providers.json:")
    print('  "xai": {"base_url": "https://api.x.ai/v1", "key_env": "XAI_API_KEY",')
    print('          "note": "Grok models"}')
    print("then call it as:  junction ask xai::grok-4 \"...\" --allow-paid")
    print("Model ids without '::' keep using the default provider.")
    return 0


def cmd_usage(_args) -> int:
    cfg = _config()
    used = _today_count()
    cap = cfg.get("daily_cap", 50)
    print(f"today: {used}/{cap} free-tier requests used")
    if used >= cap:
        print("daily cap reached -- free models will 429 until tomorrow (UTC).")
    elif used >= cap * 0.8:
        print("getting close to the daily cap.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="junction",
                                description="Talk to other LLMs via OpenRouter (free-tier by default).")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("models", help="list models live from OpenRouter")
    s.add_argument("--free", action="store_true", help="only :free models")
    s.add_argument("-q", "--query", default="", help="filter by name substring")
    s.set_defaults(fn=cmd_models)

    s = sub.add_parser("ask", help="ask one model")
    s.add_argument("model", help="model id, e.g. google/gemma-3-27b-it:free or openrouter/free")
    s.add_argument("prompt", help="the prompt (or use --file)")
    s.add_argument("--file", default="", help="read prompt from file")
    s.add_argument("--thread", default="", help="save/continue a named thread")
    s.add_argument("--system", default="", help="system prompt")
    s.add_argument("--max-tokens", type=int, default=1024)
    s.add_argument("--allow-paid", action="store_true",
                   help="permit non-free models (bills per token)")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("thread", help="continue a saved thread")
    s.add_argument("name", help="thread name")
    s.add_argument("prompt", help="follow-up prompt")
    s.add_argument("--max-tokens", type=int, default=1024)
    s.add_argument("--allow-paid", action="store_true")
    s.set_defaults(fn=cmd_thread)

    s = sub.add_parser("threads", help="list saved threads")
    s.set_defaults(fn=cmd_threads)

    s = sub.add_parser("show", help="print a thread or roundtable")
    s.add_argument("name", help="thread name")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("rm-thread", help="delete a thread")
    s.add_argument("name", help="thread name")
    s.set_defaults(fn=cmd_rm_thread)

    s = sub.add_parser("roundtable", help="ask several models, compare answers")
    s.add_argument("prompt", help="the prompt")
    s.add_argument("--models", default="",
                   help="comma-separated model ids (default: openrouter/free)")
    s.add_argument("--system", default="", help="system prompt")
    s.add_argument("--max-tokens", type=int, default=1024)
    s.add_argument("--allow-paid", action="store_true")
    s.set_defaults(fn=cmd_roundtable)

    s = sub.add_parser("usage", help="today's free-tier request count")
    s.set_defaults(fn=cmd_usage)

    s = sub.add_parser("providers", help="list connected LLM providers")
    s.set_defaults(fn=cmd_providers)
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.fn(args)
    except RuntimeError as exc:
        print(f"junction: error: {exc}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"junction: error: network problem reaching openrouter.ai: {exc}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
