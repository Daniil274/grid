"""Real Chromium mobile regression for the map page's canonical views.

Optional: pip install pytest playwright; PLAYWRIGHT_BROWSERS_PATH=...
playwright install chromium, then pytest --noconftest this file.
All routes are synthetic; no network or live project is used.

What is checked on a phone-sized viewport:

* the page opens in *Layers* - the 3D `space.js` / `space_math.js` modules are
  never requested, and no canvas/WebGL context exists;
* the four tabs are exactly Project / Files / Chats / Work - no legacy Agents
  or context island anywhere;
* the base map answer carries no transcript: the chat detail is a *second*,
  lazy read of `GET /api/projects/prj-mobile/map/chats/chat-mobile-a`, made
  when the chat is selected, and a 304 refresh asks for it again never;
* that detail is drawn as the canonical tree - Chat -> Messages / Executions
  -> Operations, with the delegated child execution nested under the `agent`
  operation and its own steps under it;
* a step's file chip opens the real file, and every row keeps its Open link;
* nothing overflows horizontally and every row/tab is a 44px touch target;
* choosing Space is the only thing that imports the 3D module (and a browser
  without WebGL2 gets an honest line and Layers, not a blank panel).

The desktop Space visual is accepted separately; this file only guards the
mobile defaults, the lazy detail boundary and the canonical hierarchy.
"""
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

playwright = pytest.importorskip('playwright.sync_api')
WEB = Path(__file__).resolve().parents[1] / 'web_chat'


def base_map(failed=False):
    """GET /api/projects/prj-mobile/map: a summary, and not one message."""
    tasks = {'available': True, 'items': [
        {'id': 'prj-mobile-a', 'title': 'Readable synthetic task', 'state': 'ready',
         'status': 'open', 'created_at': '2026-10-01T09:00:00Z', 'depends_on': []},
        {'id': 'prj-mobile-b', 'title': 'A second synthetic task', 'state': 'blocked',
         'status': 'blocked', 'created_at': '2026-10-02T09:00:00Z', 'depends_on': ['prj-mobile-a']},
    ], 'total': 2, 'counts': {'ready': 1, 'blocked': 1}}
    if failed:
        tasks = {'available': False, 'items': [], 'reason_code': 'bd_failed',
                 'reason': "The project's local tracker could not be read.",
                 'diagnosis': {'message': 'The tracker database could not be opened.', 'exit_code': 1}}
    return {
        'project': {'id': 'prj-mobile', 'title': 'Synthetic mobile project'},
        'path': [{'id': 'prj-mobile', 'title': 'Synthetic mobile project'}],
        'tasks': tasks,
        'chats': {'available': True, 'items': [
            {'chat_id': 'chat-mobile-a', 'title': 'A synthetic working chat',
             'active': True, 'awaiting_approval': False,
             'updated_at': '2026-10-04T10:00:00Z',
             'task_links': [{'task_id': 'prj-mobile-a', 'relation': 'turn'}],
             'task_links_total': 1,
             'task_link_counts': {'turn': 1, 'plan': 0}},
        ], 'total': 1},
        'files': {'available': True, 'items': [
            {'name': 'docs', 'path': 'docs', 'kind': 'dir', 'modified': '2026-10-04T10:00:00Z'},
            {'name': 'README.md', 'path': 'README.md', 'kind': 'file', 'size': 120,
             'modified': '2026-10-04T09:00:00Z'},
        ], 'total': 2},
        'files_url': '/files?context=prj-mobile&path=project',
    }


