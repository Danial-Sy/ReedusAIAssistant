# Cloud brain: pick the LLM

Lite always thinks in the cloud. By default that's **Gemini 3.7 Flash**, but you can switch to Claude (Sonnet, Opus, Haiku), GPT, Grok, DeepSeek, any model on OpenRouter, or a model on your own Ollama/LM Studio box. The robot uses the same choice when it's in cloud mode.

**ReedOS → Settings → Cloud brain.** Tap a provider, follow its "Getting a key" steps, paste the key, press **Save & test**, choose a model (**Refresh list** shows every model your key can use), then press **Use**. He switches before his next sentence. If he's talking, the switch waits for him to finish.

## Every key needs money on the account

Each provider bills per use. **Until the account has prepaid credit or a card on file, the key won't answer**, and Test tells you so. A ChatGPT Plus, Claude Pro/Max or Gemini app subscription doesn't cover the API, which is billed separately. Reedus's monthly cap (`REEDUS_CLOUD_BUDGET`, $35 by default, shown as Cloud spend in Settings) stops him before he overspends whichever brain you pick.

## Getting a key

| Provider | Where | What to do |
|---|---|---|
| **Gemini** (default) | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | Create API key (starts `AIza`), then **Set up billing** next to it. The free tier is rate-limited and Google may train on free-tier chats. |
| **Claude** | [console.anthropic.com](https://console.anthropic.com/settings/keys) | Settings → Billing → buy credits ($5 min). Then Settings → API keys → Create key (starts `sk-ant-`, shown once). Optional: set a spend limit under Limits. |
| **OpenAI** | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) | Settings → Billing → add credit ($5 min). Then create a secret key (`sk-…`, shown once). Some new models also need organization verification. |
| **OpenRouter** | [openrouter.ai/settings/keys](https://openrouter.ai/settings/keys) | Add credits, then create a key (`sk-or-…`). One key reaches hundreds of models; pick any of them by id. |
| **Grok / DeepSeek / Groq / Mistral** | console.x.ai · platform.deepseek.com · console.groq.com · console.mistral.ai | Add credit or a card in billing, then create an API key. |
| **Other** | your server | Any OpenAI-compatible base URL (`…/v1`). Ollama is `http://<box>:11434/v1` and needs no key. |

The app shows the same steps for each provider, so you don't have to come back here.

## Web search

**Automatic** (the default) uses the brain's own search when it has one:

| Brain | Who searches |
|---|---|
| Gemini | Google Search grounding (free up to 5,000/month) |
| Claude | Claude's web search tool ($10 per 1,000 searches) |
| OpenAI | OpenAI's web search tool |
| everything else | Reedus's own search router: SearXNG, then DuckDuckGo (or Brave with `BRAVE_API_KEY`) |

You can also force **Reedus's own search** (free), **Gemini grounding** (with any brain, if you also have a Gemini key) or **Off**.

## Where things live

- The choice and keys are in `memory/llm.json` on the run box (gitignored, owner-only). The app only ever gets back the last 4 characters of a key.
- Without that file, `deploy/xeon.env` sets the defaults: `REEDUS_LLM_PROVIDER`, `REEDUS_LLM_MODEL`, `REEDUS_LLM_SEARCH`, and the `*_API_KEY` variables in `xeon.secrets.env`. Anything set in the app wins.
- Code: `reedus_companion/cloud/providers.py` (catalog, steps, prices), `llm_settings.py`, `llm.py` (build, search, `/api/llm`), `anthropic_backend.py`, `openai_backend.py`. Tests: `tests/test_llm.py`.
- New dependencies: `anthropic` and `openai` in `requirements/cloud.txt`. Run `pip install -r reedus_companion/requirements/cloud.txt` on the run box.

## Tuning for speed

Replies are tuned for talking speed: low effort, and thinking turned off where the model allows it (Claude Sonnet 5.5 and Haiku 5.5). Opus 5.5 and Fable can't turn thinking off, so they run at low effort and answer a little slower. Overrides: `REEDUS_CLAUDE_EFFORT`, `REEDUS_CLAUDE_THINK=adaptive`, `REEDUS_OPENAI_EFFORT`.
