package browserdriver

import (
	"context"
	"encoding/base64"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"

	"gridnode/internal/protocol"
)

// ToolSchemas are the browser tools this driver serves.
func (d *Driver) Tools() []*protocol.Tool {
	ref := protocol.PString("The element's reference from browser_snapshot, e.g. e12")
	return []*protocol.Tool{
		{
			Name:        "browser_navigate",
			Description: "Open a URL in the current tab.",
			InputSchema: protocol.Object(map[string]any{"url": protocol.PString("The page to open.")}, "url"),
			Annotations: protocol.Do,
			Handler: func(ctx protocol.Context, args map[string]any) ([]protocol.Content, error) {
				target, err := protocol.RequiredString(args, "url")
				if err != nil {
					return nil, err
				}
				where, err := d.navigateTo(asContext(ctx), target)
				if err != nil {
					return nil, err
				}
				return []protocol.Content{protocol.Text(fmt.Sprintf("Opened %s. Call browser_snapshot to read it.", where))}, nil
			},
		},
		{
			Name:        "browser_snapshot",
			Description: "Read the current page as a text tree; every element carries [ref=…] to act on.",
			InputSchema: protocol.Object(map[string]any{}),
			Annotations: protocol.Look,
			Handler: func(ctx protocol.Context, args map[string]any) ([]protocol.Content, error) {
				page, err := d.use(asContext(ctx))
				if err != nil {
					return nil, err
				}
				tree, err := page.Evaluate(asContext(ctx), snapshotJS)
				if err != nil {
					return nil, err
				}
				where, err := d.where(asContext(ctx))
				if err != nil {
					return nil, err
				}
				if strings.TrimSpace(tree) == "" {
					tree = "(the page shows no readable elements)"
				}
				return []protocol.Content{protocol.Text("Page: " + where + "\n\n" + tree)}, nil
			},
		},
		{
			Name:        "browser_screenshot",
			Description: "Take a screenshot of the current page (for a model that sees).",
			InputSchema: protocol.Object(map[string]any{"full_page": protocol.PBool("Capture the whole page, not just the viewport.", false)}),
			Annotations: protocol.Look,
			Handler: func(ctx protocol.Context, args map[string]any) ([]protocol.Content, error) {
				return d.screenshot(asContext(ctx), protocol.Bool(args, "full_page", false))
			},
		},
		{
			Name:        "browser_click",
			Description: "Click an element by its ref, or a point by x and y (CSS pixels).",
			InputSchema: protocol.Object(map[string]any{
				"ref": ref, "x": protocol.PNumber("Point to click, x.", 0), "y": protocol.PNumber("Point to click, y.", 0),
				"double":    protocol.PBool("Double-click instead.", false),
				"confirmed": protocol.PBool("Set by the server once a person confirmed; leave it out.", false),
			}),
			Annotations: protocol.Do,
			Handler: func(ctx protocol.Context, args map[string]any) ([]protocol.Content, error) {
				return d.click(asContext(ctx), args)
			},
		},
		{
			Name:        "browser_type",
			Description: "Type text into an element by its ref; submit presses Enter after it.",
			InputSchema: protocol.Object(map[string]any{
				"ref": ref, "text": protocol.PString("The text to type."),
				"submit":    protocol.PBool("Press Enter after typing.", false),
				"clear":     protocol.PBool("Clear the field first (default true).", true),
				"confirmed": protocol.PBool("Set by the server once a person confirmed; leave it out.", false),
			}, "text"),
			Annotations: protocol.Do,
			Handler: func(ctx protocol.Context, args map[string]any) ([]protocol.Content, error) {
				return d.types(asContext(ctx), args)
			},
		},
		{
			Name:        "browser_press",
			Description: "Press a key or a combination, e.g. Enter, Escape, Control+A.",
			InputSchema: protocol.Object(map[string]any{
				"key":       protocol.PString("The key or combination."),
				"confirmed": protocol.PBool("Set by the server once a person confirmed; leave it out.", false),
			}, "key"),
			Annotations: protocol.Do,
			Handler: func(ctx protocol.Context, args map[string]any) ([]protocol.Content, error) {
				key, err := protocol.RequiredString(args, "key")
				if err != nil {
					return nil, err
				}
				page, err := d.use(asContext(ctx))
				if err != nil {
					return nil, err
				}
				if err := d.pressKey(asContext(ctx), page, key); err != nil {
					return nil, err
				}
				return []protocol.Content{protocol.Text("Pressed " + key + ".")}, nil
			},
		},
		{
			Name:        "browser_scroll",
			Description: "Scroll the page by pixels (positive down/right).",
			InputSchema: protocol.Object(map[string]any{
				"dy": protocol.PNumber("Vertical pixels (default 600).", 600),
				"dx": protocol.PNumber("Horizontal pixels (default 0).", 0),
			}),
			Annotations: protocol.Do,
			Handler: func(ctx protocol.Context, args map[string]any) ([]protocol.Content, error) {
				page, err := d.use(asContext(ctx))
				if err != nil {
					return nil, err
				}
				dx := protocol.Float(args, "dx", 0)
				dy := protocol.Float(args, "dy", 600)
				if _, err := page.Send(asContext(ctx), "Input.dispatchMouseEvent", map[string]any{
					"type": "mouseWheel", "x": 640, "y": 400, "deltaX": dx, "deltaY": dy,
				}); err != nil {
					return nil, err
				}
				return []protocol.Content{protocol.Text(fmt.Sprintf("Scrolled by dx=%g dy=%g.", dx, dy))}, nil
			},
		},
		{
			Name:        "browser_history",
			Description: "Go back or forward in the current tab.",
			InputSchema: protocol.Object(map[string]any{
				"direction": map[string]any{"type": "string", "enum": []string{"back", "forward"}, "description": "Which way."},
			}, "direction"),
			Annotations: protocol.Do,
			Handler: func(ctx protocol.Context, args map[string]any) ([]protocol.Content, error) {
				return d.history(asContext(ctx), protocol.String(args, "direction", "back"))
			},
		},
		{
			Name:        "browser_tabs",
			Description: "List tabs, open a new one, switch to one or close one.",
			InputSchema: protocol.Object(map[string]any{
				"action": map[string]any{"type": "string", "enum": []string{"list", "new", "select", "close"}, "description": "What to do."},
				"index":  protocol.PInt("The tab's index from list.", 0),
				"url":    protocol.PString("The URL a new tab opens; defaults to about:blank."),
			}, "action"),
			Annotations: protocol.Do,
			Handler: func(ctx protocol.Context, args map[string]any) ([]protocol.Content, error) {
				return d.tabs(asContext(ctx), protocol.String(args, "action", "list"), protocol.Int(args, "index", 0), protocol.String(args, "url", ""))
			},
		},
		{
			Name:        "browser_wait",
			Description: "Wait for text to appear on the page, or for some seconds (at most 30).",
			InputSchema: protocol.Object(map[string]any{
				"text":    protocol.PString("Wait until the page contains this text."),
				"seconds": protocol.PNumber("How long to wait when no text is given (default 2).", 2),
			}),
			Annotations: protocol.Look,
			Handler: func(ctx protocol.Context, args map[string]any) ([]protocol.Content, error) {
				return d.wait(asContext(ctx), protocol.String(args, "text", ""), protocol.Float(args, "seconds", 2))
			},
		},
	}
}