def chat_detail():
    """GET /api/projects/prj-mobile/map/chats/chat-mobile-a: the safe detail.

    The SECRET fields are what a real answer never carries - and what the page
    must never print even if one did.
    """
    return {
        'chat': {'id': 'chat-mobile-a', 'title': 'A synthetic working chat',
                 'href': '/?conversation=chat-mobile-a'},
        'messages': {'items': [
            {'id': 'msg-user', 'role': 'user', 'time': '2026-10-04T09:59:00Z',
             'kind': 'prompt', 'preview': 'Draw the mobile map', 'has_image': False},
            {'id': 'msg-assistant', 'role': 'assistant', 'time': '2026-10-04T10:00:00Z',
             'kind': 'final', 'preview': 'The map is drawn', 'has_image': False,
             'turn_id': 'turn-mobile'},
        ], 'total': 2, 'truncated': False},
        'executions': {'items': [
            {'id': 'turn:turn-mobile', 'turn_id': 'turn-mobile', 'agent': 'main',
             'status': 'done', 'start_time': 1759576800, 'end_time': 1759576805,
             'duration_ms': 5000, 'error': False},
        ], 'total': 1, 'truncated': False},
        'operations': {'items': [
            {'id': 'op-read', 'execution_id': 'turn:turn-mobile', 'parent_id': None,
             'kind': 'tool', 'title': 'Read the map answer', 'status': 'done',
             'at_ms': 100, 'duration_ms': 20, 'files': ['docs/readme.md'],
             'summary': 'SECRET TOOL SUMMARY'},
            {'id': 'op-agent', 'execution_id': 'turn:turn-mobile', 'parent_id': None,
             'kind': 'agent', 'title': 'Scout', 'status': 'done', 'at_ms': 200,
             'duration_ms': 60, 'files': []},
            {'id': 'op-child', 'execution_id': 'turn:turn-mobile', 'parent_id': 'op-agent',
             'kind': 'tool', 'title': 'Child read', 'status': 'done', 'at_ms': 260,
             'duration_ms': 30, 'files': []},
            {'id': 'op-say', 'execution_id': 'turn:turn-mobile', 'parent_id': None,
             'kind': 'message', 'title': 'Answer', 'status': 'done', 'at_ms': 300,
             'duration_ms': None, 'files': [], 'summary': 'The map is drawn.'},
        ], 'total': 4, 'truncated': False},
    }


def harness_html():
    """The real page shell, with the real API client: no stubbed loaders."""
    return '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/styles/tokens.css"><link rel="stylesheet" href="/static/styles/base.css">
