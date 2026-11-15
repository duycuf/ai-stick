// Reply guard: when a tool result says what the answer must be, the router makes it so.
//
// A small model follows "copy this text, add one sentence" most of the time, and that is not
// enough when the text carries numbers a person acts on. A tool that wants its answer protected
// puts two fields in the JSON it returns:
//
//	"reply": "Logged 260 kcal. Today 700/3000.",
//	"guard": {"mode": "reply_then", "max_extra": 1, "digits": "none", "ban": ["o'clock"]}
//
// With "reply_guard" set on the provider, a request whose last message is such a tool result is
// answered under these rules:
//
//	verbatim     the reply is sent as it is; the model is not asked at all
//	reply_then   the reply goes first, exactly; the model's own sentences follow, filtered
//	free         there is no fixed reply (a chat turn); the model's sentences are filtered
//
// Filtering works a sentence at a time. A sentence is dropped when it contains a banned word, or
// a number it should not contain: with "digits": "none" any digit, with "known" (the default) a
// number of two or more digits that appears neither in the tool result nor in the user's last
// message, with "any" nothing. Leaked <think> blocks are always removed. If the model answers
// with tool calls instead of text, the response passes through untouched.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"
	"unicode"
)

type guardRules struct {
	Mode     string   `json:"mode"`
	MaxExtra int      `json:"max_extra"` // most sentences the model may add; 0 means 8
	Digits   string   `json:"digits"`    // "none", "known" (default) or "any"
	Ban      []string `json:"ban"`       // a sentence containing one of these (case-insensitive) is dropped
	Fallback string   `json:"fallback"`  // "free" mode: sent when no sentence survives
	Ignore   []string `json:"ignore"`    // fields of the tool result whose numbers do not count as known
}

type replyGuard struct {
	rules  guardRules
	reply  string
	known  map[string]bool // numbers the model may repeat
	stream bool
}

var (
	numberRun  = regexp.MustCompile(`\d+(?:[.,]\d+)*`)
	thinkBlock = regexp.MustCompile(`(?s)<think>.*?</think>`)
)

func digitsOf(s string) string {
	return strings.NewReplacer(".", "", ",", "").Replace(s)
}

// messageText returns a message's content whether it is a string or a list of text parts.
func messageText(raw json.RawMessage) string {
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return s
	}
	var parts []struct {
		Text string `json:"text"`
	}
	if json.Unmarshal(raw, &parts) == nil {
		var b strings.Builder
		for _, p := range parts {
			b.WriteString(p.Text)
		}
		return b.String()
	}
	return ""
}

// guardedResult finds the JSON object carrying "guard" in a tool message. Some clients wrap the
// tool's text in an envelope ({"content": "..."}), so one level of nesting is tried too.
func guardedResult(text string) map[string]json.RawMessage {
	i := strings.Index(text, "{")
	if i < 0 {
		return nil
	}
	var obj map[string]json.RawMessage
	if json.Unmarshal([]byte(text[i:]), &obj) != nil {
		return nil
	}
	if _, ok := obj["guard"]; ok {
		return obj
	}
	var inner string
	if json.Unmarshal(obj["content"], &inner) == nil && inner != "" {
		var in map[string]json.RawMessage
		if json.Unmarshal([]byte(inner), &in) == nil {
			if _, ok := in["guard"]; ok {
				return in
			}
		}
	}
	return nil
}

// newReplyGuard returns the guard for this request, or nil when the conversation does not end
// with a tool result that asks for one.
func newReplyGuard(send map[string]json.RawMessage) *replyGuard {
	var msgs []struct {
		Role    string          `json:"role"`
		Content json.RawMessage `json:"content"`
	}
	if json.Unmarshal(send["messages"], &msgs) != nil || len(msgs) == 0 || msgs[len(msgs)-1].Role != "tool" {
		return nil
	}
	obj := guardedResult(messageText(msgs[len(msgs)-1].Content))
	if obj == nil {
		return nil
	}
	g := &replyGuard{known: map[string]bool{}}
	if json.Unmarshal(obj["guard"], &g.rules) != nil {
		return nil
	}
	switch g.rules.Mode {
	case "verbatim", "reply_then", "free":
	default:
		return nil
	}
	json.Unmarshal(obj["reply"], &g.reply)
	if g.rules.Mode != "free" && strings.TrimSpace(g.reply) == "" {
		return nil
	}
	skip := map[string]bool{"guard": true}
	for _, k := range g.rules.Ignore {
		skip[k] = true
	}
	for k, v := range obj {
		if !skip[k] {
			for _, n := range numberRun.FindAllString(string(v), -1) {
				g.known[digitsOf(n)] = true
			}
		}
	}
	for i := len(msgs) - 1; i >= 0; i-- {
		if msgs[i].Role == "user" {
			for _, n := range numberRun.FindAllString(messageText(msgs[i].Content), -1) {
				g.known[digitsOf(n)] = true
			}
			break
		}
	}
	json.Unmarshal(send["stream"], &g.stream)
	return g
}