func asContext(ctx protocol.Context) context.Context {
	if value, ok := ctx.(context.Context); ok {
		return value
	}
	return context.Background()
}

func (d *Driver) screenshot(ctx context.Context, fullPage bool) ([]protocol.Content, error) {
	page, err := d.use(ctx)
	if err != nil {
		return nil, err
	}
	result, err := page.Send(ctx, "Page.captureScreenshot", map[string]any{
		"format": "png", "captureBeyondViewport": fullPage, "fromSurface": true,
	})
	if err != nil {
		return nil, err
	}
	var answer struct {
		Data string `json:"data"`
	}
	if err := jsonUnmarshal(result, &answer); err != nil {
		return nil, err
	}
	if answer.Data == "" {
		return nil, fmt.Errorf("Chrome returned an empty screenshot")
	}
	if _, err := base64.StdEncoding.DecodeString(answer.Data); err != nil {
		return nil, err
	}
	where, err := d.where(ctx)
	if err != nil {
		where = "the page"
	}
	return []protocol.Content{protocol.Text("Screenshot of " + where), protocol.ImageBase64(answer.Data, "image/png")}, nil
}

func (d *Driver) click(ctx context.Context, args map[string]any) ([]protocol.Content, error) {
	page, err := d.use(ctx)
	if err != nil {
		return nil, err
	}
	double := protocol.Bool(args, "double", false)
	ref := protocol.String(args, "ref", "")
	if ref != "" {
		script := fmt.Sprintf(`(() => {
			const el = document.querySelector('[data-grid-ref=%q]');
			if (!el) return 'missing';
			el.scrollIntoView({block:'center', inline:'center'});
			el.focus && el.focus();
			el.click();
			if (%t) el.dispatchEvent(new MouseEvent('dblclick', {bubbles:true, cancelable:true}));
			return 'clicked';
		})()`, ref, double)
		result, err := page.Evaluate(ctx, script)
		if err != nil {
			return nil, err
		}
		if result == "missing" {
			return nil, fmt.Errorf("no element with ref %s on the page; call browser_snapshot again", ref)
		}
		where, _ := d.where(ctx)
		return []protocol.Content{protocol.Text("Clicked " + ref + " on " + where + ".")}, nil
	}
	x := protocol.Float(args, "x", -1)
	y := protocol.Float(args, "y", -1)
	if x < 0 || y < 0 {
		return nil, fmt.Errorf("give ref, or x and y")
	}
	clicks := 1
	if double {
		clicks = 2
	}
	for count := 1; count <= clicks; count++ {
		if _, err := page.Send(ctx, "Input.dispatchMouseEvent", map[string]any{
			"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": count,
		}); err != nil {
			return nil, err
		}
		if _, err := page.Send(ctx, "Input.dispatchMouseEvent", map[string]any{
			"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": count,
		}); err != nil {
			return nil, err
		}
	}
	return []protocol.Content{protocol.Text(fmt.Sprintf("Clicked at %g,%g.", x, y))}, nil
}

