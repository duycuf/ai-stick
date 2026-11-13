package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// upstream is a fake provider: it records what it received and answers as told.
type upstream struct {
	*httptest.Server
	hits      int
	lastModel string
	lastAuth  string
}

func newUpstream(t *testing.T, handler func(w http.ResponseWriter, model string)) *upstream {
	u := &upstream{}
	u.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Model string `json:"model"`
		}
		json.NewDecoder(r.Body).Decode(&body)
		u.hits++
		u.lastModel, u.lastAuth = body.Model, r.Header.Get("Authorization")
		if r.URL.Path != "/v1/chat/completions" {
			t.Errorf("upstream got path %s", r.URL.Path)
		}
		handler(w, body.Model)
	}))
	t.Cleanup(u.Close)
	return u
}

func okJSON(w http.ResponseWriter, model string) {
	w.Header().Set("Content-Type", "application/json")
	fmt.Fprintf(w, `{"model":%q,"choices":[{"message":{"content":"hi"}}],"usage":{"prompt_tokens":7,"completion_tokens":3}}`, model)
}

func status(code int) func(http.ResponseWriter, string) {
	return func(w http.ResponseWriter, _ string) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(code)
		fmt.Fprintf(w, `{"error":{"message":"upstream said %d"}}`, code)
	}
}

func newTestRouter(t *testing.T, cfg Config) *httptest.Server {
	r, err := NewRouter(cfg)
	if err != nil {
		t.Fatal(err)
	}
	s := httptest.NewServer(r)
	t.Cleanup(s.Close)
	return s
}

func post(t *testing.T, url, key, body string) (*http.Response, string) {
	req, _ := http.NewRequest(http.MethodPost, url+"/v1/chat/completions", strings.NewReader(body))
	if key != "" {
		req.Header.Set("Authorization", "Bearer "+key)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	return resp, string(b)
}

func twoProviders(a, b *upstream) Config {
	return Config{
		Providers: []Provider{
			{Name: "a", BaseURL: a.URL + "/v1", APIKey: "key-a"},
			{Name: "b", BaseURL: b.URL + "/v1/", APIKey: "key-b"},
		},
		Routes: map[string][]Target{"smart": {{"a", "model-a"}, {"b", "model-b"}}},
	}
}

func TestFirstTargetAnswers(t *testing.T) {
	a, b := newUpstream(t, okJSON), newUpstream(t, okJSON)
	s := newTestRouter(t, twoProviders(a, b))
	resp, body := post(t, s.URL, "", `{"model":"smart","messages":[]}`)
	if resp.StatusCode != 200 || !strings.Contains(body, `"model-a"`) {
		t.Fatalf("got %d %s", resp.StatusCode, body)
	}
	if a.lastModel != "model-a" || a.lastAuth != "Bearer key-a" || b.hits != 0 {
		t.Fatalf("a got model=%q auth=%q, b hits=%d", a.lastModel, a.lastAuth, b.hits)
	}
	if got := resp.Header.Get("X-Stick-Router-Target"); got != "a/model-a" {
		t.Fatalf("target header = %q", got)
	}
}

// rawUpstream records the raw request bodies it gets and answers with the given status.
func rawUpstream(t *testing.T, code int, got *[]map[string]any) *httptest.Server {
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var m map[string]any
		json.NewDecoder(r.Body).Decode(&m)
		*got = append(*got, m)
		if code != 200 {
			w.WriteHeader(code)
			return
		}
		okJSON(w, fmt.Sprint(m["model"]))
	}))
	t.Cleanup(s.Close)
	return s
}

func TestProviderBodyFieldsOverrideOnlyForThatProvider(t *testing.T) {
	var qwen, other []map[string]any
	q := rawUpstream(t, 503, &qwen)
	o := rawUpstream(t, 200, &other)
	s := newTestRouter(t, Config{
		Providers: []Provider{
			{Name: "qwen", BaseURL: q.URL + "/v1", Body: map[string]json.RawMessage{
				"chat_template_kwargs": json.RawMessage(`{"enable_thinking":false}`),
			}},
			{Name: "other", BaseURL: o.URL + "/v1"},
		},
		Routes: map[string][]Target{"default": {{"qwen", "Qwen/Qwen3.8-27B"}, {"other", "m2"}}},
	})
	resp, body := post(t, s.URL, "", `{"model":"default","temperature":0.3,"chat_template_kwargs":{"enable_thinking":true}}`)
	if resp.StatusCode != 200 {
		t.Fatalf("got %d %s", resp.StatusCode, body)
	}
	if len(qwen) != 1 || len(other) != 1 {
		t.Fatalf("qwen hits %d, other hits %d", len(qwen), len(other))
	}
	if k, _ := qwen[0]["chat_template_kwargs"].(map[string]any); k["enable_thinking"] != false || qwen[0]["temperature"] != 0.3 {
		t.Fatalf("qwen body = %v", qwen[0])
	}
	if qwen[0]["model"] != "Qwen/Qwen3.8-27B" {
		t.Fatalf("qwen model = %v", qwen[0]["model"])
	}
	// The fallback provider gets the client's own body, not qwen's override.
	if k, _ := other[0]["chat_template_kwargs"].(map[string]any); k["enable_thinking"] != true || other[0]["model"] != "m2" {
		t.Fatalf("other body = %v", other[0])
	}
}

