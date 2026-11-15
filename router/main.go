// stick-router: one OpenAI-compatible endpoint in front of several LLM providers,
// with ordered fallback. Small enough to run on a 4G USB dongle.
//
// The client asks for a route name (for example "smart") as the model. The router
// tries that route's targets in order and streams back the first good answer.
// Standard library only; nothing is written to disk.
package main

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

const version = "0.4.0"

type Provider struct {
	Name      string            `json:"name"`
	BaseURL   string            `json:"base_url"`    // up to and including /v1, e.g. https://openrouter.ai/api/v1
	APIKey    string            `json:"api_key"`     // or:
	APIKeyEnv string            `json:"api_key_env"` // name of an environment variable holding the key
	Headers   map[string]string `json:"headers"`     // extra request headers
	// Extra top-level JSON fields set on every request body sent to this provider, overriding the
	// client's value, e.g. {"chat_template_kwargs": {"enable_thinking": false}} for a vLLM Qwen.
	Body map[string]json.RawMessage `json:"body"`
	// Name of a tool the model must call first on every turn that ends with a user message.
	// Small models skip tools now and then; this makes "always run the message through the
	// program before answering" a property of the request instead of a hope in the prompt.
	// Turns that end with a tool result are left alone, so the model can still answer or use
	// other tools afterwards.
	FirstTool string `json:"first_tool"`
	// Enforce the "guard" a tool result asks for: send its reply word for word and filter what
	// the model adds. See guard.go. Off unless set; tool results without a guard are untouched.
	ReplyGuard bool `json:"reply_guard"`
}

type Target struct {
	Provider string `json:"provider"`
	Model    string `json:"model"`
}

type Config struct {
	Listen                string              `json:"listen"`
	APIKey                string              `json:"api_key"`     // key clients must send; empty = no check
	APIKeyEnv             string              `json:"api_key_env"` // or read it from this environment variable
	Providers             []Provider          `json:"providers"`
	Routes                map[string][]Target `json:"routes"`
	CooldownSeconds       int                 `json:"cooldown_seconds"`        // skip a failed target for this long
	AttemptTimeoutSeconds int                 `json:"attempt_timeout_seconds"` // wait this long for response headers
}

type stat struct {
	Requests         int64  `json:"requests"`
	Errors           int64  `json:"errors"`
	PromptTokens     int64  `json:"prompt_tokens"`
	CompletionTokens int64  `json:"completion_tokens"`
	LastStatus       int    `json:"last_status"`
	LastUsed         string `json:"last_used,omitempty"`
}

type Router struct {
	cfg       Config
	providers map[string]Provider
	client    *http.Client
	cooldown  time.Duration
	started   time.Time
	inFlight  atomic.Int64 // requests being proxied right now (a status LED can show "thinking")

	mu       sync.Mutex
	stats    map[string]*stat
	coolTill map[string]time.Time
}

const maxBody = 20 << 20 // request bodies carry images; replies are streamed, not buffered

func loadConfig(path string) (Config, error) {
	var cfg Config
	raw, err := os.ReadFile(path)
	if err != nil {
		return cfg, err
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&cfg); err != nil {
		return cfg, fmt.Errorf("%s: %w", path, err)
	}
	return cfg, nil
}