func (d *Driver) types(ctx context.Context, args map[string]any) ([]protocol.Content, error) {
	page, err := d.use(ctx)
	if err != nil {
		return nil, err
	}
	text := protocol.String(args, "text", "")
	ref := protocol.String(args, "ref", "")
	clear := protocol.Bool(args, "clear", true)
	if ref != "" {
		script := fmt.Sprintf(`(() => {
			const el = document.querySelector('[data-grid-ref=%q]');
			if (!el) return 'missing';
			el.scrollIntoView({block:'center'});
			el.focus && el.focus();
			if (%t) {
				const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
				const setter = Object.getOwnPropertyDescriptor(proto, 'value');
				if (setter && setter.set) setter.set.call(el, ''); else el.value = '';
				el.dispatchEvent(new Event('input', {bubbles:true}));
			}
			return 'ok';
		})()`, ref, clear)
		result, err := page.Evaluate(ctx, script)
		if err != nil {
			return nil, err
		}
		if result == "missing" {
			return nil, fmt.Errorf("no element with ref %s on the page; call browser_snapshot again", ref)
		}
	}
	if text != "" {
		if _, err := page.Send(ctx, "Input.insertText", map[string]any{"text": text}); err != nil {
			return nil, err
		}
	}
	if protocol.Bool(args, "submit", false) {
		if err := d.pressKey(ctx, page, "Enter"); err != nil {
			return nil, err
		}
	}
	where, _ := d.where(ctx)
	return []protocol.Content{protocol.Text(fmt.Sprintf("Typed %d character(s) into %s.", len([]rune(text)), where))}, nil
}