// stripThink removes reasoning that leaked into the answer: whole <think> blocks, everything up
// to a closing tag whose opening tag was cut off, and an opening tag that never closes.
func stripThink(s string) string {
	s = thinkBlock.ReplaceAllString(s, "")
	if i := strings.LastIndex(s, "</think>"); i >= 0 {
		s = s[i+len("</think>"):]
	}
	if i := strings.Index(s, "<think>"); i >= 0 {
		s = s[:i]
	}
	return strings.TrimSpace(s)
}

// splitSentences cuts after . ! ? … when white space or the end follows, and at line breaks.
// "3.000" and "65,2" stay whole. Each piece keeps its trailing white space.
func splitSentences(s string) []string {
	var out []string
	r := []rune(s)
	start := 0
	for i := 0; i < len(r); i++ {
		end := false
		switch r[i] {
		case '\n':
			end = true
		case '.', '!', '?', '…':
			end = i+1 == len(r) || unicode.IsSpace(r[i+1])
		}
		if !end {
			continue
		}
		j := i + 1
		for j < len(r) && unicode.IsSpace(r[j]) {
			j++
		}
		out = append(out, string(r[start:j]))
		start, i = j, j-1
	}
	if start < len(r) {
		out = append(out, string(r[start:]))
	}
	return out
}

func (g *replyGuard) allowed(sentence string) bool {
	low := strings.ToLower(sentence)
	for _, b := range g.rules.Ban {
		if b != "" && strings.Contains(low, strings.ToLower(b)) {
			return false
		}
	}
	switch g.rules.Digits {
	case "any":
		return true
	case "none":
		return !strings.ContainsFunc(sentence, unicode.IsDigit)
	}
	for _, n := range numberRun.FindAllString(sentence, -1) {
		if d := digitsOf(n); len(d) >= 2 && !g.known[d] {
			return false
		}
	}
	return true
}

func (g *replyGuard) filter(text string) string {
	limit := g.rules.MaxExtra
	if limit <= 0 {
		limit = 8
	}
	var b strings.Builder
	kept := 0
	for _, s := range splitSentences(text) {
		if strings.TrimSpace(s) == "" {
			b.WriteString(s)
			continue
		}
		if kept < limit && g.allowed(s) {
			b.WriteString(s)
			kept++
		}
	}
	return strings.TrimSpace(b.String())
}

// afterReply returns what the model wrote after its own copy of the reply. Models change the
// spacing and add quotes or bold marks when they copy, so those are skipped while comparing.
// When the text does not start with the reply, all of it is returned.
func afterReply(text, reply string) string {
	skip := func(r rune) bool { return unicode.IsSpace(r) || strings.ContainsRune("\"'“”‘’*`>", r) }
	t, want := []rune(text), []rune(reply)
	i, j := 0, 0
	for j < len(want) {
		if skip(want[j]) {
			j++
			continue
		}
		for i < len(t) && skip(t[i]) {
			i++
		}
		if i == len(t) || t[i] != want[j] {
			return text
		}
		i++
		j++
	}
	return string(t[i:])
}

// apply turns the model's text into the answer the client receives.
func (g *replyGuard) apply(text string) string {
	text = stripThink(text)
	switch g.rules.Mode {
	case "verbatim":
		return g.reply
	case "reply_then":
		extra := g.filter(strings.TrimLeft(afterReply(text, g.reply), "\"'“”‘’*` \t\r\n"))
		if extra == "" {
			return g.reply
		}
		return g.reply + "\n\n" + extra
	}
	if out := g.filter(text); out != "" {
		return out
	}
	if g.rules.Fallback != "" {
		return g.rules.Fallback
	}
	return text
}

