//go:build windows

package browserdriver

import (
	"os/exec"
	"strconv"
	"syscall"
)

const createNoWindow = 0x08000000

// prepareBrowser hides the browser's console window.
func prepareBrowser(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.HideWindow = true
	cmd.SysProcAttr.CreationFlags = createNoWindow
}

// closeBrowser stops the browser and every renderer it started.
func closeBrowser(cmd *exec.Cmd) {
	if cmd.Process == nil {
		return
	}
	pid := strconv.Itoa(cmd.Process.Pid)
	if killer, err := exec.LookPath("taskkill"); err == nil {
		_ = exec.Command(killer, "/F", "/T", "/PID", pid).Run()
	}
	_ = cmd.Process.Kill()
}
