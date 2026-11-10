// Package mcp is the Model Context Protocol server a node serves over the
// tunnel's "mcp" channel: JSON-RPC 2.0, the methods a Grid server's session
// calls (initialize, notifications/initialized, ping, tools/list, tools/call),
// and nothing a client would not understand.
package mcp

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"runtime/debug"
	"strings"

	"gridnode/internal/protocol"
)

// Protocol versions this node speaks; the newest first is the fallback.
var SupportedVersions = []string{"2025-06-18", "2025-03-26", "2024-11-05", "2025-11-25"}

// DefaultProtocolVersion is offered when the client asks for one we do not know.
const DefaultProtocolVersion = "2024-11-05"

// Server answers MCP requests for a set of tools.
type Server struct {
	Name    string
	Version string
	Tools   []*protocol.Tool
	Log     *log.Logger
}

type request struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params"`
}

func (r *request) hasID() bool {
	return len(r.ID) > 0 && string(r.ID) != "null"
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
	Data    any    `json:"data,omitempty"`
}

type response struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Result  any             `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

// Handle reads one MCP message and returns the answer, if one is due.
// Notifications get no answer: reply is nil.
func (s *Server) Handle(ctx context.Context, raw []byte) []byte {
	var req request
	if err := json.Unmarshal(raw, &req); err != nil {
		return s.errorAnswer(json.RawMessage("null"), -32700, "Parse error: "+err.Error())
	}
	if req.Method == "" {
		// A response to something we asked, or malformed: nothing to say.
		return nil
	}
	notification := strings.HasPrefix(req.Method, "notifications/")
	if notification {
		return nil
	}
	if !req.hasID() {
		return nil
	}
	switch req.Method {
	case "initialize":
		return s.answer(req.ID, s.initialize(req.Params))
	case "ping":
		return s.answer(req.ID, map[string]any{})
	case "tools/list":
		return s.answer(req.ID, map[string]any{"tools": s.listTools()})
	case "tools/call":
		return s.call(ctx, req)
	default:
		return s.errorAnswer(req.ID, -32601, "Method not found: "+req.Method)
	}
}

func (s *Server) initialize(params json.RawMessage) map[string]any {
	asked := ""
	var given struct {
		ProtocolVersion string `json:"protocolVersion"`
	}
	if len(params) > 0 {
		_ = json.Unmarshal(params, &given)
		asked = given.ProtocolVersion
	}
	version := DefaultProtocolVersion
	for _, supported := range SupportedVersions {
		if asked == supported {
			version = asked
			break
		}
	}
	name := s.Name
	if name == "" {
		name = "grid-node"
	}
	versionOfServer := s.Version
	if versionOfServer == "" {
		versionOfServer = "0.3.0-go"
	}
	return map[string]any{
		"protocolVersion": version,
		"capabilities": map[string]any{
			"tools": map[string]any{"listChanged": false},
		},
		"serverInfo": map[string]any{"name": name, "version": versionOfServer},
	}
}

func (s *Server) listTools() []map[string]any {
	tools := make([]map[string]any, 0, len(s.Tools))
	for _, tool := range s.Tools {
		entry := map[string]any{
			"name":        tool.Name,
			"description": tool.Description,
			"inputSchema": tool.InputSchema,
		}
		if len(tool.Annotations) > 0 {
			entry["annotations"] = tool.Annotations
		}
		tools = append(tools, entry)
	}
	return tools
}

type callParams struct {
	Name      string         `json:"name"`
	Arguments map[string]any `json:"arguments"`
}

func (s *Server) call(ctx context.Context, req request) (answer []byte) {
	var params callParams
	if len(req.Params) > 0 {
		if err := json.Unmarshal(req.Params, &params); err != nil {
			return s.errorAnswer(req.ID, -32602, "Invalid params: "+err.Error())
		}
	}
	var tool *protocol.Tool
	for _, candidate := range s.Tools {
		if candidate.Name == params.Name {
			tool = candidate
			break
		}
	}
	if tool == nil {
		return s.errorAnswer(req.ID, -32602, "unknown tool: "+params.Name)
	}
	if params.Arguments == nil {
		params.Arguments = map[string]any{}
	}
	defer func() {
		if recovered := recover(); recovered != nil {
			if s.Log != nil {
				s.Log.Printf("tool %s panicked: %v\n%s", tool.Name, recovered, debug.Stack())
			}
			answer = s.answer(req.ID, failure(fmt.Sprintf("tool %s failed: %v", tool.Name, recovered)))
		}
	}()
	content, err := tool.Handler(ctx, params.Arguments)
	if err != nil {
		return s.answer(req.ID, failure(err.Error()))
	}
	if content == nil {
		content = []protocol.Content{}
	}
	return s.answer(req.ID, map[string]any{"content": content, "isError": false})
}

func failure(message string) map[string]any {
	return map[string]any{
		"content": []protocol.Content{protocol.Text("Error: " + message)},
		"isError": true,
	}
}

func (s *Server) answer(id json.RawMessage, result any) []byte {
	payload, err := json.Marshal(response{JSONRPC: "2.0", ID: id, Result: result})
	if err != nil {
		return s.errorAnswer(id, -32603, "Internal error: "+err.Error())
	}
	return payload
}

func (s *Server) errorAnswer(id json.RawMessage, code int, message string) []byte {
	payload, err := json.Marshal(response{JSONRPC: "2.0", ID: id, Error: &rpcError{Code: code, Message: message}})
	if err != nil {
		return []byte(`{"jsonrpc":"2.0","id":null,"error":{"code":-32603,"message":"Internal error"}}`)
	}
	return payload
}
