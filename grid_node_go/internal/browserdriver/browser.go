// Package browserdriver drives a real Chrome, Edge, Brave or Chromium over the
// DevTools Protocol, without Playwright or Node.js: it launches the browser on
// a persistent profile, reads pages as text with element references, acts by
// reference or by point, and streams the page to the live view.
package browserdriver

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"

	"gridnode/internal/protocol"
)

// ViewSink takes the frames of the screen for the server's live view.
type ViewSink func(frame map[string]any)

// Driver is the browser of one node; it starts on the first tool that needs it.
type Driver struct {
	StateDir   string
	Profile    string
	Headless   bool
	Executable string
	Log        *log.Logger

	mu         sync.Mutex
	cmd        *exec.Cmd
	page       *client
	browserURL string
	httpBase   string
	sink       ViewSink
	viewing    bool
}

// New makes the driver; the profile lives in <state-dir>/profile.
func New(stateDir, profile string, headless bool) *Driver {
	if profile == "" {
		profile = filepath.Join(stateDir, "profile")
	}
	return &Driver{StateDir: stateDir, Profile: profile, Headless: headless, Log: log.Default()}
}

// Capabilities says what this driver adds to a node's hello.
func Capabilities() []string { return []string{"browser", "screen"} }

// findExecutable looks for a Chromium-family browser on the device.
func findExecutable(configured string) (string, error) {
	if configured != "" {
		if info, err := os.Stat(configured); err == nil && !info.IsDir() {
			return configured, nil
		}
		return "", fmt.Errorf("configured browser %s does not exist", configured)
	}
	candidates := []string{}
	switch runtime.GOOS {
	case "windows":
		roots := []string{os.Getenv("ProgramFiles"), os.Getenv("ProgramFiles(x86)"), os.Getenv("LOCALAPPDATA")}
		relative := []string{
			`Google\Chrome\Application\chrome.exe`,
			`Microsoft\Edge\Application\msedge.exe`,
			`BraveSoftware\Brave-Browser\Application\brave.exe`,
			`Chromium\Application\chrome.exe`,
		}
		for _, root := range roots {
			if root == "" {
				continue
			}
			for _, path := range relative {
				candidates = append(candidates, filepath.Join(root, path))
			}
		}
	case "darwin":
		candidates = append(candidates,
			"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
			"/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
			"/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
			"/Applications/Chromium.app/Contents/MacOS/Chromium",
		)
	default:
		candidates = append(candidates,
			"/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
			"/usr/bin/chromium", "/usr/bin/chromium-browser",
			"/snap/bin/chromium", "/snap/bin/google-chrome",
			"/opt/google/chrome/chrome", "/usr/bin/microsoft-edge",
			"/usr/bin/brave-browser",
		)
	}
	for _, name := range []string{"google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "brave-browser", "chrome"} {
		if path, err := exec.LookPath(name); err == nil {
			candidates = append(candidates, path)
		}
	}
	for _, candidate := range candidates {
		if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
			return candidate, nil
		}
	}
	return "", fmt.Errorf("no Chrome, Chromium, Edge or Brave found on this device")
}

// start launches the browser and connects to its first page.
func (d *Driver) start(ctx context.Context) error {
	if d.page != nil {
		select {
		case <-d.page.closed:
			d.page = nil
		default:
			return nil
		}
	}
	executable, err := findExecutable(d.Executable)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(d.Profile, 0o755); err != nil {
		return err
	}
	args := []string{
		"--remote-debugging-port=0",
		"--user-data-dir=" + d.Profile,
		"--no-first-run",
		"--no-default-browser-check",
		"--disable-background-networking",
		"--disable-background-timer-throttling",
		"--disable-renderer-backgrounding",
		"--disable-features=Translate,MediaRouter",
		"--window-size=1280,800",
		"--window-position=0,0",
	}
	if d.Headless {
		args = append(args, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--mute-audio")
	}
	if runtime.GOOS == "linux" {
		args = append(args, "--no-sandbox", "--disable-dev-shm-usage")
	}
	args = append(args, "about:blank")
	cmd := exec.Command(executable, args...)
	cmd.Env = append(os.Environ(), "TZ="+envOr("TZ", "UTC"))
	prepareBrowser(cmd)
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return err
	}
	if d.Log != nil {
		d.Log.Printf("starting browser %s", executable)
	}
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("cannot start %s: %w", executable, err)
	}
	d.cmd = cmd
	devtools := make(chan string, 1)
	go func() {
		reader := bufio.NewReaderSize(stderr, 64*1024)
		for {
			line, err := reader.ReadString('\n')
			if strings.Contains(line, "DevTools listening on ") {
				url := strings.TrimSpace(strings.SplitN(line, "DevTools listening on ", 2)[1])
				select {
				case devtools <- url:
				default:
				}
			}
			if err != nil {
				if err != io.EOF && d.Log != nil {
					d.Log.Printf("browser stderr: %v", err)
				}
				return
			}
		}
	}()
	go func() {
		if err := cmd.Wait(); err != nil && d.Log != nil {
			d.Log.Printf("browser exited: %v", err)
		}
	}()

	var browserURL string
	select {
	case browserURL = <-devtools:
	case <-time.After(30 * time.Second):
		d.stop()
		return fmt.Errorf("the browser did not open a DevTools port in time")
	case <-ctx.Done():
		d.stop()
		return ctx.Err()
	}
	d.browserURL = browserURL
	parsed, err := url.Parse(browserURL)
	if err != nil {
		d.stop()
		return err
	}
	d.httpBase = "http://" + parsed.Host
	page, err := d.connectPage(ctx)
	if err != nil {
		d.stop()
		return err
	}
	d.page = page
	d.watchScreencast(page)
	return nil
}

