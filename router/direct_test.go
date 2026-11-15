package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const logTool = `"tools":[{"type":"function","function":{"name":"log","parameters":{"type":"object"}}}]`

func directRouter(t *testing.T, u *httptest.Server) *httptest.Server {
	return newTestRouter(t, Config{
		Providers: []Provider{{Name: "a", BaseURL: u.URL + "/v1", FirstTool: "log", FirstToolArg: "text",
			FirstToolTrim: `^\[[^\]]*\]\s*`, ReplyGuard: true}},
		Routes: map[string][]Target{"default": {{Provider: "a", Model: "m"}}},
	})
}

func TestFirstToolIsCalledWithoutTheModel(t *testing.T) {
	hits := 0
	u := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { hits++ }))
	defer u.Close()
	s := directRouter(t, u)

	_, body := post(t, s.URL, "", `{"model":"default",`+logTool+`,"messages":[{"role":"system","content":"x"},{"role":"user","content":"[2026-11-15 16:55:27 +07:00] 100g rice, \"quoted\""}]}`)
	var c struct {
		Choices []struct {
			FinishReason string `json:"finish_reason"`
			Message      struct {
				ToolCalls []struct {
					Function struct{ Name, Arguments string } `json:"function"`
				} `json:"tool_calls"`
			} `json:"message"`
		} `json:"choices"`
	}
	if err := json.Unmarshal([]byte(body), &c); err != nil || len(c.Choices) != 1 || len(c.Choices[0].Message.ToolCalls) != 1 {
		t.Fatalf("body = %s", body)
	}
	call := c.Choices[0].Message.ToolCalls[0].Function
	var args map[string]string
	json.Unmarshal([]byte(call.Arguments), &args)
	if call.Name != "log" || args["text"] != `100g rice, "quoted"` || c.Choices[0].FinishReason != "tool_calls" {
		t.Fatalf("call = %+v args = %v", call, args)
	}

	resp, body := post(t, s.URL, "", `{"model":"default","stream":true,`+logTool+`,"messages":[{"role":"user","content":"hello"}]}`)
	if !strings.HasPrefix(resp.Header.Get("Content-Type"), "text/event-stream") || !strings.Contains(body, `"name":"log"`) ||
		!strings.Contains(body, `"finish_reason":"tool_calls"`) || !strings.HasSuffix(strings.TrimSpace(body), "data: [DONE]") {
		t.Fatalf("stream = %s", body)
	}
	if hits != 0 {
		t.Fatalf("the model was called %d times", hits)
	}
}

func TestDirectCallLeavesOtherTurnsToTheModel(t *testing.T) {
	u := newUpstream(t, okJSON)
	s := directRouter(t, u.Server)
	cases := []string{
		// the tool is not offered
		`{"model":"default","messages":[{"role":"user","content":"hi"}]}`,
		// content in parts (an image)
		`{"model":"default",` + logTool + `,"messages":[{"role":"user","content":[{"type":"text","text":"what is this"},{"type":"image_url","image_url":{"url":"data:,"}}]}]}`,
		// nothing left after the prefix is removed
		`{"model":"default",` + logTool + `,"messages":[{"role":"user","content":"[2026-11-15 16:55:27 +07:00] "}]}`,
		// the turn ends with a tool result that has no guard
		`{"model":"default",` + logTool + `,"messages":[{"role":"user","content":"hi"},{"role":"assistant","content":null},{"role":"tool","content":"{}"}]}`,
	}
	for i, body := range cases {
		before := u.hits
		if _, got := post(t, s.URL, "", body); u.hits != before+1 || !strings.Contains(got, `"hi"`) {
			t.Errorf("case %d: hits %d -> %d, body %s", i, before, u.hits, got)
		}
	}
}

func TestFirstToolTrimMustBeAValidPattern(t *testing.T) {
	_, err := NewRouter(Config{
		Providers: []Provider{{Name: "a", BaseURL: "http://x/v1", FirstTool: "log", FirstToolArg: "text", FirstToolTrim: "["}},
		Routes:    map[string][]Target{"default": {{Provider: "a", Model: "m"}}},
	})
	if err == nil {
		t.Fatal("an invalid first_tool_trim must be rejected")
	}
}