// completion is the part of a chat completion (whole, or one stream chunk) the guard reads.
type completion struct {
	ID      string `json:"id"`
	Model   string `json:"model"`
	Created int64  `json:"created"`
	Choices []struct {
		Delta   *completionMessage `json:"delta"`
		Message *completionMessage `json:"message"`
	} `json:"choices"`
	Usage json.RawMessage `json:"usage"`
}

type completionMessage struct {
	Content   *string         `json:"content"`
	ToolCalls json.RawMessage `json:"tool_calls"`
}

func hasToolCalls(raw json.RawMessage) bool {
	t := bytes.TrimSpace(raw)
	return len(t) > 0 && !bytes.Equal(t, []byte("null")) && !bytes.Equal(t, []byte("[]"))
}

// writeAnswer sends text as a finished assistant message, as a stream or as one JSON body.
func (g *replyGuard) writeAnswer(w http.ResponseWriter, id, model string, created int64, usage json.RawMessage, text string) {
	if id == "" {
		id = fmt.Sprintf("chatcmpl-guard-%d", time.Now().UnixNano())
	}
	if created == 0 {
		created = time.Now().Unix()
	}
	if len(bytes.TrimSpace(usage)) == 0 || bytes.Equal(bytes.TrimSpace(usage), []byte("null")) {
		usage = json.RawMessage(`{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}`)
	}
	if !g.stream {
		body, _ := json.Marshal(map[string]any{
			"id": id, "object": "chat.completion", "created": created, "model": model, "usage": usage,
			"choices": []any{map[string]any{"index": 0, "finish_reason": "stop",
				"message": map[string]any{"role": "assistant", "content": text}}}})
		w.Write(append(body, '\n'))
		return
	}
	chunk := func(delta map[string]any, finish any, withUsage bool) {
		c := map[string]any{"id": id, "object": "chat.completion.chunk", "created": created, "model": model,
			"choices": []any{map[string]any{"index": 0, "delta": delta, "finish_reason": finish}}}
		if withUsage {
			c["usage"] = usage
		}
		b, _ := json.Marshal(c)
		fmt.Fprintf(w, "data: %s\n\n", b)
	}
	chunk(map[string]any{"role": "assistant", "content": text}, nil, false)
	chunk(map[string]any{}, "stop", true)
	io.WriteString(w, "data: [DONE]\n\n")
	if f, ok := w.(http.Flusher); ok {
		f.Flush()
	}
}

// answerWithoutModel serves a verbatim reply directly: nothing the model could add is wanted.
func (g *replyGuard) answerWithoutModel(w http.ResponseWriter, model string) {
	if g.stream {
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-cache")
	} else {
		w.Header().Set("Content-Type", "application/json")
	}
	w.WriteHeader(http.StatusOK)
	g.writeAnswer(w, "", model, 0, nil, g.reply)
}

// rewrite reads the provider's whole answer, then sends the guarded version. An answer made of
// tool calls, or one the guard cannot read, is passed on as it came.
func (g *replyGuard) rewrite(w http.ResponseWriter, src io.Reader, sniff *usageSniffer) {
	body, _ := io.ReadAll(io.LimitReader(src, 4<<20))
	sniff.feed(body)
	var text strings.Builder
	var first completion
	var usage json.RawMessage
	tools, seen := false, false
	take := func(raw []byte) {
		var c completion
		if json.Unmarshal(raw, &c) != nil {
			return
		}
		if !seen {
			first, seen = c, true
		}
		if len(c.Usage) > 0 && !bytes.Equal(c.Usage, []byte("null")) {
			usage = c.Usage
		}
		for _, ch := range c.Choices {
			for _, m := range []*completionMessage{ch.Delta, ch.Message} {
				if m == nil {
					continue
				}
				if hasToolCalls(m.ToolCalls) {
					tools = true
				}
				if m.Content != nil {
					text.WriteString(*m.Content)
				}
			}
		}
	}
	if sniff.sse {
		for _, line := range bytes.Split(body, []byte("\n")) {
			line = bytes.TrimSpace(line)
			if bytes.HasPrefix(line, []byte("data:")) {
				if data := bytes.TrimSpace(line[5:]); !bytes.Equal(data, []byte("[DONE]")) {
					take(data)
				}
			}
		}
	} else {
		take(body)
	}
	if tools || !seen {
		w.Write(body)
		return
	}
	g.stream = sniff.sse
	g.writeAnswer(w, first.ID, first.Model, first.Created, usage, g.apply(text.String()))
}
