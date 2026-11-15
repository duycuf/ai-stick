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

// toolTurn builds a request body that ends with a tool result carrying the given JSON.
func toolTurn(t *testing.T, user string, result map[string]any, stream bool) string {
	t.Helper()
	res, _ := json.Marshal(result)
	body, _ := json.Marshal(map[string]any{
		"model": "default", "stream": stream,
		"messages": []any{
			map[string]any{"role": "user", "content": user},
			map[string]any{"role": "assistant", "content": nil, "tool_calls": []any{map[string]any{
				"id": "c1", "type": "function", "function": map[string]any{"name": "log", "arguments": "{}"}}}},
			map[string]any{"role": "tool", "tool_call_id": "c1", "content": string(res)},
		},
	})
	return string(body)
}

func guardFor(t *testing.T, user string, result map[string]any) *replyGuard {
	t.Helper()
	var send map[string]json.RawMessage
	if err := json.Unmarshal([]byte(toolTurn(t, user, result, false)), &send); err != nil {
		t.Fatal(err)
	}
	return newReplyGuard(send)
}

func TestGuardIsOffWithoutAGuardField(t *testing.T) {
	if g := guardFor(t, "hi", map[string]any{"reply": "Logged."}); g != nil {
		t.Fatal("a tool result without guard must not be guarded")
	}
	var send map[string]json.RawMessage
	json.Unmarshal([]byte(`{"messages":[{"role":"user","content":"hi"}]}`), &send)
	if newReplyGuard(send) != nil {
		t.Fatal("a turn that ends with a user message must not be guarded")
	}
	if g := guardFor(t, "hi", map[string]any{"reply": "", "guard": map[string]any{"mode": "verbatim"}}); g != nil {
		t.Fatal("verbatim with an empty reply must not be guarded")
	}
}

func TestGuardReadsAnEnvelopedToolResult(t *testing.T) {
	inner, _ := json.Marshal(map[string]any{"reply": "Logged.", "guard": map[string]any{"mode": "verbatim"}})
	var send map[string]json.RawMessage
	body, _ := json.Marshal(map[string]any{"messages": []any{
		map[string]any{"role": "user", "content": "x"},
		map[string]any{"role": "tool", "content": []any{map[string]any{"type": "text",
			"text": `{"attachments":[],"content":` + string(mustJSON(string(inner))) + `}`}}},
	}})
	json.Unmarshal(body, &send)
	g := newReplyGuard(send)
	if g == nil || g.reply != "Logged." {
		t.Fatalf("guard = %+v", g)
	}
}

func mustJSON(v any) []byte {
	b, _ := json.Marshal(v)
	return b
}

func TestReplyThenKeepsTheReplyAndFiltersTheRest(t *testing.T) {
	reply := "Logged 260 kcal. Today 1.400/3.000 kcal."
	g := guardFor(t, "dinner 200g rice", map[string]any{"reply": reply, "hint": "it is 21:30",
		"guard": map[string]any{"mode": "reply_then", "max_extra": 1, "digits": "none", "ban": []string{"o'clock"}, "ignore": []string{"hint"}}})
	cases := []struct{ model, want string }{
		// a faithful copy plus one clean sentence
		{reply + "\n\nThat is a small dinner for a big goal.", reply + "\n\nThat is a small dinner for a big goal."},
		// the copy was reformatted and quoted: still recognised
		{"\"Logged 260 kcal.  Today 1.400/3.000 kcal.\" Eat more.", reply + "\n\nEat more."},
		// a sentence with a number is dropped, the next clean one is kept
		{reply + " You need at least 1.500 kcal tonight. Go and eat.", reply + "\n\nGo and eat."},
		// a banned word
		{reply + " It is nine o'clock already.", reply},
		// only one extra sentence
		{reply + " First. Second. Third.", reply + "\n\nFirst."},
		// the model changed a number while copying: its version never reaches the client
		{"Logged 280 kcal. Today 1.400/3.000 kcal. Eat.", reply + "\n\nEat."},
		// leaked reasoning
		{"<think>let me see</think>" + reply + " Fine.", reply + "\n\nFine."},
		{"reasoning...</think>" + reply, reply},
	}
	for _, c := range cases {
		if got := g.apply(c.model); got != c.want {
			t.Errorf("apply(%q)\n got  %q\n want %q", c.model, got, c.want)
		}
	}
}

func TestFreeModeDropsInventedNumbers(t *testing.T) {
	g := guardFor(t, "how long should I rest, I slept 6 hours", map[string]any{"reply": "",
		"facts":    []string{"Rest 2 to 3 minutes on big lifts, 60 to 90 seconds on small ones."},
		"schedule": "it is 20:05",
		"guard":    map[string]any{"mode": "free", "fallback": "I have no solid number for that.", "ignore": []string{"schedule"}}})
	got := g.apply("Rest 2 to 3 minutes on big lifts. On small ones 60 to 90 seconds. Sleep at least 480 minutes. It is 20:05 now.")
	want := "Rest 2 to 3 minutes on big lifts. On small ones 60 to 90 seconds."
	if got != want {
		t.Errorf("got %q\nwant %q", got, want)
	}
	if got := g.apply("You need 1.500 kcal."); got != "I have no solid number for that." {
		t.Errorf("fallback: got %q", got)
	}
	// single digits and numbers the user typed are fine
	if got := g.apply("With 6 hours of sleep, keep the weight."); got != "With 6 hours of sleep, keep the weight." {
		t.Errorf("got %q", got)
	}
}

