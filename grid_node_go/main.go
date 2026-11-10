// Command grid-node is a device's side of Grid: one static binary that pairs
// with a Grid server, serves the device's files, shell, browser and tool
// packages as an MCP server over a WebSocket tunnel, and installs itself as a
// background service.
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"

	"gridnode/internal/browserdriver"
	"gridnode/internal/fsdriver"
	"gridnode/internal/node"
	"gridnode/internal/selfupdate"
	"gridnode/internal/service"
	"gridnode/internal/shelldriver"
	"gridnode/internal/state"
	"gridnode/internal/toolrunner"
)

const usage = `grid-node - a device's side of Grid

Usage:
  grid-node [flags]                      pair once, then serve the device
  grid-node install --server URL --pair CODE [flags]
  grid-node version

Flags:
  --server URL        the Grid server, e.g. https://grid.example
  --pair CODE         the one-time pairing code from the Devices page
  --token TOKEN       a device token (otherwise the saved one is used)
  --name NAME         the device's name (default: the hostname)
  --kind KIND         desktop, browser or mobile (default: desktop)
  --driver LIST       fs,shell,browser,tools or all (default: all)
  --workdir DIR       the folder tools stay inside (default: ~/GridWorkspace)
  --state-dir DIR     where the token, tools and profile live (default: ~/.grid-node)
  --listen HOST:PORT  serve the node for a server that connects to it
  --headless          run the browser without a window
  --profile DIR       the browser profile (default: <state-dir>/profile)
  --browser PATH      the Chrome/Chromium/Edge binary to use
`

// listFlag collects a repeatable, comma-separated flag.
type listFlag []string

func (l *listFlag) String() string { return strings.Join(*l, ",") }

func (l *listFlag) Set(value string) error {
	for _, part := range strings.Split(value, ",") {
		part = strings.ToLower(strings.TrimSpace(part))
		if part != "" {
			*l = append(*l, part)
		}
	}
	return nil
}

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