func envOr(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

// httpJSON reads one of the browser's JSON endpoints (/json/list, /json/new, ...).
func (d *Driver) httpJSON(ctx context.Context, method, path string) ([]byte, error) {
	request, err := http.NewRequestWithContext(ctx, method, d.httpBase+path, nil)
	if err != nil {
		return nil, err
	}
	response, err := (&http.Client{Timeout: 30 * time.Second}).Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, 8*1024*1024))
	if err != nil {
		return nil, err
	}
	if response.StatusCode >= 400 {
		return nil, fmt.Errorf("DevTools HTTP %d: %s", response.StatusCode, strings.TrimSpace(string(body)))
	}
	return body, nil
}

type pageTarget struct {
	ID                   string `json:"id"`
	Type                 string `json:"type"`
	URL                  string `json:"url"`
	Title                string `json:"title"`
	WebSocketDebuggerURL string `json:"webSocketDebuggerUrl"`
}

func (d *Driver) targets(ctx context.Context) ([]pageTarget, error) {
	body, err := d.httpJSON(ctx, http.MethodGet, "/json/list")
	if err != nil {
		return nil, err
	}
	var targets []pageTarget
	if err := json.Unmarshal(body, &targets); err != nil {
		return nil, err
	}
	return targets, nil
}

func (d *Driver) connectPage(ctx context.Context) (*client, error) {
	deadline := time.Now().Add(30 * time.Second)
	for {
		targets, err := d.targets(ctx)
		if err == nil {
			for _, target := range targets {
				if target.Type == "page" && target.WebSocketDebuggerURL != "" {
					page, err := dialCDP(ctx, target.WebSocketDebuggerURL)
					if err != nil {
						return nil, err
					}
					_, _ = page.Send(ctx, "Page.enable", map[string]any{})
					_, _ = page.Send(ctx, "Runtime.enable", map[string]any{})
					_, _ = page.Send(ctx, "Page.setViewport", map[string]any{"width": 1280, "height": 800})
					return page, nil
				}
			}
			// No page yet: open one.
			if _, err := d.httpJSON(ctx, http.MethodPut, "/json/new?about:blank"); err != nil {
				if _, err := d.httpJSON(ctx, http.MethodGet, "/json/new?about:blank"); err != nil && d.Log != nil {
					d.Log.Printf("cannot open a page: %v", err)
				}
			}
			time.Sleep(200 * time.Millisecond)
			continue
		}
		if time.Now().After(deadline) {
			return nil, fmt.Errorf("cannot reach the browser's DevTools endpoint: %w", err)
		}
		time.Sleep(200 * time.Millisecond)
	}
}

// use returns the page, starting the browser the first time it is needed.
func (d *Driver) use(ctx context.Context) (*client, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if err := d.start(ctx); err != nil {
		return nil, err
	}
	return d.page, nil
}

func (d *Driver) navigateTo(ctx context.Context, url string) (string, error) {
	page, err := d.use(ctx)
	if err != nil {
		return "", err
	}
	if !strings.Contains(url, "://") {
		url = "https://" + url
	}
	result, err := page.Send(ctx, "Page.navigate", map[string]any{"url": url})
	if err != nil {
		return "", err
	}
	var answer struct {
		ErrorText string `json:"errorText"`
		FrameID   string `json:"frameId"`
	}
	_ = json.Unmarshal(result, &answer)
	if answer.ErrorText != "" {
		return "", fmt.Errorf("cannot open %s: %s", url, answer.ErrorText)
	}
	deadline := time.Now().Add(45 * time.Second)
	for time.Now().Before(deadline) {
		state, err := page.Evaluate(ctx, "document.readyState")
		if err == nil && state == "complete" {
			break
		}
		time.Sleep(150 * time.Millisecond)
	}
	return d.where(ctx)
}

