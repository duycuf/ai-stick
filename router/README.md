# stick-router

One OpenAI-compatible endpoint in front of several LLM providers, with ordered fallback. Written for the AI Stick, where every megabyte counts: a single static Go binary (about 6 MB), standard library only, about 13 MB of RAM on the dongle, nothing written to disk.

The assistant on the stick talks to `http://127.0.0.1:20128/v1` and asks for a **route** by name. The router tries that route's targets in order and streams back the first good answer.

```
assistant ──> stick-router ──> provider 1   (rate limited)
                           └─> provider 2   (answers)
```

## What it does

- `POST /v1/chat/completions` and any other `POST /v1/...` path: rewrites `model`, adds the provider's key, forwards the body, streams the reply back as it arrives.
- Fallback: a network error, a timeout, or HTTP 401, 403, 404, 408, 429 or 5xx moves on to the next target. HTTP 400, 413 and 422 mean the request itself is wrong and go straight back to the client.
- Cooldown: a target that just failed is tried last for `cooldown_seconds`.
- `GET /v1/models` lists the route names. `GET /stats` shows requests, errors and token counts per target (in memory, reset on restart), and how many requests are in flight. `GET /health` needs no key.
- The response header `X-Stick-Router-Target` says which target answered.

What it does not do: translate between API formats (every provider must speak the OpenAI chat format), log in with subscription accounts, cache, or compress prompts.

## Model names

| The client sends | The router uses |
|---|---|
| a route name, e.g. `default` | that route's targets, in order |
| `provider/model`, e.g. `openrouter/openai/gpt-5` | that provider only, no fallback |
| anything else | the `default` route, if there is one |

## Configure

Copy [`config.example.json`](config.example.json) to `/etc/stick-router/config.json` and edit it. The model ids in the example are placeholders: use the ids your providers list.

Keys stay out of the config file. Put them in `/etc/stick-router/keys.env`, readable by root only:

```shell
sudo install -m 600 /dev/null /etc/stick-router/keys.env
sudoedit /etc/stick-router/keys.env     # ANTHROPIC_API_KEY=... one per line
stick-router -check                     # validates the config, then exits
```

| Setting | Default | |
|---|---|---|
| `listen` | `127.0.0.1:20128` | keep it on localhost unless other machines need it |
| `api_key` / `api_key_env` | none | key that clients must send as `Authorization: Bearer ...`; set it if `listen` is not localhost |
| `cooldown_seconds` | 60 | |
| `attempt_timeout_seconds` | 60 | wait for response headers before moving on |
| `providers[].headers` | none | extra request headers for that provider |
| `providers[].body` | none | extra top-level JSON fields set on every request, e.g. to turn a model's thinking mode off |
| `providers[].first_tool` | none | name of a tool the model must call first on every turn that ends with a user message. Small models skip tools now and then; this turns "always run the message through my program first" into a property of the request. Turns that end with a tool result are left alone |
| `providers[].first_tool_arg` | none | the argument of `first_tool` that takes the user's message as it is. When set, the router writes the first tool call itself: the tool gets the person's exact words, the turn costs no tokens, and with a `verbatim` reply guard a whole exchange needs no model at all |
| `providers[].first_tool_trim` | none | a pattern removed from the start of the user's message before it goes to the tool, e.g. `^\[[^\]]*\]\s*` for a client that puts a timestamp in front |
| `providers[].reply_guard` | false | enforce the `guard` a tool result asks for: send the tool's `reply` word for word and filter what the model adds. See [Reply guard](#reply-guard) |

## Reply guard

A small model follows "copy this text, then add one sentence" most of the time. When the text carries numbers someone acts on, most of the time is not enough. With `reply_guard` on, a tool can ask the router to make it certain by returning two extra fields in its JSON result:

```json
{"reply": "Logged 260 kcal. Today 700/3000.",
 "guard": {"mode": "reply_then", "max_extra": 1, "digits": "none", "ban": ["o'clock"]}}
```

| `mode` | What the client receives |
|---|---|
| `verbatim` | `reply`, exactly. The model is not called at all, so the answer takes milliseconds |
| `reply_then` | `reply` first, exactly, then at most `max_extra` sentences from the model that pass the filter |
| `free` | no fixed reply (a chat turn): the model's sentences that pass the filter, or `fallback` when none does |

A sentence is dropped when it contains a word from `ban`, or a number it should not contain: with `"digits": "none"` any digit; with `"known"` (the default) a number of two or more digits that appears neither in the tool result nor in the user's last message; with `"any"` nothing. Fields listed in `ignore` do not count as known. Leaked `<think>` blocks are always removed. An answer made of tool calls passes through untouched, and so does every turn whose tool result has no `guard`.

## Build and install

```shell
cd router
go test ./...
GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o stick-router .

# on the stick
sudo install -m 755 stick-router /usr/local/bin/
sudo install -m 644 stick-router.service /etc/systemd/system/
sudo systemctl enable --now stick-router
journalctl -u stick-router -f          # one line per request, no message contents
```

No Go on your machine? Build in Docker:

```shell
docker run --rm -v "$PWD:/src" -w /src -e GOOS=linux -e GOARCH=arm64 -e CGO_ENABLED=0 \
    golang:1.26 go build -trimpath -ldflags='-s -w' -o stick-router .
```