<link rel="stylesheet" href="/static/styles/grid.css"></head>
<body class="gridPage"><main class="gridShell">
<header class="gridHeader"><div><a href="/" class="gridBrand">GRID / MAP</a><h1 id="grid-title">Project map</h1></div>
<div class="gridActions"><label class="gridViewSwitch" for="grid-view"><span class="gridViewSwitch__label">View</span>
<select id="grid-view" class="gridViewSwitch__select"><option value="space">Space</option>
<option value="layers">Layers</option><option value="timeline">Timeline</option><option value="map">Map</option></select></label>
<button id="grid-refresh" class="btn btn--ghost" type="button">Refresh</button></div></header>
<nav id="grid-breadcrumb" class="gridCrumbs"></nav>
<div id="grid-status" class="gridStatus" role="status" aria-live="polite">Loading the map…</div>
<div class="gridBody"><div class="gridStage">
<div id="grid-map" class="gridMap__viewport" aria-label="Map of the project"></div>
<div id="grid-space" class="gridSpace__viewport" aria-hidden="true" hidden></div>
<div id="grid-projection" class="gridProjection__viewport" aria-hidden="true" hidden></div>
</div><aside id="grid-overview" class="gridOverview" aria-label="Overview"></aside></div>
</main>
<script type="module">
import { GridPage } from '/static/js/grid/page.js';
import { probeWebGL } from '/static/js/grid/view.js';
const page = new GridPage({
  status: document.getElementById('grid-status'),
  title: document.getElementById('grid-title'),
  breadcrumb: document.getElementById('grid-breadcrumb'),
  map: document.getElementById('grid-map'),
  space: document.getElementById('grid-space'),
  projection: document.getElementById('grid-projection'),
  view: document.getElementById('grid-view'),
  overview: document.getElementById('grid-overview'),
  refresh: document.getElementById('grid-refresh'),
}, {
  projectId: 'prj-mobile',
  win: window,
  doc: document,
  storage: (() => { try { return window.localStorage; } catch (error) { return null; } })(),
  webgl: probeWebGL(document),
});
window.gridPage = page;
window.pageErrors = [];
window.addEventListener('error', (event) => window.pageErrors.push(String(event.message)));
window.addEventListener('unhandledrejection', (event) => window.pageErrors.push(String(event.reason)));
await page.load();
window.ready = true;
</script>'''


class Served:
    """One synthetic server: the page shell, the modules, and a tiny API."""

    def __init__(self, failed=False):
        self.map = base_map(failed)
        self.detail = chat_detail()
        self.requested = []
        self.map_etags = []
        self.detail_etags = []

    def route(self, request):
        url = request.request.url
        parts = urlsplit(url)
        path = parts.path
        self.requested.append(path)
        if path == '/':
            request.fulfill(body=harness_html(), content_type='text/html')
        elif path.startswith(('/static/styles/', '/static/js/', '/styles/', '/js/')):
            rel = path.split('/static/', 1)[-1] if '/static/' in path else path.lstrip('/')
            target = WEB / rel
            if target.is_file() and '..' not in rel:
                kind = 'text/css' if target.suffix == '.css' else 'text/javascript'
                request.fulfill(path=str(target), content_type=kind)
            else:
                request.fulfill(status=404, body='missing')
        elif path == '/api/projects/prj-mobile/map':
            etag = request.request.headers.get('if-none-match') or ''
            self.map_etags.append(etag)
            if etag == '"one"':
                request.fulfill(status=304, headers={'ETag': '"one"'})
            else:
                request.fulfill(status=200, body=json.dumps(self.map),
                                 content_type='application/json', headers={'ETag': '"one"'})
        elif path == '/api/projects/prj-mobile/map/chats/chat-mobile-a':
            etag = request.request.headers.get('if-none-match') or ''
            self.detail_etags.append(etag)
            if etag == '"detail"':
                request.fulfill(status=304, headers={'ETag': '"detail"'})
            else:
                request.fulfill(status=200, body=json.dumps(self.detail),
                                 content_type='application/json', headers={'ETag': '"detail"'})
        elif path == '/api/workspace/tree':
            request.fulfill(status=200, content_type='application/json', body=json.dumps({
                'path': 'project/docs',
                'entries': [
                    {'name': 'readme.md', 'path': 'project/docs/readme.md', 'kind': 'file', 'size': 32},
                ],
            }))
        elif path == '/api/workspace/text':
            request.fulfill(status=200, content_type='application/json', body=json.dumps({
                'path': 'project/docs/readme.md', 'size': 32, 'truncated': False,
                'binary': False, 'text': '# The map\\nDrawn from the canonical tree.\\n',
            }))
        else:
            request.abort()

    def three_d(self):
        return [path for path in self.requested
                if path.endswith(('/grid/space.js', '/grid/space_math.js'))]


def open_page(playwright_api, width, failed=False):
    served = Served(failed=failed)
    browser = playwright_api.chromium.launch(args=['--enable-unsafe-swiftshader'])
    page = browser.new_page(viewport={'width': width, 'height': 850})
    page.add_init_script("try { localStorage.clear(); } catch (error) {}")
    page.route('**/*', served.route)
    page.goto('http://map.test/')
    page.wait_for_function('window.ready === true', timeout=15000)
    return served, browser, page


def metrics(page):
    return page.evaluate('''() => {
      const tabs = [...document.querySelectorAll('.gridProjection__tab')];
      const rows = [...document.querySelectorAll('.gridProjection__row')];
      return {
        overflow: document.documentElement.scrollWidth > innerWidth,
        mode: document.querySelector('.gridProjection')?.getAttribute('data-mode'),
        tabs: tabs.map(tab => ({id: tab.getAttribute('data-tab'), text: tab.textContent,
                                height: tab.getBoundingClientRect().height})),
        rowHeights: rows.map(row => row.getBoundingClientRect().height),
        rowCount: rows.length,
      };
    }''')


@pytest.mark.parametrize('width', [320, 390, 720])
@pytest.mark.parametrize('failed', [False, True])
def test_mobile_opens_in_layers_without_webgl(width, failed):
    with playwright.sync_playwright() as p:
        served, browser, page = open_page(p, width, failed)
        try:
            # -- the default: Layers, and no 3D module on the wire -------------
            assert page.evaluate("document.getElementById('grid-view').value") == 'layers'
            assert page.locator('#grid-projection').is_visible()
            assert not page.locator('#grid-map').is_visible()
            assert page.locator('canvas').count() == 0, 'mobile opens no WebGL canvas'
            assert served.three_d() == [], served.requested

            found = metrics(page)
            assert not found['overflow'], found
            assert found['mode'] == 'layers', found
            assert [tab['id'] for tab in found['tabs']] == ['project', 'files', 'chats', 'work'], found
            assert not any(any(word in tab['text'].lower() for word in ('agent', 'activity', 'context'))
                           for tab in found['tabs']), found
            assert all(tab['height'] >= 44 for tab in found['tabs']), found

            # -- the base map is a summary: the chat detail is not read yet ----
            assert page.evaluate("document.getElementById('grid-data') === null") is True
            assert page.evaluate("JSON.stringify(window.gridPage.map).includes('preview')") is False
            assert page.evaluate("JSON.stringify(window.gridPage.map).includes('activity')") is False
            assert page.evaluate("document.getElementById('grid-overview').innerText.includes('Draw the mobile map')") is False
            assert served.detail_etags == [], 'selecting nothing reads no chat'

            if not failed:
                # -- a Layers row still reaches the shared inspector -----------
                page.locator('.gridProjection__tab[data-tab="work"]').click()
                page.wait_for_function("document.querySelector('.gridProjection__tab[data-tab=\"work\"]')?.getAttribute('aria-selected') === 'true'")
                page.locator('.gridProjection__row[data-id="task:prj-mobile-a"] .gridProjection__title').click()
                page.wait_for_function("document.querySelector('.gridProjection__row--selected') !== null")
                overview = page.locator('#grid-overview').inner_text()
                assert 'Readable synthetic task' in overview, overview
                assert 'Open task' in overview, overview
                assert served.detail_etags == [], 'a task never reads a chat detail'

                # -- the Timeline reads the same scene, still without 3D ------
                page.select_option('#grid-view', 'timeline')
                page.wait_for_function("document.querySelector('.gridProjection[data-mode=\"timeline\"]') !== null")
                timeline = page.evaluate('''() => ({
                  groups: [...document.querySelectorAll('.gridProjection__groupLabel')].map(n => n.textContent),
                  rows: document.querySelectorAll('.gridProjection__row').length,
                  selected: document.querySelectorAll('.gridProjection__row--selected').length,
                })''')
                assert timeline['rows'] >= 3, timeline
                assert timeline['selected'] == 1, timeline
                assert served.three_d() == [], served.requested

                # -- the Map stays a real fallback, drawn in the DOM ----------
                page.select_option('#grid-view', 'map')
                page.wait_for_function("document.getElementById('grid-map').hidden === false")
                assert page.locator('.gridNode').count() >= 3
                assert page.evaluate('document.getElementById("grid-space").hidden') is True

            # -- Space is the only thing that imports the 3D module ------------
            page.select_option('#grid-view', 'space')
            page.wait_for_function(
                "document.querySelector('.grid-space-canvas') !== null || "
                "(document.getElementById('grid-view').value === 'layers' && "
                " document.getElementById('grid-status').dataset.tone === 'error')",
                timeout=15000,
            )
            assert any(path.endswith('/grid/space.js') for path in served.requested), served.requested

            page.select_option('#grid-view', 'layers')
            page.wait_for_function("document.getElementById('grid-projection').hidden === false")
            assert page.locator('.gridProjection').count() == 1
            assert page.evaluate('document.getElementById("grid-space").hidden') is True
            assert page.locator('canvas').count() == 0, 'leaving Space releases the canvas'
            assert page.evaluate('window.pageErrors') == []
        finally:
            browser.close()


def test_mobile_chat_tree_is_canonical_and_lazy():
    """Chat -> Messages / Executions -> Operations, read once, drawn exactly."""
    with playwright.sync_playwright() as p:
        served, browser, page = open_page(p, 390)
        try:
            # -- one lazy read, on selecting the chat --------------------------
            page.locator('.gridProjection__row[data-id="chat:chat-mobile-a"] .gridProjection__title').click()
            page.wait_for_function("document.querySelector('#grid-overview .gridInspector__tree') !== null", timeout=10000)
            assert served.detail_etags == [''], 'the detail is read once, without an ETag to revalidate'
            assert page.evaluate('window.gridPage.chatDetails.size') == 1

            tree = page.evaluate('''() => {
              const root = document.querySelector('#grid-overview .gridInspector__tree');
              const rows = [...root.querySelectorAll('.gridInspector__treeRow')];
              const child = root.querySelector('.gridInspector__childExecution');
              const childRow = child && child.querySelector('.gridInspector__treeRow');
              const say = root.querySelector('.gridInspector__treeRow[data-id="op-say"]');
              return {
                labels: [...root.querySelectorAll('.gridInspector__treeLabel')].map(n => n.textContent),
                rows: rows.map(r => ({kind: r.dataset.kind, id: r.dataset.id})),
                openHrefs: [...root.querySelectorAll('.gridInspector__rowOpen')].map(a => a.getAttribute('href')),
                childAgent: child && child.dataset.agent,
                childTitle: child && child.querySelector('.gridInspector__treeTitle').textContent,
                childRows: child ? [...child.querySelectorAll('.gridInspector__treeRow')].map(r => r.dataset.id) : [],
                childParent: childRow && childRow.closest('.gridInspector__childExecution') ? childRow.dataset.id : null,
                sayNested: Boolean(say && say.closest('.gridInspector__childExecution')),
                fileChips: [...root.querySelectorAll('.gridFileRef')].map(b => b.dataset.path),
                text: root.innerText,
              };
            }''')
            assert tree['labels'][:2] == ['Messages', 'Executions'], tree
            ids = [row['id'] for row in tree['rows']]
            for expected in ('msg-user', 'msg-assistant', 'turn:turn-mobile', 'op-read', 'op-agent', 'op-child', 'op-say'):
                assert expected in ids, (expected, ids)
            assert 'Draw the mobile map' in tree['text'], tree
            assert 'The map is drawn' in tree['text'], tree
            assert tree['childAgent'] == 'op-agent', tree
            assert tree['childRows'] == ['op-child'], tree
            assert tree['childParent'] == 'op-child', 'the child step lives under the delegated execution'
            assert tree['sayNested'] is False, 'a root step is not inside the child execution'
            assert tree['fileChips'] == ['project/docs/readme.md'], tree
            assert '/?conversation=chat-mobile-a' in tree['openHrefs'], tree
            assert 'SECRET' not in tree['text'], tree
            assert 'SECRET' not in page.locator('#grid-overview').inner_text()

            # -- the same hierarchy in Layers, with real parent links ---------
            layers = page.evaluate('''() => ({
              rows: [...document.querySelectorAll('.gridProjection__row')].map(r => ({
                id: r.getAttribute('data-id'), parent: r.getAttribute('data-parent'),
              })),
              openHrefs: [...document.querySelectorAll('.gridProjection__open')].map(a => a.getAttribute('href')),
              selected: document.querySelectorAll('.gridProjection__row--selected').length,
            })''')
            byId = {row['id']: row for row in layers['rows']}
            assert 'message:chat-mobile-a:msg-user' in byId, layers
            assert 'execution:chat-mobile-a:turn:turn-mobile' in byId, layers
            assert byId['execution:sub:chat-mobile-a:op-agent']['parent'] == 'operation:chat-mobile-a:op-agent', layers
            assert byId['operation:chat-mobile-a:op-child']['parent'] == 'execution:sub:chat-mobile-a:op-agent', layers
            assert byId['operation:chat-mobile-a:op-say']['parent'] == 'execution:chat-mobile-a:turn:turn-mobile', layers
            assert layers['selected'] == 1, layers
            assert '/?conversation=chat-mobile-a' in layers['openHrefs'], layers

            # -- clicking a step selects that exact node, and reads nothing ---
            page.evaluate('''() => document
              .querySelector('#grid-overview .gridInspector__treeRow[data-id="op-child"]')
              .click()''')
            page.wait_for_function(
                "document.querySelector('#grid-overview .gridInspector__treeRow[data-id=\"op-child\"]')"
                ".dataset.selected === 'true'")
            assert page.evaluate('window.gridPage.inspector.selection') == {
                'kind': 'operation', 'chatId': 'chat-mobile-a', 'id': 'op-child'}
            assert len(served.detail_etags) == 1, 'a selection inside a read chat reads nothing'

            # -- a 304 refresh keeps both the map and the read detail ---------
            page.evaluate('() => window.gridPage.load()')
            assert len(served.map_etags) == 2 and served.map_etags[-1] == '"one"', served.map_etags
            assert len(served.detail_etags) == 1, 'a 304 never re-reads the chat detail'
            assert page.evaluate('window.gridPage.chatDetails.size') == 1
            assert page.locator('#grid-overview .gridInspector__tree').count() == 1
            assert page.evaluate("document.querySelector('.gridProjection__row--selected') !== null") is True

            # -- a step's file chip opens the real file ------------------------
            page.evaluate('''() => document
              .querySelector('#grid-overview .gridFileRef[data-path="project/docs/readme.md"]')
              .click()''')
            page.wait_for_function("document.querySelector('#grid-overview .gridPreview__line') !== null", timeout=10000)
            preview = page.locator('#grid-overview').inner_text()
            assert 'The map' in preview, preview
            hrefs = page.evaluate("[...document.querySelectorAll('#grid-overview a.gridInspector__open')]"
                                  ".map(a => a.getAttribute('href'))")
            assert any('file=project%2Fdocs%2Freadme.md' in href for href in hrefs), hrefs
            assert page.evaluate('window.gridPage.inspector.selection.kind') == 'file'
            assert served.three_d() == [], served.requested
            assert page.evaluate('window.pageErrors') == []
        finally:
            browser.close()
