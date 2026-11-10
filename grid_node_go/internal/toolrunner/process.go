package toolrunner

import "os/exec"

// prepareProcess is where a platform would put the tool in its own process
// group; a tool package is short-lived and killed with its command.
func prepareProcess(cmd *exec.Cmd) {}
