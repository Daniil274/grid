// Package service registers a paired node so it starts with the device: a
// systemd user unit on Linux, a Task Scheduler task and a Run entry on
// Windows, a launchd agent on macOS.
package service

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

// Options describe the node a service starts.
type Options struct {
	Executable string
	Server     string
	Workdir    string
	StateDir   string
	Driver     string
	Headless   bool
	Name       string
	Kind       string
}

// Arguments are the flags the service runs the node with.
func (o Options) Arguments() []string {
	arguments := []string{"--server", o.Server}
	if o.Workdir != "" {
		arguments = append(arguments, "--workdir", o.Workdir)
	}
	if o.StateDir != "" {
		arguments = append(arguments, "--state-dir", o.StateDir)
	}
	if o.Driver != "" {
		arguments = append(arguments, "--driver", o.Driver)
	}
	if o.Headless {
		arguments = append(arguments, "--headless")
	}
	if o.Name != "" {
		arguments = append(arguments, "--name", o.Name)
	}
	if o.Kind != "" {
		arguments = append(arguments, "--kind", o.Kind)
	}
	return arguments
}

// DefaultName is the name used when the options do not carry one.
const DefaultName = "grid-node"

// DisplayName is the node name, falling back to DefaultName.
func (o Options) DisplayName() string {
	if strings.TrimSpace(o.Name) == "" {
		return DefaultName
	}
	return o.Name
}

// Command is the executable and arguments, joined for a service file.
func (o Options) Command() string {
	parts := []string{quote(o.Executable)}
	for _, argument := range o.Arguments() {
		parts = append(parts, quote(argument))
	}
	return strings.Join(parts, " ")
}

func quote(value string) string {
	if value == "" {
		return `""`
	}
	if strings.ContainsAny(value, " \t\"'") {
		return `"` + strings.ReplaceAll(value, `"`, `\"`) + `"`
	}
	return value
}

// UnitPath is where the Linux systemd user unit lives.
func UnitPath() (string, error) {
	config, err := os.UserConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(config, "systemd", "user", "grid-node.service"), nil
}

// RenderUnit is the systemd user unit for options.
func RenderUnit(o Options) string {
	workdir := o.Workdir
	if workdir == "" {
		workdir = "."
	}
	return fmt.Sprintf(`[Unit]
Description=Grid node (%s)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=%s
WorkingDirectory=%s
Restart=always
RestartSec=5
Environment=HOME=%s

[Install]
WantedBy=default.target
`, o.DisplayName(), o.Command(), workdir, homeDir())
}

func homeDir() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return home
}

// Install registers the service and starts it.
func Install(o Options) (string, error) {
	if o.Executable == "" {
		executable, err := os.Executable()
		if err != nil {
			return "", err
		}
		o.Executable = executable
	}
	if o.Server == "" {
		return "", fmt.Errorf("a server url is required to install a node service")
	}
	switch runtime.GOOS {
	case "windows":
		return installWindows(o)
	case "darwin":
		return installLaunchd(o)
	default:
		return installSystemd(o)
	}
}

func installSystemd(o Options) (string, error) {
	path, err := UnitPath()
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return "", err
	}
	if err := os.WriteFile(path, []byte(RenderUnit(o)), 0o644); err != nil {
		return "", err
	}
	message := "wrote " + path
	if systemctl, err := exec.LookPath("systemctl"); err == nil {
		_ = run(systemctl, "--user", "daemon-reload")
		if err := run(systemctl, "--user", "enable", "--now", "grid-node.service"); err != nil {
			message += fmt.Sprintf("; enabling it failed (%v) - start it with: systemctl --user enable --now grid-node.service", err)
		} else {
			message += "; enabled and started with systemd --user"
		}
	} else {
		message += "; systemctl not found - start it with: systemctl --user enable --now grid-node.service"
	}
	return message, nil
}

func installLaunchd(o Options) (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	path := filepath.Join(home, "Library", "LaunchAgents", "chat.grid.node.plist")
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return "", err
	}
	arguments := []string{o.Executable}
	arguments = append(arguments, o.Arguments()...)
	var items strings.Builder
	for _, argument := range arguments {
		items.WriteString("    <string>" + escapeXML(argument) + "</string>\n")
	}
	plist := fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>chat.grid.node</string>
  <key>ProgramArguments</key>
  <array>
%s  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
`, items.String())
	if err := os.WriteFile(path, []byte(plist), 0o644); err != nil {
		return "", err
	}
	message := "wrote " + path
	if launchctl, err := exec.LookPath("launchctl"); err == nil {
		_ = run(launchctl, "unload", path)
		if err := run(launchctl, "load", "-w", path); err != nil {
			message += fmt.Sprintf("; loading it failed (%v)", err)
		} else {
			message += "; loaded with launchd"
		}
	}
	return message, nil
}

func installWindows(o Options) (string, error) {
	config, err := os.UserConfigDir()
	if err != nil {
		return "", err
	}
	dir := filepath.Join(config, "grid-node")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	command := o.Command()
	batch := filepath.Join(dir, "grid-node.cmd")
	if err := os.WriteFile(batch, []byte("@echo off\r\n"+command+"\r\n"), 0o644); err != nil {
		return "", err
	}
	message := "wrote " + batch
	if schtasks, err := exec.LookPath("schtasks"); err == nil {
		if err := run(schtasks, "/Create", "/SC", "ONLOGON", "/TN", "GridNode", "/TR", command, "/F"); err != nil {
			message += fmt.Sprintf("; creating the scheduled task failed (%v)", err)
		} else {
			message += "; scheduled task GridNode created (at logon)"
		}
	}
	if reg, err := exec.LookPath("reg"); err == nil {
		if err := run(reg, "add", `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, "/v", "GridNode", "/t", "REG_SZ", "/d", command, "/f"); err != nil {
			message += fmt.Sprintf("; adding the Run entry failed (%v)", err)
		} else {
			message += "; Run entry GridNode added"
		}
	}
	return message, nil
}

func run(name string, args ...string) error {
	command := exec.Command(name, args...)
	output, err := command.CombinedOutput()
	if err != nil {
		return fmt.Errorf("%s: %w: %s", strings.Join(append([]string{name}, args...), " "), err, strings.TrimSpace(string(output)))
	}
	return nil
}

func escapeXML(value string) string {
	replacer := strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;", `"`, "&quot;")
	return replacer.Replace(value)
}
