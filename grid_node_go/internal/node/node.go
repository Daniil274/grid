// Package node assembles a device's tools into one MCP server and serves it on
// a tunnel: the hello that names the device, the tools of its drivers, the
// live view and the control channel.
package node

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/gorilla/websocket"

	"gridnode/internal/mcp"
	"gridnode/internal/protocol"
	"gridnode/internal/state"
	"gridnode/internal/tunnel"
)

// Version is what the node says it is in its hello and in MCP.
const Version = "0.3.0-go"

// Node is a device and its tools, ready to serve on a tunnel.
type Node struct {
	Name           string
	Kind           string
	Platform       string
	Host           string
	DefaultWorkdir string
	StateDir       string
	Capabilities   []string
	Tools          []*protocol.Tool
	View           bool
	ViewStart      func(ctx context.Context, sink func(map[string]any)) error
	ViewStop       func(ctx context.Context)
	ViewInput      func(ctx context.Context, event map[string]any) error
	Update         func(ctx context.Context, url, sha256 string) error
	OnClose        func()
	Log            *log.Logger

	server *mcp.Server
}

// Prepare fills the defaults and builds the MCP server.
func (n *Node) Prepare() error {
	if n.Log == nil {
		n.Log = log.Default()
	}
	if n.Name == "" {
		host, err := os.Hostname()
		if err == nil {
			n.Name = host
		} else {
			n.Name = "grid-node"
		}
	}
	if n.Kind == "" {
		n.Kind = "desktop"
	}
	if n.Platform == "" {
		n.Platform = runtime.GOOS + " " + runtime.GOARCH
	}
	if n.Host == "" {
		n.Host, _ = os.Hostname()
	}
	if n.DefaultWorkdir == "" {
		n.DefaultWorkdir = state.DefaultWorkdir()
	}
	if n.StateDir == "" {
		n.StateDir = state.DefaultStateDir()
	}
	if n.Capabilities == nil {
		n.Capabilities = []string{}
	}
	seen := map[string]bool{}
	tools := make([]*protocol.Tool, 0, len(n.Tools))
	for _, tool := range n.Tools {
		if tool == nil || seen[tool.Name] {
			return fmt.Errorf("two drivers bring a tool named %q", tool.Name)
		}
		seen[tool.Name] = true
		tools = append(tools, tool)
	}
	n.Tools = tools
	n.server = &mcp.Server{Name: "grid-node", Version: Version, Tools: n.Tools, Log: n.Log}
	return nil
}

// Hello is the ctl frame that opens a connection.
func (n *Node) Hello() map[string]any {
	return map[string]any{
		"type":            "hello",
		"node":            Version,
		"name":            n.Name,
		"kind":            n.Kind,
		"platform":        n.Platform,
		"host":            n.Host,
		"capabilities":    n.Capabilities,
		"view":            n.View,
		"default_workdir": n.DefaultWorkdir,
	}
}

// Serve runs the node on one connection until it closes or ctx ends.
func (n *Node) Serve(ctx context.Context, conn *tunnel.Conn) error {
	if n.server == nil {
		if err := n.Prepare(); err != nil {
			return err
		}
	}
	callCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	conn.On(tunnel.CTL, func(raw json.RawMessage) {
		var message struct {
			Type   string `json:"type"`
			URL    string `json:"url"`
			SHA256 string `json:"sha256"`
		}
		if err := json.Unmarshal(raw, &message); err != nil {
			n.logf("bad ctl frame: %v", err)
			return
		}
		switch message.Type {
		case "ping":
			_ = conn.Send(tunnel.CTL, map[string]any{"type": "pong"})
		case "update":
			if n.Update == nil {
				n.logf("asked to update, but this build cannot update itself")
				return
			}
			go func() {
				n.logf("updating from %s", message.URL)
				if err := n.Update(ctx, message.URL, message.SHA256); err != nil {
					n.logf("update failed: %v", err)
				}
			}()
		case "bye":
			conn.Close("")
		}
	})
	conn.On(tunnel.VIEW, func(raw json.RawMessage) {
		var message map[string]any
		if err := json.Unmarshal(raw, &message); err != nil {
			return
		}
		kind := protocol.String(message, "type", "")
		switch kind {
		case "watch":
			want := protocol.Bool(message, "on", false)
			if want {
				if n.ViewStart == nil {
					return
				}
				if err := n.ViewStart(callCtx, func(frame map[string]any) {
					if err := conn.Send(tunnel.VIEW, frame); err != nil {
						n.logf("cannot send a view frame: %v", err)
					}
				}); err != nil {
					n.logf("cannot start the view: %v", err)
				}
			} else if n.ViewStop != nil {
				n.ViewStop(callCtx)
			}
		case "input":
			if n.ViewInput != nil {
				if err := n.ViewInput(callCtx, message); err != nil {
					n.logf("cannot apply input: %v", err)
				}
			}
		}
	})

	if err := conn.Send(tunnel.CTL, n.Hello()); err != nil {
		return err
	}
	readErr := make(chan error, 1)
	go func() {
		readErr <- conn.Run(func(payload json.RawMessage) {
			copied := append(json.RawMessage(nil), payload...)
			go func() {
				answer := n.server.Handle(callCtx, copied)
				if answer == nil {
					return
				}
				if err := conn.Send(tunnel.MCP, json.RawMessage(answer)); err != nil {
					n.logf("cannot send an MCP answer: %v", err)
				}
			}()
		})
	}()
	var err error
	select {
	case err = <-readErr:
	case <-ctx.Done():
		err = nil
	}
	cancel()
	if n.OnClose != nil {
		n.OnClose()
	}
	conn.Close("")
	if err != nil && ctx.Err() == nil {
		return err
	}
	return nil
}

