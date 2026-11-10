package node

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"

	"gridnode/internal/protocol"
	"gridnode/internal/tunnel"
)

func testNode(t *testing.T) *Node {
	t.Helper()
	device := &Node{
		Name:           "Laptop",
		Kind:           "desktop",
		DefaultWorkdir: t.TempDir(),
		StateDir:       t.TempDir(),
		Capabilities:   []string{"fs", "shell", "browser", "tools"},
		View:           true,
		Tools: []*protocol.Tool{{
			Name:        "echo",
			Description: "Echo text.",
			InputSchema: protocol.Object(map[string]any{"text": protocol.PString("what to say")}, "text"),
			Annotations: protocol.Look,
			Handler: func(ctx context.Context, args map[string]any) ([]protocol.Content, error) {
				return []protocol.Content{protocol.Text("hello " + protocol.String(args, "text", ""))}, nil
			},
		}},
	}
	if err := device.Prepare(); err != nil {
		t.Fatal(err)
	}
	return device
}

func readFrame(t *testing.T, conn *websocket.Conn) (string, json.RawMessage) {
	t.Helper()
	if err := conn.SetReadDeadline(time.Now().Add(10 * time.Second)); err != nil {
		t.Fatal(err)
	}
	_, payload, err := conn.ReadMessage()
	if err != nil {
		t.Fatalf("server side could not read: %v", err)
	}
	var frame struct {
		Channel string          `json:"ch"`
		Data    json.RawMessage `json:"d"`
	}
	if err := json.Unmarshal(payload, &frame); err != nil {
		t.Fatalf("frame is not JSON: %v (%s)", err, payload)
	}
	return frame.Channel, frame.Data
}

func sendFrame(t *testing.T, conn *websocket.Conn, channel string, data any) {
	t.Helper()
	payload, err := json.Marshal(map[string]any{"ch": channel, "d": data})
	if err != nil {
		t.Fatal(err)
	}
	if err := conn.WriteMessage(websocket.TextMessage, payload); err != nil {
		t.Fatalf("server side could not write: %v", err)
	}
}