func TestSplitSentencesKeepsNumbersWhole(t *testing.T) {
	got := splitSentences("Today 1.400/3.000 kcal, 65,2 kg. Next! Line one\nLine two")
	want := []string{"Today 1.400/3.000 kcal, 65,2 kg. ", "Next! ", "Line one\n", "Line two"}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Errorf("got %q", got)
	}
}

func guardedRouter(t *testing.T, u *httptest.Server) *httptest.Server {
	return newTestRouter(t, Config{
		Providers: []Provider{{Name: "a", BaseURL: u.URL + "/v1", ReplyGuard: true}},
		Routes:    map[string][]Target{"default": {{Provider: "a", Model: "m"}}},
	})
}

func TestVerbatimReplyIsServedWithoutCallingTheModel(t *testing.T) {
	hits := 0
	u := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { hits++ }))
	defer u.Close()
	s := guardedRouter(t, u)
	result := map[string]any{"reply": "Stop and rest. Call the emergency number if it gets worse.", "guard": map[string]any{"mode": "verbatim"}}

	_, body := post(t, s.URL, "", toolTurn(t, "chest pain", result, false))
	var c struct {
		Choices []struct {
			Message struct{ Content string } `json:"message"`
		} `json:"choices"`
	}
	if json.Unmarshal([]byte(body), &c) != nil || len(c.Choices) != 1 || c.Choices[0].Message.Content != result["reply"] {
		t.Fatalf("body = %s", body)
	}
	resp, body := post(t, s.URL, "", toolTurn(t, "chest pain", result, true))
	if !strings.HasPrefix(resp.Header.Get("Content-Type"), "text/event-stream") ||
		!strings.Contains(body, "Stop and rest.") || !strings.HasSuffix(strings.TrimSpace(body), "data: [DONE]") {
		t.Fatalf("stream = %q", body)
	}
	if hits != 0 {
		t.Fatalf("the model was called %d times", hits)
	}
}

func TestStreamedAnswerIsRewritten(t *testing.T) {
	reply := "Logged 260 kcal."
	u := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		for _, part := range []string{"</think>Logged 2", "60 kcal. ", "It is 18:30, eat 1.500 more. ", "Go eat."} {
			b, _ := json.Marshal(map[string]any{"id": "x1", "model": "m", "choices": []any{map[string]any{"delta": map[string]any{"content": part}}}})
			fmt.Fprintf(w, "data: %s\n\n", b)
		}
		io.WriteString(w, `data: {"id":"x1","model":"m","choices":[],"usage":{"prompt_tokens":40,"completion_tokens":9}}`+"\n\ndata: [DONE]\n\n")
	}))
	defer u.Close()
	s := guardedRouter(t, u)
	result := map[string]any{"reply": reply, "guard": map[string]any{"mode": "reply_then", "digits": "none"}}
	_, body := post(t, s.URL, "", toolTurn(t, "200g rice", result, true))
	var text strings.Builder
	for _, line := range strings.Split(body, "\n") {
		var c completion
		if strings.HasPrefix(line, "data: {") && json.Unmarshal([]byte(line[6:]), &c) == nil {
			for _, ch := range c.Choices {
				if ch.Delta != nil && ch.Delta.Content != nil {
					text.WriteString(*ch.Delta.Content)
				}
			}
		}
	}
	if text.String() != reply+"\n\nGo eat." {
		t.Fatalf("client saw %q\nraw: %s", text.String(), body)
	}
	if !strings.Contains(body, `"prompt_tokens":40`) {
		t.Fatalf("usage was lost: %s", body)
	}
}

func TestToolCallAnswerPassesThrough(t *testing.T) {
	const upstreamBody = `{"id":"x","model":"m","choices":[{"message":{"content":null,"tool_calls":[{"id":"c2","type":"function","function":{"name":"estimate","arguments":"{}"}}]}}]}`
	u := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, upstreamBody)
	}))
	defer u.Close()
	s := guardedRouter(t, u)
	result := map[string]any{"reply": "", "guard": map[string]any{"mode": "free"}}
	_, body := post(t, s.URL, "", toolTurn(t, "2 bowls", result, false))
	if body != upstreamBody {
		t.Fatalf("body = %s", body)
	}
}

func TestGuardNeedsTheProviderSwitch(t *testing.T) {
	u := newUpstream(t, okJSON)
	s := newTestRouter(t, Config{
		Providers: []Provider{{Name: "a", BaseURL: u.URL + "/v1"}},
		Routes:    map[string][]Target{"default": {{Provider: "a", Model: "m"}}},
	})
	result := map[string]any{"reply": "Logged.", "guard": map[string]any{"mode": "verbatim"}}
	_, body := post(t, s.URL, "", toolTurn(t, "x", result, false))
	if u.hits != 1 || !strings.Contains(body, `"hi"`) {
		t.Fatalf("hits=%d body=%s", u.hits, body)
	}
}