func (n *Node) logf(format string, args ...any) {
	if n.Log != nil {
		n.Log.Printf(format, args...)
	}
}

// connectURL turns a server's http(s) address into its tunnel URL.
func connectURL(server string) string {
	base := strings.TrimRight(strings.TrimSpace(server), "/")
	if strings.HasSuffix(base, "/node/connect") {
		base = strings.TrimSuffix(base, "/node/connect")
	}
	switch {
	case strings.HasPrefix(base, "https://"):
		return "wss://" + strings.TrimPrefix(base, "https://") + "/node/connect"
	case strings.HasPrefix(base, "http://"):
		return "ws://" + strings.TrimPrefix(base, "http://") + "/node/connect"
	case strings.HasPrefix(base, "wss://"), strings.HasPrefix(base, "ws://"):
		return base + "/node/connect"
	default:
		return base + "/node/connect"
	}
}

// Dial keeps the node connected to the server, calling back after every drop.
func (n *Node) Dial(ctx context.Context, server, token string) error {
	if n.server == nil {
		if err := n.Prepare(); err != nil {
			return err
		}
	}
	url := connectURL(server)
	dialer := websocket.Dialer{
		HandshakeTimeout:  30 * time.Second,
		EnableCompression: false,
		Proxy:             http.ProxyFromEnvironment,
	}
	delay := time.Second
	for ctx.Err() == nil {
		headers := http.Header{}
		headers.Set("Authorization", "Bearer "+token)
		headers.Set("User-Agent", "grid-node/"+Version)
		connection, response, err := dialer.DialContext(ctx, url, headers)
		if err == nil {
			n.logf("connected to %s as %s", url, n.Name)
			delay = time.Second
			started := time.Now()
			serveErr := n.Serve(ctx, tunnel.New(connection))
			if ctx.Err() != nil {
				return nil
			}
			if serveErr != nil {
				n.logf("connection ended: %v", serveErr)
			}
			if time.Since(started) > 30*time.Second {
				delay = time.Second
			}
		} else {
			status := ""
			if response != nil {
				status = fmt.Sprintf(" (HTTP %d)", response.StatusCode)
			}
			n.logf("connection to %s failed%s: %v", url, status, err)
		}
		sleep := delay + time.Duration(time.Now().UnixNano()%int64(time.Second))
		select {
		case <-time.After(sleep):
		case <-ctx.Done():
			return nil
		}
		if delay < 60*time.Second {
			delay *= 2
			if delay > 60*time.Second {
				delay = 60 * time.Second
			}
		}
	}
	return nil
}

// Listen serves the node to the server that connects with token, one
// connection at a time.
func (n *Node) Listen(ctx context.Context, address, token string) error {
	if n.server == nil {
		if err := n.Prepare(); err != nil {
			return err
		}
	}
	if token == "" {
		token = os.Getenv("GRID_DEVICE_TOKEN")
	}
	if token == "" {
		return fmt.Errorf("--listen needs --token (or GRID_DEVICE_TOKEN) to check the server's bearer token")
	}
	var busy sync.Mutex
	upgrader := websocket.Upgrader{
		ReadBufferSize:  64 * 1024,
		WriteBufferSize: 64 * 1024,
		CheckOrigin:     func(*http.Request) bool { return true },
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/node/connect", func(writer http.ResponseWriter, request *http.Request) {
		offered := bearer(request)
		if !secureEqual(offered, token) {
			http.Error(writer, "unauthorized", http.StatusUnauthorized)
			return
		}
		if !busy.TryLock() {
			writer.Header().Set("Retry-After", "5")
			http.Error(writer, "already connected", http.StatusConflict)
			return
		}
		defer busy.Unlock()
		connection, err := upgrader.Upgrade(writer, request, nil)
		if err != nil {
			return
		}
		n.logf("server connected to %s as %s", address, n.Name)
		if err := n.Serve(ctx, tunnel.New(connection)); err != nil {
			n.logf("connection ended: %v", err)
		}
	})
	mux.HandleFunc("/node/health", func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		_, _ = writer.Write([]byte(`{"ok":true,"node":"` + Version + `","name":"` + jsonEscape(n.Name) + `"}`))
	})
	server := &http.Server{Addr: address, Handler: mux}
	listener, err := net.Listen("tcp", address)
	if err != nil {
		return err
	}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
	}()
	n.logf("listening on %s as %s", listener.Addr(), n.Name)
	err = server.Serve(listener)
	if err == http.ErrServerClosed {
		return nil
	}
	return err
}

func bearer(request *http.Request) string {
	value := request.Header.Get("Authorization")
	if strings.HasPrefix(strings.ToLower(value), "bearer ") {
		return strings.TrimSpace(value[7:])
	}
	return ""
}

func secureEqual(first, second string) bool {
	if len(first) != len(second) {
		return false
	}
	var difference byte
	for index := 0; index < len(first); index++ {
		difference |= first[index] ^ second[index]
	}
	return difference == 0
}

func jsonEscape(value string) string {
	encoded, err := json.Marshal(value)
	if err != nil {
		return ""
	}
	return string(encoded[1 : len(encoded)-1])
}

// SignalContext is cancelled by SIGINT or SIGTERM.
func SignalContext() (context.Context, context.CancelFunc) {
	return signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
}
