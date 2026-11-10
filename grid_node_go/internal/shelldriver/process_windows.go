//go:build windows

package shelldriver

import (
	"os/exec"
	"strconv"
	"syscall"
)

// createNewProcessGroup is CREATE_NEW_PROCESS_GROUP.
const createNewProcessGroup = 0x00000200

// createNoWindow keeps a console window from flashing up.
const createNoWindow = 0x08000000

func prepare(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.CreationFlags = createNewProcessGroup | createNoWindow
}

// killTree kills the command and everything it started.
func killTree(cmd *exec.Cmd) {
	if cmd.Process == nil {
		return
	}
	pid := strconv.Itoa(cmd.Process.Pid)
	if killer, err := exec.LookPath("taskkill"); err == nil {
		_ = exec.Command(killer, "/F", "/T", "/PID", pid).Run()
	}
	_ = cmd.Process.Kill()
}
