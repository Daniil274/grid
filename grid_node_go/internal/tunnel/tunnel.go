// Package tunnel carries a node's channels over one WebSocket, exactly as
// grid_node/tunnel.py does: JSON text frames {"ch": channel, "d": data} on
// "mcp" (JSON-RPC both ways), "ctl" (hello, ping, bye, update) and "view"
// (the live picture of the screen and a person's input).
package tunnel

import (
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

// Channels of a tunnel.
const (
	MCP  = "mcp"
	CTL  = "ctl"
	VIEW = "view"
)

// MaxFrame is the largest frame either side accepts (a screenshot, encoded).
const MaxFrame = 48 * 1024 * 1024

// ErrClosed says the other side went away.
var ErrClosed = errors.New("tunnel closed")

// Handler takes one frame's data of its channel.
type Handler func(data json.RawMessage)

// Conn is one tunnel: a WebSocket, its frames, and who reads which channel.
type Conn struct {
	ws       *websocket.Conn
	sendMu   sync.Mutex
	handlers sync.Map // channel -> Handler
	closed   chan struct{}
	once     sync.Once
	Log      *log.Logger
}

// New wraps a WebSocket as a tunnel.
func New(ws *websocket.Conn) *Conn {
	ws.SetReadLimit(MaxFrame)
	return &Conn{ws: ws, closed: make(chan struct{})}
}

// On hands every frame of channel to handler.
func (c *Conn) On(channel string, handler Handler) {
	c.handlers.Store(channel, handler)
}

func (c *Conn) handler(channel string) (Handler, bool) {
	value, ok := c.handlers.Load(channel)
	if !ok {
		return nil, false
	}
	handler, ok := value.(Handler)
	return handler, ok
}

type frame struct {
	Channel string          `json:"ch"`
	Data    json.RawMessage `json:"d"`
}

// Send writes one frame of channel.
func (c *Conn) Send(channel string, data any) error {
	payload, err := json.Marshal(struct {
		Channel string `json:"ch"`
		Data    any    `json:"d"`
	}{Channel: channel, Data: data})
	if err != nil {
		return err
	}
	if len(payload) > MaxFrame {
		return fmt.Errorf("frame of %d bytes is larger than %d", len(payload), MaxFrame)
	}
	c.sendMu.Lock()
	defer c.sendMu.Unlock()
	select {
	case <-c.closed:
		return ErrClosed
	default:
	}
	if err := c.ws.SetWriteDeadline(time.Now().Add(30 * time.Second)); err != nil {
		return err
	}
	return c.ws.WriteMessage(websocket.TextMessage, payload)
}

// Run reads frames until the connection closes. MCP frames go to onMCP;
// handlers of the other channels are called as the frames come.
func (c *Conn) Run(onMCP func(payload json.RawMessage)) error {
	defer c.markClosed()
	for {
		kind, message, err := c.ws.ReadMessage()
		if err != nil {
			var closed *websocket.CloseError
			if errors.As(err, &closed) {
				return fmt.Errorf("closed: %d %s", closed.Code, closed.Text)
			}
			return err
		}
		if kind != websocket.TextMessage && kind != websocket.BinaryMessage {
			continue
		}
		if len(message) > MaxFrame {
			c.logf("Dropped a frame of %d bytes", len(message))
			continue
		}
		var parsed frame
		if err := json.Unmarshal(message, &parsed); err != nil || parsed.Channel == "" {
			c.logf("Dropped a malformed frame")
			continue
		}
		switch parsed.Channel {
		case MCP:
			if onMCP != nil {
				onMCP(parsed.Data)
			}
		default:
			handler, ok := c.handler(parsed.Channel)
			if !ok {
				continue
			}
			go c.call(parsed.Channel, handler, parsed.Data)
		}
	}
}

func (c *Conn) call(channel string, handler Handler, data json.RawMessage) {
	defer func() {
		if recovered := recover(); recovered != nil {
			c.logf("Handler of %s panicked: %v", channel, recovered)
		}
	}()
	handler(data)
}

// Closed is closed once the other side went away.
func (c *Conn) Closed() <-chan struct{} { return c.closed }

// IsClosed reports whether the tunnel is gone.
func (c *Conn) IsClosed() bool {
	select {
	case <-c.closed:
		return true
	default:
		return false
	}
}

// Close says bye (best effort) and closes the WebSocket.
func (c *Conn) Close(reason string) {
	if c.IsClosed() {
		return
	}
	if reason != "" {
		_ = c.Send(CTL, map[string]any{"type": "bye", "reason": reason})
	}
	_ = c.ws.Close()
	c.markClosed()
}

// Wait blocks until the tunnel is closed or done is closed.
func (c *Conn) Wait(done <-chan struct{}) error {
	select {
	case <-c.closed:
		return ErrClosed
	case <-done:
		return nil
	}
}

func (c *Conn) markClosed() {
	c.once.Do(func() { close(c.closed) })
}

func (c *Conn) logf(format string, args ...any) {
	if c.Log != nil {
		c.Log.Printf(format, args...)
	}
}
