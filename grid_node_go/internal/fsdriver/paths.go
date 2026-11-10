package fsdriver

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"gridnode/internal/protocol"
	"gridnode/internal/state"
)

// Driver serves the filesystem and search tools of one node, keeping every
// path inside the workdir unless the caller explicitly allows otherwise.
type Driver struct {
	DefaultWorkdir string
}

// New makes the driver; workdir is what the node was started with.
func New(workdir string) *Driver {
	if workdir == "" {
		workdir = state.DefaultWorkdir()
	}
	return &Driver{DefaultWorkdir: workdir}
}

// Workdir is the workdir an argument names, or the node's default.
func (d *Driver) Workdir(args map[string]any) string {
	value := strings.TrimSpace(protocol.String(args, "workdir", ""))
	if value == "" {
		value = d.DefaultWorkdir
	}
	value = state.ExpandHome(value)
	if abs, err := filepath.Abs(value); err == nil {
		value = abs
	}
	return value
}

// canonical resolves symlinks on the longest existing prefix of path and
// rejoins the part that does not exist yet, so a path that will be created is
// checked as the file it would become.
func canonical(path string) (string, error) {
	absolute, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	absolute = filepath.Clean(absolute)
	missing := []string{}
	current := absolute
	for {
		resolved, err := filepath.EvalSymlinks(current)
		if err == nil {
			for index := len(missing) - 1; index >= 0; index-- {
				resolved = filepath.Join(resolved, missing[index])
			}
			return filepath.Clean(resolved), nil
		}
		if !os.IsNotExist(err) {
			return "", err
		}
		parent := filepath.Dir(current)
		if parent == current {
			return absolute, nil
		}
		missing = append(missing, filepath.Base(current))
		current = parent
	}
}

// inside reports whether target is base or below it.
func inside(base, target string) bool {
	base = filepath.Clean(base)
	target = filepath.Clean(target)
	if target == base {
		return true
	}
	relative, err := filepath.Rel(base, target)
	if err != nil {
		return false
	}
	if relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return false
	}
	if filepath.IsAbs(relative) {
		return false
	}
	// Windows paths compare case-insensitively.
	if os.PathSeparator == '\\' && strings.EqualFold(base, target) {
		return true
	}
	return true
}

// Resolve turns a tool's path argument into an absolute path, checked against
// the workdir. An empty path means the workdir itself.
func (d *Driver) Resolve(args map[string]any, path string) (string, error) {
	workdir := d.Workdir(args)
	base, err := canonical(workdir)
	if err != nil {
		return "", fmt.Errorf("workdir %s: %w", workdir, err)
	}
	if path == "" {
		path = "."
	}
	path = state.ExpandHome(path)
	target := path
	if !filepath.IsAbs(target) {
		target = filepath.Join(base, target)
	}
	resolved, err := canonical(target)
	if err != nil {
		return "", err
	}
	if protocol.Bool(args, "allow_external", false) {
		return filepath.Clean(resolved), nil
	}
	if !inside(base, resolved) {
		return "", fmt.Errorf("path '%s' is outside working directory '%s'", path, workdir)
	}
	return filepath.Clean(resolved), nil
}

// Relative is a path shown to the agent: relative to dir when it is inside it.
func Relative(dir, path string) string {
	relative, err := filepath.Rel(dir, path)
	if err != nil || strings.HasPrefix(relative, "..") {
		return filepath.ToSlash(path)
	}
	if relative == "." {
		return "."
	}
	return filepath.ToSlash(relative)
}
