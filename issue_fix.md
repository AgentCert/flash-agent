# Flash Agent v3.0.0 – Issue Fix Log

**Date:** March 16, 2026  
**File:** `test.py` (agent-mcp-llm.py adapted for local testing)

---

## Issue 1: Invalid Model Name – 400 Bad Request

**Error:**
```
HTTP/1.1 400 Bad Request
{'error': {'message': "Invalid model name passed in model=gpt-4o. Call `/v1/models` to view available models for your key."}}
```

**Root Cause:**  
The code had `MODEL_ALIAS` defaulting to `gpt-4o`, but the LiteLLM proxy was configured with OpenRouter as the backend. The available models in LiteLLM were:
- `gemini-3-flash`
- `gemini-2.5-flash`
- `gemini-2.5-flash-lite`
- `auto-free`

`gpt-4o` did not exist in the LiteLLM config, so it was rejected with a 400 error.

**Fix:**  
Changed the default `MODEL_ALIAS` from `gpt-4o` to `auto-free` (a working model in the LiteLLM config).

```python
# Before
MODEL_ALIAS = os.getenv("MODEL_ALIAS", "gpt-4o")

# After
MODEL_ALIAS = os.getenv("MODEL_ALIAS", "auto-free")
```

---

## Issue 2: Gemini API Key Invalid – 401 Unauthorized

**Error:**
```
HTTP/1.1 401 Unauthorized
litellm.AuthenticationError: GeminiException - API key not valid. Please pass a valid API key.
Received Model Group=gemini-2.5-flash
```

**Root Cause:**  
After switching to `gemini-2.5-flash`, LiteLLM routed the request directly to Google's Gemini API (googleapis.com) instead of through OpenRouter. The Gemini API key configured in LiteLLM was invalid or expired.

**Fix:**  
Switched `MODEL_ALIAS` to `auto-free`, which routes through OpenRouter's free tier and does not require a separate Gemini API key.

---

## Issue 3: MCP Server 400 Bad Request – Wrong Protocol

**Error:**
```
MCP kubernetes HTTP 400: 400 Client Error: Bad Request for url: http://localhost:8086/mcp
```

**Root Cause:**  
The code was sending a **custom JSON payload** to the MCP servers:
```json
{"query": "...", "namespace": "default", "agent": "flash-agent"}
```

But MCP servers expect the **JSON-RPC 2.0** protocol with proper message format:
```json
{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "pods_list_in_namespace", "arguments": {"namespace": "default"}}}
```

Additionally, the Prometheus MCP server requires a **session ID** (obtained via an `initialize` handshake) in the `Mcp-Session-Id` header.

**Fix:**  
Rewrote the MCP communication layer with three new functions:

1. **`_mcp_jsonrpc_call()`** – Sends JSON-RPC 2.0 requests and parses SSE (Server-Sent Events) responses. Handles session ID propagation via headers.

2. **`_mcp_init_session()`** – Performs the MCP `initialize` handshake to obtain a session ID.

3. **`agent_call_mcp_server()`** – Rewritten to:
   - Initialize an MCP session first
   - Call specific MCP tools via `tools/call` method:
     - **Kubernetes MCP:** `pods_list_in_namespace` + `events_list`
     - **Prometheus MCP:** `execute_query` (PromQL queries)
   - Parse SSE response format (`data: {...}` lines)

---

## Issue 4: LLM Returns `content=None` – NoneType Error

**Error:**
```
LLM tool-selection failed: 'NoneType' object has no attribute 'strip' – defaulting to kubernetes
```

**Root Cause:**  
The `auto-free` model routed to a **reasoning model** (e.g., via OpenRouter) that returns its output in the `reasoning_content` field instead of `content`. The code called `.content.strip()` without checking for `None`.

**Fix:**  
Added fallback logic to check both `content` and `reasoning_content`:

```python
# Before
output_text = resp.choices[0].message.content.strip().lower()

# After
msg = resp.choices[0].message
raw_text = msg.content or getattr(msg, "reasoning_content", None) or ""
output_text = raw_text.strip().lower()
```

Also increased `max_tokens` from `8` to `50` to give reasoning models enough space.

---

## Issue 5: Model Doesn't Support System Role – 400 Bad Request

**Error:**
```
HTTP/1.1 400 Bad Request
OpenrouterException - "Developer instruction is not enabled for models/gemma-3n-e2b-it"
```

**Root Cause:**  
The `auto-free` model routed to `gemma-3n-e2b-it` (Google Gemma), which does **not support the `system` role** in chat messages. The code was sending:
```json
[
  {"role": "system", "content": "You are an expert..."},
  {"role": "user", "content": "Data to analyse..."}
]
```

Additionally, `response_format: {"type": "json_object"}` is not universally supported across all models.

**Fix:**  
1. **Merged system prompt into user message** for both tool selection and analysis:
   ```python
   # Before
   messages = [
       {"role": "system", "content": _ANALYSIS_SYSTEM},
       {"role": "user",   "content": payload_text},
   ]

   # After
   combined_prompt = f"INSTRUCTIONS:\n{_ANALYSIS_SYSTEM}\n\nDATA TO ANALYSE:\n{payload_text}"
   messages = [
       {"role": "user", "content": combined_prompt},
   ]
   ```

2. **Removed `response_format`** parameter from the API call.

3. **Added JSON extraction** from markdown code fences (models often wrap JSON in ` ```json ... ``` `):
   ```python
   if "```json" in json_text:
       json_text = json_text.split("```json", 1)[1].split("```", 1)[0]
   ```

---

## Issue 6: Logging Format Error – `%d` with NoneType

**Error:**
```
TypeError: %d format: a real number is required, not NoneType
Arguments: ('flash-agent-default-...', 6.56, 'kubernetes', 100, 0, None)
```

**Root Cause:**  
The LLM returned `"total_pods": null` in the JSON response. The log format string used `%d` (integer format) for `total_pods`, which failed when the value was `None`.

**Fix:**  
Changed the format specifier and added a fallback:

```python
# Before
"health=%s | issues=%d | pods=%d ═══",
...
health.get("total_pods", 0),

# After
"health=%s | issues=%d | pods=%s ═══",
...
health.get("total_pods", 0) or 0,
```

---

## Summary

| # | Issue | HTTP Code | Root Cause | Fix |
|---|-------|-----------|------------|-----|
| 1 | Invalid model name | 400 | `gpt-4o` not in LiteLLM config | Changed to `auto-free` |
| 2 | Gemini API key invalid | 401 | LiteLLM routing to Gemini directly | Used `auto-free` via OpenRouter |
| 3 | MCP wrong protocol | 400 | Custom JSON instead of JSON-RPC 2.0 | Rewrote MCP layer with proper protocol |
| 4 | `content=None` from LLM | — | Reasoning model returns `reasoning_content` | Fallback to `reasoning_content` |
| 5 | System role unsupported | 400 | `gemma-3n-e2b-it` rejects system role | Merged into user message |
| 6 | Logging NoneType error | — | LLM returned `null` for `total_pods` | Added null-safe formatting |

---

## Final Result

After all fixes, the full agent flow works end-to-end:

```
✅ LLM tool selection    → kubernetes      (200 OK)
✅ MCP session init      → connected
✅ MCP pods_list         → data received
✅ MCP events_list       → data received
✅ LLM analysis          → health=100      (200 OK)
✅ Scan complete         → ~4.6s
```