func NewRouter(cfg Config) (*Router, error) {
	if cfg.Listen == "" {
		cfg.Listen = "127.0.0.1:20128"
	}
	if cfg.CooldownSeconds == 0 {
		cfg.CooldownSeconds = 60
	}
	if cfg.AttemptTimeoutSeconds == 0 {
		cfg.AttemptTimeoutSeconds = 60
	}
	if cfg.APIKey == "" && cfg.APIKeyEnv != "" {
		cfg.APIKey = os.Getenv(cfg.APIKeyEnv)
		if cfg.APIKey == "" {
			return nil, fmt.Errorf("api_key_env %s is empty", cfg.APIKeyEnv)
		}
	}
	r := &Router{cfg: cfg, providers: map[string]Provider{}, stats: map[string]*stat{},
		coolTill: map[string]time.Time{}, cooldown: time.Duration(cfg.CooldownSeconds) * time.Second,
		started: time.Now()}
	for _, p := range cfg.Providers {
		if p.Name == "" || strings.Contains(p.Name, "/") {
			return nil, fmt.Errorf("provider name %q must be non-empty and contain no slash", p.Name)
		}
		if !strings.HasPrefix(p.BaseURL, "http://") && !strings.HasPrefix(p.BaseURL, "https://") {
			return nil, fmt.Errorf("provider %s: base_url must start with http:// or https://", p.Name)
		}
		p.BaseURL = strings.TrimRight(p.BaseURL, "/")
		if p.APIKey == "" && p.APIKeyEnv != "" {
			p.APIKey = os.Getenv(p.APIKeyEnv)
			if p.APIKey == "" {
				return nil, fmt.Errorf("provider %s: environment variable %s is empty", p.Name, p.APIKeyEnv)
			}
		}
		if _, dup := r.providers[p.Name]; dup {
			return nil, fmt.Errorf("provider %s is defined twice", p.Name)
		}
		r.providers[p.Name] = p
	}
	if len(r.providers) == 0 {
		return nil, errors.New("no providers configured")
	}
	for name, targets := range cfg.Routes {
		if len(targets) == 0 {
			return nil, fmt.Errorf("route %s has no targets", name)
		}
		for _, t := range targets {
			if _, ok := r.providers[t.Provider]; !ok {
				return nil, fmt.Errorf("route %s: unknown provider %q", name, t.Provider)
			}
			if t.Model == "" {
				return nil, fmt.Errorf("route %s: target for %s has no model", name, t.Provider)
			}
		}
	}
	r.client = &http.Client{Transport: &http.Transport{
		Proxy:                 http.ProxyFromEnvironment,
		DialContext:           (&net.Dialer{Timeout: 15 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		TLSHandshakeTimeout:   15 * time.Second,
		ResponseHeaderTimeout: time.Duration(cfg.AttemptTimeoutSeconds) * time.Second,
		IdleConnTimeout:       90 * time.Second,
		MaxIdleConnsPerHost:   4,
	}}
	return r, nil
}

// resolve turns the client's model name into an ordered list of targets:
// a route name, or "provider/model" for a direct call, or the "default" route.
func (r *Router) resolve(model string) ([]Target, bool) {
	if t, ok := r.cfg.Routes[model]; ok {
		return t, true
	}
	if i := strings.Index(model, "/"); i > 0 {
		if _, ok := r.providers[model[:i]]; ok {
			return []Target{{Provider: model[:i], Model: model[i+1:]}}, true
		}
	}
	if t, ok := r.cfg.Routes["default"]; ok {
		return t, true
	}
	return nil, false
}

// retryable reports whether another target could plausibly do better. 400/413/422
// mean the request itself is wrong, so they go straight back to the client.
func retryable(status int) bool {
	switch status {
	case 401, 403, 404, 408, 429:
		return true
	}
	return status >= 500
}

func (r *Router) cooling(key string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return time.Now().Before(r.coolTill[key])
}

func (r *Router) record(key string, status int, failed bool, prompt, completion int64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	s := r.stats[key]
	if s == nil {
		s = &stat{}
		r.stats[key] = s
	}
	s.Requests++
	s.LastStatus = status
	s.LastUsed = time.Now().UTC().Format(time.RFC3339)
	s.PromptTokens += prompt
	s.CompletionTokens += completion
	if failed {
		s.Errors++
		r.coolTill[key] = time.Now().Add(r.cooldown)
	} else {
		delete(r.coolTill, key)
	}
}

func writeError(w http.ResponseWriter, status int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(map[string]any{"error": map[string]any{"message": msg, "type": "stick_router_error"}})
}

func (r *Router) authorized(req *http.Request) bool {
	if r.cfg.APIKey == "" {
		return true
	}
	got := strings.TrimPrefix(req.Header.Get("Authorization"), "Bearer ")
	if got == "" {
		got = req.Header.Get("x-api-key")
	}
	return subtle.ConstantTimeCompare([]byte(got), []byte(r.cfg.APIKey)) == 1
}

func (r *Router) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	switch {
	case req.URL.Path == "/health":
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"ok":true,"version":%q,"uptime_seconds":%d,"in_flight":%d}`+"\n", version,
			int(time.Since(r.started).Seconds()), r.inFlight.Load())
	case !r.authorized(req):
		writeError(w, http.StatusUnauthorized, "missing or wrong API key")
	case req.URL.Path == "/stats" && req.Method == http.MethodGet:
		r.handleStats(w)
	case req.URL.Path == "/v1/models" && req.Method == http.MethodGet:
		r.handleModels(w)
	case strings.HasPrefix(req.URL.Path, "/v1/") && req.Method == http.MethodPost:
		r.handleProxy(w, req)
	default:
		writeError(w, http.StatusNotFound, "unknown endpoint")
	}
}

func (r *Router) handleModels(w http.ResponseWriter) {
	names := make([]string, 0, len(r.cfg.Routes))
	for n := range r.cfg.Routes {
		names = append(names, n)
	}
	sort.Strings(names)
	data := make([]map[string]any, 0, len(names))
	for _, n := range names {
		data = append(data, map[string]any{"id": n, "object": "model", "owned_by": "stick-router"})
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{"object": "list", "data": data})
}

func (r *Router) handleStats(w http.ResponseWriter) {
	r.mu.Lock()
	out := map[string]any{"version": version, "uptime_seconds": int(time.Since(r.started).Seconds()), "targets": r.stats,
		"in_flight": r.inFlight.Load()}
	cool := map[string]int{}
	for k, t := range r.coolTill {
		if left := int(time.Until(t).Seconds()); left > 0 {
			cool[k] = left
		}
	}
	out["cooling_down_seconds"] = cool
	b, _ := json.MarshalIndent(out, "", "  ")
	r.mu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	w.Write(append(b, '\n'))
}

func (r *Router) handleProxy(w http.ResponseWriter, req *http.Request) {
	r.inFlight.Add(1)
	defer r.inFlight.Add(-1)
	body, err := io.ReadAll(http.MaxBytesReader(w, req.Body, maxBody))
	if err != nil {
		writeError(w, http.StatusRequestEntityTooLarge, "request body too large or unreadable")
		return
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(body, &fields); err != nil {
		writeError(w, http.StatusBadRequest, "request body must be a JSON object")
		return
	}
	var model string
	json.Unmarshal(fields["model"], &model)
	targets, ok := r.resolve(model)
	if !ok {
		writeError(w, http.StatusBadRequest, fmt.Sprintf("unknown model %q: use a route name, provider/model, or define a \"default\" route", model))
		return
	}

	// Prefer targets that are not cooling down, but never refuse outright.
	ordered := make([]Target, 0, len(targets))
	var cold []Target
	for _, t := range targets {
		if r.cooling(t.Provider + "/" + t.Model) {
			cold = append(cold, t)
		} else {
			ordered = append(ordered, t)
		}
	}
	ordered = append(ordered, cold...)

	start := time.Now()
	var lastStatus int
	var lastBody []byte
	var lastType string
	for i, t := range ordered {
		key := t.Provider + "/" + t.Model
		p := r.providers[t.Provider]
		send := make(map[string]json.RawMessage, len(fields)+len(p.Body))
		for k, v := range fields {
			send[k] = v
		}
		for k, v := range p.Body {
			send[k] = v
		}
		if p.FirstTool != "" {
			forceFirstTool(send, p.FirstTool)
		}
		var guard *replyGuard
		if p.ReplyGuard {
			if guard = newReplyGuard(send); guard != nil && guard.rules.Mode == "verbatim" {
				w.Header().Set("X-Stick-Router-Target", key)
				guard.answerWithoutModel(w, t.Model)
				log.Printf("route=%s target=%s guard=verbatim ms=%d", model, key, time.Since(start).Milliseconds())
				return
			}
		}
		send["model"], _ = json.Marshal(t.Model)
		out, _ := json.Marshal(send)
		up, err := http.NewRequestWithContext(req.Context(), http.MethodPost, p.BaseURL+strings.TrimPrefix(req.URL.Path, "/v1"), bytes.NewReader(out))
		if err != nil {
			continue
		}
		up.Header.Set("Content-Type", "application/json")
		if a := req.Header.Get("Accept"); a != "" {
			up.Header.Set("Accept", a)
		}
		if p.APIKey != "" {
			up.Header.Set("Authorization", "Bearer "+p.APIKey)
		}
		for k, v := range p.Headers {
			up.Header.Set(k, v)
		}
		resp, err := r.client.Do(up)
		if err != nil {
			if req.Context().Err() != nil {
				return // the client went away
			}
			r.record(key, 0, true, 0, 0)
			log.Printf("route=%s try=%d target=%s error=%v", model, i+1, key, err)
			lastStatus, lastBody, lastType = http.StatusBadGateway, nil, ""
			continue
		}
		if retryable(resp.StatusCode) && i < len(ordered)-1 {
			lastBody, _ = io.ReadAll(io.LimitReader(resp.Body, 64<<10))
			resp.Body.Close()
			lastStatus, lastType = resp.StatusCode, resp.Header.Get("Content-Type")
			r.record(key, resp.StatusCode, true, 0, 0)
			log.Printf("route=%s try=%d target=%s status=%d next", model, i+1, key, resp.StatusCode)
			continue
		}
		// This answer goes to the client, good or bad.
		for k, vs := range resp.Header {
			switch k {
			case "Connection", "Keep-Alive", "Transfer-Encoding", "Upgrade", "Proxy-Authenticate", "Proxy-Authorization", "Te", "Trailer", "Set-Cookie":
				continue
			}
			w.Header()[k] = vs
		}
		w.Header().Set("X-Stick-Router-Target", key)
		if guard != nil && resp.StatusCode == http.StatusOK {
			w.Header().Del("Content-Length") // the body is rewritten
		} else {
			guard = nil
		}
		w.WriteHeader(resp.StatusCode)
		sniff := &usageSniffer{sse: strings.HasPrefix(resp.Header.Get("Content-Type"), "text/event-stream")}
		if guard != nil {
			guard.rewrite(w, resp.Body, sniff)
		} else {
			copyStream(w, resp.Body, sniff)
		}
		resp.Body.Close()
		prompt, completion := sniff.result()
		failed := resp.StatusCode >= 400
		r.record(key, resp.StatusCode, failed && retryable(resp.StatusCode), prompt, completion)
		log.Printf("route=%s try=%d target=%s status=%d ms=%d tokens_in=%d tokens_out=%d", model, i+1, key,
			resp.StatusCode, time.Since(start).Milliseconds(), prompt, completion)
		return
	}
	// Every target failed before producing a usable response.
	if lastBody != nil {
		if lastType != "" {
			w.Header().Set("Content-Type", lastType)
		}
		w.WriteHeader(lastStatus)
		w.Write(lastBody)
		return
	}
	writeError(w, http.StatusBadGateway, "no provider could be reached")
}

// forceFirstTool sets tool_choice to the named function when the conversation ends with a user
// message and the request offers that tool. It changes nothing otherwise.
func forceFirstTool(send map[string]json.RawMessage, name string) {
	var msgs []struct {
		Role string `json:"role"`
	}
	if json.Unmarshal(send["messages"], &msgs) != nil || len(msgs) == 0 || msgs[len(msgs)-1].Role != "user" {
		return
	}
	var tools []struct {
		Function struct {
			Name string `json:"name"`
		} `json:"function"`
	}
	if json.Unmarshal(send["tools"], &tools) != nil {
		return
	}
	for _, t := range tools {
		if t.Function.Name == name {
			send["tool_choice"], _ = json.Marshal(map[string]any{
				"type": "function", "function": map[string]string{"name": name}})
			return
		}
	}
}

func copyStream(w http.ResponseWriter, src io.Reader, sniff *usageSniffer) {
	flusher, _ := w.(http.Flusher)
	buf := make([]byte, 16<<10)
	for {
		n, err := src.Read(buf)
		if n > 0 {
			sniff.feed(buf[:n])
			if _, werr := w.Write(buf[:n]); werr != nil {
				return
			}
			if flusher != nil {
				flusher.Flush()
			}
		}
		if err != nil {
			return
		}
	}
}

// usageSniffer watches a response go by and picks out the token counts, without
// holding the whole response in memory.
type usageSniffer struct {
	sse        bool
	buf        []byte
	prompt     int64
	completion int64
}

type usageBody struct {
	Usage *struct {
		PromptTokens     int64 `json:"prompt_tokens"`
		CompletionTokens int64 `json:"completion_tokens"`
		InputTokens      int64 `json:"input_tokens"`
		OutputTokens     int64 `json:"output_tokens"`
	} `json:"usage"`
}

func (s *usageSniffer) parse(b []byte) {
	if !bytes.Contains(b, []byte(`"usage"`)) {
		return
	}
	var u usageBody
	if json.Unmarshal(b, &u) != nil || u.Usage == nil {
		return
	}
	if p := u.Usage.PromptTokens + u.Usage.InputTokens; p > 0 {
		s.prompt = p
	}
	if c := u.Usage.CompletionTokens + u.Usage.OutputTokens; c > 0 {
		s.completion = c
	}
}

func (s *usageSniffer) feed(b []byte) {
	if !s.sse {
		if len(s.buf)+len(b) <= 2<<20 {
			s.buf = append(s.buf, b...)
		}
		return
	}
	s.buf = append(s.buf, b...)
	for {
		i := bytes.IndexByte(s.buf, '\n')
		if i < 0 {
			break
		}
		line := bytes.TrimSpace(s.buf[:i])
		s.buf = s.buf[i+1:]
		if bytes.HasPrefix(line, []byte("data:")) {
			s.parse(bytes.TrimSpace(line[5:]))
		}
	}
	if len(s.buf) > 1<<20 { // a single absurdly long line: stop tracking it
		s.buf = s.buf[:0]
	}
}

func (s *usageSniffer) result() (int64, int64) {
	if !s.sse {
		s.parse(s.buf)
	}
	return s.prompt, s.completion
}

func main() {
	cfgPath := flag.String("config", "/etc/stick-router/config.json", "path to the JSON config file")
	check := flag.Bool("check", false, "validate the config and exit")
	showVersion := flag.Bool("version", false, "print the version and exit")
	flag.Parse()
	if *showVersion {
		fmt.Println("stick-router", version)
		return
	}
	cfg, err := loadConfig(*cfgPath)
	if err != nil {
		log.Fatalf("config: %v", err)
	}
	r, err := NewRouter(cfg)
	if err != nil {
		log.Fatalf("config: %v", err)
	}
	if *check {
		fmt.Printf("config ok: %d providers, %d routes, listen %s\n", len(r.providers), len(r.cfg.Routes), r.cfg.Listen)
		return
	}
	log.SetFlags(0) // systemd adds timestamps
	srv := &http.Server{Addr: r.cfg.Listen, Handler: r, ReadHeaderTimeout: 15 * time.Second}
	go func() {
		sig := make(chan os.Signal, 1)
		signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
		<-sig
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		srv.Shutdown(ctx)
	}()
	log.Printf("stick-router %s listening on %s (%d providers, %d routes)", version, r.cfg.Listen, len(r.providers), len(r.cfg.Routes))
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}