func (d *Driver) history(ctx context.Context, direction string) ([]protocol.Content, error) {
	page, err := d.use(ctx)
	if err != nil {
		return nil, err
	}
	result, err := page.Send(ctx, "Page.getNavigationHistory", map[string]any{})
	if err != nil {
		return nil, err
	}
	var history struct {
		CurrentIndex int `json:"currentIndex"`
		Entries      []struct {
			ID    int    `json:"id"`
			URL   string `json:"url"`
			Title string `json:"title"`
		} `json:"entries"`
	}
	if err := jsonUnmarshal(result, &history); err != nil {
		return nil, err
	}
	target := history.CurrentIndex
	if direction == "forward" {
		target++
	} else {
		target--
	}
	if target < 0 || target >= len(history.Entries) {
		return nil, fmt.Errorf("no %s entry in this tab's history", direction)
	}
	if _, err := page.Send(ctx, "Page.navigateToHistoryEntry", map[string]any{"entryId": history.Entries[target].ID}); err != nil {
		return nil, err
	}
	time.Sleep(300 * time.Millisecond)
	where, err := d.where(ctx)
	if err != nil {
		return nil, err
	}
	return []protocol.Content{protocol.Text("Went " + direction + " to " + where + ".")}, nil
}

func (d *Driver) tabs(ctx context.Context, action string, index int, target string) ([]protocol.Content, error) {
	if _, err := d.use(ctx); err != nil {
		return nil, err
	}
	switch action {
	case "new":
		if target == "" {
			target = "about:blank"
		}
		if !strings.Contains(target, "://") && target != "about:blank" {
			target = "https://" + target
		}
		if _, err := d.httpJSON(ctx, http.MethodPut, "/json/new?"+url.QueryEscape(target)); err != nil {
			// Older Chrome only accepts GET.
			if _, err := d.httpJSON(ctx, http.MethodGet, "/json/new?"+url.QueryEscape(target)); err != nil {
				return nil, err
			}
		}
		time.Sleep(300 * time.Millisecond)
		page, err := d.connectPage(ctx)
		if err != nil {
			return nil, err
		}
		d.mu.Lock()
		d.page = page
		d.mu.Unlock()
		d.watchScreencast(page)
	case "select":
		pages, err := d.targets(ctx)
		if err != nil {
			return nil, err
		}
		pages = pageOnly(pages)
		if index < 0 || index >= len(pages) {
			return nil, fmt.Errorf("no tab with index %d", index)
		}
		page, err := dialCDP(ctx, pages[index].WebSocketDebuggerURL)
		if err != nil {
			return nil, err
		}
		_, _ = page.Send(ctx, "Page.enable", map[string]any{})
		_, _ = page.Send(ctx, "Runtime.enable", map[string]any{})
		d.mu.Lock()
		old := d.page
		d.page = page
		viewing := d.viewing
		d.viewing = false
		d.mu.Unlock()
		if old != nil {
			old.Close()
		}
		d.watchScreencast(page)
		if viewing {
			_ = d.StartView(ctx, d.currentSink())
		}
	case "close":
		pages, err := d.targets(ctx)
		if err != nil {
			return nil, err
		}
		pages = pageOnly(pages)
		if index < 0 || index >= len(pages) {
			return nil, fmt.Errorf("no tab with index %d", index)
		}
		if _, err := d.httpJSON(ctx, http.MethodGet, "/json/close/"+url.PathEscape(pages[index].ID)); err != nil {
			return nil, err
		}
		time.Sleep(200 * time.Millisecond)
		page, err := d.connectPage(ctx)
		if err != nil {
			return nil, err
		}
		d.mu.Lock()
		d.page = page
		d.mu.Unlock()
		d.watchScreencast(page)
	}
	pages, err := d.targets(ctx)
	if err != nil {
		return nil, err
	}
	pages = pageOnly(pages)
	lines := []string{fmt.Sprintf("%d tab(s):", len(pages))}
	for position, target := range pages {
		title := target.Title
		if title == "" {
			title = "(no title)"
		}
		lines = append(lines, fmt.Sprintf("%d. %s - %s", position, title, target.URL))
	}
	return []protocol.Content{protocol.Text(strings.Join(lines, "\n"))}, nil
}

func (d *Driver) currentSink() ViewSink {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.sink
}

