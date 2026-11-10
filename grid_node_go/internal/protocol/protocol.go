// Package protocol holds what every driver of a node shares: the content a
// tool answers with (text or an image), the tool itself with its JSON schema
// and MCP annotations, and small readers for JSON-RPC arguments.
package protocol

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math"
	"strings"
)

// Content is one part of a tool result, as MCP says it.
type Content struct {
	Type     string `json:"type"`
	Text     string `json:"text,omitempty"`
	Data     string `json:"data,omitempty"`
	MimeType string `json:"mimeType,omitempty"`
}

// Text is a text part.
func Text(value string) Content { return Content{Type: "text", Text: value} }

// Image is an image part, base64-encoded.
func Image(data []byte, mime string) Content {
	if mime == "" {
		mime = "image/png"
	}
	return Content{Type: "image", Data: base64.StdEncoding.EncodeToString(data), MimeType: mime}
}

// ImageBase64 is an already encoded image part.
func ImageBase64(data, mime string) Content {
	if mime == "" {
		mime = "image/png"
	}
	return Content{Type: "image", Data: data, MimeType: mime}
}

// Tool is one tool a node serves over MCP.
type Tool struct {
	Name        string
	Description string
	InputSchema map[string]any
	Annotations map[string]any
	Handler     func(ctx Context, args map[string]any) ([]Content, error)
}

// Context is what a tool call gets: the tunnel's context.
type Context = context.Context

// Annotations MCP clients read to judge a tool, as grid_node/node.py sets them.
var (
	Look = map[string]any{"readOnlyHint": true, "destructiveHint": false, "openWorldHint": true}
	Do   = map[string]any{"readOnlyHint": false, "destructiveHint": false, "openWorldHint": true}
	Run  = map[string]any{"readOnlyHint": false, "destructiveHint": true, "openWorldHint": false}
)

// Object builds a JSON object schema.
func Object(properties map[string]any, required ...string) map[string]any {
	schema := map[string]any{
		"type":                 "object",
		"properties":           properties,
		"required":             required,
		"additionalProperties": false,
	}
	return schema
}

// PString is a string property.
func PString(description string) map[string]any {
	return map[string]any{"type": "string", "description": description}
}

// PStringDefault is a string property with a default.
func PStringDefault(description, value string) map[string]any {
	return map[string]any{"type": "string", "description": description, "default": value}
}

// PBool is a boolean property.
func PBool(description string, value bool) map[string]any {
	return map[string]any{"type": "boolean", "description": description, "default": value}
}

// PInt is an integer property.
func PInt(description string, value int) map[string]any {
	return map[string]any{"type": "integer", "description": description, "default": value}
}

// PNumber is a number property.
func PNumber(description string, value float64) map[string]any {
	return map[string]any{"type": "number", "description": description, "default": value}
}

// PEnum is a string property limited to a set of values.
func PEnum(description string, values ...string) map[string]any {
	return map[string]any{"type": "string", "description": description, "enum": values}
}

// String reads a string argument, or the default when absent.
func String(args map[string]any, key, def string) string {
	if value, ok := args[key]; ok {
		if text, ok := value.(string); ok {
			return text
		}
		if value != nil {
			return fmt.Sprint(value)
		}
	}
	return def
}

// RequiredString reads a string argument that must be there and not empty.
func RequiredString(args map[string]any, key string) (string, error) {
	value := strings.TrimSpace(String(args, key, ""))
	if value == "" {
		return "", fmt.Errorf("%s is required", key)
	}
	return value, nil
}

// Bool reads a boolean argument.
func Bool(args map[string]any, key string, def bool) bool {
	value, ok := args[key]
	if !ok || value == nil {
		return def
	}
	switch typed := value.(type) {
	case bool:
		return typed
	case string:
		switch strings.ToLower(strings.TrimSpace(typed)) {
		case "true", "1", "yes", "on":
			return true
		case "false", "0", "no", "off":
			return false
		}
	case float64:
		return typed != 0
	case int:
		return typed != 0
	}
	return def
}

// Int reads an integer argument.
func Int(args map[string]any, key string, def int) int {
	value, ok := args[key]
	if !ok || value == nil {
		return def
	}
	switch typed := value.(type) {
	case float64:
		return int(typed)
	case float32:
		return int(typed)
	case int:
		return typed
	case int64:
		return int(typed)
	case json.Number:
		if parsed, err := typed.Int64(); err == nil {
			return int(parsed)
		}
	}
	return def
}

// Float reads a number argument.
func Float(args map[string]any, key string, def float64) float64 {
	value, ok := args[key]
	if !ok || value == nil {
		return def
	}
	switch typed := value.(type) {
	case float64:
		return typed
	case float32:
		return float64(typed)
	case int:
		return float64(typed)
	case int64:
		return float64(typed)
	case json.Number:
		if parsed, err := typed.Float64(); err == nil {
			return parsed
		}
	}
	return def
}

// ObjectArg reads an object argument (a tool's "arguments").
func ObjectArg(args map[string]any, key string) map[string]any {
	value, ok := args[key]
	if !ok || value == nil {
		return map[string]any{}
	}
	if typed, ok := value.(map[string]any); ok {
		return typed
	}
	return map[string]any{}
}

// Clamp limits value to [low, high].
func Clamp(value, low, high int) int {
	if value < low {
		return low
	}
	if value > high {
		return high
	}
	return value
}

// ClampFloat limits value to [low, high].
func ClampFloat(value, low, high float64) float64 {
	return math.Max(low, math.Min(high, value))
}