// where describes the current page for a tool's answer.
func (d *Driver) where(ctx context.Context) (string, error) {
	page := d.page
	if page == nil {
		return "", fmt.Errorf("the browser is not running")
	}
	text, err := page.Evaluate(ctx, "JSON.stringify({title: document.title || '', url: location.href, ready: document.readyState})")
	if err != nil {
		return "", err
	}
	var info struct {
		Title string `json:"title"`
		URL   string `json:"url"`
		Ready string `json:"ready"`
	}
	_ = json.Unmarshal([]byte(text), &info)
	title := info.Title
	if title == "" {
		title = "(no title)"
	}
	return fmt.Sprintf("%s - %s", title, info.URL), nil
}

// stop closes the browser and its profile lock.
func (d *Driver) stop() {
	if d.page != nil {
		d.page.Close()
		d.page = nil
	}
	if d.cmd != nil && d.cmd.Process != nil {
		closeBrowser(d.cmd)
		d.cmd = nil
	}
}

// Stop is the driver's shutdown hook.
func (d *Driver) Stop() {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.stop()
}

// -- the live view -------------------------------------------------------------

func (d *Driver) watchScreencast(page *client) {
	page.On("Page.screencastFrame", func(raw json.RawMessage) {
		var event struct {
			Data      string `json:"data"`
			SessionID int    `json:"sessionId"`
			Metadata  struct {
				Width  float64 `json:"deviceWidth"`
				Height float64 `json:"deviceHeight"`
			} `json:"metadata"`
		}
		if err := json.Unmarshal(raw, &event); err != nil {
			return
		}
		_, _ = page.Send(context.Background(), "Page.screencastFrameAck", map[string]any{"sessionId": event.SessionID})
		d.mu.Lock()
		sink := d.sink
		d.mu.Unlock()
		if sink == nil {
			return
		}
		frame := map[string]any{"type": "frame", "data": event.Data, "mime": "image/jpeg"}
		if event.Metadata.Width > 0 {
			frame["width"] = int(event.Metadata.Width)
			frame["height"] = int(event.Metadata.Height)
		}
		sink(frame)
	})
}

// StartView starts streaming the page to sink.
func (d *Driver) StartView(ctx context.Context, sink ViewSink) error {
	d.mu.Lock()
	d.sink = sink
	already := d.viewing
	d.viewing = true
	d.mu.Unlock()
	if already {
		return nil
	}
	page, err := d.use(ctx)
	if err != nil {
		return err
	}
	_, err = page.Send(ctx, "Page.startScreencast", map[string]any{
		"format": "jpeg", "quality": 65, "maxWidth": 1280, "maxHeight": 800, "everyNthFrame": 1,
	})
	return err
}

// StopView ends the stream.
func (d *Driver) StopView(ctx context.Context) {
	d.mu.Lock()
	d.sink = nil
	wasViewing := d.viewing
	d.viewing = false
	page := d.page
	d.mu.Unlock()
	if wasViewing && page != nil {
		_, _ = page.Send(ctx, "Page.stopScreencast", map[string]any{})
	}
}

// HandleInput applies a person's input from the live view.
func (d *Driver) HandleInput(ctx context.Context, event map[string]any) error {
	kind := protocol.String(event, "kind", "")
	page, err := d.use(ctx)
	if err != nil {
		return err
	}
	x := protocol.Float(event, "x", 640)
	y := protocol.Float(event, "y", 400)
	switch kind {
	case "click":
		button := protocol.String(event, "button", "left")
		_, err = page.Send(ctx, "Input.dispatchMouseEvent", map[string]any{
			"type": "mousePressed", "x": x, "y": y, "button": button, "clickCount": 1,
		})
		if err == nil {
			_, err = page.Send(ctx, "Input.dispatchMouseEvent", map[string]any{
				"type": "mouseReleased", "x": x, "y": y, "button": button, "clickCount": 1,
			})
		}
		return err
	case "wheel":
		_, err = page.Send(ctx, "Input.dispatchMouseEvent", map[string]any{
			"type": "mouseWheel", "x": x, "y": y,
			"deltaX": protocol.Float(event, "dx", 0), "deltaY": protocol.Float(event, "dy", 0),
		})
		return err
	case "text":
		_, err = page.Send(ctx, "Input.insertText", map[string]any{"text": protocol.String(event, "text", "")})
		return err
	case "key":
		return d.pressKey(ctx, page, protocol.String(event, "key", ""))
	case "navigate":
		_, err = d.navigateTo(ctx, protocol.String(event, "url", ""))
		return err
	}
	return nil
}