func pageOnly(targets []pageTarget) []pageTarget {
	pages := []pageTarget{}
	for _, target := range targets {
		if target.Type == "page" && target.WebSocketDebuggerURL != "" {
			pages = append(pages, target)
		}
	}
	return pages
}

func (d *Driver) wait(ctx context.Context, text string, seconds float64) ([]protocol.Content, error) {
	page, err := d.use(ctx)
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(text) == "" {
		if seconds <= 0 {
			seconds = 2
		}
		if seconds > 30 {
			seconds = 30
		}
		select {
		case <-time.After(time.Duration(seconds * float64(time.Second))):
		case <-ctx.Done():
			return nil, ctx.Err()
		}
		return []protocol.Content{protocol.Text(fmt.Sprintf("Waited %g seconds.", seconds))}, nil
	}
	if seconds <= 0 {
		seconds = 15
	}
	if seconds > 30 {
		seconds = 30
	}
	deadline := time.Now().Add(time.Duration(seconds * float64(time.Second)))
	encoded := jsQuote(text)
	for time.Now().Before(deadline) {
		found, err := page.Evaluate(ctx, "(document.body ? document.body.innerText : '').includes("+encoded+")")
		if err == nil && found == "true" {
			return []protocol.Content{protocol.Text(fmt.Sprintf("Found %q on the page.", text))}, nil
		}
		select {
		case <-time.After(250 * time.Millisecond):
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	return []protocol.Content{protocol.Text(fmt.Sprintf("Waited %g seconds; %q did not appear.", seconds, text))}, nil
}

// snapshotJS reads the page as an outline and gives every actionable element a ref.
const snapshotJS = `(() => {
  const out = [];
  let n = 0;
  const visible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const box = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return box.width > 1 && box.height > 1 && style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
  };
  const nameOf = (el) => {
    const raw = el.getAttribute('aria-label') || el.getAttribute('alt') || el.getAttribute('placeholder') ||
      el.getAttribute('title') || el.innerText || el.value || '';
    return String(raw).replace(/\s+/g, ' ').trim().slice(0, 160);
  };
  const roles = {A: 'link', BUTTON: 'button', INPUT: 'textbox', SELECT: 'combobox', TEXTAREA: 'textbox',
    IMG: 'image', H1: 'heading', H2: 'heading', H3: 'heading', H4: 'heading', LABEL: 'label'};
  const roleOf = (el) => el.getAttribute('role') || roles[el.tagName] || el.tagName.toLowerCase();
  const actionable = (el) => /^(A|BUTTON|SELECT|TEXTAREA|INPUT)$/.test(el.tagName) ||
    el.hasAttribute('role') || el.hasAttribute('onclick') || el.isContentEditable;
  const selector = 'a,button,input,select,textarea,[role],[onclick],[contenteditable],h1,h2,h3,h4,label,img';
  for (const el of document.querySelectorAll(selector)) {
    if (!visible(el)) continue;
    let line = '- ' + roleOf(el);
    const name = nameOf(el);
    if (name) line += ' "' + name + '"';
    if (el.tagName === 'INPUT') {
      line += ' [' + (el.type || 'text') + (el.disabled ? ', disabled' : '') + ']';
    }
    if (actionable(el)) {
      const ref = 'e' + (++n);
      el.setAttribute('data-grid-ref', ref);
      line += ' [ref=' + ref + ']';
    }
    out.push(line);
  }
  const body = (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n').trim();
  let text = out.join('\n');
  if (text.length > 30000) text = text.slice(0, 30000) + '\n… [more elements not shown]';
  if (body) text += '\n\n-- visible text --\n' + body.slice(0, 8000);
  return text.slice(0, 40000);
})()`

func jsQuote(value string) string {
	replaced := strings.NewReplacer(`\`, `\\`, `"`, `\"`, "\n", `\n`, "\r", `\r`, "\t", `\t`).Replace(value)
	return `"` + replaced + `"`
}

func jsonUnmarshal(raw []byte, target any) error {
	return jsonUnmarshalImpl(raw, target)
}