func TestFallsBackOnRateLimitAndCoolsDown(t *testing.T) {
	a, b := newUpstream(t, status(429)), newUpstream(t, okJSON)
	s := newTestRouter(t, twoProviders(a, b))
	resp, body := post(t, s.URL, "", `{"model":"smart"}`)
	if resp.StatusCode != 200 || !strings.Contains(body, `"model-b"`) || b.lastAuth != "Bearer key-b" {
		t.Fatalf("got %d %s", resp.StatusCode, body)
	}
	// The failed target is cooling down, so the next call goes straight to b.
	post(t, s.URL, "", `{"model":"smart"}`)
	if a.hits != 1 || b.hits != 2 {
		t.Fatalf("hits a=%d b=%d, want 1 and 2", a.hits, b.hits)
	}
}

func TestBadRequestIsNotRetried(t *testing.T) {
	a, b := newUpstream(t, status(400)), newUpstream(t, okJSON)
	s := newTestRouter(t, twoProviders(a, b))
	resp, body := post(t, s.URL, "", `{"model":"smart"}`)
	if resp.StatusCode != 400 || !strings.Contains(body, "upstream said 400") || b.hits != 0 {
		t.Fatalf("got %d %s, b hits=%d", resp.StatusCode, body, b.hits)
	}
}

func TestAllTargetsFailReturnsLastError(t *testing.T) {
	a, b := newUpstream(t, status(500)), newUpstream(t, status(503))
	s := newTestRouter(t, twoProviders(a, b))
	resp, body := post(t, s.URL, "", `{"model":"smart"}`)
	if resp.StatusCode != 503 || !strings.Contains(body, "upstream said 503") {
		t.Fatalf("got %d %s", resp.StatusCode, body)
	}
}

func TestUnreachableProviderFallsBack(t *testing.T) {
	a, b := newUpstream(t, okJSON), newUpstream(t, okJSON)
	cfg := twoProviders(a, b)
	a.Close() // connection refused
	s := newTestRouter(t, cfg)
	resp, body := post(t, s.URL, "", `{"model":"smart"}`)
	if resp.StatusCode != 200 || !strings.Contains(body, `"model-b"`) {
		t.Fatalf("got %d %s", resp.StatusCode, body)
	}
}

func TestDirectProviderModelAndUnknownModel(t *testing.T) {
	a, b := newUpstream(t, okJSON), newUpstream(t, okJSON)
	s := newTestRouter(t, twoProviders(a, b))
	if _, body := post(t, s.URL, "", `{"model":"b/vendor/some-model"}`); !strings.Contains(body, `"vendor/some-model"`) {
		t.Fatalf("direct call got %s", body)
	}
	if resp, _ := post(t, s.URL, "", `{"model":"nope"}`); resp.StatusCode != 400 {
		t.Fatalf("unknown model got %d", resp.StatusCode)
	}
}

func TestStreamingPassesThroughAndCountsTokens(t *testing.T) {
	a := newUpstream(t, func(w http.ResponseWriter, _ string) {
		w.Header().Set("Content-Type", "text/event-stream")
		for _, chunk := range []string{
			`data: {"choices":[{"delta":{"content":"he"}}]}`,
			`data: {"choices":[{"delta":{"content":"llo"}}]}`,
			`data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":2}}`,
			`data: [DONE]`,
		} {
			fmt.Fprint(w, chunk+"\n\n")
			w.(http.Flusher).Flush()
		}
	})
	r, err := NewRouter(Config{Providers: []Provider{{Name: "a", BaseURL: a.URL + "/v1"}},
		Routes: map[string][]Target{"default": {{"a", "m"}}}})
	if err != nil {
		t.Fatal(err)
	}
	s := httptest.NewServer(r)
	defer s.Close()
	resp, body := post(t, s.URL, "", `{"model":"anything","stream":true}`)
	if resp.Header.Get("Content-Type") != "text/event-stream" || strings.Count(body, "data:") != 4 || !strings.Contains(body, "[DONE]") {
		t.Fatalf("stream got %q", body)
	}
	st := r.stats["a/m"]
	if st == nil || st.PromptTokens != 11 || st.CompletionTokens != 2 || st.Requests != 1 {
		t.Fatalf("stats = %+v", st)
	}
}

func TestClientKeyIsRequiredWhenConfigured(t *testing.T) {
	a, b := newUpstream(t, okJSON), newUpstream(t, okJSON)
	cfg := twoProviders(a, b)
	cfg.APIKey = "local-secret"
	s := newTestRouter(t, cfg)
	if resp, _ := post(t, s.URL, "", `{"model":"smart"}`); resp.StatusCode != 401 {
		t.Fatalf("no key got %d", resp.StatusCode)
	}
	if resp, _ := post(t, s.URL, "wrong", `{"model":"smart"}`); resp.StatusCode != 401 {
		t.Fatalf("wrong key got %d", resp.StatusCode)
	}
	if resp, _ := post(t, s.URL, "local-secret", `{"model":"smart"}`); resp.StatusCode != 200 {
		t.Fatalf("right key got %d", resp.StatusCode)
	}
	if a.lastAuth != "Bearer key-a" {
		t.Fatalf("client key leaked upstream: %q", a.lastAuth)
	}
	resp, err := http.Get(s.URL + "/health")
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("health needs no key, got %v", err)
	}
}

func TestConfigValidation(t *testing.T) {
	bad := []Config{
		{},
		{Providers: []Provider{{Name: "a", BaseURL: "ftp://x"}}},
		{Providers: []Provider{{Name: "a/b", BaseURL: "https://x"}}},
		{Providers: []Provider{{Name: "a", BaseURL: "https://x"}}, Routes: map[string][]Target{"r": {{"missing", "m"}}}},
		{Providers: []Provider{{Name: "a", BaseURL: "https://x", APIKeyEnv: "STICK_ROUTER_TEST_UNSET"}}},
	}
	for i, cfg := range bad {
		if _, err := NewRouter(cfg); err == nil {
			t.Errorf("config %d should be rejected", i)
		}
	}
}
