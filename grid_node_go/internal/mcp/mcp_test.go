package mcp

import (
	"context"
	"encoding/json"
	"testing"

	"gridnode/internal/protocol"
)

func testServer() *Server {
	echo := &protocol.Tool{
		Name:        "echo",
		Description: "Echo the text back.",
		InputSchema: protocol.Object(map[string]any{"text": protocol.PString("what to say")}, "text"),
		Annotations: protocol.Look,
		Handler: func(ctx context.Context, args map[string]any) ([]protocol.Content, error) {
			return []protocol.Content{protocol.Text("echo: " + protocol.String(args, "text", ""))}, nil
		},
	}
	failing := &protocol.Tool{
		Name:        "fail",
		Description: "Fail on purpose.",
		InputSchema: protocol.Object(map[string]any{}),
		Annotations: protocol.Run,
		Handler: func(ctx context.Context, args map[string]any) ([]protocol.Content, error) {
			return nil, context.DeadlineExceeded
		},
	}
	return &Server{Name: "grid-node", Version: "0.3.0-go", Tools: []*protocol.Tool{echo, failing}}
}

func decode(t *testing.T, raw []byte) map[string]any {
	t.Helper()
	var parsed map[string]any
	if err := json.Unmarshal(raw, &parsed); err != nil {
		t.Fatalf("answer is not JSON: %v (%s)", err, raw)
	}
	return parsed
}

func TestInitializeKeepsTheClientsVersion(t *testing.T) {
	server := testServer()
	answer := server.Handle(context.Background(), []byte(`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}`))
	parsed := decode(t, answer)
	result := parsed["result"].(map[string]any)
	if result["protocolVersion"] != "2025-06-18" {
		t.Fatalf("protocolVersion is %v", result["protocolVersion"])
	}
	info := result["serverInfo"].(map[string]any)
	if info["name"] != "grid-node" || info["version"] != "0.3.0-go" {
		t.Fatalf("serverInfo is %v", info)
	}
	if _, ok := result["capabilities"].(map[string]any)["tools"]; !ok {
		t.Fatalf("no tools capability: %v", result)
	}
}

func TestInitializeFallsBackForAnUnknownVersion(t *testing.T) {
	answer := testServer().Handle(context.Background(), []byte(`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"1999-01-01"}}`))
	result := decode(t, answer)["result"].(map[string]any)
	if result["protocolVersion"] != DefaultProtocolVersion {
		t.Fatalf("fallback is %v", result["protocolVersion"])
	}
}

func TestNotificationGetsNoAnswer(t *testing.T) {
	if answer := testServer().Handle(context.Background(), []byte(`{"jsonrpc":"2.0","method":"notifications/initialized"}`)); answer != nil {
		t.Fatalf("a notification was answered: %s", answer)
	}
}

func TestToolsList(t *testing.T) {
	answer := testServer().Handle(context.Background(), []byte(`{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}`))
	tools := decode(t, answer)["result"].(map[string]any)["tools"].([]any)
	if len(tools) != 2 {
		t.Fatalf("listed %d tools", len(tools))
	}
	first := tools[0].(map[string]any)
	if first["name"] != "echo" {
		t.Fatalf("first tool is %v", first["name"])
	}
	if first["annotations"].(map[string]any)["readOnlyHint"] != true {
		t.Fatalf("annotations are %v", first["annotations"])
	}
	if first["inputSchema"].(map[string]any)["type"] != "object" {
		t.Fatalf("schema is %v", first["inputSchema"])
	}
}

func TestToolsCallReturnsText(t *testing.T) {
	answer := testServer().Handle(context.Background(), []byte(`{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"echo","arguments":{"text":"hi"}}}`))
	result := decode(t, answer)["result"].(map[string]any)
	if result["isError"] != false {
		t.Fatalf("isError is %v", result["isError"])
	}
	content := result["content"].([]any)[0].(map[string]any)
	if content["type"] != "text" || content["text"] != "echo: hi" {
		t.Fatalf("content is %v", content)
	}
}

func TestToolsCallReportsToolErrors(t *testing.T) {
	answer := testServer().Handle(context.Background(), []byte(`{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"fail","arguments":{}}}`))
	result := decode(t, answer)["result"].(map[string]any)
	if result["isError"] != true {
		t.Fatalf("isError is %v", result["isError"])
	}
	text := result["content"].([]any)[0].(map[string]any)["text"].(string)
	if text == "" {
		t.Fatal("no error text")
	}
}

func TestUnknownToolIsAnError(t *testing.T) {
	answer := testServer().Handle(context.Background(), []byte(`{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"nope"}}`))
	parsed := decode(t, answer)
	if parsed["error"] == nil {
		t.Fatalf("no error: %v", parsed)
	}
}

func TestUnknownMethodIsAnError(t *testing.T) {
	answer := testServer().Handle(context.Background(), []byte(`{"jsonrpc":"2.0","id":6,"method":"resources/list"}`))
	parsed := decode(t, answer)
	problem := parsed["error"].(map[string]any)
	if int(problem["code"].(float64)) != -32601 {
		t.Fatalf("code is %v", problem["code"])
	}
}

func TestPingAndParseError(t *testing.T) {
	answer := testServer().Handle(context.Background(), []byte(`{"jsonrpc":"2.0","id":7,"method":"ping"}`))
	if decode(t, answer)["result"] == nil {
		t.Fatalf("ping answered %s", answer)
	}
	bad := testServer().Handle(context.Background(), []byte(`not-json`))
	problem := decode(t, bad)["error"].(map[string]any)
	if int(problem["code"].(float64)) != -32700 {
		t.Fatalf("parse error code is %v", problem["code"])
	}
}
