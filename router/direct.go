// Direct first tool: when the first tool always receives the user's message as it is, the model
// has nothing to decide on that turn. With "first_tool_arg" set, the router writes the tool call
// itself instead of asking the model for it:
//
//   - the tool gets the user's exact words, not the model's copy of them
//   - the turn costs no tokens and takes no time
//   - together with a verbatim reply guard, a whole exchange needs no model at all, so it keeps
//     working when every provider is down
//
// Messages that are not plain text (an image, for example) still go to the model.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"
)

// directToolCall returns the user's text when the conversation ends with a plain-text user
// message and the request offers the named tool.
func directToolCall(send map[string]json.RawMessage, name string, trim *regexp.Regexp) (string, bool) {
	var msgs []struct {
		Role    string          `json:"role"`
		Content json.RawMessage `json:"content"`
	}
	if json.Unmarshal(send["messages"], &msgs) != nil || len(msgs) == 0 || msgs[len(msgs)-1].Role != "user" {
		return "", false
	}
	var text string
	if json.Unmarshal(msgs[len(msgs)-1].Content, &text) != nil {
		return "", false // content in parts: let the model look at it
	}
	var tools []struct {
		Function struct {
			Name string `json:"name"`
		} `json:"function"`
	}
	if json.Unmarshal(send["tools"], &tools) != nil {
		return "", false
	}
	offered := false
	for _, t := range tools {
		offered = offered || t.Function.Name == name
	}
	if !offered {
		return "", false
	}
	if trim != nil {
		text = trim.ReplaceAllString(text, "")
	}
	text = strings.TrimSpace(text)
	return text, text != ""
}

// writeToolCall answers the request with one call to the tool, as a stream or as one JSON body.
func writeToolCall(w http.ResponseWriter, stream bool, model, name, arg, text string) {
	args, _ := json.Marshal(map[string]string{arg: text})
	now := time.Now()
	id := fmt.Sprintf("chatcmpl-direct-%d", now.UnixNano())
	call := map[string]any{"id": fmt.Sprintf("call-direct-%d", now.UnixNano()), "type": "function",
		"function": map[string]string{"name": name, "arguments": string(args)}}
	usage := map[string]int{"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
	if !stream {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		body, _ := json.Marshal(map[string]any{
			"id": id, "object": "chat.completion", "created": now.Unix(), "model": model, "usage": usage,
			"choices": []any{map[string]any{"index": 0, "finish_reason": "tool_calls",
				"message": map[string]any{"role": "assistant", "content": "", "tool_calls": []any{call}}}}})
		w.Write(append(body, '\n'))
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.WriteHeader(http.StatusOK)
	chunk := func(delta map[string]any, finish any, withUsage bool) {
		c := map[string]any{"id": id, "object": "chat.completion.chunk", "created": now.Unix(), "model": model,
			"choices": []any{map[string]any{"index": 0, "delta": delta, "finish_reason": finish}}}
		if withUsage {
			c["usage"] = usage
		}
		b, _ := json.Marshal(c)
		fmt.Fprintf(w, "data: %s\n\n", b)
	}
	call["index"] = 0
	chunk(map[string]any{"role": "assistant", "content": "", "tool_calls": []any{call}}, nil, false)
	chunk(map[string]any{}, "tool_calls", true)
	io.WriteString(w, "data: [DONE]\n\n")
	if f, ok := w.(http.Flusher); ok {
		f.Flush()
	}
}