func run(args []string, stdout, stderr io.Writer) int {
	logger := log.New(stderr, "", log.LstdFlags)
	if len(args) > 0 {
		switch args[0] {
		case "version", "--version", "-v":
			fmt.Fprintf(stdout, "grid-node %s (%s %s)\n", node.Version, runtime.GOOS, runtime.GOARCH)
			return 0
		case "help", "--help", "-h":
			fmt.Fprint(stdout, usage)
			return 0
		case "install":
			return installCommand(args[1:], stdout, stderr, logger)
		case "run":
			args = args[1:]
		}
	}
	flags := flag.NewFlagSet("grid-node", flag.ContinueOnError)
	flags.SetOutput(stderr)
	flags.Usage = func() { fmt.Fprint(stderr, usage) }
	server := flags.String("server", "", "the Grid server address")
	pair := flags.String("pair", "", "the one-time pairing code")
	tokenFlag := flags.String("token", "", "the device token")
	name := flags.String("name", "", "the device's name")
	kind := flags.String("kind", "desktop", "desktop, browser or mobile")
	var drivers listFlag
	flags.Var(&drivers, "driver", "fs,shell,browser,tools or all")
	workdir := flags.String("workdir", "", "the folder tools stay inside")
	stateDirFlag := flags.String("state-dir", "", "where the token and tools live")
	listen := flags.String("listen", "", "HOST:PORT to serve the server from")
	headless := flags.Bool("headless", false, "run the browser without a window")
	profile := flags.String("profile", "", "the browser profile directory")
	browserPath := flags.String("browser", "", "the Chrome/Chromium/Edge binary")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	if flags.NArg() > 0 {
		fmt.Fprintf(stderr, "unexpected argument %q\n", flags.Arg(0))
		return 2
	}

	directory := *stateDirFlag
	if directory == "" {
		directory = state.DefaultStateDir()
	}
	directory = state.ExpandHome(directory)
	if absolute, err := filepath.Abs(directory); err == nil {
		directory = absolute
	}
	workspace := *workdir
	if workspace == "" {
		workspace = state.DefaultWorkdir()
	}
	workspace = state.ExpandHome(workspace)
	if absolute, err := filepath.Abs(workspace); err == nil {
		workspace = absolute
	}
	if err := os.MkdirAll(workspace, 0o755); err != nil {
		logger.Printf("cannot create the workspace %s: %v", workspace, err)
		return 1
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	token := strings.TrimSpace(*tokenFlag)
	if token == "" {
		token = os.Getenv("GRID_DEVICE_TOKEN")
	}
	if *server != "" && *pair != "" {
		paired, path, err := node.Pair(ctx, *server, *pair, *name, *kind, directory)
		if err != nil {
			logger.Printf("pairing failed: %v", err)
			return 1
		}
		token = paired.Token
		fmt.Fprintf(stdout, "Paired as %s; the token is kept in %s\n", paired.Device, path)
	}
	if *server != "" && token == "" {
		saved, err := state.LoadToken(directory, *server)
		if err != nil && !os.IsNotExist(err) {
			logger.Printf("cannot use the saved token: %v", err)
		}
		token = saved
	}
	if *listen == "" && *server == "" {
		logger.Print("pair first: grid-node --server URL --pair CODE (or pass --listen)")
		return 2
	}
	if token == "" {
		logger.Print("no token: pair with --pair CODE, pass --token, or set GRID_DEVICE_TOKEN")
		return 2
	}

	device, err := buildNode(*name, *kind, workspace, directory, drivers, *headless, *profile, *browserPath, *server, token, logger)
	if err != nil {
		logger.Printf("%v", err)
		return 1
	}
	if *listen != "" {
		if err := device.Listen(ctx, *listen, token); err != nil {
			logger.Printf("listen failed: %v", err)
			return 1
		}
		return 0
	}
	logger.Printf("grid-node %s starting: %s as %s (workspace %s)", node.Version, *server, device.Name, workspace)
	if err := device.Dial(ctx, *server, token); err != nil {
		logger.Printf("%v", err)
		return 1
	}
	return 0
}

// buildNode turns the requested drivers into one node.
func buildNode(name, kind, workspace, directory string, drivers []string, headless bool, profile, browserPath, server, token string, logger *log.Logger) (*node.Node, error) {
	if len(drivers) == 0 {
		drivers = []string{"all"}
	}
	if contains(drivers, "all") {
		drivers = []string{"fs", "shell", "browser", "tools"}
	}
	files := fsdriver.New(workspace)
	device := &node.Node{
		Name:           name,
		Kind:           kind,
		DefaultWorkdir: workspace,
		StateDir:       directory,
		Log:            logger,
		Update: func(ctx context.Context, url, sha string) error {
			return selfupdate.Update(ctx, url, sha)
		},
	}
	capabilities := []string{}
	for _, driver := range drivers {
		switch driver {
		case "fs":
			device.Tools = append(device.Tools, files.Tools()...)
			capabilities = append(capabilities, "fs")
		case "shell":
			device.Tools = append(device.Tools, shelldriver.New(files).Tools()...)
			capabilities = append(capabilities, "shell")
		case "browser":
			browser := browserdriver.New(directory, profile, headless)
			browser.Executable = browserPath
			browser.Log = logger
			device.Tools = append(device.Tools, browser.Tools()...)
			capabilities = append(capabilities, "browser")
			device.View = true
			device.ViewStart = func(ctx context.Context, sink func(map[string]any)) error {
				return browser.StartView(ctx, browserdriver.ViewSink(sink))
			}
			device.ViewStop = func(ctx context.Context) { browser.StopView(ctx) }
			device.ViewInput = func(ctx context.Context, event map[string]any) error {
				return browser.HandleInput(ctx, event)
			}
			device.OnClose = browser.Stop
		case "tools":
			runner := toolrunner.New(directory, server, token, workspace)
			runner.Log = logger
			device.Tools = append(device.Tools, runner.Tools()...)
			capabilities = append(capabilities, "tools")
		default:
			return nil, fmt.Errorf("unknown driver %q (use fs, shell, browser, tools or all)", driver)
		}
	}
	device.Capabilities = unique(capabilities)
	if err := device.Prepare(); err != nil {
		return nil, err
	}
	return device, nil
}

func contains(list []string, value string) bool {
	for _, item := range list {
		if item == value {
			return true
		}
	}
	return false
}

func unique(list []string) []string {
	seen := map[string]bool{}
	result := []string{}
	for _, item := range list {
		if !seen[item] {
			seen[item] = true
			result = append(result, item)
		}
	}
	return result
}

// installCommand pairs the device, then registers it to start with the device.
func installCommand(args []string, stdout, stderr io.Writer, logger *log.Logger) int {
	flags := flag.NewFlagSet("grid-node install", flag.ContinueOnError)
	flags.SetOutput(stderr)
	server := flags.String("server", "", "the Grid server address")
	pair := flags.String("pair", "", "the one-time pairing code")
	name := flags.String("name", "", "the device's name")
	kind := flags.String("kind", "desktop", "desktop, browser or mobile")
	workdir := flags.String("workdir", "", "the folder tools stay inside")
	stateDirFlag := flags.String("state-dir", "", "where the token and tools live")
	driverFlag := flags.String("driver", "all", "fs,shell,browser,tools or all")
	headless := flags.Bool("headless", false, "run the browser without a window")
	printUnit := flags.Bool("print-unit", false, "print the systemd unit and exit")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	if *server == "" {
		fmt.Fprintln(stderr, "install needs --server URL (and --pair CODE for the first time)")
		return 2
	}
	directory := *stateDirFlag
	if directory == "" {
		directory = state.DefaultStateDir()
	}
	directory = state.ExpandHome(directory)
	workspace := *workdir
	if workspace == "" {
		workspace = state.DefaultWorkdir()
	}
	workspace = state.ExpandHome(workspace)

	executable, err := os.Executable()
	if err != nil {
		logger.Printf("cannot find this binary: %v", err)
		return 1
	}
	options := service.Options{
		Executable: executable,
		Server:     *server,
		Workdir:    workspace,
		StateDir:   directory,
		Driver:     *driverFlag,
		Headless:   *headless,
		Name:       *name,
		Kind:       *kind,
	}
	if *printUnit {
		fmt.Fprint(stdout, service.RenderUnit(options))
		return 0
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if *pair != "" {
		paired, path, err := node.Pair(ctx, *server, *pair, *name, *kind, directory)
		if err != nil {
			logger.Printf("pairing failed: %v", err)
			return 1
		}
		fmt.Fprintf(stdout, "Paired as %s; the token is kept in %s\n", paired.Device, path)
	} else if _, err := state.LoadToken(directory, *server); err != nil {
		fmt.Fprintf(stderr, "no saved token for %s: pass --pair CODE the first time (%v)\n", *server, err)
		return 1
	}
	message, err := service.Install(options)
	if err != nil {
		logger.Printf("install failed: %v", err)
		return 1
	}
	fmt.Fprintln(stdout, message)
	return 0
}
