// Package state keeps what a node remembers between runs: the token it was
// paired with, its workdir, and where tool packages and runtimes live.
package state

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

// Token is what pairing left on disk.
type Token struct {
	Server string `json:"server"`
	Device string `json:"device"`
	Token  string `json:"token"`
}

// DefaultStateDir is ~/.grid-node.
func DefaultStateDir() string {
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		return ".grid-node"
	}
	return filepath.Join(home, ".grid-node")
}

// DefaultWorkdir is ~/GridWorkspace.
func DefaultWorkdir() string {
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		return "GridWorkspace"
	}
	return filepath.Join(home, "GridWorkspace")
}

// ExpandHome replaces a leading ~ (or ~/) with the user's home.
func ExpandHome(path string) string {
	if path == "" {
		return path
	}
	if path == "~" {
		home, err := os.UserHomeDir()
		if err == nil {
			return home
		}
		return path
	}
	if strings.HasPrefix(path, "~/") || strings.HasPrefix(path, `~\`) {
		home, err := os.UserHomeDir()
		if err == nil {
			return filepath.Join(home, path[2:])
		}
	}
	return path
}

// Abs resolves a path against the user's home and the working directory.
func Abs(path string) (string, error) {
	return filepath.Abs(ExpandHome(path))
}

// TokenPath is <state-dir>/token.json.
func TokenPath(stateDir string) string {
	return filepath.Join(stateDir, "token.json")
}

// LoadToken reads the saved token when it belongs to server.
func LoadToken(stateDir, server string) (string, error) {
	payload, err := os.ReadFile(TokenPath(stateDir))
	if err != nil {
		return "", err
	}
	var saved Token
	if err := json.Unmarshal(payload, &saved); err != nil {
		return "", err
	}
	if saved.Server == "" || saved.Token == "" {
		return "", errors.New("token.json holds no token")
	}
	if server != "" && strings.TrimRight(saved.Server, "/") != strings.TrimRight(server, "/") {
		return "", fmt.Errorf("the saved token belongs to %s, not %s", saved.Server, server)
	}
	return saved.Token, nil
}

// SaveToken writes the token, readable by this user only.
func SaveToken(stateDir string, token Token) (string, error) {
	path := TokenPath(stateDir)
	if err := os.MkdirAll(stateDir, 0o700); err != nil {
		return path, err
	}
	payload, err := json.Marshal(token)
	if err != nil {
		return path, err
	}
	temp := path + ".tmp"
	if err := os.WriteFile(temp, payload, 0o600); err != nil {
		return path, err
	}
	if runtime.GOOS != "windows" {
		_ = os.Chmod(temp, 0o600)
	}
	if err := os.Rename(temp, path); err != nil {
		_ = os.Remove(temp)
		return path, err
	}
	if runtime.GOOS != "windows" {
		_ = os.Chmod(path, 0o600)
	}
	return path, nil
}

// ToolsDir is <state-dir>/tools.
func ToolsDir(stateDir string) string { return filepath.Join(stateDir, "tools") }

// ToolDir is the cache of one tool package, keyed by its hash.
func ToolDir(stateDir, hash string) string { return filepath.Join(ToolsDir(stateDir), hash) }

// RuntimeDir is <state-dir>/runtime.
func RuntimeDir(stateDir string) string { return filepath.Join(stateDir, "runtime") }

// PythonRuntimeDir is <state-dir>/runtime/python.
func PythonRuntimeDir(stateDir string) string { return filepath.Join(RuntimeDir(stateDir), "python") }

// PythonPath is the portable interpreter's path inside the runtime directory.
func PythonPath(stateDir string) string {
	dir := PythonRuntimeDir(stateDir)
	if runtime.GOOS == "windows" {
		return filepath.Join(dir, "python.exe")
	}
	return filepath.Join(dir, "bin", "python3")
}

// EnsureDir makes a directory and returns it.
func EnsureDir(path string, mode os.FileMode) (string, error) {
	if path == "" {
		return path, errors.New("empty directory")
	}
	if err := os.MkdirAll(path, mode); err != nil {
		return path, err
	}
	return path, nil
}
