// Package shelldriver runs commands on the device: bash/sh on Linux and macOS,
// PowerShell (UTF-8) on Windows, with a timeout that kills the whole process
// tree so nothing is left behind.
package shelldriver

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os/exec"
	"runtime"
	"strings"
	"sync"
	"time"

	"gridnode/internal/fsdriver"
	"gridnode/internal/protocol"
)

// Result is what shell_exec answers.
type Result struct {
	ExitCode int    `json:"exit_code"`
	Stdout   string `json:"stdout"`
	Stderr   string `json:"stderr"`
	TimedOut bool   `json:"timed_out"`
}

// Driver serves shell_exec for one workdir.
type Driver struct {
	Files *fsdriver.Driver
}

// New makes the driver; files resolves and guards working_dir.
func New(files *fsdriver.Driver) *Driver { return &Driver{Files: files} }

// Tools is what this driver brings to a node.
func (d *Driver) Tools() []*protocol.Tool {
	return []*protocol.Tool{{
		Name: "shell_exec",
		Description: "Run a command on the device in working_dir (bash -c on Linux, powershell on Windows). " +
			"The timeout kills the whole process tree.",
		InputSchema: protocol.Object(map[string]any{
			"command":        protocol.PString("The command line to run."),
			"working_dir":    protocol.PStringDefault("Where to run it, relative to the working folder.", "."),
			"timeout":        protocol.PInt("Seconds before the command is killed (default 60, at most 900).", 60),
			"workdir":        protocol.PString("The working folder to stay inside."),
			"allow_external": protocol.PBool("Allow working_dir outside the working folder (default false).", false),
		}, "command"),
		Annotations: protocol.Run,
		Handler: func(ctx protocol.Context, args map[string]any) ([]protocol.Content, error) {
			return d.Exec(ctx, args)
		},
	}}
}

// Exec implements shell_exec.
func (d *Driver) Exec(ctx protocol.Context, args map[string]any) ([]protocol.Content, error) {
	command, err := protocol.RequiredString(args, "command")
	if err != nil {
		return nil, err
	}
	workingDir := protocol.String(args, "working_dir", ".")
	target, err := d.Files.Resolve(args, workingDir)
	if err != nil {
		return nil, err
	}
	timeout := protocol.Clamp(protocol.Int(args, "timeout", 60), 1, 900)
	shell, shellArgs := shellCommand(command)

	cmd := exec.Command(shell, shellArgs...)
	cmd.Dir = target
	stdout := &limitedBuffer{limit: 4 * 1024 * 1024}
	stderr := &limitedBuffer{limit: 4 * 1024 * 1024}
	cmd.Stdout = stdout
	cmd.Stderr = stderr
	prepare(cmd)
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("cannot start %s: %w", shell, err)
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	timer := time.NewTimer(time.Duration(timeout) * time.Second)
	defer timer.Stop()

	result := Result{}
	select {
	case err := <-done:
		result.ExitCode = exitCode(err)
	case <-timer.C:
		result.TimedOut = true
		result.ExitCode = -1
		killTree(cmd)
		select {
		case <-done:
		case <-time.After(5 * time.Second):
		}
	case <-ctx.Done():
		killTree(cmd)
		<-done
		result.ExitCode = -1
		result.TimedOut = true
	}
	result.Stdout = stdout.String()
	result.Stderr = stderr.String()
	if result.TimedOut {
		result.Stderr = strings.TrimRight(result.Stderr, "\n")
		if result.Stderr != "" {
			result.Stderr += "\n"
		}
		result.Stderr += fmt.Sprintf("command timed out after %d seconds and was killed", timeout)
	}
	payload, err := json.Marshal(result)
	if err != nil {
		return nil, err
	}
	return []protocol.Content{protocol.Text(string(payload))}, nil
}

func shellCommand(command string) (string, []string) {
	if runtime.GOOS == "windows" {
		return "powershell.exe", []string{"-NoProfile", "-NonInteractive", "-Command",
			"[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command}
	}
	if path, err := exec.LookPath("bash"); err == nil {
		return path, []string{"-c", command}
	}
	return "/bin/sh", []string{"-c", command}
}

func exitCode(err error) int {
	if err == nil {
		return 0
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		return exit.ExitCode()
	}
	return -1
}

// limitedBuffer keeps the first limit bytes and notes how much was dropped.
type limitedBuffer struct {
	mu      sync.Mutex
	buffer  bytes.Buffer
	limit   int
	dropped int64
}

func (b *limitedBuffer) Write(payload []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	room := b.limit - b.buffer.Len()
	if room > 0 {
		if len(payload) <= room {
			b.buffer.Write(payload)
		} else {
			b.buffer.Write(payload[:room])
			b.dropped += int64(len(payload) - room)
		}
	} else {
		b.dropped += int64(len(payload))
	}
	return len(payload), nil
}

func (b *limitedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	text := b.buffer.String()
	if b.dropped > 0 {
		text += fmt.Sprintf("\n… [%d more bytes dropped]", b.dropped)
	}
	return text
}
