package browserdriver

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

// cdpError is the error a Chrome DevTools Protocol command answers with.
type cdpError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

func (e *cdpError) Error() string { return fmt.Sprintf("CDP error %d: %s", e.Code, e.Message) }

type cdpMessage struct {
	ID     int             `json:"id"`
	Method string          `json:"method"`
	Params json.RawMessage `json:"params"`
	Result json.RawMessage `json:"result"`
	Error  *cdpError       `json:"error"`
}

// client is one DevTools WebSocket: commands out, results and events in.
type client struct {
	ws      *websocket.Conn
	sendMu  sync.Mutex
	mu      sync.Mutex
	nextID  int
	pending map[int]chan cdpMessage
	events  map[string][]func(json.RawMessage)
	closed  chan struct{}
	once    sync.Once
}

func dialCDP(ctx context.Context, url string) (*client, error) {
	dialer := websocket.Dialer{HandshakeTimeout: 20 * time.Second, EnableCompression: false}
	ws, _, err := dialer.DialContext(ctx, url, http.Header{})
	if err != nil {
		return nil, fmt.Errorf("cannot connect to DevTools at %s: %w", url, err)
	}
	ws.SetReadLimit(64 * 1024 * 1024)
	c := &client{
		ws:      ws,
		pending: map[int]chan cdpMessage{},
		events:  map[string][]func(json.RawMessage){},
		closed:  make(chan struct{}),
	}
	go c.read()
	return c, nil
}

func (c *client) read() {
	defer c.shutdown()
	for {
		_, payload, err := c.ws.ReadMessage()
		if err != nil {
			return
		}
		var message cdpMessage
		if err := json.Unmarshal(payload, &message); err != nil {
			continue
		}
		if message.ID != 0 {
			c.mu.Lock()
			waiter, ok := c.pending[message.ID]
			delete(c.pending, message.ID)
			c.mu.Unlock()
			if ok {
				waiter <- message
			}
			continue
		}
		c.mu.Lock()
		handlers := append([]func(json.RawMessage){}, c.events[message.Method]...)
		c.mu.Unlock()
		for _, handler := range handlers {
			go handler(message.Params)
		}
	}
}

func (c *client) shutdown() {
	c.once.Do(func() {
		close(c.closed)
		c.mu.Lock()
		for id, waiter := range c.pending {
			close(waiter)
			delete(c.pending, id)
		}
		c.mu.Unlock()
	})
}

// On registers a handler for an event; the handler gets the raw params.
func (c *client) On(event string, handler func(json.RawMessage)) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.events[event] = append(c.events[event], handler)
}

// Send runs one CDP command and returns its result.
func (c *client) Send(ctx context.Context, method string, params any) (json.RawMessage, error) {
	c.mu.Lock()
	c.nextID++
	id := c.nextID
	waiter := make(chan cdpMessage, 1)
	c.pending[id] = waiter
	c.mu.Unlock()

	payload := map[string]any{"id": id, "method": method}
	if params != nil {
		payload["params"] = params
	}
	encoded, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	c.sendMu.Lock()
	_ = c.ws.SetWriteDeadline(time.Now().Add(30 * time.Second))
	err = c.ws.WriteMessage(websocket.TextMessage, encoded)
	c.sendMu.Unlock()
	if err != nil {
		return nil, err
	}
	select {
	case message, ok := <-waiter:
		if !ok {
			return nil, errors.New("DevTools connection closed")
		}
		if message.Error != nil {
			return nil, message.Error
		}
		return message.Result, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-time.After(90 * time.Second):
		return nil, fmt.Errorf("DevTools did not answer %s in time", method)
	case <-c.closed:
		return nil, errors.New("DevTools connection closed")
	}
}

// Evaluate runs JavaScript in the page and returns its value as text (a JSON
// value is returned as JSON).
func (c *client) Evaluate(ctx context.Context, expression string) (string, error) {
	result, err := c.Send(ctx, "Runtime.evaluate", map[string]any{
		"expression":    expression,
		"returnByValue": true,
		"awaitPromise":  true,
		"userGesture":   true,
	})
	if err != nil {
		return "", err
	}
	var parsed struct {
		Result struct {
			Type        string          `json:"type"`
			Value       json.RawMessage `json:"value"`
			Description string          `json:"description"`
		} `json:"result"`
		ExceptionDetails *struct {
			Text      string `json:"text"`
			Exception struct {
				Description string `json:"description"`
			} `json:"exception"`
		} `json:"exceptionDetails"`
	}
	if err := json.Unmarshal(result, &parsed); err != nil {
		return "", err
	}
	if parsed.ExceptionDetails != nil {
		detail := parsed.ExceptionDetails.Exception.Description
		if detail == "" {
			detail = parsed.ExceptionDetails.Text
		}
		return "", fmt.Errorf("page script failed: %s", detail)
	}
	if parsed.Result.Type == "undefined" {
		return "", nil
	}
	if len(parsed.Result.Value) > 0 {
		var text string
		if err := json.Unmarshal(parsed.Result.Value, &text); err == nil {
			return text, nil
		}
		return string(parsed.Result.Value), nil
	}
	return parsed.Result.Description, nil
}

func (c *client) Close() {
	_ = c.ws.Close()
	c.shutdown()
}
