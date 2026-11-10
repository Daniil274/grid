package browserdriver

import (
	"context"
	"encoding/json"
	"strconv"
	"strings"
)

// jsonUnmarshalImpl keeps tools.go free of another import alias.
func jsonUnmarshalImpl(raw []byte, target any) error { return json.Unmarshal(raw, target) }

type keyInfo struct {
	key       string
	text      string
	code      int
	vk        int
	modifiers int
}

var namedKeys = map[string]struct {
	key  string
	text string
	code int
	vk   int
}{
	"Enter":      {"Enter", "\r", 13, 13},
	"Tab":        {"Tab", "", 9, 9},
	"Escape":     {"Escape", "", 27, 27},
	"Esc":        {"Escape", "", 27, 27},
	"Backspace":  {"Backspace", "", 8, 8},
	"Delete":     {"Delete", "", 46, 46},
	"Space":      {" ", " ", 32, 32},
	"ArrowUp":    {"ArrowUp", "", 38, 38},
	"Up":         {"ArrowUp", "", 38, 38},
	"ArrowDown":  {"ArrowDown", "", 40, 40},
	"Down":       {"ArrowDown", "", 40, 40},
	"ArrowLeft":  {"ArrowLeft", "", 37, 37},
	"Left":       {"ArrowLeft", "", 37, 37},
	"ArrowRight": {"ArrowRight", "", 39, 39},
	"Right":      {"ArrowRight", "", 39, 39},
	"Home":       {"Home", "", 36, 36},
	"End":        {"End", "", 35, 35},
	"PageUp":     {"PageUp", "", 33, 33},
	"PageDown":   {"PageDown", "", 34, 34},
	"Insert":     {"Insert", "", 45, 45},
}

// parseKey turns "Control+Shift+A" into what Input.dispatchKeyEvent wants.
func parseKey(combo string) keyInfo {
	parts := strings.Split(strings.TrimSpace(combo), "+")
	modifiers := 0
	for len(parts) > 1 {
		switch strings.ToLower(parts[0]) {
		case "alt":
			modifiers |= 1
		case "control", "ctrl":
			modifiers |= 2
		case "meta", "cmd", "command", "super":
			modifiers |= 4
		case "shift":
			modifiers |= 8
		default:
			// Not a modifier: the whole string is the key (e.g. a "+" key).
			parts = []string{strings.Join(parts, "+")}
			goto done
		}
		parts = parts[1:]
	}
done:
	name := parts[0]
	if named, ok := namedKeys[name]; ok {
		return keyInfo{key: named.key, text: named.text, code: named.code, vk: named.vk, modifiers: modifiers}
	}
	if len(name) == 1 {
		character := name
		upper := strings.ToUpper(character)
		vk := int(upper[0])
		return keyInfo{key: character, text: character, vk: vk, modifiers: modifiers}
	}
	if strings.HasPrefix(strings.ToUpper(name), "F") {
		if number, err := strconv.Atoi(name[1:]); err == nil && number >= 1 && number <= 12 {
			return keyInfo{key: name, code: 111 + number, vk: 111 + number, modifiers: modifiers}
		}
	}
	return keyInfo{key: name, vk: 0, modifiers: modifiers}
}

// pressKey sends one key press (and its release) to the page.
func (d *Driver) pressKey(ctx context.Context, page *client, combo string) error {
	info := parseKey(combo)
	down := map[string]any{
		"type": "keyDown", "key": info.key, "modifiers": info.modifiers,
		"windowsVirtualKeyCode": info.vk, "nativeVirtualKeyCode": info.vk,
	}
	if info.text != "" && info.modifiers&(1|2|4) == 0 {
		down["text"] = info.text
		down["unmodifiedText"] = info.text
	}
	if _, err := page.Send(ctx, "Input.dispatchKeyEvent", down); err != nil {
		return err
	}
	up := map[string]any{
		"type": "keyUp", "key": info.key, "modifiers": info.modifiers,
		"windowsVirtualKeyCode": info.vk, "nativeVirtualKeyCode": info.vk,
	}
	_, err := page.Send(ctx, "Input.dispatchKeyEvent", up)
	return err
}
