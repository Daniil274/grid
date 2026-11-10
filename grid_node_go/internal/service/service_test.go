package service

import (
	"strings"
	"testing"
)

func TestRenderUnitNamesTheBinaryAndServer(t *testing.T) {
	unit := RenderUnit(Options{
		Executable: "/opt/grid node/grid-node",
		Server:     "https://grid.example",
		Workdir:    "/home/user/GridWorkspace",
		StateDir:   "/home/user/.grid-node",
		Driver:     "all",
		Name:       "Laptop",
		Kind:       "desktop",
	})
	for _, expected := range []string{
		"ExecStart=\"/opt/grid node/grid-node\" --server https://grid.example",
		"--workdir /home/user/GridWorkspace",
		"--state-dir /home/user/.grid-node",
		"--driver all",
		"WorkingDirectory=/home/user/GridWorkspace",
		"Restart=always",
		"WantedBy=default.target",
	} {
		if !strings.Contains(unit, expected) {
			t.Fatalf("unit lacks %q:\n%s", expected, unit)
		}
	}
}

func TestArgumentsAndQuoting(t *testing.T) {
	options := Options{Executable: "grid-node", Server: "https://grid.example", Headless: true, Name: "My PC"}
	arguments := options.Arguments()
	joined := strings.Join(arguments, " ")
	if !strings.Contains(joined, "--server https://grid.example") {
		t.Fatalf("arguments are %v", arguments)
	}
	if !strings.Contains(joined, "--headless") {
		t.Fatalf("headless is missing: %v", arguments)
	}
	if !strings.Contains(options.Command(), `"My PC"`) {
		t.Fatalf("a name with a space was not quoted: %s", options.Command())
	}
}

func TestInstallNeedsAServer(t *testing.T) {
	if _, err := Install(Options{Executable: "grid-node"}); err == nil {
		t.Fatal("install without a server succeeded")
	}
}

func TestAndroidRegistersNothingAndSaysHowToStart(t *testing.T) {
	message := startOnAndroid(Options{Executable: "/data/data/com.termux/files/home/grid-node",
		Server: "https://grid.example", Kind: "desktop"})
	for _, expected := range []string{
		"no service manager",
		"--server https://grid.example",
		"termux-wake-lock",
	} {
		if !strings.Contains(message, expected) {
			t.Fatalf("the Android answer lacks %q:\n%s", expected, message)
		}
	}
}
