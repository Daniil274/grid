//go:build !windows

package browserdriver

import (
	"os/exec"
	"syscall"
)

// prepareBrowser puts the browser in its own process group, so it and every
// renderer it started can be killed together.
func prepareBrowser(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Setpgid = true
}

// closeBrowser stops the browser and everything it started.
func closeBrowser(cmd *exec.Cmd) {
	if cmd.Process == nil {
		return
	}
	pid := cmd.Process.Pid
	_ = syscall.Kill(-pid, syscall.SIGTERM)
	_ = syscall.Kill(pid, syscall.SIGTERM)
}