func TestServeSpeaksHelloAndMCP(t *testing.T) {
	device := testNode(t)
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	serverSide := make(chan *websocket.Conn, 1)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		connection, err := upgrader.Upgrade(writer, request, nil)
		if err != nil {
			t.Errorf("upgrade failed: %v", err)
			return
		}
		serverSide <- connection
	}))
	defer server.Close()

	headers := http.Header{"Authorization": []string{"Bearer secret"}}
	client, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(server.URL, "http"), headers)
	if err != nil {
		t.Fatal(err)
	}
	other := <-serverSide

	// The fake server is the MCP client; the node serves on the other end.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	finished := make(chan error, 1)
	go func() { finished <- device.Serve(ctx, tunnel.New(client)) }()

	channel, helloRaw := readFrame(t, other)
	if channel != tunnel.CTL {
		t.Fatalf("first frame is on %q, not ctl", channel)
	}
	var hello map[string]any
	if err := json.Unmarshal(helloRaw, &hello); err != nil {
		t.Fatal(err)
	}
	if hello["type"] != "hello" || hello["name"] != "Laptop" || hello["node"] != Version {
		t.Fatalf("hello is %v", hello)
	}
	if hello["view"] != true || hello["default_workdir"] == "" {
		t.Fatalf("hello lacks view/default_workdir: %v", hello)
	}
	capabilities, _ := hello["capabilities"].([]any)
	if len(capabilities) != 4 {
		t.Fatalf("capabilities are %v", hello["capabilities"])
	}

	sendFrame(t, other, tunnel.MCP, map[string]any{
		"jsonrpc": "2.0", "id": 1, "method": "initialize",
		"params": map[string]any{"protocolVersion": "2024-11-05", "capabilities": map[string]any{}, "clientInfo": map[string]any{"name": "test"}},
	})
	channel, answerRaw := readFrame(t, other)
	if channel != tunnel.MCP {
		t.Fatalf("initialize answered on %q", channel)
	}
	var answer map[string]any
	if err := json.Unmarshal(answerRaw, &answer); err != nil {
		t.Fatal(err)
	}
	result := answer["result"].(map[string]any)
	if result["protocolVersion"] != "2024-11-05" {
		t.Fatalf("protocolVersion is %v", result["protocolVersion"])
	}

	sendFrame(t, other, tunnel.MCP, map[string]any{"jsonrpc": "2.0", "method": "notifications/initialized"})
	sendFrame(t, other, tunnel.MCP, map[string]any{"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
	_, listedRaw := readFrame(t, other)
	var listed map[string]any
	if err := json.Unmarshal(listedRaw, &listed); err != nil {
		t.Fatal(err)
	}
	tools := listed["result"].(map[string]any)["tools"].([]any)
	if len(tools) != 1 || tools[0].(map[string]any)["name"] != "echo" {
		t.Fatalf("tools are %v", tools)
	}

	sendFrame(t, other, tunnel.MCP, map[string]any{
		"jsonrpc": "2.0", "id": 3, "method": "tools/call",
		"params": map[string]any{"name": "echo", "arguments": map[string]any{"text": "world"}},
	})
	_, calledRaw := readFrame(t, other)
	var called map[string]any
	if err := json.Unmarshal(calledRaw, &called); err != nil {
		t.Fatal(err)
	}
	content := called["result"].(map[string]any)["content"].([]any)[0].(map[string]any)
	if content["text"] != "hello world" {
		t.Fatalf("tool answered %v", content["text"])
	}

	sendFrame(t, other, tunnel.CTL, map[string]any{"type": "ping"})
	channel, pongRaw := readFrame(t, other)
	if channel != tunnel.CTL {
		t.Fatalf("pong came on %q", channel)
	}
	var pong map[string]any
	if err := json.Unmarshal(pongRaw, &pong); err != nil {
		t.Fatal(err)
	}
	if pong["type"] != "pong" {
		t.Fatalf("answer to ping is %v", pong)
	}

	_ = other.Close()
	select {
	case <-finished:
	case <-time.After(10 * time.Second):
		t.Fatal("Serve did not return after the connection closed")
	}
}

func TestConnectURL(t *testing.T) {
	cases := map[string]string{
		"https://grid.example":              "wss://grid.example/node/connect",
		"https://grid.example/":             "wss://grid.example/node/connect",
		"http://127.0.0.1:8000":             "ws://127.0.0.1:8000/node/connect",
		"https://grid.example/node/connect": "wss://grid.example/node/connect",
	}
	for given, expected := range cases {
		if got := connectURL(given); got != expected {
			t.Fatalf("connectURL(%q) = %q, want %q", given, got, expected)
		}
	}
}

func TestSecureEqual(t *testing.T) {
	if !secureEqual("abc", "abc") {
		t.Fatal("equal tokens differ")
	}
	if secureEqual("abc", "abd") || secureEqual("abc", "ab") {
		t.Fatal("different tokens are equal")
	}
}

func TestListenChecksTheBearerToken(t *testing.T) {
	device := testNode(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	port := freePort(t)
	address := "127.0.0.1:" + port
	go func() { _ = device.Listen(ctx, address, "letmein") }()
	time.Sleep(300 * time.Millisecond)

	_, response, err := websocket.DefaultDialer.Dial("ws://"+address+"/node/connect", http.Header{"Authorization": []string{"Bearer wrong"}})
	if err == nil {
		t.Fatal("a wrong bearer token was accepted")
	}
	if response != nil && response.StatusCode != http.StatusUnauthorized && response.StatusCode != http.StatusConflict {
		t.Fatalf("unexpected status %d", response.StatusCode)
	}

	connection, _, err := websocket.DefaultDialer.Dial("ws://"+address+"/node/connect", http.Header{"Authorization": []string{"Bearer letmein"}})
	if err != nil {
		t.Fatalf("the right bearer token was refused: %v", err)
	}
	channel, hello := readFrame(t, connection)
	if channel != tunnel.CTL || !strings.Contains(string(hello), "hello") {
		t.Fatalf("first frame is %s %s", channel, hello)
	}
	_ = connection.Close()
}

func netListen() (net.Listener, error) { return net.Listen("tcp", "127.0.0.1:0") }

func freePort(t *testing.T) string {
	t.Helper()
	listener, err := netListen()
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	return strings.Split(listener.Addr().String(), ":")[1]
}
