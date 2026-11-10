package toolrunner

import (
	"bytes"
	_ "embed"
	"os"
	"path/filepath"

	"gridnode/internal/state"
)

// gridToolRuntime is the runtime a Grid tool package imports as `grid_tool`:
// the node carries its own copy so a package runs on a bare device.
//
//go:embed runtime/grid_tool.py
var gridToolRuntime []byte

// GridToolScript is where the embedded runtime is kept on this device.
func (r *Runner) GridToolScript() (string, error) {
	path := filepath.Join(state.RuntimeDir(r.StateDir), "grid_tool.py")
	if existing, err := os.ReadFile(path); err == nil && bytes.Equal(existing, gridToolRuntime) {
		return path, nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return "", err
	}
	if err := os.WriteFile(path, gridToolRuntime, 0o644); err != nil {
		return "", err
	}
	return path, nil
}
